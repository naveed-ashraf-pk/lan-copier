"""Tests for tree_exporter."""
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

def test_tree_export_local_entries_and_yaml():
    d = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(d, "src", "nested"))
        with open(os.path.join(d, "src", "index.js"), "w") as f:
            f.write("console.log('hi')\n")
        with open(os.path.join(d, "src", "nested", "deep.txt"), "w") as f:
            f.write("deep")
        if hasattr(os, "symlink"):
            try:
                os.symlink("index.js", os.path.join(d, "src", "link.js"))
            except OSError:
                pass
        entries = tree_exporter.collect_local_entries(d, max_depth=2)
        assert "src/" in entries
        assert "src/index.js" in entries
        assert "src/nested/" in entries
        assert "src/nested/deep.txt" not in entries
        doc = tree_exporter.build_export_document(
            panel="source",
            host_info={"host_name": "box", "host_ip": "1.2.3.4", "host_display": "box (1.2.3.4)"},
            root_path=d,
            scope="all",
            depth_label="2",
            entries=entries,
        )
        yaml_text = tree_exporter.dump_yaml_document(doc)
        assert "host_name: \"box\"" in yaml_text
        assert "\"src/index.js\":" in yaml_text
        assert doc["meta"]["total_entries"] == len(entries)
        assert f"total_entries: {len(entries)}" in yaml_text, yaml_text
        if os.path.islink(os.path.join(d, "src", "link.js")):
            assert "src/link.js" in entries
            assert entries["src/link.js"]["type"] == "symlink"
            assert entries["src/link.js"]["symlink_target"] == "index.js"
            assert "\"src/link.js\":" in yaml_text
    finally:
        shutil.rmtree(d, ignore_errors=True)




def test_tree_export_selected_depth_and_filename():
    d = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(d, "a", "b"))
        with open(os.path.join(d, "a", "f.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(d, "a", "b", "g.txt"), "w") as f:
            f.write("y")
        entries = tree_exporter.collect_local_entries(
            d, selected_paths=[os.path.join(d, "a")], max_depth=1)
        assert sorted(entries) == ["a/"]
        name = tree_exporter.suggest_export_filename(
            "host name", "/tmp/demo folder",
            time.strptime("20260822-153022", "%Y%m%d-%H%M%S"))
        assert name == "host-name-demo-folder-20260822-153022.yaml", name
    finally:
        shutil.rmtree(d, ignore_errors=True)




def test_tree_export_remote_python_fallback():
    class FakeExportConn:
        host = "10.0.0.7"
        target = "user@10.0.0.7"

        def __init__(self):
            self.last_error = ""

        def _opts(self):
            return []

        def _run_cmd(self, argv, timeout=None):
            remote = argv[-1]
            if "powershell -NoProfile -EncodedCommand" in remote:
                return 127, "", "powershell: not found"
            if "python3 -c" in remote:
                payload = {
                    "host_name": "winbox",
                    "host_ip": "10.0.0.7",
                    "entries": {
                        "dir/": {"type": "directory", "modified": "2026-08-22 10:00:00", "created": "2026-08-21 09:00:00", "mode": "0755"},
                        "dir/file.txt": {"type": "file", "size_bytes": 5, "modified": "2026-08-22 10:01:00", "created": "2026-08-21 09:01:00", "mode": "0644"},
                    },
                }
                return 0, json.dumps(payload), ""
            return 127, "", "missing"

    host_info, entries = tree_exporter.collect_remote_entries(FakeExportConn(), "/root/project", max_depth=None)
    assert host_info["host_display"] == "winbox (10.0.0.7)"
    assert sorted(entries) == ["dir/", "dir/file.txt"]
    assert entries["dir/file.txt"]["size_bytes"] == 5




def test_tree_export_python_scanner_executes():
    import base64 as _b64
    d = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(d, "src", "nested"))
        with open(os.path.join(d, "src", "index.js"), "w") as f:
            f.write("x")
        if hasattr(os, "symlink"):
            try:
                os.symlink("index.js", os.path.join(d, "src", "link.js"))
            except OSError:
                pass
        payload = {
            "root_path": d,
            "selected_paths": [],
            "max_depth": None,
            "host_hint": "10.9.9.9",
        }
        code_b64 = _b64.b64encode(tree_exporter.PYTHON_SCANNER.encode("utf-8")).decode("ascii")
        arg = json.dumps(payload, separators=(",", ":"))
        bootstrap = "import base64,sys;exec(base64.b64decode(sys.argv[1]))"
        proc = subprocess.run(
            [sys.executable, "-c", bootstrap, code_b64, arg],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert "src/" in data["entries"]
        assert "src/index.js" in data["entries"]
        assert data["entries"]["src/index.js"]["size_bytes"] >= 1
        assert data["host_name"]
    finally:
        shutil.rmtree(d, ignore_errors=True)




def test_tree_export_unix_find_parsers():
    darwin_out = (
        "/scan/src\tDirectory\t755\t0\t1590000000\t1580000000\t\n"
        "/scan/src/a.txt\tRegular File\t644\t5\t1590000001\t1580000001\t\n"
        "/scan/src/ln\tSymbolic Link\t777\t2\t1590000002\t1580000002\ta.txt\n"
    )
    gnu_out = (
        "d\t755\t0\t1590000000.0\t1580000000.0\t/scan/src\t\n"
        "f\t644\t5\t1590000001.0\t1580000001.0\t/scan/src/a.txt\t\n"
        "l\t777\t2\t1590000002.0\t1580000002.0\t/scan/src/ln\ta.txt\n"
    )

    class FakeFindConn:
        host = "10.0.0.8"

        def __init__(self, output):
            self.output = output
            self.last_error = ""
            self.target = "user@10.0.0.8"

        def _opts(self):
            return []

        def _run_cmd(self, argv, timeout=None):
            return 0, self.output, ""

    conn_d = FakeFindConn(darwin_out)
    data_d = tree_exporter._scan_remote_via_unix_find(
        conn_d, {"root_path": "/scan", "selected_paths": [], "max_depth": None}, "Darwin")
    assert data_d["entries"]["src/"]["type"] == "directory"
    assert data_d["entries"]["src/a.txt"]["size_bytes"] == 5
    assert data_d["entries"]["src/a.txt"]["mode"] == "0644"
    assert data_d["entries"]["src/ln"]["symlink_target"] == "a.txt"

    conn_g = FakeFindConn(gnu_out)
    data_g = tree_exporter._scan_remote_via_unix_find(
        conn_g, {"root_path": "/scan", "selected_paths": [], "max_depth": None}, "Linux")
    assert data_g["entries"]["src/a.txt"]["size_bytes"] == 5
    assert data_g["entries"]["src/ln"]["symlink_target"] == "a.txt"




def test_tree_export_uname_dispatch_and_host():
    class FakeDispatchConn:
        host = "10.5.5.5"

        def __init__(self):
            self.last_error = ""
            self.calls = []
            self.target = "user@10.5.5.5"

        def _opts(self):
            return []

        def _run_cmd(self, argv, timeout=None):
            remote = argv[-1]
            self.calls.append(remote)
            if remote == "uname -s":
                return 0, "Darwin\n", ""
            if remote.startswith("hostname "):
                return 0, "MacBook-Pro.local\n", ""
            if "python3 -c" not in remote and "powershell " not in remote:
                return 127, "", "no match"
            return 127, "", "boom"

    conn = FakeDispatchConn()
    host_info = tree_exporter.describe_remote_host(conn)
    assert host_info["host_name"] == "MacBook-Pro.local", host_info
    assert "uname -s" in conn.calls
    assert conn._export_uname == "Darwin"




ALL_TESTS = (
    test_tree_export_local_entries_and_yaml,
    test_tree_export_selected_depth_and_filename,
    test_tree_export_remote_python_fallback,
    test_tree_export_python_scanner_executes,
    test_tree_export_unix_find_parsers,
    test_tree_export_uname_dispatch_and_host,
)
