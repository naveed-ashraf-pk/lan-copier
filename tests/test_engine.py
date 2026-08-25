"""Tests for the transfer engine (remote-destination copy)."""
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

def test_engine_local_to_posix_file_and_dir():
    te, src, dest, sroot, droot = _engine_src_and_dest()
    try:
        with open(os.path.join(sroot, "notes.txt"), "w") as fh:
            fh.write("lan is nice")
        os.makedirs(os.path.join(sroot, "pkg", "nested"))
        with open(os.path.join(sroot, "pkg", "a.bin"), "wb") as fh:
            fh.write(b"\x00" * 1024)
        with open(os.path.join(sroot, "pkg", "nested", "deep.txt"), "w") as fh:
            fh.write("deep")
        with open(os.path.join(droot, "old.bin"), "wb") as fh:
            fh.write(b"\x00" * 512)

        st, detail = te.run(dest, src, os.path.join(sroot, "notes.txt"),
                            os.path.join(droot, "notes.txt"), policy="overwrite")
        assert st == "done", (st, detail)
        with open(os.path.join(droot, "notes.txt")) as fh:
            assert fh.read() == "lan is nice"

        st, detail = te.run(dest, src, os.path.join(sroot, "pkg"),
                            os.path.join(droot, "pkg"), policy="overwrite")
        assert st == "done", (st, detail)
        assert os.path.isdir(os.path.join(droot, "pkg", "nested"))
        assert os.path.getsize(os.path.join(droot, "pkg", "a.bin")) == 1024
        with open(os.path.join(droot, "pkg", "nested", "deep.txt")) as fh:
            assert fh.read() == "deep"
        assert os.path.lexists(os.path.join(droot, "old.bin")), "extra preserved"

        leftovers = [n for n in os.listdir(droot) if ".lan-copier-part" in n]
        assert leftovers == [], leftovers
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)




def test_engine_dir_onto_existing_merge_keeps_extras():
    te, src, dest, sroot, droot = _engine_src_and_dest()
    try:
        os.makedirs(os.path.join(sroot, "tree", "sub"))
        with open(os.path.join(sroot, "tree", "file1.txt"), "w") as fh:
            fh.write("new")
        with open(os.path.join(sroot, "tree", "sub", "inner.txt"), "w") as fh:
            fh.write("x")
        os.makedirs(os.path.join(droot, "tree"))
        with open(os.path.join(droot, "tree", "keepme.dat"), "w") as fh:
            fh.write("keep")
        with open(os.path.join(droot, "tree", "file1.txt"), "w") as fh:
            fh.write("old")

        st, detail = te.run(dest, src, os.path.join(sroot, "tree"),
                            os.path.join(droot, "tree"), policy="overwrite")
        assert st == "done", (st, detail)
        with open(os.path.join(droot, "tree", "file1.txt")) as fh:
            assert fh.read() == "new"
        with open(os.path.join(droot, "tree", "keepme.dat")) as fh:
            assert fh.read() == "keep"
        assert os.path.isdir(os.path.join(droot, "tree", "sub"))
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)




def test_engine_conflict_policies_posix():
    te, src, dest, sroot, droot = _engine_src_and_dest()
    try:
        sp = os.path.join(sroot, "f.txt")
        with open(sp, "w") as fh:
            fh.write("abcdef")
        dp = os.path.join(droot, "f.txt")
        with open(dp, "w") as fh:
            fh.write("abcdef")
        st, detail = te.run(dest, src, sp, dp, policy="skip")
        assert st == "skipped", (st, detail)

        asked = []
        st, detail = te.run(dest, src, sp, dp, policy="ask",
                            on_ask=lambda *a: asked.append(a) or "overwrite")
        assert st == "skipped" and asked == [], (st, detail)

        st, detail = te.run(dest, src, sp, dp, policy="keep_both")
        assert st == "done", (st, detail)
        assert os.path.exists(os.path.join(droot, "f (1).txt"))

        with open(dp, "w") as fh:
            fh.write("different size!")
        picked = []
        st, detail = te.run(dest, src, sp, dp, policy="ask",
                            on_ask=lambda final, rs, ls: picked.append((rs, ls)) or "overwrite")
        assert st == "done", (st, detail)
        assert picked and picked[0] == (6, 15), picked
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)




def test_engine_missing_parent_fails():
    te, src, dest, sroot, droot = _engine_src_and_dest()
    try:
        st, detail = te.run(dest, src, "/nonexistent-x",
                            os.path.join(droot, "sub", "x"), policy="overwrite")
        assert st == "failed", (st, detail)
        assert "does not exist" in detail
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)




# -- SSH -> SSH (both endpoints are the local-filesystem posix fake) -------

def test_engine_ssh_to_posix_file_and_dir():
    import transfer_engine
    sroot = tempfile.mkdtemp(prefix="lan-eng2-src-")
    droot = tempfile.mkdtemp(prefix="lan-eng2-dst-")
    try:
        with open(os.path.join(sroot, "r.txt"), "w") as fh:
            fh.write("remote source file")
        os.makedirs(os.path.join(sroot, "tree", "deep"))
        with open(os.path.join(sroot, "tree", "deep", "x"), "wb") as fh:
            fh.write(b"\x01" * 2048)

        src = FakePosixSsh(sroot)
        dest = FakePosixSsh(droot)

        st, detail = transfer_engine.run(
            dest, src, os.path.join(sroot, "r.txt"),
            os.path.join(droot, "r.txt"), policy="overwrite")
        assert st == "done", (st, detail)
        with open(os.path.join(droot, "r.txt")) as fh:
            assert fh.read() == "remote source file"

        st, detail = transfer_engine.run(
            dest, src, os.path.join(sroot, "tree"),
            os.path.join(droot, "tree"), policy="overwrite")
        assert st == "done", (st, detail)
        assert os.path.getsize(os.path.join(droot, "tree", "deep", "x")) == 2048
        leftovers = [n for n in os.listdir(droot) if ".lan-copier-part" in n]
        assert leftovers == [], leftovers
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)


def test_engine_remote_stale_part_swept():
    """A leftover staging dir for the target (from a killed run) must be
    removed before a fresh transfer, and never mixed into the result."""
    import transfer_engine
    sroot = tempfile.mkdtemp(prefix="lan-eng-sweep-src-")
    droot = tempfile.mkdtemp(prefix="lan-eng-sweep-dst-")
    try:
        with open(os.path.join(sroot, "f.dat"), "wb") as fh:
            fh.write(b"\x01" * 100)
        src = FakePosixSsh(sroot)
        dest = FakePosixSsh(droot)
        # simulate a stale staging dir from a previous crashed run
        stale = os.path.join(droot, ".f.dat.lan-copier-part-999-0")
        os.makedirs(stale)
        with open(os.path.join(stale, "x"), "w") as fh:
            fh.write("stale")
        st, detail = transfer_engine.run(
            dest, src, os.path.join(sroot, "f.dat"),
            os.path.join(droot, "f.dat"), policy="overwrite")
        assert st == "done", (st, detail)
        assert not os.path.lexists(stale), "stale part dir must be swept"
        with open(os.path.join(droot, "f.dat"), "rb") as fh:
            assert len(fh.read()) == 100
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)


def test_engine_local_to_posix_part_naming_unique():
    """Two concurrent-ish transfers to the same final never collide on the
    staging dir, and every part dir lands in the target's parent."""
    import transfer_engine
    sroot = tempfile.mkdtemp(prefix="lan-eng-uniq-src-")
    droot = tempfile.mkdtemp(prefix="lan-eng-uniq-dst-")
    try:
        os.makedirs(os.path.join(sroot, "a"))
        os.makedirs(os.path.join(sroot, "b"))
        with open(os.path.join(sroot, "a", "x"), "w") as fh:
            fh.write("a")
        with open(os.path.join(sroot, "b", "x"), "w") as fh:
            fh.write("b")
        src = FakePosixSsh(sroot)
        dest = FakePosixSsh(droot)
        # use the same final basename from two different sources: each
        # must get its own staging dir, then one becomes 'dup' and one 'dup (1)'
        st1, _ = transfer_engine.run(dest, src, os.path.join(sroot, "a"),
                                     os.path.join(droot, "dup"), policy="overwrite")
        st2, _ = transfer_engine.run(dest, src, os.path.join(sroot, "b"),
                                     os.path.join(droot, "dup"), policy="keep_both")
        assert st1 == "done" and st2 == "done"
        assert os.path.isfile(os.path.join(droot, "dup", "x"))
        assert os.path.isfile(os.path.join(droot, "dup (1)", "x"))
        leftovers = [n for n in os.listdir(droot)
                     if ".lan-copier-part" in n]
        assert leftovers == [], leftovers
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)


# -- engine move (fast-path + bridge) --------------------------------------

def test_engine_move_same_endpoint_fast_path():
    """Same-endpoint file move is a single rename (no copy stream), and a
    dir-onto-missing-name move also renames."""
    import transfer_engine
    root = tempfile.mkdtemp(prefix="lan-mv-fast-")
    try:
        with open(os.path.join(root, "a.txt"), "w") as fh:
            fh.write("data")
        os.makedirs(os.path.join(root, "adir", "sub"))
        with open(os.path.join(root, "adir", "sub", "x"), "w") as fh:
            fh.write("x")
        ep = FakePosixSsh(root)
        before = len(ep.shell_cmds)
        st, detail = transfer_engine.move(
            ep, ep, os.path.join(root, "a.txt"), os.path.join(root, "b.txt"),
            policy="overwrite")
        assert st == "done", (st, detail)
        assert not os.path.lexists(os.path.join(root, "a.txt"))
        assert os.path.exists(os.path.join(root, "b.txt"))
        # rename fast-path must not have streamed anything
        assert len(ep.shell_cmds) == before, ep.shell_cmds

        st, detail = transfer_engine.move(
            ep, ep, os.path.join(root, "adir"), os.path.join(root, "bdir"),
            policy="overwrite")
        assert st == "done", (st, detail)
        assert os.path.isfile(os.path.join(root, "bdir", "sub", "x"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_engine_move_guards():
    import transfer_engine
    root = tempfile.mkdtemp(prefix="move-guard-")
    try:
        os.makedirs(os.path.join(root, "tree", "child"))
        ep = FakePosixSsh(root)
        st, detail = transfer_engine.move(
            ep, ep, os.path.join(root, "tree"), os.path.join(root, "tree", "x"),
            policy="overwrite")
        assert st == "failed" and "sub-folder" in detail, (st, detail)
        st, detail = transfer_engine.move(
            ep, ep, os.path.join(root, "tree"), os.path.join(root, "tree"),
            policy="overwrite")
        assert st == "noop", (st, detail)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_engine_move_cross_endpoint_copy_then_delete():
    """A cross-endpoint move transfers to the destination and only then deletes
    the (fake remote) source."""
    import transfer_engine
    sroot = tempfile.mkdtemp(prefix="mv-x-src-")
    droot = tempfile.mkdtemp(prefix="mv-x-dst-")
    try:
        with open(os.path.join(sroot, "f.txt"), "w") as fh:
            fh.write("move me")
        os.makedirs(os.path.join(sroot, "dir"))
        with open(os.path.join(sroot, "dir", "inner"), "w") as fh:
            fh.write("i")
        src = FakePosixSsh(sroot, host="src")
        dest = FakePosixSsh(droot, host="dst")

        st, detail = transfer_engine.move(
            dest, src, os.path.join(sroot, "f.txt"), os.path.join(droot, "f.txt"),
            policy="overwrite")
        assert st == "done", (st, detail)
        assert not os.path.lexists(os.path.join(sroot, "f.txt")), "source deleted after copy"
        with open(os.path.join(droot, "f.txt")) as fh:
            assert fh.read() == "move me"

        st, detail = transfer_engine.move(
            dest, src, os.path.join(sroot, "dir"), os.path.join(droot, "dir"),
            policy="overwrite")
        assert st == "done", (st, detail)
        assert not os.path.lexists(os.path.join(sroot, "dir"))
        assert os.path.isfile(os.path.join(droot, "dir", "inner"))
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)


def test_engine_move_copy_ok_delete_fail():
    """If the transfer lands but source deletion fails, the move reports
    'copied' and keeps both copies."""
    import transfer_engine
    sroot = tempfile.mkdtemp(prefix="mv-keepsrc-")
    droot = tempfile.mkdtemp(prefix="mv-keepdst-")
    try:
        with open(os.path.join(sroot, "f.txt"), "w") as fh:
            fh.write("payload")
        src = FakePosixSsh(sroot, host="src-host")
        dest = FakePosixSsh(droot, host="dst-host")

        def boom(path):
            return False, "boom: cannot delete"

        st, detail = transfer_engine.move(
            dest, src, os.path.join(sroot, "f.txt"), os.path.join(droot, "f.txt"),
            policy="overwrite")
        # with default delete it succeeds normally
        assert st == "done", (st, detail)
        # now force the source delete to fail
        with open(os.path.join(sroot, "g.txt"), "w") as fh:
            fh.write("x")
        src.delete = boom
        st, detail = transfer_engine.move(
            dest, src, os.path.join(sroot, "g.txt"), os.path.join(droot, "g.txt"),
            policy="overwrite")
        assert st == "copied", (st, detail)
        assert os.path.lexists(os.path.join(sroot, "g.txt")), "source kept"
        assert os.path.exists(os.path.join(droot, "g.txt")), "dest kept"
    finally:
        shutil.rmtree(sroot, ignore_errors=True)
        shutil.rmtree(droot, ignore_errors=True)


ALL_TESTS = (
    test_engine_local_to_posix_file_and_dir,
    test_engine_dir_onto_existing_merge_keeps_extras,
    test_engine_conflict_policies_posix,
    test_engine_missing_parent_fails,
    test_engine_ssh_to_posix_file_and_dir,
    test_engine_remote_stale_part_swept,
    test_engine_local_to_posix_part_naming_unique,
    test_engine_move_same_endpoint_fast_path,
    test_engine_move_guards,
    test_engine_move_cross_endpoint_copy_then_delete,
    test_engine_move_copy_ok_delete_fail,
)
