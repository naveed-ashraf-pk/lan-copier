"""Shared fixtures/helpers for the lan-copier test suite (plain asserts)."""
import glob
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssh_transport import (
    SSHConnection, POLICY_ASK, POLICY_OVERWRITE, POLICY_KEEP_BOTH, POLICY_SKIP,
)
from local_transport import LocalConnection, dir_list, dir_tree, delete_local_item
import tree_exporter

from tests import common
from tests.common import *

def make_conn():
    c = SSHConnection("host", 22, "user", "pw")
    c._ensure_master = lambda: True
    return c




def fake_run_ok(argv, timeout=None):
    with open(argv[-1], "w") as f:
        f.write("data")
    return 0, "", ""




def fake_run_fail(argv, timeout=None):
    return 1, "", "some error"




def fake_run_killed(argv, timeout=None):
    return -9, "", ""




def fake_scp_spawn(argv, env, stdin=None, stdout=None):
    with open(argv[-1], "w") as f:
        f.write("data")
    return subprocess.Popen(["sleep", "0"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)




def _fake_proc():
    return subprocess.Popen(["sleep", "0"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)




def scp_ok(c):
    c._spawn = fake_scp_spawn
    c._wait_or_kill = lambda p, la, stall=120.0, paused=None: 0




def scp_fail(c):
    c._spawn = lambda argv, env, stdin=None, stdout=None: _fake_proc()
    c._wait_or_kill = lambda p, la, stall=120.0, paused=None: 1




def scp_killed(c):
    c._spawn = lambda argv, env, stdin=None, stdout=None: _fake_proc()
    c._wait_or_kill = lambda p, la, stall=120.0, paused=None: -9




def parts_in(d):
    return [p for p in os.listdir(d) if ".lan-copier-part" in p]




def _ps_decode(cmd):
    """Return the decoded PowerShell script body for an encoded command."""
    assert cmd.startswith("powershell -NoProfile -EncodedCommand ")
    b64 = cmd.split(" ", 3)[-1]
    return base64.b64decode(b64).decode("utf-16le")




class FakePosixSsh(SSHConnection):
    """A real POSIX connection living on the local filesystem. It executes
    the exact remote command strings the engine emits (tar / sh merge / rm)
    through a real shell, so the whole remote-destination pipeline is tested
    end-to-end on this machine without a network host."""

    kind = "ssh"
    os_type = "Linux"

    def __init__(self, root, host="fake", port=22, user="u"):
        super().__init__(host, port, user, "")
        self._os_type = "Linux"
        self.host = host
        self.port = port
        self.user = user
        self.root = root
        self.last_error = ""
        self.shell_cmds = []

    def _ensure_master(self):
        return True

    def key(self):
        return ("ssh", self.host, self.port, self.user)

    def expand_remote(self, p):
        return str(p)

    @property
    def family(self):
        return "posix"

    def _os_windows(self):
        return False

    def exists(self, path):
        return os.path.lexists(path)

    def size(self, path):
        st = self.stat(path)
        return st["bytes"] if st else None

    def stat(self, path):
        if os.path.isdir(path) and not os.path.islink(path):
            total = 0
            n = 0
            for dp, _, fs in os.walk(path):
                for f in fs:
                    total += os.path.getsize(os.path.join(dp, f))
                    n += 1
            return {"bytes": total, "files": n}
        try:
            return {"bytes": os.path.getsize(path), "files": 1}
        except OSError:
            return None

    def is_dir(self, path):
        return os.path.isdir(path) and not os.path.islink(path)

    def mkdir(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            return True, ""
        except OSError as e:
            return False, str(e)

    def rename(self, src, dst):
        try:
            os.rename(src, dst)
            return True, ""
        except OSError as e:
            return False, str(e)

    def unique_path(self, path):
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(f"{base} ({i}){ext}"):
            i += 1
        return f"{base} ({i}){ext}"

    def rm_remote(self, path, recursive=True):
        from local_transport import delete_local_item
        return delete_local_item(path)

    def rmdir(self, path):
        try:
            self.rm_remote(path, recursive=True)
        except Exception:
            pass

    def _run_cmd(self, argv, timeout=None):
        cmd = argv[-1]
        self.shell_cmds.append(cmd)
        try:
            r = subprocess.run(["sh", "-c", cmd], capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        return r.returncode, r.stdout, r.stderr

    def spawn_ssh(self, remote_cmd, stdin=subprocess.DEVNULL):
        p = subprocess.Popen(["sh", "-c", remote_cmd],
                             stdin=stdin, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        p._lancopier_tmpd = None
        return p




def _engine_src_and_dest():
    import transfer_engine
    from local_transport import LocalConnection
    src_root = tempfile.mkdtemp(prefix="lan-eng-src-")
    dst_root = tempfile.mkdtemp(prefix="lan-eng-dst-")
    src = LocalConnection()
    dest = FakePosixSsh(dst_root)
    return transfer_engine, src, dest, src_root, dst_root





__all__ = [
    "make_conn", "fake_run_ok", "fake_run_fail", "fake_run_killed",
    "fake_scp_spawn", "_fake_proc", "scp_ok", "scp_fail", "scp_killed",
    "parts_in", "_ps_decode", "FakePosixSsh", "_engine_src_and_dest",
]
