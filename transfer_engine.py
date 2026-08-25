"""Transfer engine: drives a copy between two arbitrary endpoints.

The engine is the single place that implements the full copy lifecycle for a
remote *destination*: conflict policy resolution, destination-side staging
(live-part tracking + stale sweep), the tar streaming bridge between the
source reader and the destination extractor, stall handling for both legs,
and final placement (atomic rename / per-entry merge).

When the destination is the local endpoint the engine delegates to the
existing, tested legacy `copy` pipeline (src._copy_legacy), so every
historical Local→Local and SSH→Local flow keeps working byte-identically.
"""

import shlex as _shlex
import subprocess
import threading
import time

import commands.paths as rp
import commands.posix as posix_cmd
import commands.powershell as ps_cmd
from ssh_transport import POLICY_ASK, POLICY_KEEP_BOTH, POLICY_SKIP, SSHConnection

POLICY_OVERWRITE = "overwrite"
STALL_SECONDS = 120.0


def run(dest, src, src_path, dest_path, policy=POLICY_ASK, method="scp",
        is_dir=None, on_ask=None, on_part=None, on_bytes=None, proc_sink=None,
        on_finish=None):
    """Copy `src_path` on `src` onto `dest_path` on `dest`.

    Returns (status, detail) with status ∈ done/skipped/failed/aborted/
    cancelled. `is_dir` lets the caller skip a remote type probe; otherwise it
    is auto-detected on the source endpoint.
    """
    if dest.kind == "local":
        return src._copy_legacy(src_path, dest_path, policy=policy,
                                method=method, on_ask=on_ask, on_part=on_part,
                                on_bytes=on_bytes, proc_sink=proc_sink,
                                on_finish=on_finish)
    return _remote_dest_copy(dest, src, src_path, dest_path, policy, is_dir,
                             on_ask, on_part, on_bytes, proc_sink, on_finish)


def move(dest, src, src_path, dest_path, policy=POLICY_ASK,
         on_ask=None, on_bytes=None, proc_sink=None):
    """Move `src_path` from `src` to `dest_path` on `dest`.

    Same-endpoint moves that are not own-subtree and not self-overwrite become
    a single atomic rename when the target slot is free or a replaceable file;
    dir-onto-dir and cross-filesystem cases fall back to a copy (per policy)
    followed by a source delete only once the copy fully succeeded.

    Returns (status, detail):
      "done"     — moved (rename fast-path or copy+delete both succeeded)
      "noop"     — source equals destination
      "skipped"  — conflict policy left it in place
      "cancelled"— user declined
      "failed"   — with a message; source may or may not still exist
      "copied"   — transfer landed but source delete failed (both copies exist)
    """
    src_path = src.expand_remote(src_path)
    dest_path = dest.expand_remote(dest_path)
    family = dest.family
    same = dest.key() == src.key()

    if same and rp.is_same_path(src_path, dest_path, family):
        return "noop", "source equals destination"
    if same and rp.is_subpath(src_path, dest_path, family):
        return "failed", "cannot move an item into its own sub-folder"

    if same:
        entry_is_dir = bool(src.is_dir(src_path))
        busy = dest.exists(dest_path)
        parent_exists = dest.exists(rp.dirname(dest_path, family))
        # fast path: rename unless the target is a busy directory (per-entry
        # merge needs the bridge) or the destination folder is missing.
        target_blocked = busy and entry_is_dir and dest.is_dir(dest_path)
        if parent_exists and not target_blocked:
            ok, err = dest.rename(src_path, dest_path)
            if ok:
                return "done", dest_path
            if not _cross_device(err):
                return "failed", err
            # cross-filesystem rename -> fall through to the streaming bridge

    # bridge move: transfer per policy, delete the source only on success
    status, detail = run(dest, src, src_path, dest_path, policy=policy,
                         on_ask=on_ask, on_bytes=on_bytes, proc_sink=proc_sink)
    if status != "done":
        return status, detail
    ok, err = src.delete(src_path)
    if not ok:
        return "copied", (f"copied to {detail}, but the source could not be "
                          f"deleted: {err}")
    return "done", detail


def _cross_device(err):
    low = (err or "").lower()
    return ("invalid cross-device" in low or "cross-device link" in low
            or "exdev" in low or "different device" in low)


# ---------------------------------------------------------------------------
# remote destination
# ---------------------------------------------------------------------------

def _remote_dest_copy(dest, src, src_path, dest_path, policy, is_dir,
                      on_ask, on_part, on_bytes, proc_sink, on_finish):
    if not dest._ensure_master():
        return "failed", dest.last_error or "destination not reachable"
    if src.kind == "ssh" and not src._ensure_master():
        return "failed", src.last_error or "source not reachable"

    src_path = src.expand_remote(src_path)
    dest_path = dest.expand_remote(dest_path)
    family = dest.family

    final = dest_path
    dest_parent = rp.dirname(final, family)
    if not dest.exists(dest_parent):
        return "failed", f"destination folder does not exist: {dest_parent}"
    if dest.exists(final):
        if policy == POLICY_SKIP:
            return "skipped", "already exists"
        if policy == POLICY_KEEP_BOTH:
            final = dest.unique_path(final)
        elif policy == POLICY_ASK:
            remote_size = src.size(src_path)
            local_size = dest.size(final)
            if remote_size is not None and local_size == remote_size:
                return "skipped", "already exists (same size)"
            if on_ask is not None:
                choice = on_ask(final, remote_size, local_size)
                if choice is None:
                    return "cancelled", "declined by user"
                if choice == POLICY_SKIP:
                    return "skipped", "already exists"
                if choice == POLICY_KEEP_BOTH:
                    final = dest.unique_path(final)

    if is_dir is None:
        is_dir = bool(src.is_dir(src_path))
    src_basename = rp.basename(src_path, src.family)

    # ---- staged part lifecycle (destination side) ----
    dest.sweep_remote_parts(final)
    part = dest.remote_part_path(final)
    ok, err = dest.mkdir(part)
    if not ok:
        return "failed", err
    with dest._live_parts_lock:
        dest._live_parts.add(part)
    status = None
    detail = ""
    try:
        if on_part is not None:
            try:
                on_part(part)
            except Exception:
                pass
        status, detail = _stream(part, final, src_basename, is_dir,
                                 dest, src, src_path, on_bytes, proc_sink)
        if status == "done":
            if on_finish is not None:
                try:
                    on_finish()
                except Exception:
                    pass
            return "done", final
        return status, detail
    finally:
        with dest._live_parts_lock:
            dest._live_parts.discard(part)
        if status != "done":
            dest.rm_remote(part, recursive=True)


def _stream(part, final, src_basename, is_dir, dest, src, src_path,
            on_bytes, proc_sink):
    """Pipe a tar stream from the source reader into the dest extractor and,
    on success, place the single produced entry onto `final`."""
    reader = _spawn_reader(src, src_path)
    if reader is None:
        return "failed", "could not start source tar reader"
    extractor = _spawn_extractor(dest, part)
    if extractor is None:
        _close_proc(reader, src)
        return "failed", "could not start destination tar extractor"

    _attach(reader, src, proc_sink)
    _attach(extractor, dest, proc_sink)

    last_activity = {"t": 0.0}

    def on_activity(b, f):
        last_activity["t"] = _now()
        if on_bytes is not None:
            try:
                on_bytes(b, f)
            except Exception:
                pass

    th = threading.Thread(
        target=SSHConnection._pump,
        args=(reader.stdout, extractor.stdin, on_activity), daemon=True)
    th.start()
    try:
        reader_rc = _wait(reader, last_activity)
        extractor_rc = _wait(extractor, last_activity)
        th.join(timeout=10)
        if reader_rc == "STALLED" or extractor_rc == "STALLED":
            _kill_both(reader, extractor)
            return "failed", "transfer stalled (no progress for a while)"
        if _killed(reader_rc) or _killed(extractor_rc):
            return "aborted", "killed"
        if _code(reader_rc) != 0:
            return "failed", "source tar reader failed"
        if _code(extractor_rc) != 0:
            return "failed", "destination tar extractor failed"
    finally:
        _close_proc(reader, src)
        _close_proc(extractor, dest)

    # ---- placement: the part dir holds exactly the single source entry ----
    entry = _join(dest.family, part, src_basename)
    if not dest.exists(entry):
        return "failed", f"staged entry missing after extraction: {entry}"
    ok, err = _place_remote(dest, part, entry, final, is_dir)
    if not ok:
        return "failed", err
    return "done", None


# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------

def _spawn_reader(src, src_path):
    if src.kind == "local":
        clean = src_path.rstrip("/")
        parent, _, name = clean.rpartition("/")
        parent = parent or "/"
        try:
            return subprocess.Popen(
                posix_cmd.tar_read_local(parent, name),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except OSError as e:
            src.last_error = f"local tar spawn failed: {e}"
            return None
    return src.spawn_ssh(_reader_cmd(src, src_path))


def _reader_cmd(conn, src_path):
    parent = conn._remote_parent(src_path)
    name = rp.basename(src_path, conn.family)
    if conn._os_windows():
        return ps_cmd.tar_read(parent, name)
    return posix_cmd.tar_read_remote(parent, name)


def _spawn_extractor(dest, part):
    return dest.spawn_ssh(
        ps_cmd.tar_extract(part) if dest._os_windows()
        else posix_cmd.tar_extract_remote(part), stdin=subprocess.PIPE)


def _attach(proc, conn, sink):
    conn._track(proc)
    conn._sink_add(proc, sink)


def _close_proc(proc, conn):
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
    for f in (getattr(proc, "stdin", None), getattr(proc, "stdout", None),
              getattr(proc, "stderr", None)):
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
    tmpd = getattr(proc, "_lancopier_tmpd", None)
    if tmpd:
        import shutil
        shutil.rmtree(tmpd, ignore_errors=True)
    if conn is not None:
        try:
            conn._untrack(proc)
        except Exception:
            pass


def _kill_both(a, b):
    for p in (a, b):
        try:
            p.kill()
        except OSError:
            pass
    for p in (a, b):
        try:
            p.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _wait(proc, last_activity):
    """Stall-aware wait for a reader/extractor proc. `last_activity` is shared
    by both legs, so progress on either keeps the other alive."""
    while True:
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if last_activity.get("t") and _now() - last_activity["t"] > STALL_SECONDS:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                return "STALLED"


def _place_remote(dest, part, entry, final, entry_is_dir):
    """Move/merge the single part entry onto final, mirroring local `_place`:
    atomic rename when the target is free; per-entry merge for dir-onto-dir;
    type-mismatch cleanup otherwise."""
    if not dest.exists(final):
        ok, err = dest.rename(entry, final)
        if ok:
            dest.rmdir(part)
        return ok, err
    if dest.is_dir(final) and entry_is_dir:
        return _merge_dir_remote(dest, part, entry, final)
    # overwrite with type-mismatch cleanup (mirrors local _place)
    dest.rm_remote(final, recursive=True)
    ok, err = dest.rename(entry, final)
    if ok:
        dest.rmdir(part)
    return ok, err


def _merge_dir_remote(dest, part, entry, final):
    if dest._os_windows():
        cmd = ps_cmd.merge_into(entry, final)
        rc, _, err = dest._run_cmd(["ssh"] + dest._opts() + [dest.target, cmd], timeout=None)
    else:
        remote = ("sh -c " + _shlex.quote(posix_cmd.merge_dir_script()) + " x "
                  + _shlex.quote(entry) + " " + _shlex.quote(final))
        rc, _, err = dest._run_cmd(["ssh"] + dest._opts() + [dest.target, remote], timeout=None)
    if rc == 0:
        dest.rm_remote(entry, recursive=True)
        dest.rmdir(part)
        return True, ""
    return False, dest._clean_err(err) or "remote merge failed"


def _join(family, *parts):
    return rp.join(family, *parts)


def _now():
    return time.monotonic()


def _code(rc):
    return rc if not isinstance(rc, str) else -1


def _killed(rc):
    return not isinstance(rc, str) and rc < 0