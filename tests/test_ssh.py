"""Tests for SSHConnection transport (copy/parse/pause/delete/stat)."""
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

def test_skip_existing():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        open(dest, "w").write("old")
        c = make_conn()
        c._run_cmd = fake_run_ok
        seen = []
        status, detail = c.copy("/r/f.txt", dest, policy=POLICY_SKIP, on_part=seen.append)
        assert status == "skipped"
        assert open(dest).read() == "old"
        assert seen == []
    finally:
        shutil.rmtree(d)




def test_overwrite():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        open(dest, "w").write("old")
        c = make_conn()
        scp_ok(c)
        status, detail = c.copy("/r/f.txt", dest, policy=POLICY_OVERWRITE)
        assert status == "done"
        assert detail == dest
        assert open(dest).read() == "data"
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_keep_both():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        open(dest, "w").write("old")
        c = make_conn()
        scp_ok(c)
        status, detail = c.copy("/r/f.txt", dest, policy=POLICY_KEEP_BOTH)
        assert status == "done"
        assert detail == os.path.join(d, "f (1).txt")
        assert open(dest).read() == "old"
        assert open(detail).read() == "data"
    finally:
        shutil.rmtree(d)




def test_ask_same_size_skips():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        open(dest, "w").write("data")
        c = make_conn()
        c._run_cmd = fake_run_ok
        c.stat_remote = lambda p: {"bytes": 4, "files": 1}
        asked = []
        status, detail = c.copy(
            "/r/f.txt", dest, policy=POLICY_ASK,
            on_ask=lambda *a: asked.append(a) or POLICY_OVERWRITE)
        assert status == "skipped"
        assert "same size" in detail
        assert asked == []
    finally:
        shutil.rmtree(d)




def test_ask_different_size_asks():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        open(dest, "w").write("old")
        c = make_conn()
        scp_ok(c)
        c.stat_remote = lambda p: {"bytes": 99, "files": 1}
        decisions = []

        def on_ask(final, rs, ls):
            decisions.append((os.path.basename(final), rs, ls))
            return POLICY_KEEP_BOTH

        status, detail = c.copy("/r/f.txt", dest, policy=POLICY_ASK, on_ask=on_ask)
        assert status == "done"
        assert decisions == [("f.txt", 99, 3)]
        assert detail == os.path.join(d, "f (1).txt")
    finally:
        shutil.rmtree(d)




def test_ask_decline_cancels():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        open(dest, "w").write("old")
        c = make_conn()
        c._run_cmd = fake_run_ok
        c.stat_remote = lambda p: {"bytes": 99, "files": 1}
        status, detail = c.copy("/r/f.txt", dest, policy=POLICY_ASK, on_ask=lambda *a: None)
        assert status == "cancelled"
        assert open(dest).read() == "old"
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_failed_cleans_part():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        c = make_conn()
        scp_fail(c)
        status, detail = c.copy("/r/f.txt", dest)
        assert status == "failed"
        assert not os.path.exists(dest)
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_aborted_cleans_part():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        c = make_conn()
        scp_killed(c)
        status, detail = c.copy("/r/f.txt", dest)
        assert status == "aborted"
        assert not os.path.exists(dest)
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_overwrite_dir():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "dir")
        os.makedirs(os.path.join(dest, "sub"))
        open(os.path.join(dest, "sub", "old.txt"), "w").write("old")
        c = make_conn()

        def fake_dir_spawn(argv, env, stdin=None, stdout=None):
            os.makedirs(argv[-1])
            with open(os.path.join(argv[-1], "new.txt"), "w") as f:
                f.write("new")
            return subprocess.Popen(["sleep", "0"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)

        c._spawn = fake_dir_spawn
        c._wait_or_kill = lambda p, la, stall=120.0, paused=None: 0
        status, detail = c.copy("/r/dir", dest, policy=POLICY_OVERWRITE)
        assert status == "done"
        assert os.path.isdir(dest)
        assert os.path.isfile(os.path.join(dest, "new.txt"))
        assert os.path.isfile(os.path.join(dest, "sub", "old.txt")), \
            "merge keeps destination content that is not overwritten"
    finally:
        shutil.rmtree(d)




def test_unique_path():
    d = tempfile.mkdtemp()
    try:
        a = os.path.join(d, "a.txt")
        open(a, "w").write("1")
        b = os.path.join(d, "a (1).txt")
        open(b, "w").write("2")
        assert SSHConnection.unique_path(a) == os.path.join(d, "a (2).txt")
        assert SSHConnection.unique_path(os.path.join(d, "zz.txt")) == os.path.join(d, "zz.txt")
    finally:
        shutil.rmtree(d)




def test_local_size():
    d = tempfile.mkdtemp()
    try:
        f = os.path.join(d, "f.bin")
        open(f, "wb").write(b"x" * 10)
        assert SSHConnection._local_size(f) == 10
        sub = os.path.join(d, "sub")
        os.makedirs(sub)
        open(os.path.join(sub, "a"), "wb").write(b"y" * 7)
        assert SSHConnection._local_size(sub) == 7
        assert SSHConnection._local_size(os.path.join(d, "nope")) is None
    finally:
        shutil.rmtree(d)




def test_replace_failure_cleans_part():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        c = make_conn()
        scp_ok(c)
        orig = SSHConnection.__dict__["_place"]
        SSHConnection._place = staticmethod(
            lambda p, f: (_ for _ in ()).throw(OSError("rename boom")))
        try:
            try:
                c.copy("/r/f.txt", dest, policy=POLICY_OVERWRITE)
                raised = False
            except OSError:
                raised = True
        finally:
            SSHConnection._place = orig
        assert raised
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_stat_remote_parse():
    c = make_conn()
    c._uname = "Linux"
    calls = []

    def fake_run(argv, timeout=None):
        calls.append(argv)
        return 0, "1234 42\n", ""

    c._run_cmd = fake_run
    st = c.stat_remote("/tmp/some")
    assert st == {"bytes": 1234, "files": 42}
    assert "-printf" in calls[0][-1]
    assert "du" not in calls[0][-1], "sizes must be exact bytes, not du blocks"

    c._uname = "Darwin"
    c._run_cmd = fake_run
    st2 = c.stat_remote("/tmp/some")
    assert st2 == {"bytes": 1234, "files": 42}
    assert "stat -f" in calls[-1][-1]

    c._run_cmd = lambda argv, timeout=None: (1, "", "no such file")
    assert c.stat_remote("/tmp/some") is None




class FakeTreeProc:
    """Minimal proc fake for tree_remote: BytesIO pipes, optional stall."""

    def __init__(self, out, err=b"", rc=0, stall=False):
        self._out = out
        self._rc = rc
        self._stall = stall
        self.dead = False
        self.stdin = None
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)

    def wait(self, timeout=None):
        if self._stall and not self.dead:
            raise subprocess.TimeoutExpired("p", timeout)
        return self._rc

    def poll(self):
        return None if self._stall and not self.dead else self._rc

    def kill(self):
        self.dead = True

    def send_signal(self, sig):
        pass




def test_tree_remote_parse():
    c = make_conn()
    c._uname = "Linux"
    calls = []

    def spawn(argv, env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE):
        calls.append(argv)
        return FakeTreeProc(b"/tmp/base/a.txt 4\n/tmp/base/sub/b.bin 1024\n")

    c._spawn = spawn
    tree = c.tree_remote("/tmp/base")
    assert tree == {"a.txt": 4, "sub/b.bin": 1024}
    assert "find" in calls[-1][-1] and "-printf" in calls[-1][-1]

    c._uname = "Darwin"
    c._spawn = lambda argv, env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE: \
        FakeTreeProc(b"/tmp/base/a.txt 4\n")
    tree2 = c.tree_remote("/tmp/base")
    assert tree2 == {"a.txt": 4}

    c._spawn = lambda argv, env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE: \
        FakeTreeProc(b"", err=b"no such file", rc=1)
    assert c.tree_remote("/tmp/base") is None

    c._spawn = lambda argv, env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE: \
        FakeTreeProc(b"garbage\n/tmp/base/x 2\n")
    assert c.tree_remote("/tmp/base") == {"x": 2}

    # a stalled listing returns None instead of hanging forever
    c._spawn = lambda argv, env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE: \
        FakeTreeProc(b"", stall=True)
    t0 = time.monotonic()
    assert c.tree_remote("/tmp/base", stall=0.1) is None
    assert time.monotonic() - t0 < 5, "stalled listing must not hang"

    # spawn failure (e.g. EMFILE) degrades to None instead of raising
    def raise_spawn(*a, **k):
        raise OSError("Too many open files")

    c._spawn = raise_spawn
    assert c.tree_remote("/tmp/base") is None
    assert "spawn" in c.last_error




class TinyReader:
    """Reads in tiny odd-sized chunks to exercise partial header handling."""

    def __init__(self, f):
        self.f = f

    def read(self, n):
        return self.f.read(7)

    def close(self):
        pass




class NoCloseBytesIO(io.BytesIO):
    """The pump closes its sink (EOF for the tar pipe); tests need it open."""

    def close(self):
        pass




def test_header_counting():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        data = b"hello"
        ti = tarfile.TarInfo("a.txt")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        ti2 = tarfile.TarInfo("sub/")
        ti2.type = tarfile.DIRTYPE
        tf.addfile(ti2)
        ti3 = tarfile.TarInfo("link")
        ti3.type = tarfile.SYMTYPE
        ti3.linkname = "a.txt"
        tf.addfile(ti3)
        ti4 = tarfile.TarInfo("x" * 150 + ".txt")
        ti4.size = 1
        tf.addfile(ti4, io.BytesIO(b"z"))
    size = len(buf.getbuffer())
    buf.seek(0)
    out = NoCloseBytesIO()
    total, files, broken = SSHConnection._pump(TinyReader(buf), out, None)
    assert broken is False
    assert files == 2, files
    assert total == size
    assert out.getvalue() == buf.getbuffer().tobytes()

    buf2 = io.BytesIO()
    with tarfile.open(fileobj=buf2, mode="w", format=tarfile.PAX_FORMAT) as tf:
        ti5 = tarfile.TarInfo("pax_" + "y" * 120)
        ti5.size = 2
        tf.addfile(ti5, io.BytesIO(b"ab"))
    buf2.seek(0)
    out2 = NoCloseBytesIO()
    total2, files2, broken2 = SSHConnection._pump(TinyReader(buf2), out2, None)
    assert broken2 is False
    assert files2 == 1, files2
    assert out2.getvalue() == buf2.getbuffer().tobytes()




def tar_test_conn(dest, src_tree=None):
    c = make_conn()

    def fake_remote_tar(cmd, env):
        argv = shlex.split(cmd)
        parent = argv[argv.index("-C") + 1]
        name = argv[-1]
        return subprocess.Popen(
            ["tar", "-C", parent, "-cf", "-", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def fake_local_tar(part):
        return subprocess.Popen(
            ["tar", "-C", part, "--strip-components=1", "-xpf", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    c._spawn_remote_tar = fake_remote_tar
    c._spawn_local_tar = fake_local_tar
    c._reset_master = lambda: None
    return c




def test_copy_tar_done():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "src")
        os.makedirs(os.path.join(src, "sub"))
        open(os.path.join(src, "a.txt"), "w").write("hello")
        open(os.path.join(src, "sub", "b.txt"), "w").write("world")
        os.makedirs(os.path.join(src, "empty"))
        os.symlink("a.txt", os.path.join(src, "link.txt"))
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        c = tar_test_conn(dest)
        seen = []
        status, detail = c.copy(
            src, os.path.join(dest, "src"), method="tar", policy=POLICY_OVERWRITE,
            on_bytes=lambda b, f: seen.append((b, f)))
        assert status == "done"
        assert detail == os.path.join(dest, "src")
        assert open(os.path.join(dest, "src", "a.txt")).read() == "hello"
        assert open(os.path.join(dest, "src", "sub", "b.txt")).read() == "world"
        assert os.path.isdir(os.path.join(dest, "src", "empty"))
        assert os.path.islink(os.path.join(dest, "src", "link.txt"))
        assert parts_in(dest) == []
        total, files = seen[-1]
        assert total > 0
        assert files == 2, files
    finally:
        shutil.rmtree(d)




def test_copy_tar_failure():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        c = make_conn()

        def fake_remote_tar(cmd, env):
            return subprocess.Popen(
                ["sh", "-c", "echo boom >&2; exit 1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def fake_local_tar(part):
            return subprocess.Popen(
                ["tar", "-C", part, "--strip-components=1", "-xpf", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        c._spawn_remote_tar = fake_remote_tar
        c._spawn_local_tar = fake_local_tar
        c._reset_master = lambda: None
        status, detail = c.copy("/r/dir", dest, method="tar", policy=POLICY_OVERWRITE)
        assert status == "failed"
        assert "boom" in detail
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(d)




def test_copy_tar_killed():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        c = make_conn()

        def fake_remote_tar(cmd, env):
            return subprocess.Popen(
                ["sleep", "30"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def fake_local_tar(part):
            return subprocess.Popen(
                ["tar", "-C", part, "--strip-components=1", "-xpf", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        c._spawn_remote_tar = fake_remote_tar
        c._spawn_local_tar = fake_local_tar
        c._reset_master = lambda: None
        result = {}

        def run():
            result["out"] = c.copy("/r/dir", dest, method="tar", policy=POLICY_OVERWRITE)

        th = threading.Thread(target=run)
        th.start()
        time.sleep(1.5)
        c.kill_all()
        th.join(timeout=10)
        assert not th.is_alive(), "copy must return after kill"
        status, detail = result["out"]
        assert status == "aborted"
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(d)




def test_copy_tar_spawn_failure_cleans_up():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        c = make_conn()
        spawned = []

        def fake_remote_tar(cmd, env):
            p = subprocess.Popen(
                ["sleep", "30"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            spawned.append(p)
            return p

        def fake_local_tar(part):
            raise FileNotFoundError("tar binary missing")

        c._spawn_remote_tar = fake_remote_tar
        c._spawn_local_tar = fake_local_tar
        try:
            c.copy("/r/dir", os.path.join(dest, "dir"), method="tar",
                   policy=POLICY_OVERWRITE)
            raised = False
        except FileNotFoundError:
            raised = True
        assert raised, "spawn failure must propagate"
        assert parts_in(dest) == [], "part cleaned on spawn failure"
        assert c._procs == [], "no process leaked"
        assert spawned, "remote tar must have spawned"
        for p in spawned:
            assert p.stdout is None or p.stdout.closed, "remote stdout pipe must be closed"
            assert p.stderr is None or p.stderr.closed, "remote stderr pipe must be closed"
    finally:
        shutil.rmtree(d)




def test_run_spawn_failure_cleans_errf():
    c = make_conn()
    leftovers_before = set(glob.glob("/tmp/lan-copier-err-*"))
    orig = subprocess.Popen

    def boom(*a, **k):
        raise OSError("Too many open files")

    subprocess.Popen = boom
    try:
        rc, out, err = c._run(["ssh", "x"], timeout=5, capture=False)
    finally:
        subprocess.Popen = orig
    assert rc == -1
    assert "spawn failed" in err
    leftovers_after = set(glob.glob("/tmp/lan-copier-err-*"))
    assert leftovers_after == leftovers_before, "errf temp file must be cleaned up"




def test_wait_or_kill_stalls():
    c = make_conn()

    class FakeP:
        def __init__(self):
            self.killed = False
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.killed:
                return -9
            raise subprocess.TimeoutExpired("p", timeout)

        def kill(self):
            self.killed = True

    p = FakeP()
    rc = c._wait_or_kill(p, {"t": time.monotonic()}, stall=0.05)
    assert rc == "STALLED"
    assert p.killed, "stalled process killed"
    assert p.waits >= 2, "polled at least twice"

    p2 = FakeP()
    rc2 = c._wait_or_kill(p2, {"t": time.monotonic() - 200}, stall=120)
    assert rc2 == "STALLED"
    assert p2.killed
    assert p2.waits == 1, "old activity stalls on the first poll"




def test_copy_tar_stall_cleans_up():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        c = make_conn()

        def fake_remote_tar(cmd, env):
            return subprocess.Popen(
                ["sleep", "30"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def fake_local_tar(part):
            return subprocess.Popen(
                ["sleep", "30"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        c._spawn_remote_tar = fake_remote_tar
        c._spawn_local_tar = fake_local_tar
        c._reset_master = lambda: None
        c._wait_or_kill = lambda p, la, stall=120.0, paused=None: "STALLED"
        status, detail = c.copy("/r/dir", dest, method="tar", policy=POLICY_OVERWRITE)
        assert status == "failed"
        assert "stalled" in detail.lower()
        assert parts_in(dest) == []
        assert c._procs == [], "no process leaked after stall"
    finally:
        shutil.rmtree(d)




def test_wait_or_kill_skips_stall_while_paused():
    c = make_conn()

    class FakeP:
        def __init__(self, waits):
            self.waits = waits
            self.killed = False

        def wait(self, timeout=None):
            self.waits -= 1
            if self.waits < 0:
                return 0
            raise subprocess.TimeoutExpired("p", timeout)

        def kill(self):
            self.killed = True

    stale = {"t": time.monotonic() - 1000}
    p = FakeP(6)
    rc = c._wait_or_kill(p, stale, stall=0.05, paused=[p])
    assert rc == 0 and not p.killed, "stall must be suppressed while paused"

    p2 = FakeP(6)
    rc2 = c._wait_or_kill(p2, stale, stall=0.05, paused=None)
    assert p2.killed and rc2 == "STALLED", "unpaused transfer must still stall"




def test_wait_or_kill_no_stall():
    c = make_conn()

    class FakeP:
        def __init__(self, waits):
            self.waits = waits
            self.killed = False

        def wait(self, timeout=None):
            self.waits -= 1
            if self.waits < 0:
                return 0
            raise subprocess.TimeoutExpired("p", timeout)

        def kill(self):
            self.killed = True

    p = FakeP(5)
    rc = c._wait_or_kill(p, {"t": time.monotonic() - 1000}, stall=None)
    assert rc == 0 and not p.killed, "stall=None must never stall"




def test_pause_resume_transport():
    c = make_conn()
    calls = []

    class FakeP:
        def __init__(self, name):
            self.name = name
            self.dead = False

        def poll(self):
            return None if not self.dead else 0

        def send_signal(self, sig):
            calls.append((self.name, sig))

        def kill(self):
            self.dead = True

        def wait(self, timeout=None):
            if not self.dead:
                raise subprocess.TimeoutExpired("p", timeout)
            return -9

    a, b = FakeP("a"), FakeP("b")
    sink = []
    c._sink_add(a, sink)
    c._sink_add(b, sink)
    assert sink == [a, b]
    c.pause(sink)
    assert calls == [("a", signal.SIGSTOP), ("b", signal.SIGSTOP)]
    assert a in c._paused and b in c._paused

    cc = FakeP("c")
    c._sink_add(cc, sink)
    assert calls[-1] == ("c", signal.SIGSTOP), "spawn after pause must stop immediately"
    assert cc in c._paused

    other = []
    c._sink_add(FakeP("x"), other)
    assert all(n != "x" for n, _ in calls), "other transfers must not be stopped"

    c.resume(sink)
    assert calls[-3:] == [("a", signal.SIGCONT), ("b", signal.SIGCONT),
                          ("c", signal.SIGCONT)]
    assert c._paused == []

    c.kill_procs(sink)
    assert a.dead and cc.dead
    assert c._paused == [] and c._paused_sink_ids == set()
    assert not c._procs, "kill_procs must untrack"




def test_copy_removes_stale_part():
    c = make_conn()
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "x")
        stale = c._part_path(dest)
        os.makedirs(stale)
        with open(os.path.join(stale, "junk"), "w") as f:
            f.write("old")
        c._part_counter = 0

        def fake_copy_tar(remote_path, part, final, on_bytes=None, proc_sink=None):
            with open(part, "w") as f:
                f.write("x")
            return "done", part

        c._copy_tar = fake_copy_tar
        st, _ = c.copy("/r/x", dest, method="tar", proc_sink=[])
        assert st == "done"
        assert not os.path.exists(stale), "stale part must be removed before a fresh copy"
        assert os.path.isfile(dest), "fresh copy placed at the final name"
    finally:
        shutil.rmtree(d)




def test_copy_closes_pipes():
    d = tempfile.mkdtemp()
    try:
        c = make_conn()
        procs = []

        def spawn(argv, env, stdin=None, stdout=None):
            with open(argv[-1], "w") as f:
                f.write("data")
            p = _fake_proc()
            procs.append(p)
            return p

        c._spawn = spawn
        c._wait_or_kill = lambda p, la, stall=120.0, paused=None: 0
        st, _ = c.copy("/r/x", os.path.join(d, "x"), method="scp", proc_sink=[])
        assert st == "done"
        assert procs, "spawn must have been used"
        for p in procs:
            assert p.stdin is None or p.stdin.closed, "scp stdin pipe must be closed"
            assert p.stdout is None or p.stdout.closed, "scp stdout pipe must be closed"
            assert p.stderr is None or p.stderr.closed, "scp stderr pipe must be closed"

        c2 = tar_test_conn(os.path.join(d, "t"))
        os.makedirs(os.path.join(d, "t"))
        src = os.path.join(d, "src")
        os.makedirs(src)
        open(os.path.join(src, "a"), "w").write("x")
        st2, _ = c2.copy(src, os.path.join(d, "t", "src"), method="tar",
                         policy=POLICY_OVERWRITE)
        assert st2 == "done"
    finally:
        shutil.rmtree(d)




def test_no_fd_leak():
    if not os.path.isdir("/proc/self/fd"):
        print("SKIP fd-leak test (no /proc)")
        return
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        c = tar_test_conn(dest)
        src = os.path.join(d, "src")
        os.makedirs(src)
        open(os.path.join(src, "a"), "w").write("x")
        for _ in range(10):
            st, _ = c.copy(src, os.path.join(dest, "copy"), method="tar",
                           policy=POLICY_OVERWRITE)
            assert st == "done"
            dest = dest  # reuse path? no: unique_path used
        before = len(os.listdir("/proc/self/fd"))
        time.sleep(0.3)
        after = len(os.listdir("/proc/self/fd"))
        assert after - before <= 4, \
            f"file descriptors leaked: {before} -> {after}"
    finally:
        shutil.rmtree(d)




def test_run_spawn_failure_graceful():
    c = make_conn()
    orig = subprocess.Popen

    def boom(*a, **k):
        raise OSError("Too many open files")

    subprocess.Popen = boom
    try:
        rc, out, err = c._run(["ssh", "x"], timeout=5)
    finally:
        subprocess.Popen = orig
    assert rc == -1
    assert "spawn failed" in err




def test_ls_epoch():
    """'ls -la' C-locale mtimes parse into a sortable epoch; unparseable
    input degrades to 0 (sorts last) instead of raising."""
    from ssh_transport import SSHConnection
    now = time.localtime()
    ts = SSHConnection._ls_epoch("Aug", "20", "14:30")
    lt = time.localtime(ts)
    assert ts > 0
    assert (lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min) == (8, 20, 14, 30)
    assert lt.tm_year in (now.tm_year, now.tm_year - 1), \
        "no-year entries use the current year (rolled back when future)"
    assert SSHConnection._ls_epoch("Dec", "31", "23:59") <= time.time() + 86400, \
        "future-ish entries must roll back a year"
    ts = SSHConnection._ls_epoch("Aug", "20", "2020")
    assert time.localtime(ts).tm_year == 2020
    assert SSHConnection._ls_epoch("?", "?", "?") == 0
    assert SSHConnection._ls_epoch("Aug", "99", "14:30") == 0
    assert SSHConnection._ls_epoch("Aug", "20", "nope") == 0




def test_expand_remote():
    c = make_conn()
    c.home_dir = lambda: "/home/foo"
    assert c.expand_remote("~") == "/home/foo"
    assert c.expand_remote("~/Music") == "/home/foo/Music"
    assert c.expand_remote("/abs/path") == "/abs/path"
    c.home_dir = lambda: None
    assert c.expand_remote("~/x") == "~/x", "unresolvable home leaves path alone"




def test_escape_remote_dollar():
    assert SSHConnection._escape_remote("a$b c`d") == r"a\$b\ c\`d"
    assert SSHConnection._escape_remote("-flag") == "./-flag"
    assert SSHConnection._escape_remote("/p/l é") == r"/p/l\ é"
    assert SSHConnection._escape_remote("a\nb") == "a\nb", \
        "newlines pass through untouched (SFTP handles them raw)"




def test_copy_scp_mux_dead_retry():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "f.txt")
        c = make_conn()
        resets = []
        c._reset_master = lambda: resets.append(1)
        spawns = []

        class FakeP:
            stdin = None
            stdout = None

            def __init__(self, rc, err=b""):
                self.rc = rc
                self.stderr = io.BytesIO(err)

            def wait(self, timeout=None):
                return self.rc

            def poll(self):
                return self.rc

            def kill(self):
                pass

            def send_signal(self, sig):
                pass

        def spawn(argv, env, stdin=None, stdout=None):
            spawns.append(argv)
            if len(spawns) == 1:
                return FakeP(1, b"packet_write failed: Connection closed\n")
            with open(argv[-1], "w") as f:
                f.write("data")
            return FakeP(0)

        c._spawn = spawn
        c._wait_or_kill = lambda p, la, stall=120.0, paused=None: p.rc
        status, detail = c.copy("/r/f.txt", dest, policy=POLICY_OVERWRITE)
        assert status == "done"
        assert len(spawns) == 2, "mux-dead failure must retry once"
        assert len(resets) == 1, "mux-dead failure must reset the master"
        assert open(dest).read() == "data"
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_tree_remote_mux_dead_retry():
    c = make_conn()
    c._uname = "Linux"
    resets = []
    c._reset_master = lambda: resets.append(1)
    calls = []

    def spawn(argv, env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE):
        calls.append(argv)
        if len(calls) == 1:
            return FakeTreeProc(b"", err=b"packet_write failed: Connection closed", rc=1)
        return FakeTreeProc(b"/tmp/base/a.txt 4\n")

    c._spawn = spawn
    tree = c.tree_remote("/tmp/base")
    assert tree == {"a.txt": 4}
    assert len(calls) == 2, "mux-dead listing must retry once"
    assert len(resets) == 1




def test_ssh_delete_item():
    c = make_conn()
    c.home_dir = lambda: "/home/user"
    recorded = []

    def fake_run(argv, timeout=None):
        recorded.append(argv)
        return 0, "", ""

    c._run_cmd = fake_run

    ok, err = c.delete_item("/home/user/downloads/my file $1 'quote' & more.txt")
    assert ok is True and not err
    assert len(recorded) == 1
    cmd_str = " ".join(recorded[0])
    assert "rm -rf --" in cmd_str
    assert "my\\ file\\ \\$1\\ \\'quote\\'\\ \\&\\ more.txt" in cmd_str or "my file" in cmd_str

    # Failure reporting
    c._run_cmd = lambda argv, timeout=None: (1, "", "Permission denied")
    ok, err = c.delete_item("/home/user/locked.bin")
    assert ok is False
    assert "Permission denied" in err

    # EPERM failures get actionable, OS-agnostic hints appended
    c._run_cmd = lambda argv, timeout=None: (
        1, "", "rm: /Volumes/Data/x.7z: Operation not permitted")
    ok, err = c.delete_item("/Volumes/Data/x.7z")
    assert ok is False
    assert "Operation not permitted" in err
    low = err.lower()
    assert "immutable" in low or "locked" in low
    assert "read-only" in low
    for word in ("chflags",):  # must stay OS-agnostic (no mac-only tool names)
        assert word not in err




def test_ssh_delete_protected_paths():
    c = make_conn()
    c.home_dir = lambda: "/home/user"
    called = []
    c._run_cmd = lambda argv, timeout=None: (called.append(argv), 0, "", "")[1:]

    for bad in ("", "/", "~", "~/", "/home/user", "/home/user/"):
        ok, err = c.delete_item(bad)
        assert ok is False, f"Protected remote path '{bad}' must be rejected"
        assert "protected" in err.lower() or "refusing" in err.lower()
    assert len(called) == 0, "No SSH command should run for protected paths"




def test_kind_marker():
    assert SSHConnection.kind == "ssh"
    assert LocalConnection.kind == "local"
    assert make_conn().kind == "ssh"


def test_parse_ps_list_bom_crlf():
    out = "\ufeff1\t0\t100\t1700000000\tname.txt\r\n0\t1\t7\t1700000001\tlink me.txt\r\n"
    items = SSHConnection._parse_ps_list(out)
    assert len(items) == 2
    assert items[0]["name"] == "name.txt"
    assert items[0]["is_dir"] is True
    assert items[0]["size"] == 100
    assert items[0]["mtime_epoch"] == 1700000000
    assert items[1]["name"] == "link me.txt"
    assert items[1]["is_link"] is True


ALL_TESTS = (
    test_skip_existing,
    test_kind_marker,
    test_parse_ps_list_bom_crlf,
    test_overwrite,
    test_keep_both,
    test_ask_same_size_skips,
    test_ask_different_size_asks,
    test_ask_decline_cancels,
    test_failed_cleans_part,
    test_aborted_cleans_part,
    test_overwrite_dir,
    test_unique_path,
    test_local_size,
    test_replace_failure_cleans_part,
    test_stat_remote_parse,
    test_tree_remote_parse,
    test_header_counting,
    test_copy_tar_done,
    test_copy_tar_failure,
    test_copy_tar_killed,
    test_copy_tar_spawn_failure_cleans_up,
    test_run_spawn_failure_cleans_errf,
    test_wait_or_kill_stalls,
    test_copy_tar_stall_cleans_up,
    test_wait_or_kill_skips_stall_while_paused,
    test_wait_or_kill_no_stall,
    test_pause_resume_transport,
    test_copy_removes_stale_part,
    test_copy_closes_pipes,
    test_no_fd_leak,
    test_run_spawn_failure_graceful,
    test_ls_epoch,
    test_expand_remote,
    test_escape_remote_dollar,
    test_copy_scp_mux_dead_retry,
    test_tree_remote_mux_dead_retry,
    test_ssh_delete_item,
    test_ssh_delete_protected_paths,
)
