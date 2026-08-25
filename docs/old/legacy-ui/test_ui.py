"""Tests for ui.py helpers plus the GTK UI smoke test."""
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

def test_compare_trees():
    try:
        from ui import compare_trees
    except Exception as e:
        print(f"SKIP test_compare_trees (no display?): {e}")
        return
    remote = {"a.txt": 10, "b.txt": 10, "sub/c.txt": 5, "sub/d.txt": 5, "same.txt": 7}
    local = {"a.txt": 10, "b.txt": 99, "same.txt": 7}
    missing, diff, top = compare_trees(remote, local)
    assert missing == {"sub/c.txt", "sub/d.txt"}
    assert diff == {"b.txt"}
    assert top == {"sub", "b.txt"}

    empty_remote = {}
    assert compare_trees(empty_remote, local) == (set(), set(), set())




def test_classify_items():
    from ui import classify_items

    remote = [
        {"name": "only-r.txt", "is_dir": False, "size": 10},
        {"name": "diff.bin", "is_dir": False, "size": 100},
        {"name": "same.bin", "is_dir": False, "size": 50},
        {"name": "dir-both", "is_dir": True, "size": 0},
        {"name": "type-clash", "is_dir": False, "size": 5},
        {"name": "case.bin", "is_dir": False, "size": 7},
    ]
    local = [
        {"name": "diff.bin", "is_dir": False, "size": 90},
        {"name": "same.bin", "is_dir": False, "size": 50},
        {"name": "dir-both", "is_dir": True, "size": 0},
        {"name": "type-clash", "is_dir": True, "size": 0},
        {"name": "only-l.txt", "is_dir": False, "size": 3},
        {"name": "CASE.bin", "is_dir": False, "size": 7},
    ]
    st = classify_items(remote, local)
    assert st["only-r.txt"] == "missing"
    assert st["diff.bin"] == "differ"
    assert st["same.bin"] == "same"
    assert st["dir-both"] == "same", "folders compare by kind, not size"
    assert st["type-clash"] == "conflict"
    assert st["only-l.txt"] == "extra"
    assert st["case.bin"] == "missing", "case-sensitive: CASE.bin is a different name"
    assert st["CASE.bin"] == "extra"
    assert classify_items([], []) == {}
    assert classify_items([{"name": "a", "is_dir": False, "size": 1}], []) == {"a": "missing"}
    assert classify_items([], [{"name": "a", "is_dir": False, "size": 1}]) == {"a": "extra"}




def test_color_palettes():
    from ui import LIGHT_COLORS, DARK_COLORS, TRANSFER_COLOR_KEYS, STATE_PRIO

    keys = set(STATE_PRIO) | set(TRANSFER_COLOR_KEYS)
    assert set(LIGHT_COLORS) == keys, "light palette must cover every state/status"
    assert set(DARK_COLORS) == keys, "dark palette must cover every state/status"
    assert DARK_COLORS != LIGHT_COLORS, "dark palette must differ from light"
    for k in keys:
        assert LIGHT_COLORS[k].startswith("#") and DARK_COLORS[k].startswith("#")
    assert LIGHT_COLORS["missing"] == "#c62828"
    assert LIGHT_COLORS["failed"] == "#c62828"
    assert LIGHT_COLORS["same"] == "#2e7d32"
    assert LIGHT_COLORS["running"] == "#1565c0"
    assert LIGHT_COLORS["queued"] == "#757575"
    assert LIGHT_COLORS["paused"] == "#f9a825"




def ui_smoke():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, GLib
        import ui
        from ui import CopierWindow, Transfer
    except Exception as e:
        print(f"SKIP ui smoke (no display?): {e}")
        return

    orig_prof_path = ui.profiles._path
    prof_dir = tempfile.mkdtemp()
    ui.profiles._path = lambda: os.path.join(prof_dir, "profiles.json")
    try:
        _ui_smoke(Gtk, GLib, ui, CopierWindow, Transfer)
    finally:
        ui.profiles._path = orig_prof_path
        shutil.rmtree(prof_dir, ignore_errors=True)




def _ui_smoke(Gtk, GLib, ui, CopierWindow, Transfer):
    class FakeConn:
        host = "fake-host-xyz"
        port = 22
        user = "tester-xyz"
        target = "tester-xyz@fake-host-xyz"

        def __init__(self, fail=False):
            self.fail = fail
            self.last_error = ""

        @staticmethod
        def friendly_error(err):
            return ("Error", err or "Unknown error.")

        def list_dir(self, p):
            return [
                {"name": "a.txt", "path": p + "/a.txt", "is_dir": False,
                 "is_link": False, "size": 5, "mtime": "x", "mtime_epoch": 0},
                {"name": "sub", "path": p + "/sub", "is_dir": True,
                 "is_link": False, "size": 0, "mtime": "x", "mtime_epoch": 0},
            ]

        def stat_remote(self, p):
            return {"bytes": 200 * 1024, "files": 100}

        def tree_remote(self, p):
            return {"new.bin": 5, "sub/inner.txt": 3}

        def copy(self, src, dest, policy="ask", method="scp", on_ask=None,
                 on_part=None, on_bytes=None, proc_sink=None, on_finish=None):
            if self.fail:
                return "failed", "boom"
            part = os.path.join(os.path.dirname(dest), "." + os.path.basename(dest) + ".part")
            on_part(part)
            total = 0
            if method == "tar":
                os.makedirs(part, exist_ok=True)
                for i in range(1, 101):
                    with open(os.path.join(part, "f.bin"), "ab") as f:
                        f.write(b"x" * 1024)
                    total += 1024
                    if on_bytes:
                        on_bytes(total, i)
                    time.sleep(0.01)
                os.rename(part, dest)
                return "done", dest
            with open(part, "wb") as f:
                for _ in range(200):
                    f.write(b"x" * 1024)
                    total += 1024
                    if on_bytes:
                        on_bytes(total, 1)
                    time.sleep(0.01)
            os.rename(part, dest)
            return "done", dest

        def kill_all(self):
            pass

        def pause(self, sink):
            pass

        def resume(self, sink):
            pass

        def kill_procs(self, sink):
            pass

        def close(self):
            pass

        def _opts(self):
            return []

        def _run_cmd(self, argv, timeout=None):
            remote = argv[-1]
            if "powershell -NoProfile -EncodedCommand" in remote:
                return 127, "", "powershell: not found"
            if "python3 -c" in remote:
                payload = {
                    "host_name": "fake-host-xyz",
                    "host_ip": "10.1.2.3",
                    "entries": {
                        "a.txt": {
                            "type": "file", "size_bytes": 5,
                            "modified": "2026-08-22 10:00:00",
                            "created": "2026-08-22 09:00:00",
                            "mode": "0644",
                        },
                    },
                }
                return 0, json.dumps(payload), ""
            return 127, "", "missing"

    dest = tempfile.mkdtemp()
    win = CopierWindow()
    win.show_all()
    win.conn = FakeConn()
    assert win._export_depth_value("1") == 1
    assert win._export_depth_value("full") is None
    assert win.notebook.get_n_pages() == 3, "Selected / Destination / Transfers"
    assert win.notebook.get_tab_label(win.notebook.get_nth_page(0)).get_text() == "Destination", \
        "Destination tab must come first"
    assert win.notebook.get_tab_label(win.notebook.get_nth_page(1)).get_text() \
        .startswith("Selected"), "Selected tab must come second"
    assert win.notebook.get_current_page() == 0, "Destination is the default tab"
    src_ctx = win._export_context("source")
    assert src_ctx["host_info"]["host_name"] == "fake-host-xyz"
    assert win._export_context("dest")["panel"] == "dest"

    def row_of(t):
        return win.transfers_model[win._trow[t.id]]

    def wait_status(t, status):
        loop = GLib.MainLoop()

        def check():
            if t.status == status:
                loop.quit()
                return False
            return True

        GLib.timeout_add(200, check)
        GLib.timeout_add(30000, lambda: (loop.quit(), False)[1])
        loop.run()
        assert t.status == status, t.status

    win.dest_entry.set_text(dest)
    win.selected = {"/remote/folder/file.bin": {"name": "file.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    t = win._transfers[0]
    assert win.notebook.get_current_page() == 2, "auto-switch to Transfers tab"
    saw = {"pct": False, "speed": False, "fill": False}
    loop = GLib.MainLoop()

    def done_check():
        if t.status == "done":
            loop.quit()
            return False
        if t.status == "running":
            r = row_of(t)
            saw["pct"] = saw["pct"] or "%" in r[2]
            saw["speed"] = saw["speed"] or r[4].endswith("/s")
            saw["fill"] = saw["fill"] or (0 < r[3] < 100)
        return True

    GLib.timeout_add(200, done_check)
    GLib.timeout_add(30000, lambda: (loop.quit(), False)[1])
    loop.run()
    assert t.status == "done"
    assert os.path.isfile(os.path.join(dest, "file.bin"))
    r = row_of(t)
    assert r[2] == "done"
    assert r[3] == 100
    assert r[6] == "✓"
    assert r[9] is None
    assert "Copied to" in r[10]
    assert saw["pct"], "progress percentage shown while running"
    assert saw["fill"], "progress cell shows a partial fill while running"
    assert saw["speed"], "speed shown while running"
    assert win.summary_lbl.get_text() == "1 done · 0 failed"
    assert win.notebook.get_current_page() == 2, "stays on Transfers tab after batch"
    assert win.sel_tab_lbl.get_text() == "Selected (1)"
    assert win.transfers_tab_lbl.get_text() == "Transfers (1)"
    assert not win.cancel_all_btn.get_visible()
    assert win.clear_finished_btn.get_visible()

    # folder via tar: file counter visible while running
    win.selected = {"/remote/folder/sub": {"name": "sub", "dest": dest, "is_dir": True}}
    win._on_transfer(None)
    td = win._transfers[1]
    saw_file = {"v": False}
    loop = GLib.MainLoop()

    def done_check2():
        if td.status == "done":
            loop.quit()
            return False
        if td.status == "running":
            saw_file["v"] = saw_file["v"] or ("file " in row_of(td)[2])
        return True

    GLib.timeout_add(200, done_check2)
    GLib.timeout_add(30000, lambda: (loop.quit(), False)[1])
    loop.run()
    assert td.status == "done"
    assert os.path.isdir(os.path.join(dest, "sub"))
    assert saw_file["v"], "tar file counter shown while running"
    assert win.summary_lbl.get_text() == "2 done · 0 failed"

    # failure -> retry via grid row
    win.conn = FakeConn(fail=True)
    win.selected = {"/remote/folder/fail.bin": {"name": "fail.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    tf = win._transfers[2]
    assert win.cancel_all_btn.get_visible(), "cancel-all shown while queued/running"
    wait_status(tf, "failed")
    r = row_of(tf)
    assert r[6] == "✗"
    assert r[7] == win._colors["failed"]
    assert r[9] == "view-refresh", "retry icon shown after failure"
    assert "boom" in r[10]
    assert win.summary_lbl.get_text() == "2 done · 1 failed"
    assert not win.cancel_all_btn.get_visible()

    win.conn = FakeConn()
    tf.conn = FakeConn()
    win._retry_for_path(win._trow[tf.id])
    wait_status(tf, "done")
    assert os.path.isfile(os.path.join(dest, "fail.bin"))
    assert row_of(tf)[9] is None
    assert win.summary_lbl.get_text() == "3 done · 1 failed"

    # worker exception safety: copy() raising must end as failed, not stuck
    class BoomConn(FakeConn):
        def copy(self, *a, **k):
            raise OSError("disk exploded")

    win.conn = BoomConn()
    win.selected = {"/remote/folder/boom.bin": {"name": "boom.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    tb = win._transfers[-1]
    wait_status(tb, "failed")
    assert row_of(tb)[6] == "✗"
    assert "disk exploded" in row_of(tb)[10]
    assert win.summary_lbl.get_text() == "3 done · 2 failed"

    # dynamic concurrency: raise the spinner and queued items start at once;
    # lower it and running items finish while new starts are throttled
    def pump(secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            while Gtk.events_pending():
                Gtk.main_iteration()
            time.sleep(0.01)

    win.parallel_spin.set_value(2)
    win.conn = FakeConn()
    win.selected = {}
    for n in ("c1.bin", "c2.bin", "c3.bin"):
        win.selected[f"/remote/folder/{n}"] = {"name": n, "dest": dest, "is_dir": False}
    win._on_transfer(None)
    ts = win._transfers[-3:]
    end = time.monotonic() + 5
    while time.monotonic() < end and sum(1 for t in ts if t.status == "running") != 2:
        pump(0.02)
    assert sum(1 for t in ts if t.status == "running") == 2, [t.status for t in ts]
    win.parallel_spin.set_value(1)
    pump(0.4)
    assert win._max_parallel == 1, "gate follows the spinner"
    assert sum(1 for t in ts if t.status == "running") == 2, \
        "lowering parallelism must not stop running items"
    assert sum(1 for t in ts if t.status == "queued") == 1, \
        "extra item waits for a free slot when lowered"
    for t in ts:
        wait_status(t, "done")
    assert all(row_of(t)[3] == 100 for t in ts)

    # clear finished while a transfer is running: its row path must stay valid
    win.conn = FakeConn()
    win.selected = {"/remote/folder/keep.bin": {"name": "keep.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    tk = win._transfers[-1]
    wait_status(tk, "running")
    win._on_clear_finished(None)
    assert row_of(tk)[1] == "keep.bin", "running row path stays valid after clear"
    wait_status(tk, "done")
    assert row_of(tk)[6] == "✓"

    # regression: clearing 10+ rows must not corrupt row paths (string-sorted
    # removal used to take '10' after '9' and crash mid-loop)
    win.parallel_spin.set_value(8)
    win.conn = FakeConn()
    win.selected = {}
    for n in range(14):
        win.selected[f"/remote/folder/m{n}.bin"] = {"name": f"m{n}.bin", "dest": dest, "is_dir": False}
    win._on_transfer(None)
    many = win._transfers[-14:]
    for t in many:
        wait_status(t, "done")
    win._on_clear_finished(None)
    assert len(win.transfers_model) == 0, "all rows removed"
    assert win._transfers == []
    assert not any(t.id in win._trow for t in many)
    assert not any(t.id in win._by_id for t in many)
    win.parallel_spin.set_value(1)

    # a stale row path must never crash the ticker or block other rows
    win.conn = FakeConn()
    win.parallel_spin.set_value(2)
    win.selected = {
        "/remote/folder/poison.bin": {"name": "poison.bin", "dest": dest, "is_dir": False},
        "/remote/folder/healthy.bin": {"name": "healthy.bin", "dest": dest, "is_dir": False},
    }
    win._on_transfer(None)
    tp, th = win._transfers[-2], win._transfers[-1]
    wait_status(th, "running")
    win._trow[tp.id] = "99"
    filled = {"v": False}
    end = time.monotonic() + 5
    while time.monotonic() < end and not filled["v"]:
        pump(0.05)
        r = next((r for r in win.transfers_model if r[0] == th.id), None)
        filled["v"] = filled["v"] or (r is not None and 0 < r[3] < 100)
    assert filled["v"], "healthy row keeps updating while another row has a stale path"
    assert win._trow.get(tp.id) is None, "stale path dropped from _trow"
    wait_status(tp, "done")
    wait_status(th, "done")
    assert next(r for r in win.transfers_model if r[0] == th.id)[6] == "✓"
    win.parallel_spin.set_value(1)

    # clear finished removes terminal rows only
    win._on_clear_finished(None)
    assert win._transfers == [], "all rows were terminal"
    assert win.transfers_tab_lbl.get_text() == "Transfers (0)"
    assert not win.clear_finished_btn.get_visible()

    # is_dir captured at selection time and passed to the transfer
    it = win.model.append([False, "zdir", "—", "Folder", "x", True, "/r/zdir", 0, 0])
    win._on_row_toggled(None, win.model.get_path(it))
    assert win.selected["/r/zdir"]["is_dir"] is True, "dir flag captured on toggle"
    it = win.model.append([False, "zfile", "1 B", "File", "x", False, "/r/zfile", 1, 0])
    win._on_row_toggled(None, win.model.get_path(it))
    assert win.selected["/r/zfile"]["is_dir"] is False, "file flag captured on toggle"
    win.selected = {"/r/zdir": win.selected["/r/zdir"]}
    win._on_transfer(None)
    assert win._transfers[-1].is_dir is True, "transfer carries is_dir"
    wait_status(win._transfers[-1], "done")

    # multi-destination: pinned per item, grey marker, set-all re-points
    d2 = tempfile.mkdtemp()
    win.selected = {"/r/a.txt": {"name": "a.txt", "dest": dest}}
    win.dest_entry.set_text(d2)
    win._refresh_sel()
    row0 = win.sel_model[0]
    assert row0[2] == os.path.join(dest, "a.txt"), "existing item keeps its dest"
    assert row0[4] is True, "grey marker when dest differs from entry"
    win.selected["/r/b.txt"] = {"name": "b.txt", "dest": d2}
    win._refresh_sel()
    rows = {r[0]: r for r in win.sel_model}
    assert rows["b.txt"][2] == os.path.join(d2, "b.txt")
    assert rows["b.txt"][4] is False
    win.dest_entry.set_text(dest)
    win._on_set_all_dest(None)
    rows = {r[0]: r for r in win.sel_model}
    assert rows["a.txt"][2] == os.path.join(dest, "a.txt")
    assert rows["b.txt"][2] == os.path.join(dest, "b.txt")
    assert rows["b.txt"][4] is False

    # destination browser: loads a listing, mirrors remote meta, colors states
    win.dest_entry.set_text(dest)
    win._load_dest()
    end = time.monotonic() + 5
    while time.monotonic() < end and win.dest_current_path != dest:
        pump(0.02)
    assert win.dest_current_path == dest, "destination listing must load"
    assert len(win.dest_model) > 0, "destination listing must load"
    assert win._colors is not None and all(k in win._colors for k in
        ("missing", "differ", "conflict", "same", "extra", "running", "done",
         "failed", "queued", "skipped", "cancelled", "paused")), "palette populated"

    # stale-load guard: an older request must not overwrite a newer listing
    win.dest_entry.set_text(os.path.join(dest, "no-such"))
    win._load_dest()
    req_old = win._dest_req
    win.dest_entry.set_text(dest)
    win._load_dest()
    assert win._dest_req == req_old + 1, "request counter increments"
    end = time.monotonic() + 5
    while time.monotonic() < end and win.dest_current_path != dest:
        pump(0.02)
    assert win.dest_current_path == dest, "newest request wins"
    assert win._dest_meta, "dest listing restored after stale request"
    dest_names = {r[ui.DST_NAME] for r in win.dest_model}
    assert "file.bin" in dest_names
    win._remote_meta = {}
    win._dest_meta = {}
    win._apply_states()
    assert all(r[ui.DST_STATE] == 9 for r in win.dest_model), \
        "no states yet: neither side has a listing"

    # fake a remote listing with a.txt only; file.bin becomes extra, a.txt missing
    win._dest_meta = {r[ui.DST_NAME]: {"is_dir": r[ui.DST_IS_DIR], "size": 0} for r in win.dest_model}
    win._remote_meta = {"a.txt": {"is_dir": False, "size": 5}}
    win._apply_states()
    assert win._states["a.txt"] == "missing"
    assert win._states["file.bin"] == "extra"
    assert any(r[ui.DST_STATE] == 4 for r in win.dest_model), "extra rows get extra priority"
    assert "missing" in win.remote_summary_lbl.get_text()
    assert "extra" in win.dest_summary_lbl.get_text()

    # sort by state priority via the State column
    win.dest_model.set_sort_column_id(ui.DST_STATE, Gtk.SortType.ASCENDING)
    pris = [r[ui.DST_STATE] for r in win.dest_model]
    assert pris == sorted(pris), "rows sorted by state priority"
    win.dest_model.set_sort_column_id(Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID,
                                      Gtk.SortType.ASCENDING)

    # missing destination folder: notice shown, no crash
    win.dest_entry.set_text(os.path.join(dest, "no-such"))
    win._load_dest()
    end = time.monotonic() + 5
    while time.monotonic() < end and "not" not in win.dest_summary_lbl.get_text():
        pump(0.02)
    assert "not accessible" in win.dest_summary_lbl.get_text()
    assert len(win.dest_model) == 0
    win.dest_entry.set_text(dest)
    win._load_dest()
    end = time.monotonic() + 5
    while time.monotonic() < end and win.dest_current_path != dest:
        pump(0.02)
    assert win.dest_current_path == dest

    # compare: selects missing/size-differing top-level items, adds to selection
    assert win.compare_btn.get_label() == "⇄ Select missing (recursive)"
    win.conn = FakeConn()
    win.dest_entry.set_text(dest)
    win.model.clear()
    for nm, isd in (("new.bin", False), ("old.bin", False), ("sub", True)):
        win.model.append([False, nm, "—", "Folder" if isd else "File", "x", isd,
                          f"/r/{nm}", 0, 0])
    win.selected = {"/r/old.bin": {"name": "old.bin", "dest": dest, "is_dir": False}}
    win.model[1][0] = True
    win._refresh_sel()
    win._on_compare(win.compare_btn)
    loop = GLib.MainLoop()

    def compare_check():
        if win.status_label.get_text().startswith("Compare:"):
            loop.quit()
            return False
        return True

    GLib.timeout_add(100, compare_check)
    GLib.timeout_add(10000, lambda: (loop.quit(), False)[1])
    loop.run()
    assert set(win.selected) == {"/r/old.bin", "/r/new.bin", "/r/sub"}, set(win.selected)
    assert win.selected["/r/sub"]["is_dir"] is True
    assert win.selected["/r/new.bin"]["dest"] == dest
    assert win.model[0][0] is True
    assert win.model[1][0] is True, "manual selection must survive compare"
    assert win.model[2][0] is True
    assert "missing" in win.status_label.get_text()
    assert win.compare_btn.get_sensitive()

    # compare with a missing destination folder: error, selection untouched
    errors = []
    win._show_error = lambda *a: errors.append(a)
    win.dest_entry.set_text(os.path.join(dest, "no-such-dir"))
    win.selected = {"/r/x.bin": {"name": "x.bin", "dest": dest}}
    win._on_compare(None)
    assert errors, "missing destination must raise an error"
    assert set(win.selected) == {"/r/x.bin"}, "selection untouched on error"
    win.dest_entry.set_text(dest)

    # a worker exception during compare must not stick the UI
    def boom_tree(p):
        raise OSError("Too many open files")

    win.conn.tree_remote = boom_tree
    win.compare_btn.set_sensitive(False)
    win.status_label.set_text("Comparing…")
    win._on_compare(win.compare_btn)
    pump(0.5)
    assert win.compare_btn.get_sensitive(), "compare button must be re-enabled"
    assert win.status_label.get_text() == "Compare failed"
    win.conn = FakeConn()

    # selection QoL buttons act on the current listing and add to selection
    win.selected = {}
    for row in win.model:
        row[0] = False
    win._states = {"new.bin": "missing", "old.bin": "differ", "sub": "same"}
    win._on_select_missing(None)
    assert set(win.selected) == {"/r/new.bin"}, set(win.selected)
    assert win.model[0][0] is True and win.model[1][0] is False
    win._on_select_changed(None)
    assert set(win.selected) == {"/r/new.bin", "/r/old.bin"}, \
        "select changed adds missing+differ+conflict"
    win._on_invert(None)
    assert set(win.selected) == {"/r/sub"}, "invert flips only the current rows"
    win.selected = {}
    for row in win.model:
        row[0] = False
    win._on_select_folders(None)
    assert set(win.selected) == {"/r/sub"}, "folders selects directories only"
    win._on_select_files(None)
    assert set(win.selected) == {"/r/sub", "/r/new.bin", "/r/old.bin"}, \
        "files adds files without clearing folders"

    # live name filter: only matching rows visible, selection unaffected
    win.filter_entry.set_text("old")
    pump(0.05)
    assert [r[1] for r in win.tree.get_model()] == ["old.bin"], \
        "filter must hide non-matching rows"
    fiter = win.tree.get_model().get_iter_first()
    win.selected = {}
    for row in win.model:
        row[0] = False
    win._on_row_toggled(None, "0")
    assert "/r/old.bin" in win.selected, "toggling a filtered row must work"
    assert win.tree.get_model().get_path(fiter) is not None
    win.selected = {}
    win.filter_entry.set_text("")
    pump(0.05)
    assert [r[1] for r in win.tree.get_model()] == ["new.bin", "old.bin", "sub"], \
        "clearing the filter restores all rows"
    win._states = {}

    # pause/resume and remove on the transfers page
    win.conn = FakeConn()
    win.selected = {"/r/pause.bin": {"name": "pause.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    tp = win._transfers[-1]
    wait_status(tp, "running")
    assert row_of(tp)[11] == "media-playback-pause"
    win._on_pause(tp)
    assert tp.status == "paused"
    assert row_of(tp)[11] == "media-playback-start"
    win._on_pause(tp)
    assert tp.status == "running"
    assert row_of(tp)[11] == "media-playback-pause"
    win._on_remove(tp)
    assert tp in win._transfers, "running items must not be removable"
    wait_status(tp, "done")

    # remove a queued item: row gone, worker must never start it
    win.parallel_spin.set_value(8)
    tq = Transfer("q.bin", "/r/q.bin", os.path.join(dest, "q.bin"), win.batch, win.conn)
    win._enqueue(tq)
    assert row_of(tq)[11] == "edit-delete"
    n = len(win._transfers)
    win._on_remove(tq)
    assert tq not in win._transfers
    assert len(win._transfers) == n - 1
    assert tq.id not in win._by_id
    assert f"({n - 1})" in win.transfers_tab_lbl.get_text()
    pump(0.5)
    assert tq.status == "queued" and tq.removed is True, "removed item must not start"

    # remove a paused item: kills its procs and drops it
    win.parallel_spin.set_value(1)
    win.selected = {"/r/pause2.bin": {"name": "pause2.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    tp2 = win._transfers[-1]
    wait_status(tp2, "running")
    win._on_pause(tp2)
    assert tp2.status == "paused"
    killed = []
    win.conn.kill_procs = lambda sink: killed.append(sink)
    n = len(win._transfers)
    win._on_remove(tp2)
    assert tp2 not in win._transfers
    assert len(win._transfers) == n - 1
    assert killed, "paused removal must kill its processes"
    assert tp2.procs is killed[0]
    end = time.monotonic() + 5
    while time.monotonic() < end and tp2.id in win._by_id:
        while Gtk.events_pending():
            Gtk.main_iteration()
        time.sleep(0.01)
    assert tp2.id not in win._by_id
    assert tp2.status == "paused", "removed transfer must not be marked finished"

    # long names must not stretch the window: ellipsized columns bound the
    # width request even after the tree holds very long text
    win.sel_model.clear()
    win.sel_model.append(["x" * 500, "/r/" + "y" * 400, "/dest/" + "z" * 400, None, False])
    win.show_all()
    pump(0.3)
    wmin, wnat = win.get_preferred_width()
    assert wmin < 2000, f"window minimum width grew to {wmin}"
    assert wnat < 2000, f"window natural width grew to {wnat}"
    win.set_default_size(1120, 740)

    # resume via the click path: _action_for_path must handle the resume icon
    # (the old code only knew 'pause' and 'delete', so resume was a no-op)
    if win._glyphs:
        assert "media-playback-start" in win._glyphs, "resume glyph must render"
    win.conn = FakeConn()
    win.selected = {"/r/resume.bin": {"name": "resume.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    tr = win._transfers[-1]
    wait_status(tr, "running")
    assert row_of(tr)[11] == "media-playback-pause"
    win._on_pause(tr)
    assert tr.status == "paused"
    assert row_of(tr)[11] == "media-playback-start"
    win._action_for_path(win._trow[tr.id])
    assert tr.status == "running", "clicking the resume icon must resume"
    assert row_of(tr)[11] == "media-playback-pause"
    win.conn.kill_all = lambda: None
    win._on_cancel_all(None)
    wait_status(tr, "cancelled")

    # sorting: natural name order with folders always first
    win.model.clear()
    for nm in ("file10.bin", "file2.bin", "A.txt", "b.txt", "adir"):
        win.model.append([False, nm, "—", "Folder" if nm == "adir" else "File",
                          "x", nm == "adir", "/r/" + nm,
                          1 if nm != "adir" else 0, 0])
    win.model_sort.set_sort_column_id(1, Gtk.SortType.ASCENDING)
    names = [r[1] for r in win.model_sort]
    assert names == ["adir", "A.txt", "b.txt", "file2.bin", "file10.bin"], names
    assert win.dest_model.get_sort_column_id() == (ui.DST_NAME, Gtk.SortType.ASCENDING), \
        "source name sort must mirror into the destination panel"
    win.model_sort.set_sort_column_id(1, Gtk.SortType.DESCENDING)
    names = [r[1] for r in win.model_sort]
    assert names == ["adir", "file10.bin", "file2.bin", "b.txt", "A.txt"], names
    assert win.dest_model.get_sort_column_id() == (ui.DST_NAME, Gtk.SortType.DESCENDING), \
        "order toggle must mirror too"

    # size sorts numerically, never lexically ("10" < "2" as text)
    win.model.clear()
    for nm, sz in (("big.bin", 100), ("small.bin", 2), ("mid.bin", 10)):
        win.model.append([False, nm, f"{sz} B", "File", "x", False, "/r/" + nm, sz, 0])
    win.model_sort.set_sort_column_id(2, Gtk.SortType.ASCENDING)
    names = [r[1] for r in win.model_sort]
    assert names == ["small.bin", "mid.bin", "big.bin"], names
    assert win.dest_model.get_sort_column_id() == (ui.DST_SIZE_TEXT, Gtk.SortType.ASCENDING)

    # modified sorts by epoch, not by the displayed string
    win.model.clear()
    for nm, ep in (("new.bin", 3000), ("old.bin", 500), ("mid.bin", 1500)):
        win.model.append([False, nm, "1 B", "File", "x", False, "/r/" + nm, 1, ep])
    win.model_sort.set_sort_column_id(4, Gtk.SortType.ASCENDING)
    names = [r[1] for r in win.model_sort]
    assert names == ["old.bin", "mid.bin", "new.bin"], names

    # type sorts folders first, then type text, then name
    win.model.clear()
    for nm, typ in (("link.lnk", "Link"), ("file.txt", "File"), ("dir", "Folder")):
        win.model.append([False, nm, "1 B", typ, "x", typ == "Folder",
                          "/r/" + nm, 1, 0])
    win.model_sort.set_sort_column_id(3, Gtk.SortType.ASCENDING)
    names = [r[1] for r in win.model_sort]
    assert names == ["dir", "file.txt", "link.lnk"], names

    # state sort mirrors across panels and back without recursion
    win.model_sort.set_sort_column_id(0, Gtk.SortType.ASCENDING)
    assert win.dest_model.get_sort_column_id() == (ui.DST_STATE, Gtk.SortType.ASCENDING)
    win.dest_model.set_sort_column_id(ui.DST_SIZE_TEXT, Gtk.SortType.DESCENDING)
    assert win.model_sort.get_sort_column_id() == (2, Gtk.SortType.DESCENDING)
    assert win._syncing_sort is False, "sync guard must reset"
    win.dest_model.set_sort_column_id(Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID,
                                      Gtk.SortType.ASCENDING)
    assert win.model_sort.get_sort_column_id() == (2, Gtk.SortType.DESCENDING), \
        "unsorted must never be propagated (GTK3 TreeModelSort crashes on later inserts)"

    # toggling a row through the *sorted* tree paths selects the right item
    win.model_sort.set_sort_column_id(1, Gtk.SortType.ASCENDING)
    pick = None
    for i, row in enumerate(win.model_sort):
        if row[1] == "file.txt":
            pick = i
            break
    assert pick is not None, "row must be visible in the sorted model"
    win.selected = {}
    for row in win.model:
        row[0] = False
    win._on_row_toggled(None, str(win.model_sort.get_path(
        win.model_sort.get_iter((pick,)))))
    assert win.selected == {"/r/file.txt": {"name": "file.txt",
                                            "dest": win._current_dest(),
                                            "is_dir": False}}, win.selected
    win.selected = {}

    # name filter keeps working while a sort is active
    win.model.clear()
    for nm, isd in (("old.bin", False), ("new.bin", False), ("sub", True)):
        win.model.append([False, nm, "—", "Folder" if isd else "File", "x", isd,
                          f"/r/{nm}", 0, 0])
    win.model_sort.set_sort_column_id(1, Gtk.SortType.ASCENDING)
    win.filter_entry.set_text("old")
    pump(0.05)
    visible = [r[1] for r in win.tree.get_model()]
    assert visible == ["old.bin"], f"filter while sorted: {visible}"
    win.filter_entry.set_text("")
    pump(0.05)
    assert len(win.tree.get_model()) == 3
    # leave a real (non-unsorted) sort active: inserting rows into a
    # TreeModelSort that was sorted then unsorted crashes GTK3
    win.model_sort.set_sort_column_id(1, Gtk.SortType.ASCENDING)

    # quit confirmation: vetoed while transfers run, allowed once none are
    win._confirm_quit = lambda: True
    win.conn = FakeConn()
    win.selected = {"/r/active.bin": {"name": "active.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    ta = win._transfers[-1]
    wait_status(ta, "running")
    assert win._on_delete_event(None, None) is False, \
        "confirm-quit (Quit anyway) must close the window"
    win._confirm_quit = lambda: False
    assert win._on_delete_event(None, None) is True, \
        "declined confirm must keep the window open"
    win.conn.kill_all = lambda: None
    win._on_cancel_all(None)
    wait_status(ta, "cancelled")
    assert win._on_delete_event(None, None) is False, \
        "no active transfers -> close without asking"

    # model shapes: raw sort keys live at the end of each listing store
    assert win.model.get_n_columns() == 9, "source store must carry raw keys"
    assert win.dest_model.get_n_columns() == 11, "dest store must carry raw keys (including checkbox)"
    # raw size/epoch keys must be 64-bit: 32-bit gint overflows on epoch
    # seconds past 2038 (e.g. 3224467699) and crashed the loaders mid-loop,
    # which also skipped the state pass and stripped folder status colors
    assert win.model.get_column_type(7).name == "gint64"
    assert win.model.get_column_type(8).name == "gint64"
    assert win.dest_model.get_column_type(9).name == "gint64"
    assert win.dest_model.get_column_type(10).name == "gint64"
    big = 3224467699
    win.model.append([False, "future.bin", "—", "File", "x", False, "/r/future.bin", 1, big])
    win.dest_model.append([False, "future.bin", "—", "File", "x", "/d/future.bin", False, "", 9, 1, big])
    win.model_sort.set_sort_column_id(4, Gtk.SortType.DESCENDING)
    srow = next(r for r in win.model_sort if not r[ui.SRC_IS_DIR])
    assert srow[1] == "future.bin", \
        "large epoch must sort to the front among files, not overflow"
    win.dest_model.set_sort_column_id(ui.DST_MTIME_TEXT, Gtk.SortType.DESCENDING)
    drow = next(r for r in win.dest_model if not r[ui.DST_IS_DIR])
    assert drow[ui.DST_NAME] == "future.bin", \
        "large epoch must sort on the dest side without overflow"
    win._remote_meta["future.bin"] = {"is_dir": False, "size": 1}
    win._dest_meta["future.bin"] = {"is_dir": False, "size": 1}
    win._apply_states()
    cell = Gtk.CellRendererText()
    fit = next(win.model.get_iter((i,)) for i in range(len(win.model))
               if win.model[i][1] == "future.bin")
    win._state_cb(None, cell, win.model, fit)
    assert cell.get_property("text") == "same", \
        "states must still be applied and rendered after a large epoch row"
    assert cell.get_property("foreground-set") is True, \
        "color coding must survive a large epoch row"

    # quit robustness: tracked dialogs are closed, partial files removed,
    # destroy is idempotent and never raises
    win2 = CopierWindow()
    fpart = os.path.join(dest, ".quit-test.lan-copier-part-0")
    with open(fpart, "w") as f:
        f.write("x")

    class _FakeDialog:
        def __init__(self):
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    fd = _FakeDialog()
    win2._track_dialog(fd)
    tq = Transfer("q.bin", "/r/q.bin", os.path.join(dest, "q.bin"), win2.batch,
                  win2.conn or FakeConn())
    tq.status = "running"
    tq.part = fpart
    win2._transfers.append(tq)
    win2._on_destroy(win2)
    assert win2._destroyed is True
    assert fd.destroyed is True, "tracked dialogs must be destroyed on quit"
    assert win2._dialogs == []
    assert not os.path.exists(fpart), "partial file must be cleaned up on quit"
    win2._on_destroy(win2)  # second destroy must be a no-op
    assert win2._destroyed is True

    # profile session persistence: connect auto-creates/updates the profile,
    # transfer start saves source/dest/selection, clear empties the stored
    # selection but keeps locations, reconnect restores everything
    writes = []
    orig_prof_save = ui.profiles.save
    ui.profiles.save = lambda profs: (writes.append(1), orig_prof_save(profs))[1]
    try:
        win.profile_combo.set_active(-1)
        win.remember.set_active(False)
        win.pass_entry.set_text("s3cret")
        win._connected(FakeConn(), "/home/tester-xyz")
        pump(0.5)
        assert win._active_profile == "tester-xyz@fake-host-xyz:22"
        p = win.profiles["tester-xyz@fake-host-xyz:22"]
        assert p["host"] == "fake-host-xyz" and p["port"] == 22
        assert p["user"] == "tester-xyz"
        assert "password" not in p, "remember off: password must not be stored"
        assert win.current_path == "/home/tester-xyz", "no last_source yet -> home"
        assert writes, "profile creation must persist"

        win.selected = {
            "/home/tester-xyz/a.txt": {"name": "a.txt", "dest": dest, "is_dir": False},
            "/home/tester-xyz/sub": {"name": "sub", "dest": dest, "is_dir": True},
        }
        win._on_transfer(None)
        p = win.profiles["tester-xyz@fake-host-xyz:22"]
        assert p["last_source"] == "/home/tester-xyz"
        assert p["last_dest"] == dest
        assert set(p["last_selection"]) == {"/home/tester-xyz/a.txt", "/home/tester-xyz/sub"}

        win._on_clear_sel(None)
        p = win.profiles["tester-xyz@fake-host-xyz:22"]
        assert p["last_selection"] == {}
        assert p["last_source"] == "/home/tester-xyz", "clear must keep source"
        assert p["last_dest"] == dest, "clear must keep destination"

        win._on_clear_sel(None)

        win.selected = {"/home/tester-xyz/sub": {"name": "sub", "dest": dest, "is_dir": True}}
        win._save_profile_state()
        n = len(writes)
        win._save_profile_state()
        assert len(writes) == n, "no rewrite when nothing changed"

        win._connected(FakeConn(), "/home/tester-xyz")
        pump(0.5)
        assert win.current_path == "/home/tester-xyz", "last_source must be restored"
        assert win.dest_entry.get_text() == dest, "last_dest must be restored"
        assert set(win.selected) == {"/home/tester-xyz/sub"}, "selection must be restored"
        assert win.model[0][0] is True, "restored row must be checked (dirs sort first)"
        assert win.sel_model[0][0] == "sub"
        assert win._restore_selection is None, "restore flag must be consumed"
    finally:
        ui.profiles.save = orig_prof_save

    # starting a new batch must never cancel transfers of the previous one
    win.conn = FakeConn()
    win.selected = {"/r/b1.bin": {"name": "b1.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    tb1 = win._transfers[-1]
    win.selected = {"/r/b2.bin": {"name": "b2.bin", "dest": dest, "is_dir": False}}
    win._on_transfer(None)
    tb2 = win._transfers[-1]
    wait_status(tb1, "done")
    assert row_of(tb1)[6] == "✓", "a later batch must not cancel an earlier one"
    wait_status(tb2, "done")
    assert row_of(tb2)[6] == "✓"

    # a corrupted profile port must fall back to 22 instead of raising
    win._load_profiles_ui()
    win.profiles["bad-port"] = {"host": "h", "port": "abc", "user": "u"}
    win.profile_combo.append_text("bad-port")
    win.profile_combo.set_active(list(win.profiles).index("bad-port"))
    pump(0.1)
    assert win.port_spin.get_value() == 22, "corrupt profile port must default to 22"
    win.profiles.pop("bad-port", None)

    # a worker exception during a remote listing must not crash the UI
    errs = []
    win._show_error = lambda *a: errs.append(a)
    win.conn = FakeConn()

    def boom_list(p):
        raise OSError("Too many open files")

    win.conn.list_dir = boom_list
    win._load_remote()
    end = time.monotonic() + 5
    while time.monotonic() < end and not win.refresh_btn.get_sensitive():
        pump(0.02)
    assert win.refresh_btn.get_sensitive(), "refresh re-enabled after listing failure"
    win.conn = FakeConn()
    win._show_error = None

    # ---- local source mode ----
    lsrc = tempfile.mkdtemp()
    try:
        with open(os.path.join(lsrc, "ln1.txt"), "w") as f:
            f.write("aaaa")
        os.makedirs(os.path.join(lsrc, "lsub"))
        with open(os.path.join(lsrc, "lsub", "l2.txt"), "w") as f:
            f.write("bbbbbb")
        # the SSH profile has no last_source_local, so switching to local must
        # NOT auto-connect; the Browse button must stay visible (regression:
        # it used to be buried in the hidden SSH auth box).
        win._active_profile = "tester-xyz@fake-host-xyz:22"
        win.source_combo.set_active(1)
        pump(0.1)
        assert win.connect_btn.get_visible(), "Browse button must be visible in local mode"
        assert win.connect_btn.get_label() == "Browse…", win.connect_btn.get_label()
        assert not win.auth_box.get_visible(), "SSH auth hidden in local mode"
        assert win.local_hint.get_visible(), "hint shown while locally disconnected"
        assert win.conn is None

        # pick a real local source folder (explicit path, no modal dialog)
        win._connect_local(lsrc)
        assert isinstance(win.conn, LocalConnection)
        assert win.connect_btn.get_label() == "Disconnect"
        assert not win.local_hint.get_visible(), "hint hidden once connected"
        assert win.status_label.get_text().startswith("Local source")
        pump(0.3)
        names = {win.model[i][1] for i in range(len(win.model))}
        assert names == {"ln1.txt", "lsub"}, names

        # compare against the destination folder
        win.dest_entry.set_text(dest)
        win._load_dest()
        pump(0.3)
        assert win._states.get("ln1.txt") == "missing", win._states
        assert win._states.get("lsub") == "missing", win._states

        # a real local transfer of a file and a folder
        win.selected = {
            os.path.join(lsrc, "ln1.txt"): {"name": "ln1.txt", "dest": dest, "is_dir": False},
            os.path.join(lsrc, "lsub"): {"name": "lsub", "dest": dest, "is_dir": True},
        }
        n0 = len(win._transfers)
        win._on_transfer(None)
        tl1 = win._transfers[-2]
        tl2 = win._transfers[-1]
        wait_status(tl1, "done")
        wait_status(tl2, "done")
        assert open(os.path.join(dest, "ln1.txt")).read() == "aaaa"
        assert open(os.path.join(dest, "lsub", "l2.txt")).read() == "bbbbbb"
        assert parts_in(dest) == []

        # disconnect in local mode -> Browse button + hint return
        win._on_connect(None)
        assert win.conn is None
        assert win.connect_btn.get_label() == "Browse…"
        assert win.local_hint.get_visible()

        # auto-restore a saved local source when re-entering local mode
        win.profiles["tester-xyz@fake-host-xyz:22"]["last_source_local"] = lsrc
        win.source_combo.set_active(0)
        pump(0.1)
        assert win.connect_btn.get_label() == "Connect", "SSH mode restored"
        assert win.auth_box.get_visible(), "SSH auth visible again"
        win.source_combo.set_active(1)
        pump(0.1)
        assert isinstance(win.conn, LocalConnection)
        assert win.current_path == lsrc, "saved local source auto-restored"
        assert win.connect_btn.get_label() == "Disconnect"
        win._on_connect(None)  # leave disconnected
        # destination panel checkboxes & selection controls
        del_test_dir = tempfile.mkdtemp(prefix="test_ui_dest_del_")
        try:
            for f in ("d1.txt", "d2.txt", "d3.txt"):
                with open(os.path.join(del_test_dir, f), "w") as fp:
                    fp.write("del test data")
            win.dest_entry.set_text(del_test_dir)
            win._load_dest()
            end = time.monotonic() + 5
            while time.monotonic() < end and win.dest_current_path != del_test_dir:
                pump(0.02)
            assert len(win.dest_model) == 3

            # Select all
            win.dest_toggle_all_btn.set_active(True)
            assert len(win.dest_selected) == 3
            assert all(r[ui.DST_CHECK] for r in win.dest_model)
            assert win.dest_delete_btn.get_label() == "🗑 Delete Checked (3)"
            assert win.dest_delete_btn.get_sensitive() is True

            # Invert
            win._on_dest_invert(None)
            assert len(win.dest_selected) == 0
            assert not any(r[ui.DST_CHECK] for r in win.dest_model)
            assert win.dest_delete_btn.get_label() == "🗑 Delete Checked (0)"
            assert win.dest_delete_btn.get_sensitive() is False

            # Select extras
            win._states = {"d1.txt": "extra", "d2.txt": "same", "d3.txt": "extra"}
            win._on_dest_select_extras(None)
            assert len(win.dest_selected) == 2
            assert win.dest_delete_btn.get_label() == "🗑 Delete Checked (2)"
            assert win.dest_delete_btn.get_sensitive() is True

            # Mutual exclusion: active transfer disables delete button
            t_dummy = Transfer("d1.txt", "/s/d1.txt", os.path.join(del_test_dir, "d1.txt"), win.batch, win.conn)
            t_dummy.status = "running"
            win._transfers.append(t_dummy)
            win._update_transfer_controls()
            assert win.dest_delete_btn.get_sensitive() is False
            assert "Cannot delete items" in win.dest_delete_btn.get_tooltip_text()

            # Transfer done -> delete button sensitive again
            t_dummy.status = "done"
            win._update_transfer_controls()
            assert win.dest_delete_btn.get_sensitive() is True

            # Perform delete -> switches stack to delete_progress, unlinks files, returns to browser
            f1_path = os.path.join(del_test_dir, "d1.txt")
            f3_path = os.path.join(del_test_dir, "d3.txt")
            win._confirm_delete = lambda loc, items: True
            win._run_delete([(f1_path, "d1.txt", False), (f3_path, "d3.txt", False)], side="dest")
            assert win.main_stack.get_visible_child_name() == "delete_progress"
            assert win._delete_in_progress is True

            # Wait for delete worker
            end = time.monotonic() + 5
            while time.monotonic() < end and win._delete_in_progress:
                pump(0.02)

            assert win.main_stack.get_visible_child_name() == "browser"
            assert not os.path.exists(f1_path)
            assert not os.path.exists(f3_path)
            assert os.path.exists(os.path.join(del_test_dir, "d2.txt")), "unselected file must remain"
            assert len(win.dest_selected) == 0

            # Window close guard while delete is running
            win._delete_in_progress = True
            win._confirm_quit_delete = lambda: False
            assert win._on_delete_event(None, None) is True, "window close vetoed when user declines"
            win._confirm_quit_delete = lambda: True
            assert win._on_delete_event(None, None) is False, "window close allowed when user confirms"
            win._delete_in_progress = False
        finally:
            shutil.rmtree(del_test_dir, ignore_errors=True)
    finally:
        shutil.rmtree(lsrc, ignore_errors=True)

    win._on_destroy(None)
    shutil.rmtree(dest)
    shutil.rmtree(d2)
    print("ui smoke OK")


# -- command builders -------------------------------------------------------




ALL_TESTS = (
    test_compare_trees,
    test_classify_items,
    test_color_palettes,
)

SMOKE = ui_smoke


def test_ui_dest_bar_and_gate():
    """The destination side has its own connection bar and an SSH/local gate
    that routes listing/compare/delete through the endpoint contract."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        import ui
        from ui import CopierWindow
        import panes
    except Exception as e:
        print(f"SKIP test_ui_dest_bar_and_gate (no display?): {e}")
        return

    orig_prof_path = ui.profiles._path
    prof_dir = tempfile.mkdtemp()
    ui.profiles._path = lambda: os.path.join(prof_dir, "profiles.json")
    win = None
    try:
        win = CopierWindow()
        assert isinstance(win.dest_bar, panes.ConnectionBar)
        assert win.dest_bar.is_local(), "destination bar defaults to This computer"
        assert win._dest_ssh() is False
        assert win.dest_conn is None

        class DestSSH:
            kind = "ssh"
            def __init__(self):
                self.host = "dst"
                self.port = 22
                self.user = "u"
                self.calls = []
            def close(self): pass
            def kill_all(self): pass
            def is_dir(self, p): return True
            def exists(self, p): return True
            def list_dir(self, p):
                self.calls.append(("list", p))
                return [{"name": "d.txt", "is_dir": False, "is_link": False,
                         "size": 3, "mtime": "Aug  1 00:00", "mtime_epoch": 1,
                         "path": p.rstrip("/") + "/d.txt"}]
            def tree(self, p):
                self.calls.append(("tree", p))
                return {"x": 1}
            def delete(self, p):
                self.calls.append(("delete", p))
                return (True, "")

        win.dest_conn = DestSSH()
        assert win._dest_ssh() is True
        got = win.dest_conn.list_dir("/dst/f")
        assert got[0]["name"] == "d.txt"
        assert win._dest_tree_ok("/dst/x") is True
        assert win._dest_tree("/dst/x") == {"x": 1}
        assert ("list", "/dst/f") in win.dest_conn.calls
        assert ("tree", "/dst/x") in win.dest_conn.calls
    finally:
        ui.profiles._path = orig_prof_path
        if win is not None:
            win._on_destroy(None)
        shutil.rmtree(prof_dir, ignore_errors=True)


def test_ui_dest_ssh_real_connection():
    """_dest_ssh() must be True for a real SSHConnection now that SSHConnection
    carries kind='ssh' (regression for the remote-destination blocker)."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        import ui
        from ui import CopierWindow
    except Exception as e:
        print(f"SKIP test_ui_dest_ssh_real_ssh (no display?): {e}")
        return

    orig_prof_path = ui.profiles._path
    prof_dir = tempfile.mkdtemp()
    ui.profiles._path = lambda: os.path.join(prof_dir, "profiles.json")
    win = None
    try:
        win = CopierWindow()
        from ssh_transport import SSHConnection
        win.dest_conn = SSHConnection("host", 22, "u", "pw")
        assert win._dest_ssh() is True
        win.dest_conn = None
        assert win._dest_ssh() is False
    finally:
        ui.profiles._path = orig_prof_path
        if win is not None:
            win._on_destroy(None)
        shutil.rmtree(prof_dir, ignore_errors=True)


ALL_TESTS = (
    test_compare_trees,
    test_classify_items,
    test_color_palettes,
    test_ui_dest_bar_and_gate,
    test_ui_dest_ssh_real_connection,
)
SMOKE = ui_smoke
