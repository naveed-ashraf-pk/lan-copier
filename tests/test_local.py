"""Tests for LocalConnection and local-filesystem operations."""
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

def test_dir_list():
    from local_transport import dir_list

    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, "f.txt"), "w").write("hello")
        os.makedirs(os.path.join(d, "sub"))
        try:
            os.symlink("f.txt", os.path.join(d, "lnk"))
        except OSError:
            lnk = False
        else:
            lnk = True
        items = dir_list(d)
        assert items is not None
        by_name = {it["name"]: it for it in items}
        assert by_name["f.txt"]["is_dir"] is False
        assert by_name["f.txt"]["size"] == 5
        assert isinstance(by_name["f.txt"]["mtime_epoch"], int), \
            "raw epoch mtime must be present for sorting"
        assert by_name["f.txt"]["mtime_epoch"] > 0
        assert by_name["sub"]["is_dir"] is True
        assert by_name["sub"]["size"] == 0
        if lnk:
            assert by_name["lnk"]["is_link"] is True
            assert by_name["lnk"]["is_dir"] is False
        assert dir_list(os.path.join(d, "nope")) is None, \
            "missing folder must return None like the remote side"
        assert dir_list("~") is not None, "local tilde must expand to the home folder"
    finally:
        shutil.rmtree(d)




def local_conn():
    return LocalConnection()




def test_local_home_and_expand():
    c = local_conn()
    assert c._ensure_master() is True
    assert c.home_dir() == os.path.expanduser("~")
    assert c.expand_remote("~/x") == os.path.join(os.path.expanduser("~"), "x")
    assert c.expand_remote("/abs/path") == "/abs/path"
    assert c.expand_remote("relative") == "relative"




def test_local_list_dir():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "f.txt"), "w") as f:
            f.write("data")
        os.makedirs(os.path.join(d, "sub"))
        c = local_conn()
        items = c.list_dir(d)
        by_name = {it["name"]: it for it in items}
        assert "f.txt" in by_name and by_name["f.txt"]["is_dir"] is False
        assert by_name["f.txt"]["size"] == 4
        assert by_name["f.txt"]["path"] == os.path.join(d, "f.txt")
        assert "is_link" in by_name["f.txt"] and "mtime" in by_name["f.txt"]
        assert isinstance(by_name["f.txt"]["mtime_epoch"], int)
        assert by_name["sub"]["is_dir"] is True
        assert c.list_dir(os.path.join(d, "nope")) is None
        assert c.last_error, "list failure must record an error for the dialog"
    finally:
        shutil.rmtree(d)




def test_local_list_home_tilde():
    d = tempfile.mkdtemp()
    old = os.environ.get("HOME")
    os.environ["HOME"] = d
    try:
        with open(os.path.join(d, "f.txt"), "w") as f:
            f.write("x")
        c = local_conn()
        names = {it["name"] for it in c.list_dir("~")}
        assert "f.txt" in names, "~ must expand to HOME"
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old
        shutil.rmtree(d)




def test_local_list_dir_error_kinds():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "f.txt"), "w") as f:
            f.write("x")
        c = local_conn()
        afile = os.path.join(d, "f.txt")
        assert c.list_dir(afile) is None
        assert "not a directory" in c.last_error, c.last_error
        amiss = os.path.join(d, "nope")
        assert c.list_dir(amiss) is None
        assert "no such file" in c.last_error, c.last_error
        if os.geteuid() != 0:
            locked = os.path.join(d, "locked")
            os.makedirs(locked)
            os.chmod(locked, 0)
            try:
                assert c.list_dir(locked) is None
                assert "permission denied" in c.last_error, c.last_error
            finally:
                os.chmod(locked, 0o755)
    finally:
        shutil.rmtree(d)




def test_local_copy_scp_dir_rejected():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "folder")
        os.makedirs(src)
        with open(os.path.join(src, "a.txt"), "w") as f:
            f.write("data")
        c = local_conn()
        status, detail = c.copy(src, os.path.join(d, "out"), policy=POLICY_OVERWRITE)
        assert status == "failed", status
        assert "file method" in detail, detail
        assert not os.path.exists(os.path.join(d, "out")), "nothing placed"
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_local_stat_remote():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("abc")
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "sub", "b.txt"), "w") as f:
            f.write("de")
        c = local_conn()
        st = c.stat_remote(d)
        assert st["bytes"] == 5 and st["files"] == 2
        st = c.stat_remote(os.path.join(d, "a.txt"))
        assert st["bytes"] == 3 and st["files"] == 1
        assert c.stat_remote(os.path.join(d, "nope")) is None
    finally:
        shutil.rmtree(d)




def test_local_tree_remote():
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("abc")
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "sub", "b.txt"), "w") as f:
            f.write("de")
        c = local_conn()
        tree = c.tree_remote(d)
        assert tree == {"a.txt": 3, "sub/b.txt": 2}
        assert c.tree_remote(os.path.join(d, "nope")) is None, \
            "missing source folder must fail like the SSH side"
    finally:
        shutil.rmtree(d)




def test_local_copy_file_overwrite():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "src.txt")
        dest = os.path.join(d, "dest.txt")
        with open(src, "w") as f:
            f.write("new")
        with open(dest, "w") as f:
            f.write("old")
        c = local_conn()
        status, detail = c.copy(src, dest, policy=POLICY_OVERWRITE)
        assert status == "done"
        assert detail == dest
        assert open(dest).read() == "new"
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_local_copy_file_skip():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "src.txt")
        dest = os.path.join(d, "dest.txt")
        with open(src, "w") as f:
            f.write("new")
        with open(dest, "w") as f:
            f.write("old")
        c = local_conn()
        status, _ = c.copy(src, dest, policy=POLICY_SKIP)
        assert status == "skipped"
        assert open(dest).read() == "old"
    finally:
        shutil.rmtree(d)




def test_local_copy_file_keep_both():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "src.txt")
        dest = os.path.join(d, "dest.txt")
        with open(src, "w") as f:
            f.write("new")
        with open(dest, "w") as f:
            f.write("old")
        c = local_conn()
        status, detail = c.copy(src, dest, policy=POLICY_KEEP_BOTH)
        assert status == "done"
        assert detail == os.path.join(d, "dest (1).txt")
        assert open(dest).read() == "old"
        assert open(detail).read() == "new"
    finally:
        shutil.rmtree(d)




def test_local_copy_ask_same_size_skips():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "src.txt")
        dest = os.path.join(d, "dest.txt")
        with open(src, "w") as f:
            f.write("data")
        with open(dest, "w") as f:
            f.write("data")
        c = local_conn()
        asked = []
        status, _ = c.copy(src, dest, policy=POLICY_ASK,
                           on_ask=lambda f, r, l: asked.append((f, r, l)))
        assert status == "skipped", "same size must skip silently, no dialog"
        assert asked == []
    finally:
        shutil.rmtree(d)




def test_local_copy_ask_overwrite():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "src.txt")
        dest = os.path.join(d, "dest.txt")
        with open(src, "w") as f:
            f.write("newcontent")
        with open(dest, "w") as f:
            f.write("old")
        c = local_conn()
        asked = []
        status, _ = c.copy(src, dest, policy=POLICY_ASK,
                           on_ask=lambda f, r, l: asked.append((f, r, l)) or POLICY_OVERWRITE)
        assert status == "done"
        assert len(asked) == 1 and asked[0][1] == 10, asked
        assert open(dest).read() == "newcontent"
    finally:
        shutil.rmtree(d)




def test_local_copy_ask_cancel():
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "src.txt")
        dest = os.path.join(d, "dest.txt")
        with open(src, "w") as f:
            f.write("newcontent")
        with open(dest, "w") as f:
            f.write("old")
        c = local_conn()
        status, _ = c.copy(src, dest, policy=POLICY_ASK, on_ask=lambda f, r, l: None)
        assert status == "cancelled"
        assert open(dest).read() == "old"
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_local_copy_dir_merge():
    src = tempfile.mkdtemp()
    dest = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(src, "sub"))
        os.makedirs(os.path.join(src, "empty"))
        with open(os.path.join(src, "a.txt"), "w") as f:
            f.write("new")
        with open(os.path.join(src, "sub", "b.txt"), "w") as f:
            f.write("bee")
        with open(os.path.join(dest, "a.txt"), "w") as f:
            f.write("old")
        with open(os.path.join(dest, "keep.txt"), "w") as f:
            f.write("keep")
        c = local_conn()
        status, _ = c.copy(src, dest, policy=POLICY_OVERWRITE, method="tar")
        assert status == "done"
        assert open(os.path.join(dest, "a.txt")).read() == "new", \
            "same-named file replaced in the merge"
        assert open(os.path.join(dest, "keep.txt")).read() == "keep", \
            "unrelated dest entries preserved"
        assert open(os.path.join(dest, "sub", "b.txt")).read() == "bee"
        assert os.path.isdir(os.path.join(dest, "empty")), "empty folder kept"
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(src)
        shutil.rmtree(dest)




def test_local_copy_dir_to_new_name():
    src = tempfile.mkdtemp()
    dest = tempfile.mkdtemp()
    try:
        with open(os.path.join(src, "a.txt"), "w") as f:
            f.write("data")
        c = local_conn()
        status, detail = c.copy(src, os.path.join(dest, "newdir"), policy=POLICY_OVERWRITE, method="tar")
        assert status == "done"
        assert open(os.path.join(dest, "newdir", "a.txt")).read() == "data"
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(src)
        shutil.rmtree(dest)




def test_local_copy_dir_conflicts():
    src = tempfile.mkdtemp()
    dest = tempfile.mkdtemp()
    try:
        # source has a file where dest has a folder -> folder replaced by file
        with open(os.path.join(src, "x"), "w") as f:
            f.write("file")
        os.makedirs(os.path.join(dest, "x"))
        with open(os.path.join(dest, "x", "inner.txt"), "w") as f:
            f.write("inner")
        # source has a folder where dest has a file -> file replaced by folder
        os.makedirs(os.path.join(src, "y"))
        with open(os.path.join(src, "y", "inner.txt"), "w") as f:
            f.write("inner")
        with open(os.path.join(dest, "y"), "w") as f:
            f.write("file")
        c = local_conn()
        status, _ = c.copy(src, dest, policy=POLICY_OVERWRITE, method="tar")
        assert status == "done"
        assert os.path.isfile(os.path.join(dest, "x")), "file wins over folder"
        assert open(os.path.join(dest, "x")).read() == "file"
        assert os.path.isdir(os.path.join(dest, "y")), "folder wins over file"
        assert open(os.path.join(dest, "y", "inner.txt")).read() == "inner"
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(src)
        shutil.rmtree(dest)




def test_local_copy_symlinks():
    src = tempfile.mkdtemp()
    dest = tempfile.mkdtemp()
    try:
        with open(os.path.join(src, "target.txt"), "w") as f:
            f.write("data")
        try:
            os.symlink("target.txt", os.path.join(src, "lnk"))
            os.symlink("missing-target", os.path.join(src, "dangling"))
            os.symlink("target.txt", os.path.join(dest, "data"))
        except OSError:
            return  # filesystem cannot create symlinks
        c = local_conn()
        status, _ = c.copy(src, dest, policy=POLICY_OVERWRITE, method="tar")
        assert status == "done"
        assert os.path.islink(os.path.join(dest, "lnk")), "file symlink recreated as symlink"
        assert os.readlink(os.path.join(dest, "lnk")) == "target.txt"
        assert os.path.islink(os.path.join(dest, "dangling")), "dangling symlink recreated"
        assert open(os.path.join(dest, "target.txt")).read() == "data"
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(src)




def test_local_copy_part_skips_own_output():
    """Copying a folder into a destination that lives inside it must not
    recurse into the part/final directories (which would copy its own
    output, growing forever)."""
    src = tempfile.mkdtemp()
    try:
        with open(os.path.join(src, "a.txt"), "w") as f:
            f.write("data")
        dest = os.path.join(src, "out")
        c = local_conn()
        status, _ = c.copy(src, dest, policy=POLICY_OVERWRITE, method="tar")
        assert status == "done"
        names = {it["name"] for it in dir_list(dest)}
        assert names == {"a.txt"}, names
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(src)




def test_local_copy_abort_cleans_part():
    src = tempfile.mkdtemp()
    dest = tempfile.mkdtemp()
    try:
        with open(os.path.join(src, "big.bin"), "wb") as f:
            f.write(b"x" * (16 * 1024 * 1024))
        c = local_conn()
        sink = []
        result = {}
        c.pause(sink)  # stall before the copy starts -> deterministic abort
        t = threading.Thread(target=lambda: result.update(
            {"r": c.copy(src, dest, policy=POLICY_OVERWRITE, method="tar", proc_sink=sink)}))
        t.start()
        time.sleep(0.2)
        c.kill_all()
        t.join(timeout=10)
        assert "r" in result, "copy thread must finish after kill_all"
        status, _ = result["r"]
        assert status == "aborted", result["r"]
        assert not os.path.exists(os.path.join(dest, "big.bin")), "nothing placed"
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(src)
        shutil.rmtree(dest)




def test_local_copy_pause_resume():
    src = tempfile.mkdtemp()
    dest = tempfile.mkdtemp()
    try:
        with open(os.path.join(src, "big.bin"), "wb") as f:
            f.write(b"x" * (16 * 1024 * 1024))
        c = local_conn()
        sink = []
        result = {}
        c.pause(sink)  # stall before the copy starts
        t = threading.Thread(target=lambda: result.update(
            {"r": c.copy(src, dest, policy=POLICY_OVERWRITE, method="tar", proc_sink=sink)}))
        t.start()
        time.sleep(0.2)
        assert t.is_alive(), "paused copy must not finish"
        c.resume(sink)
        t.join(timeout=10)
        assert result["r"][0] == "done", result["r"]
        assert os.path.getsize(os.path.join(dest, "big.bin")) == 16 * 1024 * 1024
        assert parts_in(dest) == []
    finally:
        shutil.rmtree(src)
        shutil.rmtree(dest)




def test_local_copy_progress_and_finish():
    src = tempfile.mkdtemp()
    dest = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "a.txt"), "w") as f:
            f.write("abc")
        with open(os.path.join(src, "sub", "b.txt"), "w") as f:
            f.write("de")
        c = local_conn()
        calls = []
        done = []
        status, _ = c.copy(src, dest, policy=POLICY_OVERWRITE, method="tar",
                           on_bytes=lambda b, n: calls.append((b, n)),
                           on_finish=lambda: done.append(True))
        assert status == "done"
        assert calls and calls[-1] == (5, 2), f"final progress must flush: {calls}"
        assert all(a <= b for a, b in zip(calls, calls[1:])), "bytes must be monotonic"
        assert done == [True], "on_finish must be called before placement"
    finally:
        shutil.rmtree(src)
        shutil.rmtree(dest)




def test_local_copy_missing_source():
    d = tempfile.mkdtemp()
    try:
        c = local_conn()
        status, detail = c.copy(os.path.join(d, "nope"), os.path.join(d, "out"),
                                policy=POLICY_OVERWRITE)
        assert status == "failed"
        assert parts_in(d) == []
    finally:
        shutil.rmtree(d)




def test_local_copy_unreadable_subdir():
    if os.geteuid() == 0:
        return  # root ignores permission bits
    src = tempfile.mkdtemp()
    dest = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(src, "locked"))
        with open(os.path.join(src, "locked", "secret.txt"), "w") as f:
            f.write("data")
        with open(os.path.join(src, "ok.txt"), "w") as f:
            f.write("ok")
        os.chmod(os.path.join(src, "locked"), 0)
        try:
            c = local_conn()
            status, _ = c.copy(src, dest, policy=POLICY_OVERWRITE, method="tar")
            assert status == "failed", "unreadable source must fail, not succeed silently"
            assert parts_in(dest) == []
        finally:
            os.chmod(os.path.join(src, "locked"), 0o755)
    finally:
        shutil.rmtree(src)
        shutil.rmtree(dest)




def test_local_close_idempotent():
    c = local_conn()
    c.close()
    c.close()  # must not raise




def test_local_friendly_error():
    title, _ = LocalConnection.friendly_error("Permission denied (publickey)")
    assert title == "Permission denied", title
    title, _ = LocalConnection.friendly_error("ls: cannot access '/x': No such file or directory")
    assert title == "Folder not found", title
    title, _ = LocalConnection.friendly_error("Not a directory")
    assert title == "Not a folder", title
    title, _ = LocalConnection.friendly_error("")
    assert title == "Local error", title




def test_merge_dir_preserves_extras():
    d = tempfile.mkdtemp()
    try:
        c = make_conn()
        part = os.path.join(d, ".part")
        final = os.path.join(d, "final")
        os.makedirs(os.path.join(part, "sub"))
        open(os.path.join(part, "new.txt"), "w").write("new")
        open(os.path.join(part, "sub", "inner.txt"), "w").write("inner")
        os.makedirs(os.path.join(final, "sub"))
        open(os.path.join(final, "old.txt"), "w").write("old")
        open(os.path.join(final, "sub", "keep.txt"), "w").write("keep")
        c._merge_dir(part, final)
        assert open(os.path.join(final, "new.txt")).read() == "new"
        assert open(os.path.join(final, "sub", "inner.txt")).read() == "inner"
        assert open(os.path.join(final, "old.txt")).read() == "old", "extra kept"
        assert open(os.path.join(final, "sub", "keep.txt")).read() == "keep", "extra kept"
        assert not os.path.exists(part), "merged part dir is emptied and removed"
    finally:
        shutil.rmtree(d)




def test_place_file_atomic():
    d = tempfile.mkdtemp()
    try:
        c = make_conn()
        part = os.path.join(d, ".part")
        final = os.path.join(d, "f.txt")
        open(final, "w").write("old")
        with open(part, "w") as f:
            f.write("new")
        c._place(part, final)
        assert open(final).read() == "new", "file replaced atomically"
        assert not os.path.exists(part)
    finally:
        shutil.rmtree(d)




def test_place_dir_over_file():
    d = tempfile.mkdtemp()
    try:
        c = make_conn()
        part = os.path.join(d, ".part")
        final = os.path.join(d, "f.txt")
        open(final, "w").write("old")
        os.makedirs(os.path.join(part, "sub"))
        c._place(part, final)
        assert os.path.isdir(final), "folder replaces a file on overwrite"
        assert os.path.isdir(os.path.join(final, "sub"))
        assert not os.path.exists(part)
    finally:
        shutil.rmtree(d)




def test_sweep_ignores_live_parts():
    c = make_conn()
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "x")
        live = c._part_path(dest)
        os.makedirs(live)
        with open(os.path.join(live, "junk"), "w") as f:
            f.write("live data")
        with c._live_parts_lock:
            c._live_parts.add(live)
        stale = c._part_path(dest)
        os.makedirs(stale)
        with open(os.path.join(stale, "junk"), "w") as f:
            f.write("old")

        def fake_copy_tar(remote_path, part, final, on_bytes=None, proc_sink=None):
            with open(part, "w") as f:
                f.write("x")
            return "done", part

        c._copy_tar = fake_copy_tar
        c.copy("/r/x", dest, method="tar", proc_sink=[])
        assert os.path.exists(live), "a live part must never be swept away"
        assert not os.path.exists(stale), "a stale part must be removed"
    finally:
        shutil.rmtree(d)




def test_local_delete_file_and_dir():
    d = tempfile.mkdtemp(prefix="test_del_")
    try:
        f = os.path.join(d, "file.txt")
        with open(f, "w") as fp:
            fp.write("hello")
        sub = os.path.join(d, "subdir")
        os.makedirs(sub, exist_ok=True)
        sub_f = os.path.join(sub, "nested.txt")
        with open(sub_f, "w") as fp:
            fp.write("nested")

        ok, err = delete_local_item(f)
        assert ok is True and not err, f"file delete failed: {err}"
        assert not os.path.exists(f), "file must be gone"

        ok, err = delete_local_item(sub)
        assert ok is True and not err, f"dir delete failed: {err}"
        assert not os.path.exists(sub), "dir must be gone"

        # Non-existent path returns True (idempotent)
        ok, err = delete_local_item(os.path.join(d, "nonexistent"))
        assert ok is True and not err
    finally:
        shutil.rmtree(d, ignore_errors=True)




def test_local_delete_symlink_safety():
    d = tempfile.mkdtemp(prefix="test_del_sym_")
    try:
        target_dir = os.path.join(d, "real_folder")
        os.makedirs(target_dir, exist_ok=True)
        secret_file = os.path.join(target_dir, "precious.txt")
        with open(secret_file, "w") as fp:
            fp.write("must not be deleted")

        symlink_dir = os.path.join(d, "link_to_folder")
        os.symlink(target_dir, symlink_dir)

        # Deleting the symlink folder must NOT delete the target folder or its contents
        ok, err = delete_local_item(symlink_dir)
        assert ok is True and not err, f"symlink dir delete failed: {err}"
        assert not os.path.lexists(symlink_dir), "symlink itself must be gone"
        assert os.path.isdir(target_dir), "target directory must remain intact"
        assert os.path.isfile(secret_file), "target files must remain intact"

        # File symlink
        real_file = os.path.join(d, "real.txt")
        with open(real_file, "w") as fp:
            fp.write("real")
        symlink_file = os.path.join(d, "link_to_file")
        os.symlink(real_file, symlink_file)

        ok, err = delete_local_item(symlink_file)
        assert ok is True and not err
        assert not os.path.lexists(symlink_file), "symlink file must be gone"
        assert os.path.isfile(real_file), "real file must remain intact"
    finally:
        shutil.rmtree(d, ignore_errors=True)




def test_local_delete_readonly_ntfs():
    d = tempfile.mkdtemp(prefix="test_del_ro_")
    try:
        ro_file = os.path.join(d, "readonly.txt")
        with open(ro_file, "w") as fp:
            fp.write("ro")
        os.chmod(ro_file, 0o400)

        sub = os.path.join(d, "sub_ro")
        os.makedirs(sub, exist_ok=True)
        sub_ro_file = os.path.join(sub, "nested_ro.txt")
        with open(sub_ro_file, "w") as fp:
            fp.write("nested ro")
        os.chmod(sub_ro_file, 0o400)

        ok, err = delete_local_item(ro_file)
        assert ok is True and not err, f"readonly file delete failed: {err}"
        assert not os.path.exists(ro_file)

        ok, err = delete_local_item(sub)
        assert ok is True and not err, f"readonly dir tree delete failed: {err}"
        assert not os.path.exists(sub)
    finally:
        shutil.rmtree(d, ignore_errors=True)




def test_local_delete_protected_paths():
    home = os.path.expanduser("~")
    for bad in ("", "/", "~", "~/", ".", "..", home, home + "/"):
        ok, err = delete_local_item(bad)
        assert ok is False, f"Protected path '{bad}' must be rejected"
        assert "protected" in err.lower() or "refusing" in err.lower()




ALL_TESTS = (
    test_dir_list,
    test_local_home_and_expand,
    test_local_list_dir,
    test_local_list_home_tilde,
    test_local_list_dir_error_kinds,
    test_local_copy_scp_dir_rejected,
    test_local_stat_remote,
    test_local_tree_remote,
    test_local_copy_file_overwrite,
    test_local_copy_file_skip,
    test_local_copy_file_keep_both,
    test_local_copy_ask_same_size_skips,
    test_local_copy_ask_overwrite,
    test_local_copy_ask_cancel,
    test_local_copy_dir_merge,
    test_local_copy_dir_to_new_name,
    test_local_copy_dir_conflicts,
    test_local_copy_symlinks,
    test_local_copy_part_skips_own_output,
    test_local_copy_abort_cleans_part,
    test_local_copy_pause_resume,
    test_local_copy_progress_and_finish,
    test_local_copy_missing_source,
    test_local_copy_unreadable_subdir,
    test_local_close_idempotent,
    test_local_friendly_error,
    test_merge_dir_preserves_extras,
    test_place_file_atomic,
    test_place_dir_over_file,
    test_sweep_ignores_live_parts,
    test_local_delete_file_and_dir,
    test_local_delete_symlink_safety,
    test_local_delete_readonly_ntfs,
    test_local_delete_protected_paths,
)
