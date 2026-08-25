import glob
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import commands.paths as rp
import commands.posix as posix_cmd
import commands.powershell as ps_cmd

POLICY_ASK = "ask"
POLICY_OVERWRITE = "overwrite"
POLICY_KEEP_BOTH = "keep_both"
POLICY_SKIP = "skip"


def _ps_quote(path):
    """Single-quote a Windows path for embedding in a PowerShell string."""
    return str(path).replace("'", "''")


class SSHConnection:
    kind = "ssh"

    _MONTHS = {m: i + 1 for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}

    def __init__(self, host, port, user, password):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.last_error = ""
        self.on_command = None
        os.makedirs(os.path.expanduser("~/.ssh"), exist_ok=True)
        self.control = f"/tmp/lan-copier-{os.getpid()}-{user}-{host}-{port}.sock"
        self._procs = []
        self._procs_lock = threading.Lock()
        self._master_lock = threading.Lock()
        self._part_counter = 0
        self._part_lock = threading.Lock()
        self._live_parts = set()
        self._live_parts_lock = threading.Lock()
        self._os_type = None
        self._has_tar = None
        self._home = None
        self._hostname = None
        self._paused = []
        self._paused_sink_ids = set()

    @property
    def target(self):
        return f"{self.user}@{self.host}"

    # -- endpoint identity & OS detection (single source of truth) ----------

    def key(self):
        """Identity used for same-endpoint fast-path / profile comparisons."""
        return ("ssh", self.host, self.port, self.user)

    @property
    def family(self):
        """'windows' or 'posix' — the path/shell family of this endpoint."""
        return rp.family_for(self.os_type)

    @property
    def os_type(self):
        """Cached OS of this endpoint: 'Linux' / 'Darwin' / 'Windows'.
        Detected once per connection via `uname -s`; a failing/empty result
        (Windows OpenSSH has no POSIX uname) is treated as Windows."""
        if self._os_type is None:
            self._os_type = self._detect_os_type()
        return self._os_type

    @os_type.setter
    def os_type(self, value):
        self._os_type = value

    @property
    def _uname(self):
        """Back-compat alias for the old attribute; tests set c._uname = 'Linux'."""
        return self.os_type

    @_uname.setter
    def _uname(self, value):
        self._os_type = value

    @property
    def _export_uname(self):
        """Back-compat alias used by the tree exporter and its tests."""
        return self.os_type

    @_export_uname.setter
    def _export_uname(self, value):
        self._os_type = value if isinstance(value, str) else None

    def _os_windows(self):
        """Branch predicate (cached only): never forces OS detection, so basic
        ops on a POSIX host cost no extra round-trips. Detection is triggered
        by the browsing/stat/tree/home calls that need a real answer."""
        return self._os_type == "Windows"

    def _detect_os_type(self):
        if not self._ensure_master():
            return "Windows"
        rc, out, err = self._run_cmd(
            ["ssh"] + self._opts() + [self.target, "uname -s"], timeout=15)
        if rc == 0 and (out or "").strip():
            return out.strip()
        return "Windows"

    def has_tar(self):
        """Lazily check whether the host ships tar.exe (Windows reads/writes
        every tar stream with it). None while unknown, True/False after."""
        if self._has_tar is None:
            if self.os_type != "Windows":
                self._has_tar = True
            else:
                rc, _, _ = self._run_cmd(
                    ["ssh"] + self._opts() + [self.target, ps_cmd.has_tar()], timeout=20)
                self._has_tar = (rc == 0)
        return self._has_tar

    def _opts(self, legacy=False):
        opts = [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + os.path.expanduser("~/.ssh/known_hosts"),
            "-o", "ConnectTimeout=6",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "PreferredAuthentications=password",
            "-o", "IdentitiesOnly=yes",
            "-o", "ControlPath=" + self.control,
            "-o", "ControlMaster=no",
        ]
        if legacy:
            opts += [
                "-o", "KexAlgorithms=+diffie-hellman-group14-sha256,diffie-hellman-group14-sha1",
                "-o", "HostKeyAlgorithms=+ssh-rsa",
                "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
                "-o", "Ciphers=+aes128-cbc,aes192-cbc,aes256-cbc,3des-cbc",
            ]
        return opts

    def _askpass_env(self):
        d = tempfile.mkdtemp(prefix="lan-copier-")
        passfile = os.path.join(d, "pass")
        script = os.path.join(d, "ask")
        with open(passfile, "w") as f:
            f.write(self.password)
        with open(script, "w") as f:
            f.write("#!/bin/sh\ncat " + shlex.quote(passfile) + "\n")
        os.chmod(passfile, 0o600)
        os.chmod(script, 0o700)
        env = dict(os.environ)
        env["SSH_ASKPASS"] = script
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = ":0"
        return env, d

    def _track(self, proc):
        with self._procs_lock:
            self._procs.append(proc)

    def _untrack(self, proc):
        with self._procs_lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                pass

    def _run(self, argv, timeout=None, capture=True):
        env, d = self._askpass_env()
        errf = None
        try:
            if capture:
                streams = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            else:
                errf = tempfile.NamedTemporaryFile(prefix="lan-copier-err-", mode="w+", delete=False)
                streams = dict(stdout=subprocess.DEVNULL, stderr=errf)
            try:
                proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, text=True, **streams)
            except OSError as e:
                if errf is not None:
                    errf.close()
                    try:
                        os.unlink(errf.name)
                    except OSError:
                        pass
                return -1, "", f"spawn failed: {e}"
            self._track(proc)
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            finally:
                self._untrack(proc)
            if errf is not None:
                errf.seek(0)
                err = errf.read()
                errf.close()
                os.unlink(errf.name)
            if self.on_command:
                try:
                    self.on_command(argv, proc.returncode, err)
                except Exception:
                    pass
            return proc.returncode, out or "", err
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _run_cmd(self, argv, timeout=None):
        rc, out, err = self._run(argv, timeout=timeout)
        if rc != 0 and self._mux_dead(err):
            self._reset_master()
            rc, out, err = self._run(argv, timeout=timeout)
        return rc, out, err

    def kill_all(self):
        with self._procs_lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass
        for p in procs:
            try:
                p.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                pass
            self._untrack(p)
            self._close_proc(p)
        with self._procs_lock:
            self._paused.clear()
            self._paused_sink_ids.clear()

    def close(self):
        """Release every process and the control master without blocking the
        caller: killing and 'ssh -O exit' can take seconds, so they run in a
        daemon thread."""

        def work():
            self.kill_all()
            self._reset_master()

        threading.Thread(target=work, daemon=True).start()

    def _reset_master(self):
        self._run(["ssh"] + self._opts() + ["-O", "exit", self.target], timeout=10)
        try:
            os.remove(self.control)
        except OSError:
            pass

    def _ensure_master(self):
        with self._master_lock:
            if os.path.exists(self.control):
                rc, _, _ = self._run(["ssh"] + self._opts() + ["-O", "check", self.target], timeout=8)
                if rc == 0:
                    return True
                try:
                    os.remove(self.control)
                except OSError:
                    pass
            argv = ["ssh"] + self._opts() + ["-M", "-N", "-f", "-o", "ControlPersist=300", self.target]
            rc, _, err = self._run(argv, timeout=30, capture=False)
            if rc != 0 and self._needs_legacy(err):
                argv = ["ssh"] + self._opts(legacy=True) + ["-M", "-N", "-f", "-o", "ControlPersist=300", self.target]
                rc, _, err = self._run(argv, timeout=30, capture=False)
            if rc != 0:
                self.last_error = self._clean_err(err)
                return False
            return True

    def home_dir(self):
        if self._home is not None:
            return self._home
        if not self._ensure_master():
            return None
        if self.os_type == "Windows":
            rc, out, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, ps_cmd.home_dir()], timeout=20)
        else:
            rc, out, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, "echo $HOME"], timeout=20)
        if rc != 0:
            self.last_error = self._clean_err(err)
            return None
        self._home = out.strip().replace("\\", "/")
        return self._home

    def hostname(self):
        """Resolved remote machine name, cached. This is the stable identity
        used to recognise the same machine even when its IP changes, so a
        reconnect updates one saved profile instead of stacking "user@host 2"
        duplicates. Falls back to the typed host when the lookup fails."""
        if self._hostname is not None:
            return self._hostname
        name = self.host
        if self._ensure_master():
            if self.os_type == "Windows":
                rc, out, _ = self._run_cmd(
                    ["ssh"] + self._opts() + [self.target, ps_cmd.hostname()], timeout=20)
            else:
                rc, out, _ = self._run_cmd(
                    ["ssh"] + self._opts() + [self.target, "hostname"], timeout=20)
            if rc == 0 and (out or "").strip():
                name = out.strip()
        self._hostname = name
        return self._hostname

    def expand_remote(self, path):
        """Expand a leading ~ so users can type '~/Music'. Windows paths are
        normalized to forward slashes (PowerShell accepts both and our path
        ops stay consistent)."""
        if path in ("~", "~/"):
            return self.home_dir() or path
        if path.startswith("~/"):
            home = self.home_dir()
            if not home:
                return path
            return (home.rstrip("/") + "/" + path[2:]).replace("\\", "/")
        if self._os_type == "Windows":
            return str(path).replace("\\", "/")
        return path

    @staticmethod
    def _is_protected_path(path, home):
        """Guard against deleting a root/home/empty path. `path` must already
        be expanded (no leading ~) and is compared with trailing slashes
        stripped so '/', '/  /', 'home/', etc. are all caught."""
        clean = (path or "").rstrip("/")
        if clean in ("", "/", ".", ".."):
            return True
        if home:
            home_clean = home.rstrip("/")
            if clean == home_clean:
                return True
        return False

    @staticmethod
    def _explain_delete_error(err):
        """Append actionable hints to a failed `rm` stderr. Keeps the raw
        message first (it names the path) and stays OS-agnostic."""
        err = err or ""
        low = err.lower()
        hint = None
        if "operation not permitted" in low:
            hint = ("The remote system refused the delete (EPERM), usually because:\n"
                    "• the item is locked or flagged immutable "
                    "(macOS Finder 'Locked' / Linux 'chattr +i')\n"
                    "• the volume is mounted read-only\n"
                    "• your account lacks write permission on the containing folder")
        elif "permission denied" in low:
            hint = ("The account lacks permission to remove this item.\n"
                    "Deleting needs write access to the containing folder; "
                    "check owner/permissions there.")
        elif "read-only file system" in low:
            hint = "The remote volume is mounted read-only; nothing can be deleted from it."
        elif "resource busy" in low or "text file busy" in low:
            hint = "The item is in use by another process on the remote host."
        return err if hint is None else f"{err}\n{hint}"

    def delete_item(self, remote_path):
        """Permanently delete a file, folder, or symlink on the remote host.
        Never dereferences a symlink. Returns (ok, error_message)."""
        if not self._ensure_master():
            return False, "SSH connection not active"
        remote_path = self.expand_remote(remote_path)
        clean_path = remote_path.rstrip("/")
        if self._is_protected_path(clean_path, self.home_dir()):
            return False, f"Refusing to delete protected path: {remote_path}"
        if self._os_windows():
            if self._is_protected_windows_path(clean_path):
                return False, f"Refusing to delete protected path: {remote_path}"
            cmd = ["ssh"] + self._opts() + [self.target, ps_cmd.delete_recurse(clean_path)]
            rc, out, err = self._run_cmd(cmd, timeout=120)
            if self.on_command:
                self.on_command(cmd, rc, err)
            if rc != 0:
                self.last_error = self._clean_err(err)
                return False, self.last_error or "remote delete failed"
            return True, ""
        quoted = shlex.quote(clean_path)
        cmd = ["ssh"] + self._opts() + [self.target, f"rm -rf -- {quoted}"]
        rc, out, err = self._run_cmd(cmd, timeout=60)
        if self.on_command:
            self.on_command(cmd, rc, err)
        if rc != 0:
            self.last_error = self._explain_delete_error(self._clean_err(err))
            return False, self.last_error
        return True, ""

    @staticmethod
    def _is_protected_windows_path(path):
        """Block drive roots (C:, C:\\), UNC roots, and empty paths."""
        low = (path or "").lower().rstrip("/")
        if low in ("", ":") or re.match(r"^[a-z]:$", low):
            return True
        if low.startswith("//") and low.count("/") <= 2:
            return True  # UNC root like //server/share
        return False

    def list_dir(self, path):
        if not self._ensure_master():
            return None
        path = self.expand_remote(path)
        if self.os_type == "Windows":
            return self._list_dir_windows(path)
        remote = posix_cmd.ls_la(path)
        rc, out, err = self._run_cmd(
            ["ssh"] + self._opts() + [self.target, remote], timeout=30)
        if rc != 0:
            self.last_error = self._clean_err(err)
            return None
        base = path.rstrip("/") + "/" if path != "/" else "/"
        items = self._parse_ls(out)
        for it in items:
            it["path"] = base + it["name"]
        return items

    def _list_dir_windows(self, path):
        rc, out, err = self._run_cmd(
            ["ssh"] + self._opts() + [self.target, ps_cmd.list_dir(path)], timeout=30)
        if rc != 0:
            self.last_error = self._clean_err(err)
            return None
        base = path.rstrip("/") + "/"
        items = self._parse_ps_list(out)
        for it in items:
            it["path"] = base + it["name"]
        return items

    @staticmethod
    def _parse_ps_list(out):
        """Parse TSV lines: ISDIR\\tISLINK\\tSIZE\\tEPOCH\\tNAME. ISDIR/ISLINK
        are '1'/'0'. Names may themselves contain tabs; a name is everything
        after the 4th field."""
        items = []
        text = (out or "")
        if text.startswith("\ufeff"):
            text = text[1:]
        for line in text.splitlines():
            line = line.rstrip("\r")
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name = "\t".join(parts[4:])
            if not name or name in (".", ".."):
                continue
            try:
                is_dir = parts[0] == "1"
                is_link = parts[1] == "1"
                size = int(parts[2])
                epoch = int(parts[3])
            except ValueError:
                continue
            items.append({
                "name": name,
                "is_dir": is_dir,
                "is_link": is_link,
                "size": size,
                "mtime": time.strftime("%b %d %H:%M", time.localtime(epoch)),
                "mtime_epoch": epoch,
            })
        return items

    def stat(self, path):
        """{bytes: exact sum of regular-file sizes, files: N} for a path on the
        remote endpoint. Uses find/stat (POSIX) or a recursive PowerShell scan
        (Windows); returns None when the path is not accessible."""
        if not self._ensure_master():
            return None
        path = self.expand_remote(path)
        os_type = self.os_type
        if os_type == "Windows":
            rc, out, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, ps_cmd.stat_bytes_files(path)],
                timeout=60)
        elif os_type == "Darwin":
            rc, out, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, posix_cmd.find_stat_darwin(path)],
                timeout=60)
        else:
            rc, out, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, posix_cmd.find_stat_gnu(path)],
                timeout=60)
        if rc != 0:
            self.last_error = self._clean_err(err)
            return None
        parts = out.split()
        try:
            return {"bytes": int(parts[0]), "files": int(parts[1])}
        except (ValueError, IndexError):
            return None

    def stat_remote(self, path):
        """Back-compat alias for stat()."""
        return self.stat(path)

    def size(self, path):
        """Recursive byte total for a remote path, or None when inaccessible."""
        st = self.stat(path)
        return st["bytes"] if st else None

    def copy(self, remote_path, local_dest, policy=POLICY_ASK, size=None,
             method="scp", on_ask=None, on_part=None, on_bytes=None,
             proc_sink=None, on_finish=None):
        """Back-compat entry point for a copy that lands on the *local*
        destination. The transfer engine delegates here for local ends; the UI
        and tests may keep using it unchanged."""
        return self._copy_legacy(remote_path, local_dest, policy=policy,
                                 size=size, method=method, on_ask=on_ask,
                                 on_part=on_part, on_bytes=on_bytes,
                                 proc_sink=proc_sink, on_finish=on_finish)

    def _copy_legacy(self, remote_path, local_dest, policy=POLICY_ASK, size=None,
                     method="scp", on_ask=None, on_part=None, on_bytes=None,
                     proc_sink=None, on_finish=None):
        """Copy remote_path into local_dest. Returns (status, detail) where
        status is one of "done", "skipped", "failed", "aborted", "cancelled".

        method: "scp" for single items, "tar" to stream a folder via
        tar-over-ssh (much faster for many small files).
        on_bytes(total, files) is called periodically while streaming.
        on_finish() is called once the stream is complete, just before the
        finished part is placed at its final name.
        proc_sink: a list the spawned processes are appended to (and paused
        via pause()/resume()/kill_procs()).

        Copy goes to a temporary .part file/dir first, then is placed
        atomically: files are swapped with os.replace() (one atomic step),
        folders are merged into the destination entry-by-entry (rename per
        entry, never a bulk delete of the old folder). A crash can therefore
        never leave a half-written file under the final name.
        Conflicts are resolved before the transfer starts:
          overwrite / keep_both / skip are applied directly;
          ask compares sizes (same size -> skip silently, else on_ask(final,
          remote_size, local_size) must return a policy string or None to cancel).
        """
        if not self._ensure_master():
            return "failed", self.last_error
        remote_path = self.expand_remote(remote_path)
        final = local_dest
        if os.path.lexists(final):
            if policy == POLICY_SKIP:
                return "skipped", "already exists"
            if policy == POLICY_KEEP_BOTH:
                final = self.unique_path(final)
            elif policy == POLICY_ASK:
                remote_size = size if size is not None else self._remote_size(remote_path)
                local_size = self._local_size(final)
                if remote_size is not None and local_size == remote_size:
                    return "skipped", "already exists (same size)"
                if on_ask is not None:
                    choice = on_ask(final, remote_size, local_size)
                    if choice is None:
                        return "cancelled", "declined by user"
                    if choice == POLICY_SKIP:
                        return "skipped", "already exists"
                    if choice == POLICY_KEEP_BOTH:
                        final = self.unique_path(final)
        # sweep stale parts from crashed runs (any owner/counter index): they
        # would otherwise merge into a fresh copy. Parts of live transfers are
        # tracked and never touched, so two concurrent copies of the same
        # final name cannot delete each other's data.
        self._sweep_stale(final)
        part = self._part_path(final)
        if on_part is not None:
            try:
                on_part(part)
            except Exception:
                pass
        with self._live_parts_lock:
            self._live_parts.add(part)
        try:
            if method == "tar":
                status, detail = self._copy_tar(remote_path, part, final, on_bytes,
                                                proc_sink=proc_sink)
            else:
                status, detail = self._copy_scp(remote_path, part, final,
                                                proc_sink=proc_sink)
            if status != "done":
                return status, detail
            if on_finish is not None:
                try:
                    on_finish()
                except Exception:
                    pass
            if os.path.isfile(part) and not os.path.islink(part):
                self._fsync_file(part)
            try:
                self._place(part, final)
            except OSError:
                self._remove(part)
                raise
            if os.path.isdir(final) and not os.path.islink(final):
                self._fsync_dir(final)
            return "done", final
        finally:
            with self._live_parts_lock:
                self._live_parts.discard(part)

    def _sweep_stale(self, final):
        """Remove leftover .part entries for final that no live transfer owns."""
        d = os.path.dirname(final)
        try:
            candidates = glob.glob(os.path.join(
                d, f".{os.path.basename(final)}.lan-copier-part-*"))
        except OSError:
            return
        with self._live_parts_lock:
            live = set(self._live_parts)
        for old in candidates:
            if old not in live:
                self._remove(old)

    # -- remote-destination support (used by the transfer engine / UI) -----

    def spawn_ssh(self, remote_cmd, stdin=subprocess.DEVNULL):
        """Spawn an ssh invocation whose remote command is `remote_cmd`, and
        track it. The askpass temp dir is stashed on the proc and removed by
        the caller (transfer engine) when the process is closed."""
        env, d = self._askpass_env()
        try:
            proc = self._spawn(
                ["ssh"] + self._opts() + [self.target, remote_cmd],
                env, stdin=stdin, stdout=subprocess.PIPE)
        except OSError as e:
            shutil.rmtree(d, ignore_errors=True)
            self.last_error = f"spawn failed: {e}"
            return None
        proc._lancopier_tmpd = d
        return proc

    def _remote_parent(self, path):
        return rp.dirname(str(path).rstrip("/"), self.family) or "/"

    def remote_part_path(self, final):
        """A fresh staging path for `final` on this endpoint (same directory
        so the eventual rename is same-filesystem/atomic). Always a directory;
        naming is the same `.basename.lan-copier-part-pid-n` scheme everywhere."""
        final = self.expand_remote(final)
        family = self.family
        d = rp.dirname(final, family)
        base = rp.basename(final, family)
        with self._part_lock:
            while True:
                cand = rp.join(family, d,
                               f".{base}.lan-copier-part-{os.getpid()}-{self._part_counter}")
                self._part_counter += 1
                if not self.exists(cand):
                    return cand

    def sweep_remote_parts(self, final):
        """Delete stale staging dirs for `final` that no live transfer owns
        (mirrors the local `_sweep_stale` behaviour on a remote endpoint)."""
        final = self.expand_remote(final)
        family = self.family
        d = rp.dirname(final, family)
        base = rp.basename(final, family)
        if self._os_windows():
            glob = (f"$p = '{d}'.Replace('/','\\\\')\n"
                    f"Get-ChildItem -LiteralPath $p -Force -ErrorAction SilentlyContinue | "
                    f"Where-Object {{ $_.Name -like '.{base}.lan-copier-part-*' }} | "
                    f"ForEach-Object {{ $_.FullName }}")
            rc, out, _ = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, ps_cmd._script(glob)], timeout=30)
            if rc != 0 or not out.strip():
                return
            with self._live_parts_lock:
                live = set(self._live_parts)
            for line in out.splitlines():
                cand = line.replace("\\", "/")
                if cand not in live:
                    self._run_cmd(["ssh"] + self._opts()
                                  + [self.target, ps_cmd.delete_recurse(cand)], timeout=30)
        else:
            remote = (f"for f in {posix_cmd.q(d)}/.{posix_cmd.q(base)}.lan-copier-part-*; do "
                      f"[ -e \"$f\" ] || [ -L \"$f\" ] || continue; printf '%s\\n' \"$f\"; done")
            rc, out, _ = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, remote], timeout=30)
            if rc != 0 or not out.strip():
                return
            with self._live_parts_lock:
                live = set(self._live_parts)
            for line in out.splitlines():
                cand = line.strip()
                if cand not in live:
                    self._run_cmd(["ssh"] + self._opts()
                                  + [self.target, posix_cmd.rm_rf(cand)], timeout=30)

    def rm_remote(self, path, recursive=True):
        """Permanently remove a remote file/folder. Returns (ok, err)."""
        path = self.expand_remote(path)
        if self._os_windows():
            rc, _, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, ps_cmd.delete_recurse(path)], timeout=120)
        else:
            rc, _, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, posix_cmd.rm_rf(path)], timeout=60)
        if rc != 0:
            self.last_error = self._clean_err(err)
            return False, self.last_error
        return True, ""

    def rmdir(self, path):
        """Best-effort removal of a (now empty) remote directory."""
        try:
            self.rm_remote(path, recursive=True)
        except Exception:
            pass

    @staticmethod
    def _parse_ps_tree(line, base, tree):
        """Parse one `REL<T>SIZE` Windows scan line into tree (REL already
        relative to the scanned root, forward-slashed). Unparseable lines are
        dropped like their POSIX equivalents."""
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        parts = line.split("\t")
        if len(parts) != 2:
            return
        p, s = parts
        try:
            tree[p] = int(s)
        except ValueError:
            return

    def tree(self, path, stall=30.0):
        """Alias for tree_remote (unified endpoint verb)."""
        return self.tree_remote(path, stall=stall)

    def _remote_size(self, path):
        st = self.stat_remote(path)
        return st["bytes"] if st else None

    def exists(self, path):
        """True when the remote path exists (follows no symlinks for `-e` on
        POSIX — a dangling symlink reports True there and Test-Path on
        Windows, matching each platform's native semantics)."""
        if not self._ensure_master():
            return False
        path = self.expand_remote(path)
        if self._os_windows():
            rc, _, _ = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, ps_cmd.exists(path)], timeout=20)
        else:
            rc, _, _ = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, posix_cmd.test_exists(path)], timeout=20)
        return rc == 0

    def is_dir(self, path):
        """True when the remote path is an existing directory (not a symlink)."""
        path = self.expand_remote(path)
        if not self.exists(path):
            return False
        st = self.stat(path)
        if st is None:
            return False
        # stat follows symlinks; a dir symlink would then count as a dir,
        # so guard with a lstat-level check for POSIX hosts.
        if not self._os_windows():
            rc, out, _ = self._run_cmd(
                ["ssh"] + self._opts() + [self.target,
                                          f"test -d -- {shlex.quote(path)} && echo 1"],
                timeout=20)
            return rc == 0 and out.strip() == "1"
        # Windows: Test-Path -PathType Container (reparse points excluded)
        return self._run_is_dir_windows(path)

    def _run_is_dir_windows(self, path):
        body = (f"if (Test-Path -LiteralPath '{_ps_quote(path)}' -PathType Container) "
                f"{{ exit 0 }} else {{ exit 1 }}")
        rc, _, _ = self._run_cmd(
            ["ssh"] + self._opts() + [self.target, ps_cmd._script(body)], timeout=20)
        return rc == 0

    def mkdir(self, path):
        """Create a remote folder (with parents) if missing; returns (ok, err)."""
        if not self._ensure_master():
            return False, "SSH connection not active"
        path = self.expand_remote(path)
        if self._os_windows():
            cmd = ["ssh"] + self._opts() + [self.target, ps_cmd.mkdir_p(path)]
            rc, _, err = self._run_cmd(cmd, timeout=30)
        else:
            cmd = ["ssh"] + self._opts() + [self.target, posix_cmd.mkdir_p(path)]
            rc, _, err = self._run_cmd(cmd, timeout=30)
        if rc != 0:
            self.last_error = self._clean_err(err)
            return False, self.last_error
        return True, ""

    def rename(self, src, dst):
        """Rename/move a remote item. Returns (ok, err)."""
        if not self._ensure_master():
            return False, "SSH connection not active"
        src = self.expand_remote(src)
        dst = self.expand_remote(dst)
        if self._os_windows():
            rc, _, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, ps_cmd.rename(src, dst)], timeout=30)
        else:
            rc, _, err = self._run_cmd(
                ["ssh"] + self._opts() + [self.target, posix_cmd.mv(src, dst)], timeout=30)
        if rc != 0:
            self.last_error = self._clean_err(err)
            return False, self.last_error
        return True, ""

    def unique_path(self, path):
        """Return `path`, or `name (n).ext` with the first n that does not
        exist on this endpoint."""
        path = self.expand_remote(path)
        if not self.exists(path):
            return path
        base, ext = rp.splitext(path, self.family)
        i = 1
        while True:
            cand = f"{base} ({i}){ext}"
            if not self.exists(cand):
                return cand
            i += 1

    def delete(self, path):
        """Alias for delete_item (unified endpoint verb)."""
        return self.delete_item(path)

    def expand(self, path):
        """Alias for expand_remote (unified endpoint verb)."""
        return self.expand_remote(path)

    def tree_remote(self, path, stall=30.0):
        """Recursive listing of every file under path as {relative_path: size},
        streamed from the find command (bounded memory) with a stall timeout
        instead of a fixed wall-clock one. Returns None when the folder is
        not accessible. Empty folders and symlinks are not listed."""
        if not self._ensure_master():
            return None
        path = self.expand_remote(path)
        os_type = self.os_type
        is_windows = os_type == "Windows"
        if is_windows:
            remote = ps_cmd.tree(path)
        elif os_type == "Darwin":
            remote = posix_cmd.find_tree_darwin(path)
        else:
            remote = posix_cmd.find_tree_gnu(path)
        base = path.rstrip("/") + "/" if path != "/" else "/"
        parser = self._parse_ps_tree if is_windows else self._parse_tree_line
        env, d = self._askpass_env()
        try:
            attempts = 2
            for attempt in range(attempts):
                try:
                    proc = self._spawn(
                        ["ssh"] + self._opts() + [self.target, remote], env,
                        stdin=subprocess.DEVNULL)
                except OSError as e:
                    self.last_error = f"spawn failed: {e}"
                    return None
                self._track(proc)
                try:
                    tree = {}
                    err = []
                    last_activity = {"t": time.monotonic()}
                    errth = threading.Thread(
                        target=self._drain_err, args=(proc.stderr, err), daemon=True)
                    errth.start()
                    buf = b""

                    def feed(chunk):
                        nonlocal buf
                        last_activity["t"] = time.monotonic()
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            parser(line, base, tree)

                    def reader():
                        while True:
                            try:
                                chunk = proc.stdout.read(65536)
                            except (OSError, ValueError):
                                return
                            if not chunk:
                                break
                            feed(chunk)
                        if buf:
                            parser(buf, base, tree)

                    rd = threading.Thread(target=reader, daemon=True)
                    rd.start()
                    rc = self._wait_or_kill(proc, last_activity, stall=stall)
                    rd.join(timeout=10)
                    errth.join(timeout=5)
                    detail = (err[0] if err else "").decode("utf-8", "replace")
                    if rc == "STALLED":
                        self.last_error = "listing stalled (no output for a while)"
                        return None
                    if rc != 0 and self._mux_dead(detail) and attempt == 0:
                        self._reset_master()
                        continue
                    if rc != 0:
                        self.last_error = self._clean_err(detail)
                        return None
                    return tree
                finally:
                    self._untrack(proc)
                    self._close_proc(proc)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _parse_tree_line(line, base, tree):
        """Parse one 'path size' line from find output into tree. Lines that
        do not parse are dropped (the find command may emit warnings)."""
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            return
        p, s = parts
        try:
            size = int(s)
        except ValueError:
            return
        if p.startswith(base):
            tree[p[len(base):]] = size

    def _copy_scp(self, remote_path, part, final, proc_sink=None):
        target = f"{self.target}:{self._escape_remote(remote_path)}"
        env, d = self._askpass_env()
        try:
            attempts = 2
            for attempt in range(attempts):
                proc = self._spawn(
                    ["scp", "-p", "-r", "-q"] + self._opts() + [target, part],
                    env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                self._track(proc)
                self._sink_add(proc, proc_sink)
                try:
                    err = []
                    th = threading.Thread(
                        target=self._drain_err, args=(proc.stderr, err), daemon=True)
                    th.start()
                    rc = self._wait_or_kill(proc, {"t": float("inf")}, stall=None,
                                            paused=self._paused)
                    th.join(timeout=5)
                    detail = (err[0] if err else "").decode("utf-8", "replace")
                    if rc == "STALLED":
                        self._remove(part)
                        return "failed", "transfer stalled (no progress for a while)"
                    if rc is not None and rc < 0:
                        self._remove(part)
                        return "aborted", "killed"
                    if rc != 0 and self._mux_dead(detail) and attempt == 0:
                        self._reset_master()
                        self._remove(part)
                        continue
                    if rc != 0:
                        self._remove(part)
                        detail = self._clean_err(detail)
                        self.last_error = detail
                        return "failed", detail
                    return "done", part
                finally:
                    self._untrack(proc)
                    self._close_proc(proc)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _copy_tar(self, remote_path, part, final, on_bytes, proc_sink=None):
        """Stream a remote folder as a tar archive piped into a local tar.
        Progress via on_bytes(bytes, files) from a pump thread that counts
        bytes and parses tar headers (files counted are regular files only).

        Both sides are waited on with a stall budget: if neither process makes
        progress for a while (no bytes flowing through the pump), the copy is
        killed and reported as failed instead of hanging forever."""
        os.makedirs(part, exist_ok=True)
        parent = os.path.dirname(remote_path.rstrip("/")) or "/"
        name = os.path.basename(remote_path.rstrip("/"))
        remote_cmd = "LC_ALL=C tar -C " + shlex.quote(parent) + " -cf - -- " + shlex.quote(name)
        env, d = self._askpass_env()
        try:
            attempts = 2
            for attempt in range(attempts):
                remote_proc = None
                local_proc = None
                try:
                    if attempt > 0:
                        self._ensure_master()
                    remote_proc = self._spawn_remote_tar(remote_cmd, env)
                    local_proc = self._spawn_local_tar(part)
                except OSError:
                    for p in (remote_proc, local_proc):
                        if p is not None:
                            try:
                                p.kill()
                            except OSError:
                                pass
                            try:
                                p.wait(timeout=10)
                            except (subprocess.TimeoutExpired, OSError):
                                pass
                            self._close_proc(p)
                    self._remove(part)
                    raise
                self._track(remote_proc)
                self._track(local_proc)
                self._sink_add(remote_proc, proc_sink)
                self._sink_add(local_proc, proc_sink)
                try:
                    remote_err, local_err = [], []
                    threads = [
                        threading.Thread(target=self._drain_err, args=(remote_proc.stderr, remote_err), daemon=True),
                        threading.Thread(target=self._drain_err, args=(local_proc.stderr, local_err), daemon=True),
                    ]
                    for th in threads:
                        th.start()
                    last_activity = {"t": time.monotonic()}

                    def on_activity(b, f):
                        last_activity["t"] = time.monotonic()
                        if on_bytes is not None:
                            on_bytes(b, f)

                    pump = threading.Thread(
                        target=self._pump, args=(remote_proc.stdout, local_proc.stdin, on_activity), daemon=True)
                    pump.start()
                    remote_rc = self._wait_or_kill(remote_proc, last_activity,
                                                   paused=self._paused)
                    local_rc = self._wait_or_kill(local_proc, last_activity,
                                                  paused=self._paused)
                    if remote_rc == "STALLED" or local_rc == "STALLED":
                        for p, rc in ((remote_proc, remote_rc), (local_proc, local_rc)):
                            if rc == "STALLED":
                                try:
                                    p.kill()
                                except OSError:
                                    pass
                                p.wait()
                        pump.join(timeout=10)
                        for th in threads:
                            th.join(timeout=5)
                        self._remove(part)
                        return "failed", "transfer stalled (no progress for a while)"
                    pump.join(timeout=10)
                    if pump.is_alive():
                        for p in (remote_proc, local_proc):
                            try:
                                p.kill()
                            except OSError:
                                pass
                        pump.join(timeout=10)
                    for th in threads:
                        th.join(timeout=5)
                    killed_remote = remote_rc is not None and remote_rc < 0
                    killed_local = local_rc is not None and local_rc < 0
                    remote_detail = (remote_err[0] if remote_err else "").decode("utf-8", "replace")
                    local_detail = (local_err[0] if local_err else "").decode("utf-8", "replace")
                    if killed_remote or killed_local:
                        if killed_remote and not killed_local and local_rc != 0:
                            self._remove(part)
                            detail = self._clean_err(local_detail)
                            if not detail:
                                detail = "local tar failed"
                            return "failed", detail
                        self._remove(part)
                        return "aborted", "killed"
                    if remote_rc != 0 and self._mux_dead(remote_detail) and attempt == 0:
                        self._reset_master()
                        self._remove(part)
                        os.makedirs(part, exist_ok=True)
                        continue
                    if remote_rc != 0:
                        self._remove(part)
                        detail = self._clean_err(remote_detail)
                        if not detail:
                            detail = f"remote tar exited with {remote_rc}"
                        self.last_error = detail
                        return "failed", detail
                    if local_rc != 0:
                        self._remove(part)
                        detail = self._clean_err(local_detail)
                        if not detail:
                            detail = f"local tar exited with {local_rc}"
                        return "failed", detail
                    break
                finally:
                    self._untrack(remote_proc)
                    self._untrack(local_proc)
                    self._close_proc(remote_proc)
                    self._close_proc(local_proc)
            return "done", part
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _wait_or_kill(self, p, last_activity, stall=120.0, paused=None):
        """Wait for p, polling so kill_all() is honoured within ~1s. If no
        pump activity for `stall` seconds the process is killed and
        "STALLED" is returned (the caller reaps it with p.wait()).
        stall=None disables the stall check; procs in `paused` are exempt
        from it (a paused transfer must never self-destruct)."""
        while True:
            try:
                return p.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if paused is not None and p in paused:
                    continue
                if stall is not None and time.monotonic() - last_activity["t"] > stall:
                    try:
                        p.kill()
                    except OSError:
                        pass
                    return "STALLED"

    def _spawn_remote_tar(self, remote_cmd, env):
        return self._spawn(["ssh"] + self._opts() + [self.target, remote_cmd], env,
                           stdin=subprocess.DEVNULL)

    def _spawn_local_tar(self, part):
        return self._spawn(["tar", "-C", part, "--strip-components=1", "-xpf", "-"],
                           os.environ, stdin=subprocess.PIPE,
                           stdout=subprocess.DEVNULL)

    def _spawn(self, argv, env, stdin=subprocess.PIPE, stdout=subprocess.PIPE):
        proc = subprocess.Popen(
            argv, env=env, stdin=stdin, stdout=stdout,
            stderr=subprocess.PIPE)
        if self.on_command:
            try:
                self.on_command(argv, None, "")
            except Exception:
                pass
        return proc

    @staticmethod
    def _close_proc(proc):
        """Close every pipe of a finished process. Callers must join any
        drain/pump threads first so no thread is reading a pipe we close."""
        for f in (getattr(proc, "stdin", None),
                  getattr(proc, "stdout", None),
                  getattr(proc, "stderr", None)):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass

    def _sink_add(self, proc, sink):
        """Attach a spawned process to a transfer's proc list; if that
        transfer was paused before its processes existed, stop it right
        away so the pause is honoured from the first byte."""
        if sink is None:
            return
        with self._procs_lock:
            sink.append(proc)
            if id(sink) in self._paused_sink_ids:
                try:
                    proc.send_signal(signal.SIGSTOP)
                except OSError:
                    pass
                self._paused.append(proc)

    def pause(self, sink):
        """Freeze every live process of a transfer (SIGSTOP). Works even
        before any process exists: procs spawned later are stopped on
        arrival via _sink_add. The sink may be mutated by worker threads,
        so it is copied under the procs lock before signalling."""
        with self._procs_lock:
            self._paused_sink_ids.add(id(sink))
            items = list(sink)
        for p in items:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGSTOP)
                except OSError:
                    pass
                with self._procs_lock:
                    self._paused.append(p)

    def resume(self, sink):
        """Unfreeze every live process of a transfer (SIGCONT)."""
        with self._procs_lock:
            self._paused_sink_ids.discard(id(sink))
            items = list(sink)
        for p in items:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGCONT)
                except OSError:
                    pass
        with self._procs_lock:
            self._paused[:] = [x for x in self._paused
                               if all(x is not q for q in items)]

    def kill_procs(self, sink):
        """Kill and reap every process of a transfer and drop any pause
        bookkeeping for it (kills work on SIGSTOPped processes)."""
        with self._procs_lock:
            self._paused_sink_ids.discard(id(sink))
            items = list(sink)
        for p in items:
            try:
                p.kill()
            except OSError:
                pass
        for p in items:
            try:
                p.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                pass
            self._untrack(p)
            self._close_proc(p)
        with self._procs_lock:
            self._paused[:] = [x for x in self._paused
                               if all(x is not q for q in items)]

    @staticmethod
    def _drain_err(f, out):
        if f is None:
            return
        data = []
        try:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                if sum(len(x) for x in data) < 512 * 1024:
                    data.append(chunk)
        except (OSError, ValueError):
            pass
        out.append(b"".join(data))

    @staticmethod
    def _pump(src, dst, on_bytes):
        """Read a tar stream, write it through, and report
        (total_bytes, regular_file_count) via on_bytes (throttled ~0.2s,
        plus one final call). Returns (total, files, broken).

        Parses 512-byte tar headers: the size field is octal at offset 124,
        the typeflag at offset 156. Only regular files (typeflag 0/'0') with a
        non-empty name are counted, so end-of-archive zero blocks, dirs,
        symlinks and pax/GNU extension headers are ignored. Base-256 sizes
        (entries > 8 GiB) are decoded so counting continues.
        """
        total = 0
        files = 0
        buf = bytearray()
        state = "header"
        data_left = 0
        pad_left = 0
        last_cb = 0.0
        try:
            while True:
                try:
                    chunk = src.read(65536)
                except (OSError, ValueError):
                    return total, files, True
                if not chunk:
                    break
                total += len(chunk)
                buf.extend(chunk)
                pos = 0
                while True:
                    if state == "header":
                        if len(buf) - pos < 512:
                            break
                        h = buf[pos:pos + 512]
                        pos += 512
                        if h[0:100] != bytes(100) and h[156:157] in (b"0", b"\x00"):
                            files += 1
                        if h[124] & 0x80:
                            # base-256 size (entries > 8 GiB): big-endian,
                            # sign bit stored in bit 7 of the first byte
                            size = h[124] & 0x7F
                            for x in h[125:136]:
                                size = (size << 8) | x
                        else:
                            try:
                                size = int(h[124:136].strip(b" \x00") or b"0", 8)
                            except ValueError:
                                size = 0
                        data_left = size
                        pad_left = (512 - size % 512) % 512
                        state = "data"
                    elif state == "data":
                        if data_left <= 0:
                            state = "pad"
                            continue
                        n = min(data_left, len(buf) - pos)
                        data_left -= n
                        pos += n
                        if data_left > 0 and pos >= len(buf):
                            break
                    else:
                        if pad_left <= 0:
                            state = "header"
                            continue
                        n = min(pad_left, len(buf) - pos)
                        pad_left -= n
                        pos += n
                        if pad_left > 0 and pos >= len(buf):
                            break
                del buf[:pos]
                try:
                    dst.write(chunk)
                except (BrokenPipeError, OSError):
                    try:
                        src.close()
                    except OSError:
                        pass
                    return total, files, True
                now = time.monotonic()
                if on_bytes is not None and now - last_cb >= 0.2:
                    on_bytes(total, files)
                    last_cb = now
            try:
                dst.flush()
            except (BrokenPipeError, OSError):
                return total, files, True
            if on_bytes is not None:
                on_bytes(total, files)
            return total, files, False
        finally:
            try:
                src.close()
            except OSError:
                pass
            try:
                dst.close()
            except OSError:
                pass

    def _part_path(self, final):
        base = os.path.basename(final)
        d = os.path.dirname(final)
        with self._part_lock:
            while True:
                cand = os.path.join(
                    d, f".{base}.lan-copier-part-{os.getpid()}-{self._part_counter}")
                self._part_counter += 1
                if not os.path.lexists(cand):
                    return cand

    def _place(self, part, final):
        """Move a finished part onto its final name. Files are swapped with
        os.replace() (atomic: old and new exchange in one step). Directories
        merge into an existing final folder entry-by-entry (old content is
        never bulk-deleted, only updated), and otherwise rename into place.
        Type mismatches (folder where a file was, and vice versa) remove the
        old single entry first, then place the new one."""
        if os.path.isdir(part) and not os.path.islink(part):
            if os.path.isdir(final) and not os.path.islink(final):
                self._merge_dir(part, final)
                self._remove(part)
            else:
                if os.path.lexists(final):
                    os.remove(final)
                os.rename(part, final)
        else:
            if os.path.isdir(final) and not os.path.islink(final):
                shutil.rmtree(final)
            os.replace(part, final)

    def _merge_dir(self, part, final):
        """Move every entry of part into final, merging with what is already
        there: files replace same-named files atomically, directories
        recurse. Nothing in final that is not being overwritten is touched."""
        for name in os.listdir(part):
            src = os.path.join(part, name)
            dst = os.path.join(final, name)
            if os.path.isdir(src) and not os.path.islink(src):
                if os.path.isdir(dst) and not os.path.islink(dst):
                    self._merge_dir(src, dst)
                    continue
                if os.path.lexists(dst):
                    os.remove(dst)
                os.rename(src, dst)
            else:
                if os.path.isdir(dst) and not os.path.islink(dst):
                    shutil.rmtree(dst)
                os.replace(src, dst)
        try:
            os.rmdir(part)
        except OSError:
            shutil.rmtree(part, ignore_errors=True)
            if os.path.lexists(part):
                self.last_error = f"could not remove leftover part folder: {part}"
                sys.stderr.write(f"lan-copier: {self.last_error}\n")

    @staticmethod
    def _fsync_file(path):
        """Flush a finished part file to disk before it is swapped into
        place, so 'done' really means durable. Failures are ignored: some
        filesystems reject fsync and macOS cannot fsync directories."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

    @staticmethod
    def _fsync_dir(path):
        """Flush the directory entries of a merged folder to disk."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

    def _remove(self, path):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as e:
            self.last_error = f"could not remove {path}: {e}"
            sys.stderr.write(f"lan-copier: {self.last_error}\n")

    @staticmethod
    def unique_path(p):
        if not os.path.exists(p):
            return p
        base, ext = os.path.splitext(p)
        i = 1
        while os.path.exists(f"{base} ({i}){ext}"):
            i += 1
        return f"{base} ({i}){ext}"

    @staticmethod
    def _local_size(path):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                total = 0
                for dirpath, _, files in os.walk(path):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
                return total
            return os.path.getsize(path)
        except OSError:
            return None

    @staticmethod
    def _ls_epoch(mon, day, tail):
        """Best-effort epoch for 'ls -la' C-locale mtime strings so the
        Modified column can sort numerically. 'Aug 20 14:30' (no year) is
        taken as the current year, rolled back one year if it lands in the
        future; 'Aug 20  2025' uses the given year. Returns 0 when the
        string cannot be parsed (such rows sort to the end)."""
        try:
            m = SSHConnection._MONTHS.get(mon)
            d = int(day)
            if not m or not 1 <= d <= 31:
                return 0
            if ":" in tail:
                hh, mm = tail.split(":")
                year = time.localtime().tm_year
                ts = time.mktime((year, m, d, int(hh), int(mm), 0, 0, 0, -1))
                if ts > time.time() + 86400:
                    ts = time.mktime((year - 1, m, d, int(hh), int(mm), 0, 0, 0, -1))
                return int(ts)
            return int(time.mktime((int(tail), m, d, 0, 0, 0, 0, 0, -1)))
        except (ValueError, OverflowError, OSError):
            return 0

    @staticmethod
    def _parse_ls(out):
        items = []
        for line in out.splitlines():
            if not line or line[0] not in "-dlbcps":
                continue
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            perms, _, _, _, size, mon, day, t, name = parts
            if name in (".", ".."):
                continue
            items.append({
                "name": name,
                "is_dir": perms[0] == "d",
                "is_link": perms[0] == "l",
                "size": int(size) if size.isdigit() else 0,
                "mtime": f"{mon} {day} {t}",
                "mtime_epoch": SSHConnection._ls_epoch(mon, day, t),
            })
        return items

    @staticmethod
    def _escape_remote(path):
        esc = re.sub(r"([*?\[\]{}'\"\\ $`])", r"\\\1", path)
        if not path.startswith("/") and path.lstrip().startswith("-"):
            esc = "./" + esc
        return esc

    @staticmethod
    def _mux_dead(err):
        low = (err or "").lower()
        return any(s in low for s in (
            "connection closed", "control socket", "packet_write", "broken pipe",
        ))

    @staticmethod
    def _needs_legacy(err):
        low = (err or "").lower()
        return "unable to negotiate" in low or "no matching" in low

    @staticmethod
    def _clean_err(err):
        err = err or ""
        lines = [l for l in err.splitlines() if not l.startswith("Warning: Permanently added")]
        return "\n".join(lines).strip()

    @staticmethod
    def friendly_error(err):
        low = err.lower()
        if "connection refused" in low:
            return ("Connection refused",
                    "No SSH server is running on that host.\n"
                    "On macOS: System Settings → General → Sharing → turn on \"Remote Login\".\n"
                    "On Linux: install and start openssh-server.")
        if "connection timed out" in low or "no route to host" in low:
            return ("Host unreachable",
                    "The host did not answer.\nCheck it is powered on, on the same network,\n"
                    "and that no firewall blocks port 22.")
        if "could not resolve hostname" in low or "temporary failure in name resolution" in low:
            return ("Unknown host name",
                    "The host name could not be resolved.\nUse its IP address instead.")
        if "permission denied" in low:
            return ("Wrong username or password",
                    "The server rejected the login.\n"
                    "• The username must match the account's short name on that machine (case-sensitive)\n"
                    "• The password must be correct\n"
                    "• The account must be allowed to log in via SSH")
        if "too many authentication failures" in low:
            return ("Too many SSH key attempts",
                    "Your SSH agent has too many keys loaded.\nRetrying with password-only auth…")
        if "unable to negotiate" in low or "no matching" in low:
            return ("Old SSH version on that host",
                    "The host's SSH software is too old for this client.\n"
                    "Retrying with legacy compatibility…")
        if "no such file or directory" in low or "cannot access" in low:
            return ("Remote folder not found",
                    "The remote folder does not exist or is not accessible.\n"
                    "• If this is the user's home folder, it may be missing or on an unmounted disk\n"
                    "  (check on that machine: open Terminal and run  ls ~ )\n"
                    "• Otherwise check the path in the path bar")
        return ("Connection failed", err)