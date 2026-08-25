"""Local-source transport: lets the app compare/copy two folders on the
same machine without any SSH server, password or network.

LocalConnection subclasses SSHConnection on purpose: the whole atomic copy
pipeline lives in SSHConnection.copy() (policy resolution, stale-part sweep,
PID-tagged part files, fsync, os.replace for files, entry-by-entry merge for
folders) and is pure local-filesystem code. Reusing it gives local transfers
exactly the same safety semantics and keeps that logic in one place, so a
future fix applies to both modes. Only the network-touching methods are
overridden below with local implementations.

pause()/resume()/kill_procs()/kill_all() use the SAME contract the UI already
expects (they receive the proc_sink list passed to copy()): because local
copies have no processes, the sink is identified by id() and drives a flag
checked between chunks/files. Pause stalls the copy, resume continues it,
kill/cancel aborts it and removes the unfinished part file.
"""

import os
import shutil
import stat
import threading
import time

from ssh_transport import SSHConnection
import commands.local as commands_local
from commands.local import dir_list, dir_tree, delete_local_item


class _LocalAbort(Exception):
    """Internal signal: a pause was released into a cancel, or kill_all() /
    kill_procs() fired while a local copy was running. The copy handler
    removes its part file and reports "aborted"."""


class LocalConnection(SSHConnection):
    """A source folder on this machine, exposing the same surface the UI
    uses for SSHConnection. See the module docstring for the design."""

    kind = "local"
    os_type = "ThisComputer"

    def __init__(self):
        # The base constructor only allocates state (control socket path,
        # process bookkeeping, part counter, live-parts set). None of it is
        # used here because every SSH method below is overridden, but
        # inheriting it gives us copy()/place()/fsync()/sweep() for free.
        super().__init__("This computer", 0, "", "")
        self._paused_sinks = set()
        self._pause_lock = threading.Lock()
        self._cancelled_sinks = set()
        self._cancel_lock = threading.Lock()
        self._cancel_event = threading.Event()

    def key(self):
        return ("local",)

    # -- "connection" lifecycle (no network, always available) -------------

    def _ensure_master(self):
        return True

    def _reset_master(self):
        pass

    def close(self):
        # No ssh -O exit to run; just flag every running local copy to stop.
        self.kill_all()

    def kill_all(self):
        self._cancel_event.set()
        with self._pause_lock:
            self._paused_sinks.clear()

    # -- paths -------------------------------------------------------------

    def home_dir(self):
        if self._home is None:
            self._home = os.path.expanduser("~")
        return self._home

    def expand_remote(self, path):
        # Local path bar can type "~" / "~/Music" like the SSH one can.
        if path in ("~", "~/"):
            return self.home_dir()
        if path.startswith("~/"):
            return os.path.join(self.home_dir(), path[2:])
        return path

    # -- listing / sizing --------------------------------------------------

    def list_dir(self, path):
        path = self.expand_remote(path)
        items = dir_list(path)
        if items is None:
            self.last_error = self._explain_bad_path(path)
            return None
        return items

    def _explain_bad_path(self, path):
        """A precise reason for list_dir returning None, so the error dialog
        can distinguish a typo from a wrong kind of path from a permissions
        problem (dir_list only reports OSError without the detail)."""
        if not os.path.lexists(path):
            return f"no such file or directory: {path}"
        if os.path.isdir(path):
            return f"permission denied or not accessible: {path}"
        return f"not a directory: {path}"

    def stat_remote(self, path):
        """Exact regular-file bytes and file count, mirroring the SSH side
        (which sums find -type f sizes). Sizes follow symlinks so the
        smart-skip size comparison stays consistent with the destination."""
        path = self.expand_remote(path)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                total, n = 0, 0
                for dirpath, _, files in os.walk(path):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            continue
                        n += 1
                return {"bytes": total, "files": n}
            st = os.stat(path)
            return {"bytes": st.st_size, "files": 1}
        except OSError:
            return None

    def tree_remote(self, path, stall=30.0):
        """Recursive file tree for the recursive compare. Reuses dir_tree so
        the local source and local destination are walked with the same
        rules (sizes follow symlinks)."""
        path = self.expand_remote(path)
        if not os.path.isdir(path):
            self.last_error = f"'{path}' is not a folder"
            return None
        return dir_tree(path)

    def tree(self, path, stall=30.0):
        return self.tree_remote(path, stall=stall)

    # -- endpoint primitives (local implementations of the unified contract)

    def stat(self, path):
        return self.stat_remote(path)

    def exists(self, path):
        return os.path.lexists(self.expand_remote(path))

    def is_dir(self, path):
        path = self.expand_remote(path)
        return os.path.isdir(path) and not os.path.islink(path)

    def size(self, path):
        st = self.stat_remote(path)
        return st["bytes"] if st else None

    def mkdir(self, path):
        try:
            os.makedirs(self.expand_remote(path), exist_ok=True)
            return True, ""
        except OSError as e:
            self.last_error = str(e)
            return False, str(e)

    def rename(self, src, dst):
        try:
            os.rename(self.expand_remote(src), self.expand_remote(dst))
            return True, ""
        except OSError as e:
            self.last_error = str(e)
            return False, str(e)

    def unique_path(self, path):
        return commands_local.unique_path(self.expand_remote(path))

    def delete(self, path):
        return delete_local_item(path)

    def expand(self, path):
        return self.expand_remote(path)

    # -- pause / resume / cancel ------------------------------------------

    def pause(self, proc_sink):
        with self._pause_lock:
            self._paused_sinks.add(id(proc_sink))

    def resume(self, proc_sink):
        with self._pause_lock:
            self._paused_sinks.discard(id(proc_sink))

    def kill_procs(self, proc_sink):
        with self._cancel_lock:
            self._cancelled_sinks.add(id(proc_sink))
        with self._pause_lock:
            # a paused copy must wake up so it can see the cancel
            self._paused_sinks.discard(id(proc_sink))

    def _check(self, proc_sink):
        """Stall the copy while its sink is paused, and raise _LocalAbort
        when it is cancelled (per-sink or globally). Called between chunks
        and between files, so a pause takes effect within one 1 MiB chunk."""
        while True:
            with self._cancel_lock:
                cancelled = (self._cancel_event.is_set()
                             or id(proc_sink) in self._cancelled_sinks)
            if cancelled:
                raise _LocalAbort
            with self._pause_lock:
                paused = id(proc_sink) in self._paused_sinks
            if not paused:
                return
            time.sleep(0.05)

    # -- copy internals (invoked by the inherited copy() pipeline) ---------

    def _copy_scp(self, remote_path, part, final, proc_sink=None):
        """Copy a single file into the part file in 1 MiB chunks so pause /
        cancel work mid-file, then restore mode and mtime (like scp -p)."""
        try:
            st = os.stat(remote_path)
        except OSError as e:
            self.last_error = str(e)
            return "failed", str(e)
        if os.path.isdir(remote_path) and not os.path.islink(remote_path):
            # The UI always sends folders with method="tar"; guard against a
            # caller passing a folder through the file path so they get a
            # clear failure instead of a raw "[Errno 21] Is a directory".
            self.last_error = "cannot copy a folder with the file method"
            return "failed", self.last_error
        try:
            with open(remote_path, "rb") as src, open(part, "wb") as dst:
                while True:
                    self._check(proc_sink)
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            shutil.copystat(remote_path, part)
        except _LocalAbort:
            self._remove(part)
            return "aborted", "killed"
        except OSError as e:
            self._remove(part)
            self.last_error = str(e)
            return "failed", str(e)
        return "done", part

    def _copy_tar(self, remote_path, part, final, on_bytes, proc_sink=None):
        """Copy a folder tree into the part folder. Files go through
        copy2 (metadata preserved), symlinks are recreated as symlinks
        (never followed, matching scp -r / tar), empty folders are kept.
        Progress is reported via on_bytes(bytes, files) like the SSH tar
        pump. The part/final paths are pruned from the walk so a destination
        that lives inside the source cannot be copied into itself."""
        os.makedirs(part, exist_ok=True)
        part_abs = os.path.abspath(part)
        final_abs = os.path.abspath(final)
        total, files = 0, 0
        last_cb = time.monotonic()

        def inside_skip(p):
            a = os.path.abspath(p)
            return (a == part_abs or a.startswith(part_abs + os.sep)
                    or a == final_abs or a.startswith(final_abs + os.sep))

        def onerror(e):
            # os.walk silently skips directories it cannot read; fail the
            # whole copy instead so an unreadable subfolder is reported,
            # matching the SSH tar behaviour (never silently lose files).
            raise e

        try:
            for dirpath, dirnames, filenames in os.walk(remote_path, onerror=onerror):
                if inside_skip(dirpath):
                    dirnames[:] = []
                    continue
                rel = os.path.relpath(dirpath, remote_path)
                if rel != ".":
                    os.makedirs(os.path.join(part, rel), exist_ok=True)
                # pull symlinked directories out of the walk and recreate
                # them; everything left is a real directory to recurse into
                kept = []
                for d in dirnames:
                    full = os.path.join(dirpath, d)
                    if os.path.islink(full):
                        self._check(proc_sink)
                        dst = os.path.join(part, rel, d) if rel != "." else os.path.join(part, d)
                        os.symlink(os.readlink(full), dst)
                    elif not inside_skip(full):
                        kept.append(d)
                dirnames[:] = kept
                for f in filenames:
                    src = os.path.join(dirpath, f)
                    if inside_skip(src):
                        continue
                    self._check(proc_sink)
                    dst = os.path.join(part, rel, f) if rel != "." else os.path.join(part, f)
                    st = os.lstat(src)
                    if stat.S_ISLNK(st.st_mode):
                        # a symlink is recreated, not dereferenced
                        os.symlink(os.readlink(src), dst)
                    else:
                        shutil.copy2(src, dst)
                        total += st.st_size
                    files += 1
                    now = time.monotonic()
                    if on_bytes is not None and now - last_cb >= 0.2:
                        on_bytes(total, files)
                        last_cb = now
        except _LocalAbort:
            self._remove(part)
            return "aborted", "killed"
        except OSError as e:
            self._remove(part)
            self.last_error = str(e)
            return "failed", str(e)
        if on_bytes is not None:
            on_bytes(total, files)
        return "done", part

    @staticmethod
    def friendly_error(err):
        low = (err or "").lower()
        if "permission denied" in low:
            return ("Permission denied",
                    "The folder cannot be read.\nCheck its permissions in the file manager.")
        if "not a directory" in low:
            return ("Not a folder",
                    "The path points to a file, not a folder.\nType a folder path in the path bar.")
        if "no such file" in low or "cannot list" in low or "is not a folder" in low:
            return ("Folder not found",
                    "The folder does not exist or is not accessible.\nCheck the path in the path bar.")
        return ("Local error", err or "Unknown error.")