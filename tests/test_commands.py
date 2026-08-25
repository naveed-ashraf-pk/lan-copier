"""Tests for the pure command builders (commands/ package)."""
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

def test_cmd_posix_builders_exact():
    from commands import posix
    assert posix.uname() == "uname -s"
    assert posix.rm_rf("/a/b") == "rm -rf -- /a/b"
    assert posix.mv("/a", "/b") == "mv -- /a /b"
    assert posix.mkdir_p("/x y") == "mkdir -p -- '/x y'"
    assert posix.test_exists("/x") == "test -e /x"
    assert posix.ls_la("/d") == "LC_ALL=C ls -la -- /d"

    g = posix.find_stat_gnu("/tmp/s")
    assert "-printf" in g and "awk" in g and "du" not in g
    d = posix.find_stat_darwin("/tmp/s")
    assert "stat -f '%z'" in d and "awk" in d

    tree_g = posix.find_tree_gnu("/tmp/s")
    assert tree_g.endswith("-printf '%p %s\\n'")
    tree_d = posix.find_tree_darwin("/tmp/s")
    assert "stat -f '%N %z'" in tree_d

    assert (posix.tar_read_remote("/a b", "na me") ==
            "LC_ALL=C tar -C '/a b' -cf - -- 'na me'")
    assert posix.tar_read_local("/a", "n") == ["tar", "-C", "/a", "-cf", "-", "--", "n"]
    assert posix.tar_extract_remote("/p art") == "tar -C '/p art' -xpf -"
    assert (posix.tar_extract_local("/p") ==
            ["tar", "-C", "/p", "--strip-components=1", "-xpf", "-"])




def test_cmd_posix_quoting_injection_safe():
    import commands.posix as posix
    evil = "x'; $(pwned); echo '"
    out = posix.rm_rf(evil)
    assert "rm -rf -- '" in out
    # real end-to-end quoting check: roll a file with an injection-looking name
    # and copy it through a shell command built with the same quoting
    d = tempfile.mkdtemp(prefix="lan-copy-inject-")
    try:
        src = os.path.join(d, evil)
        with open(src, "w") as fh:
            fh.write("payload")
        dst = os.path.join(d, "ok.txt")
        cmd = "cp -- " + shlex.quote(src) + " " + shlex.quote(dst)
        rc = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)
        assert rc.returncode == 0, (cmd, rc.stderr)
        with open(dst) as fh:
            assert fh.read() == "payload"
        assert os.path.lexists(src), "evil name must survive as a file name"
    finally:
        shutil.rmtree(d, ignore_errors=True)




def test_cmd_local_helpers():
    import commands.local
    d = tempfile.mkdtemp(prefix="lan-copy-cmdlocal-")
    try:
        sub = os.path.join(d, "sub dir")
        commands.local.mkdir_p(sub)
        assert commands.local.is_dir(sub) and not commands.local.is_link(sub)
        f = os.path.join(sub, "a.txt")
        with open(f, "w") as fh:
            fh.write("data")
        assert commands.local.exists(f)
        assert commands.local.stat_bytes_files(f) == {"bytes": 4, "files": 1}
        assert commands.local.size_of(sub) == 4
        moved = os.path.join(d, "b.txt")
        commands.local.rename(f, moved)
        assert not commands.local.exists(f) and commands.local.exists(moved)
        up2 = commands.local.unique_path(moved)
        assert up2 != moved and not commands.local.exists(up2)
        ok, err = commands.local.delete(moved)
        assert ok and not err and not commands.local.exists(moved)
        # delete on a directory recurses
        ok, err = commands.local.delete(sub)
        assert ok and not err and not commands.local.exists(sub)
    finally:
        shutil.rmtree(d, ignore_errors=True)




def test_cmd_powershell_builders():
    from commands import powershell as ps
    assert ps.exists(r"C:\Data") .startswith("powershell -NoProfile -EncodedCommand ")
    body = _ps_decode(ps.exists(r"C:\Data\f.txt"))
    assert "Test-Path -LiteralPath 'C:\\Data\\f.txt'" in body and "exit 0" in body

    body = _ps_decode(ps.mkdir_p("C:\\New Dir"))
    assert "New-Item -ItemType Directory -Path 'C:\\New Dir' -Force" in body

    body = _ps_decode(ps.delete_recurse("C:\\x"))
    assert "$p = 'C:\\x'" in body
    assert "Remove-Item -LiteralPath $p -Recurse -Force" in body
    assert "Start-Sleep -Milliseconds 300" in body

    body = _ps_decode(ps.list_dir("C:\\d"))
    assert "Get-ChildItem -LiteralPath 'C:\\d'" in body
    assert "[Console]::OutputEncoding = [Text.Encoding]::UTF8" in body
    assert "('{0}`t" not in body
    assert "$isDir`t$isLink`t" in body

    body = _ps_decode(ps.stat_bytes_files("C:\\d"))
    assert "$p = 'C:\\d'" in body
    assert "Get-ChildItem -LiteralPath $p -Recurse -File" in body

    body = _ps_decode(ps.tree("C:\\d"))
    assert "$base" in body and "Get-ChildItem -LiteralPath 'C:\\d' -Recurse -File" in body

    body = _ps_decode(ps.rename("C:\\a", "C:\\b"))
    assert ("Move-Item -LiteralPath 'C:\\a' -Destination 'C:\\b' -Force" in body)

    body = _ps_decode(ps.unique_path("C:\\a.txt"))
    assert "GetFileNameWithoutExtension" in body

    body = _ps_decode(ps.home_dir())
    assert "USERPROFILE" in body

    body = _ps_decode(ps.has_tar())
    assert "Get-Command tar.exe" in body

    body = _ps_decode(ps.tar_read("C:\\p", "name"))
    assert "tar.exe -C 'C:\\p' -cf - -- 'name'" in body
    body = _ps_decode(ps.tar_extract("C:\\part"))
    assert "tar.exe -C 'C:\\part' -xpf -" in body




def test_cmd_powershell_path_quoting_roundtrip():
    from commands import powershell as ps
    # a path containing both kinds of quote must not break the encoded script
    tricky = r"C:\we'ird"
    cmd = ps.mkdir_p(tricky)
    body = _ps_decode(cmd)
    assert (r"New-Item -ItemType Directory -Path 'C:\we''ird' -Force" in body)
    b2 = _ps_decode(ps.exists(tricky + r"\f"))
    assert r"Test-Path -LiteralPath 'C:\we''ird\f'" in b2


# -- transfer engine (remote destination over a local-filesystem fake) ------



ALL_TESTS = (
    test_cmd_posix_builders_exact,
    test_cmd_posix_quoting_injection_safe,
    test_cmd_local_helpers,
    test_cmd_powershell_builders,
    test_cmd_powershell_path_quoting_roundtrip,
)
