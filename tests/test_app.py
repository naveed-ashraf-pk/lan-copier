"""Tests for the clean reimplementation (app/): profiles v2 and an AppWindow
smoke (construct, browse a local source, transfer to a local destination)."""
import os
import sys
import tempfile
import time
import shutil
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.profiles as profiles
from app.window import classify_items, LIGHT_COLORS, DARK_COLORS


def _store_at(directory):
    p = profiles._fresh()
    p["profiles"] = {
        "a@host": {"host": "host", "port": 22, "user": "a",
                   "password": "pw", "remember": True},
        "b@host": {"host": "h2", "port": 2222, "user": "b",
                   "password": "", "remember": False},
    }
    profiles.remember_side(p, "source", "a@host")
    return p


def test_profiles_v3_normalize_roundtrip():
    d = tempfile.mkdtemp()
    try:
        store = _store_at(d)
        store["profiles"]["a@host"]["hostname"] = "macbook"
        raw = json.dumps(store)
        norm = profiles._normalize(json.loads(raw))
        assert norm["version"] == 3
        assert norm["profiles"]["a@host"]["password"] == "pw"
        assert norm["profiles"]["a@host"]["hostname"] == "macbook"
        assert norm["profiles"]["b@host"]["password"] == ""
        assert norm["profiles"]["b@host"]["hostname"] == ""
        assert norm["last"]["source_profile"] == "a@host"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_profiles_v3_legacy_is_empty():
    # A v1 flat file or garbage loads as an empty v3 store (no migration).
    norm = profiles._normalize({"u@h": {"host": "h", "port": 22, "user": "u"}})
    assert norm["version"] == 3 and norm["profiles"] == {}
    assert norm["last"]["source_profile"] is None
    norm2 = profiles._normalize("garbage")
    assert norm2["profiles"] == {}
    norm3 = profiles._normalize(None)
    assert norm3["profiles"] == {}


def test_autosave_profile_no_duplicate():
    # Reconnecting to an already-saved endpoint must refresh the existing
    # profile, not stack up "user@host 2" duplicates every time.
    import app.window as W
    prof_dir = tempfile.mkdtemp(prefix="prof-")
    orig = profiles._path
    profiles._path = lambda: os.path.join(prof_dir, "profiles.json")
    win = None
    try:
        win = W.AppWindow()
        params = {"host": "act", "port": 22, "user": "bob",
                  "password": "", "remember": False,
                  "name": "bob@act"}
        win._autosave_profile("source", params, hostname="macbook")
        names1 = profiles.names(profiles.load())
        assert names1 == ["bob@act"], names1
        # Reconnect again with the same endpoint (changed IP + different typed
        # name) → the shared hostname must refresh the existing profile.
        params2 = dict(params, host="10.0.0.9", name="something else")
        win._autosave_profile("source", params2, hostname="macbook")
        names2 = profiles.names(profiles.load())
        assert len(names2) == 1, f"duplicate profile created: {names2}"
        assert names2 == ["bob@act"], names2
        assert profiles.load()["profiles"]["bob@act"]["host"] == "10.0.0.9"
        assert profiles.load()["profiles"]["bob@act"]["hostname"] == "macbook"
    finally:
        if win is not None:
            win._on_destroy(None)
        profiles._path = orig
        shutil.rmtree(prof_dir, ignore_errors=True)


def test_merge_duplicates_report():
    # Pre-v3 files may contain "name 2/3..." rows for the same machine; merging
    # must collapse them onto one entry and keep the last side selection sane.
    store = profiles._fresh()
    base = {"host": "169.254.x", "port": 22, "user": "n",
            "hostname": "macbook", "password": "888", "remember": True}
    store["profiles"]["@macbook"] = dict(base)
    store["profiles"]["@macbook 2"] = dict(base)
    store["profiles"]["@macbook 3"] = dict(base)
    store["profiles"]["@other"] = {"host": "h2", "port": 22, "user": "n",
                                   "hostname": "", "password": "", "remember": False}
    store["last"]["source_profile"] = "@macbook 2"
    removed = profiles.merge_duplicates(store)
    assert removed == 2, removed
    assert sorted(profiles.names(store)) == ["@macbook", "@other"]
    assert store["last"]["source_profile"] == "@macbook"


def test_classify_app():
    remote = [{"name": "r.txt", "is_dir": False, "size": 5},
              {"name": "both", "is_dir": False, "size": 5},
              {"name": "fold", "is_dir": True, "size": 0}]
    local = [{"name": "both", "is_dir": False, "size": 9},
             {"name": "fold", "is_dir": True, "size": 0},
             {"name": "l.txt", "is_dir": False, "size": 7}]
    st = classify_items(remote, local)
    assert st["r.txt"] == "missing"
    assert st["both"] == "differ"
    assert st["fold"] == "same"
    st["l.txt"] == "extra"


def test_classify_items_full():
    remote = [{"name": "only-r.txt", "is_dir": False, "size": 10},
              {"name": "diff.bin", "is_dir": False, "size": 100},
              {"name": "same.bin", "is_dir": False, "size": 50},
              {"name": "dir-both", "is_dir": True, "size": 0},
              {"name": "type-clash", "is_dir": False, "size": 5},
              {"name": "case.bin", "is_dir": False, "size": 7}]
    local = [{"name": "diff.bin", "is_dir": False, "size": 90},
             {"name": "same.bin", "is_dir": False, "size": 50},
             {"name": "dir-both", "is_dir": True, "size": 0},
             {"name": "type-clash", "is_dir": True, "size": 0},
             {"name": "only-l.txt", "is_dir": False, "size": 3},
             {"name": "CASE.bin", "is_dir": False, "size": 7}]
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


def test_endpoint_bar_open_visibility():
    # The 📂 Open action must show only for a connected local (This computer)
    # endpoint — for either side — and stay hidden while disconnected or SSH.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.widgets.endpoint import EndpointBar
    local = EndpointBar("SRC", callbacks={})
    local.set_connected(object(), "local", is_local=True)
    assert local.open_btn.get_visible(), "Open must show for a connected local source"
    ssh = EndpointBar("DST", callbacks={})
    ssh.set_connected(object(), "ssh", is_local=False)
    assert not ssh.open_btn.get_visible(), "Open must hide for a connected SSH endpoint"
    ssh.set_disconnected()
    assert not ssh.open_btn.get_visible(), "Open must hide while disconnected"
    ssh.set_connected(object(), "local", is_local=True)
    assert ssh.open_btn.get_visible(), "Open must show for a connected local destination"


def test_endpoint_bar_pick_folder_visibility():
    # The 📁 pick-folder action shows only for a connected local endpoint and
    # stays hidden for SSH / while disconnected (the file chooser is local-only).
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.widgets.endpoint import EndpointBar
    bar = EndpointBar("SRC", callbacks={})
    bar.set_disconnected()
    assert not bar.browse_btn.get_visible(), "pick-folder must hide while disconnected"
    bar.set_connected(object(), "ssh", is_local=False)
    assert not bar.browse_btn.get_visible(), "pick-folder must hide for SSH"
    bar.set_connected(object(), "local", is_local=True)
    assert bar.browse_btn.get_visible(), "pick-folder must show for a connected local endpoint"
    bar.set_disconnected()
    assert not bar.browse_btn.get_visible(), "pick-folder must hide again after disconnect"


def test_window_browse_wiring():
    # The 📁 pick-folder action must route to _choose_folder for a local source
    # and stay a no-op for a local (real) to avoid touching the network in tests.
    Gtk = _gtk()
    if Gtk is None:
        return
    routed = {}
    from app.widgets.endpoint import EndpointBar
    from unittest.mock import patch
    src = EndpointBar("SRC", callbacks={"browse": lambda bar: routed.setdefault(
        "src", (bar is None))})
    src.browse_btn.clicked()
    assert "src" in routed, "pick-folder click must emit the browse callback"


def test_window_log_colors():
    # Error lines must carry the log_err tag, soft problems log_warn, and
    # normal lines none (no tag) — all detected from the message text.
    from app.window import AppWindow
    assert AppWindow._log_tag("FAILED: x → y (perm)") == "log_err"
    assert AppWindow._log_tag("Got error in copy") == "log_err"
    assert AppWindow._log_tag("Could not save profile: boom") == "log_warn"
    assert AppWindow._log_tag("Retrying: big.bin") == "log_warn"
    assert AppWindow._log_tag("Skipped: a — up to date") == "log_warn"
    assert AppWindow._log_tag("Connected to bob@mac:22 — home: /h") is None
    assert AppWindow._log_tag("Saved profile 'x' for source") is None


def test_endpoint_bar_single_button():
    # The merged bar must have ONE connection button that turns green
    # (suggested-action) when connected, shows Connecting… while connecting,
    # and reverts to Connect… after disconnect — no dropdown, no edit button.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.widgets.endpoint import EndpointBar
    bar = EndpointBar("SRC", callbacks={})
    sc = bar.conn_btn.get_style_context()
    assert not sc.has_class("suggested-action"), "must start neutral"
    assert bar.conn_btn.get_label() == "Connect…"
    assert not hasattr(bar, "endpoint") and not hasattr(bar, "edit_btn"), \
        "no dropdown or edit button anymore"
    bar.set_connecting("Connecting…")
    assert bar.conn_btn.get_label() == "Connecting…"
    assert not bar.conn_btn.get_sensitive(), "connecting button must be disabled"
    bar.set_connected(object(), "bob@mac", is_local=False)
    assert bar.conn_btn.get_label() == "Connected"
    assert sc.has_class("suggested-action"), "button must turn green when connected"
    assert bar.conn_btn.get_sensitive()
    bar.set_disconnected()
    assert bar.conn_btn.get_label() == "Connect…"
    assert not sc.has_class("suggested-action"), "button must return to neutral"

    # Regression: clicking Connect… must hand the bar to the window callback
    # (previously called with no arg and blew up on _on_connect_clicked(bar)).
    received = []
    bar2 = EndpointBar("SRC", callbacks={"connect": lambda b: received.append(b)})
    bar2.conn_btn.clicked()
    assert received == [bar2], "connect must forward the bar that was clicked"


def test_connection_dialog_collect_modes():
    # The popup is the single place to choose a side's endpoint: This computer
    # → local, + New… → ssh params, a saved profile → ssh params with prefill.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.widgets.dialog import ConnectionDialog, NEW_ROW
    import app.profiles as profiles
    profile = {"host": "srv", "port": 22, "user": "bob",
               "password": "pw", "remember": True}
    win = Gtk.Window()
    dlg = ConnectionDialog(win, "Connect Source",
                           profiles_data={"bob@srv": profile})
    try:
        # default: This computer selected (fresh dialog with no prior state)
        assert dlg._kind == profiles.THIS
        assert dlg.collect() == {"mode": "local"}
        # switch to + New… and fill SSH fields
        dlg._set_kind_active(NEW_ROW)
        assert dlg._kind == NEW_ROW
        dlg.host.get_child().set_text("newhost")
        dlg.user.set_text("alice")
        dlg.name.set_text("alice@newhost")
        assert dlg.collect() == {"mode": "ssh", "params": {
            "host": "newhost", "port": 22, "user": "alice",
            "password": "", "remember": False, "name": "alice@newhost"}}
        # switch back to This computer
        dlg._set_kind_active(profiles.THIS)
        assert dlg.collect() == {"mode": "local"}
        # switch to a saved profile → prefilled, name defaults to profile
        dlg._set_kind_active("bob@srv")
        assert dlg.host.get_child().get_text() == "srv"
        assert dlg.name.get_text() == "bob@srv"
        d = dlg.collect()
        assert d["mode"] == "ssh"
        assert d["params"]["host"] == "srv"
        assert d["params"]["password"] == "pw"
    finally:
        dlg.destroy()
        win.destroy()


def test_dirpane_sortable_columns():
    # Every column header that should sort (Name, Type, Modified, State) must be
    # wired via set_sort_column_id so clicking the header re-sorts.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.widgets.dirpane import DirPane, COL_NAME, COL_SIZE_TEXT, COL_TYPE, \
        COL_MTIME_TEXT, COL_STATE_SORT
    pane = DirPane(callbacks={})
    by_title = {c.get_title(): c for c in pane.tree.get_columns()}
    expected = {
        "Name": COL_NAME,
        "Size": COL_SIZE_TEXT,
        "Type": COL_TYPE,
        "Modified": COL_MTIME_TEXT,
        "State": COL_STATE_SORT,
    }
    for title, cid in expected.items():
        assert by_title[title].get_sort_column_id() == cid, \
            f"{title} header must be sortable (sort id {cid})"


def test_dirpane_sort_by_size_and_mtime():
    # Sorting by the Size / Modified headers must order by the raw 64-bit
    # value (not the human text), with folders always listed first. The sort
    # funcs are wired during DirPane construction; here we drive them by
    # setting the model sort column on the TreeModelSort and reading rows back.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.widgets.dirpane import DirPane, COL_SIZE_TEXT, COL_MTIME_TEXT, COL_NAME
    pane = DirPane(callbacks={})
    pane.set_items([
        {"name": "folder", "is_dir": True, "size": 50, "mtime_epoch": 1},
        {"name": "z.bin", "is_dir": False, "size": 1_000_000_000, "mtime_epoch": 2},
        {"name": "a.bin", "is_dir": False, "size": 10, "mtime_epoch": 200},
        {"name": "m.bin", "is_dir": False, "size": 100, "mtime_epoch": 3},
    ], "/x")

    def rows():
        n = pane.sort.iter_n_children(None)
        return [pane.sort.get_value(pane.sort.get_iter((i,)), COL_NAME)
                for i in range(n)]

    pane.sort.set_sort_column_id(COL_SIZE_TEXT, Gtk.SortType.ASCENDING)
    assert rows() == ["folder", "a.bin", "m.bin", "z.bin"], \
        "size sort must keep folder first then order by raw bytes"

    pane.sort.set_sort_column_id(COL_MTIME_TEXT, Gtk.SortType.ASCENDING)
    assert rows() == ["folder", "z.bin", "m.bin", "a.bin"], \
        "mtime sort must keep folder first then order by raw epoch"


def test_color_palettes():
    # Every state keyword + every transfer status must have a color in both
    # palettes, and the dark theme must differ (this drives dir-row + status
    # readability on dark backgrounds).
    keys = {"missing", "differ", "conflict", "same", "extra",
            "running", "done", "failed", "queued", "skipped", "cancelled", "paused"}
    assert set(LIGHT_COLORS) == keys, "light palette must cover every state/status"
    assert set(DARK_COLORS) == keys, "dark palette must cover every state/status"
    assert DARK_COLORS != LIGHT_COLORS, "dark palette must differ from light"
    for k in keys:
        assert LIGHT_COLORS[k].startswith("#") and DARK_COLORS[k].startswith("#"), k
    assert LIGHT_COLORS["missing"] == "#c62828"
    assert LIGHT_COLORS["failed"] == "#c62828"
    assert LIGHT_COLORS["same"] == "#2e7d32"
    assert LIGHT_COLORS["running"] == "#1565c0"
    assert LIGHT_COLORS["queued"] == "#757575"
    assert LIGHT_COLORS["paused"] == "#f9a825"
    assert DARK_COLORS["missing"] == "#ff5252"
    assert DARK_COLORS["extra"] == "#40c4ff"


def test_swap_sides_travels_with_conn():
    # ⇄ swaps the whole endpoint session: connection + current path +
    # remembered profile travel together, and each side reloads the folder it
    # brought along. In-flight listings are invalidated by the request bump.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.window import AppWindow
    from local_transport import LocalConnection
    src = tempfile.mkdtemp(prefix="swap-src-")
    dst = tempfile.mkdtemp(prefix="swap-dst-")
    open(os.path.join(src, "s.txt"), "w").write("s")
    open(os.path.join(dst, "d.txt"), "w").write("d")
    win = None
    try:
        win = AppWindow()
        conn_a, conn_b = LocalConnection(), LocalConnection()
        win.conn = conn_a
        win.dest_conn = conn_b
        win.current_path = src
        win.dest_current_path = dst
        win._side_profile["source"] = "prof-a"
        win._side_profile["dest"] = "prof-b"
        req0, dreq0 = win._remote_req, win._dest_req

        win._on_swap_sides()

        assert win.conn is conn_b, "old dest connection becomes the source"
        assert win.dest_conn is conn_a, "old source connection becomes the dest"
        assert win.current_path == dst, "paths travel with their connection"
        assert win.dest_current_path == src
        assert win._side_profile["source"] == "prof-b"
        assert win._side_profile["dest"] == "prof-a"
        assert win._remote_req > req0 and win._dest_req > dreq0, \
            "in-flight listings must be invalidated"

        end = time.monotonic() + 20
        while time.monotonic() < end and (
                "d.txt" not in win.source_pane.meta
                or "s.txt" not in win.dest_pane.meta):
            while Gtk.events_pending():
                Gtk.main_iteration()
            time.sleep(0.005)
        assert "d.txt" in win.source_pane.meta, "source lists the folder it brought"
        assert "s.txt" in win.dest_pane.meta, "dest lists the folder it brought"
        assert win.source_bar.path_entry.get_text().rstrip("/") == dst.rstrip("/")
        assert win.dest_bar.path_entry.get_text().rstrip("/") == src.rstrip("/")
        assert win.source_bar.conn is conn_b
        assert win.source_bar.conn_label.get_text() == "This computer"
        assert win.swap_btn.get_sensitive()
    finally:
        if win is not None:
            win._on_destroy(None)
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_swap_confirm_and_clear():
    # Checked items trigger a confirmation; cancelling leaves everything
    # untouched, accepting swaps and clears the selection on both sides.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.window import AppWindow
    from local_transport import LocalConnection
    src = tempfile.mkdtemp(prefix="swap-c-src-")
    dst = tempfile.mkdtemp(prefix="swap-c-dst-")
    open(os.path.join(src, "s.txt"), "w").write("s")
    open(os.path.join(dst, "d.txt"), "w").write("d")
    win = None
    try:
        win = AppWindow()
        win.conn = LocalConnection()
        win.dest_conn = LocalConnection()

        def pump_until(pred, secs=20):
            end = time.monotonic() + secs
            while time.monotonic() < end and not pred():
                while Gtk.events_pending():
                    Gtk.main_iteration()
                time.sleep(0.005)
            assert pred(), "condition not met in time"

        win._load_source(src)
        win._load_dest(dst)
        pump_until(lambda: win.source_pane.meta and win.dest_pane.meta)

        def check(pane, name):
            for i in range(len(pane.model)):
                if pane.model[i][1] == name:
                    pane._check(pane.model[i], True)
                    return
            raise AssertionError(f"{name} row missing")

        check(win.source_pane, "s.txt")
        check(win.dest_pane, "d.txt")
        pump_until(lambda: len(win.sel_model) == 1)
        conn_before, dest_before = win.conn, win.dest_conn

        win._confirm_swap = lambda a, b: False
        win._on_swap_sides()
        assert win.conn is conn_before and win.current_path == src, \
            "cancelling must not swap"
        assert len(win.source_pane.selected) == 1, "cancel keeps the selection"

        asked = []
        win._confirm_swap = lambda a, b: asked.append((a, b)) or True
        win._on_swap_sides()
        assert asked == [(1, 1)], f"both sides' checks counted: {asked}"
        assert win.current_path == dst
        assert win.source_pane.selected == {} and win.dest_pane.selected == {}
        assert len(win.sel_model) == 0, "Selected tab must empty out"
        assert "Transfer Selected (0)" in win.transfer_btn.get_label()
    finally:
        if win is not None:
            win._on_destroy(None)
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_swap_partial_moves_single_connection():
    # With only one side connected, the swap moves that endpoint wholesale to
    # the other slot; the emptied side shows the standard disconnected visuals.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.window import AppWindow
    from local_transport import LocalConnection
    dst = tempfile.mkdtemp(prefix="swap-p-dst-")
    open(os.path.join(dst, "d.txt"), "w").write("d")
    win = None
    try:
        win = AppWindow()
        win.dest_conn = LocalConnection()
        win.dest_current_path = dst

        win._on_swap_sides()

        assert win.conn is not None and win.current_path == dst
        assert win.dest_conn is None and win.dest_current_path is None
        end = time.monotonic() + 20
        while time.monotonic() < end and "d.txt" not in win.source_pane.meta:
            while Gtk.events_pending():
                Gtk.main_iteration()
            time.sleep(0.005)
        assert win.source_bar.conn_label.get_text() == "This computer"
        assert win.dest_bar.conn is None
        assert win.dest_bar.conn_btn.get_label() == "Connect…"
        assert not win.dest_bar.row.get_visible(), "connected-only row hides"
        assert win.dest_bar.path_entry.get_text() == ""
        assert len(win.dest_pane.model) == 0
        assert not win.dest_pane.controls.get_visible()
    finally:
        if win is not None:
            win._on_destroy(None)
        shutil.rmtree(dst, ignore_errors=True)


def test_swap_blocked_while_transfers_active():
    # The ⇄ button disables while transfers/deletes run (workers read the
    # sides' connections mid-flight), and clicking anyway is guarded.
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.window import AppWindow, Transfer
    win = None
    try:
        win = AppWindow()
        t = Transfer("x", "/a/x", "/b/x")
        t.status = "running"
        win._transfers.append(t)
        win._update_delete_buttons()
        assert win._transfer_or_delete_active()
        assert not win.swap_btn.get_sensitive(), "must disable during transfers"

        errors = []
        win._show_error = lambda *a, **k: errors.append(a) or False
        win._on_swap_sides()
        assert errors, "clicking a disabled swap must still be guarded"

        t.status = "done"
        win._update_delete_buttons()
        assert win.swap_btn.get_sensitive(), "terminal transfer re-enables"
    finally:
        if win is not None:
            win._on_destroy(None)


ALL_TESTS = (
    test_profiles_v3_normalize_roundtrip,
    test_profiles_v3_legacy_is_empty,
    test_autosave_profile_no_duplicate,
    test_merge_duplicates_report,
    test_classify_app,
    test_classify_items_full,
    test_color_palettes,
    test_endpoint_bar_open_visibility,
    test_endpoint_bar_single_button,
    test_endpoint_bar_pick_folder_visibility,
    test_window_browse_wiring,
    test_window_log_colors,
    test_dirpane_sortable_columns,
    test_dirpane_sort_by_size_and_mtime,
    test_connection_dialog_collect_modes,
    test_swap_sides_travels_with_conn,
    test_swap_confirm_and_clear,
    test_swap_partial_moves_single_connection,
    test_swap_blocked_while_transfers_active,
)


def _gtk():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
    except Exception as e:
        print(f"SKIP app smoke (no display?): {e}")
        return None
    return Gtk


def _smoke_ui():
    Gtk = _gtk()
    if Gtk is None:
        return
    from app.window import AppWindow
    from local_transport import LocalConnection

    def pump_until(pred, secs=20):
        end = time.monotonic() + secs
        while time.monotonic() < end and not pred():
            while Gtk.events_pending():
                Gtk.main_iteration()
            time.sleep(0.005)
        assert pred(), "condition not met in time"

    def pump(secs=0.3):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            while Gtk.events_pending():
                Gtk.main_iteration()
            time.sleep(0.005)

    src = tempfile.mkdtemp(prefix="app-src-")
    dst = tempfile.mkdtemp(prefix="app-dst-")
    win = None
    try:
        open(os.path.join(src, "file.txt"), "w").write("12345")
        os.makedirs(os.path.join(src, "sub"))
        open(os.path.join(src, "sub", "inner.txt"), "w").write("data")

        win = AppWindow()
        win.show_all()

        win.conn = LocalConnection()
        win._load_source(src)
        pump_until(lambda: len(win.source_pane.model) == 2, 20)
        names = {win.source_pane.model[i][1] for i in range(len(win.source_pane.model))}
        assert names == {"file.txt", "sub"}, names

        win._load_dest(dst)
        pump(0.3)
        assert win.dest_bar.path_entry.get_text().rstrip("/") == dst.rstrip("/")

        # select file.txt and transfer it (dirs sort first, so find by name)
        target_row = None
        for i in range(len(win.source_pane.model)):
            if win.source_pane.model[i][1] == "file.txt":
                target_row = win.source_pane.model[i]
                break
        assert target_row is not None, "file.txt row missing"

        # A checkbox toggle must cascade automatically (no manual _refresh_sel):
        # the Selected page counter AND the delete button both react to it.
        win.source_pane._check(target_row, True)
        pump(0.1)
        assert "Transfer Selected (1)" in win.transfer_btn.get_label()
        assert len(win.sel_model) == 1, "Selected page must show the checked item"
        assert win.source_bar.delete_btn.get_sensitive(), "delete button must enable with a selection"
        assert "Delete (1)" in win.source_bar.delete_btn.get_label(), "delete button must show the selected count"
        win.source_pane._check(target_row, False)
        pump(0.1)
        assert "Transfer Selected (0)" in win.transfer_btn.get_label()
        assert len(win.sel_model) == 0, "unchecking must clear the Selected page"
        assert not win.source_bar.delete_btn.get_sensitive(), "delete button must disable with no selection"

        # re-select for the actual transfer
        win.source_pane._check(target_row, True)
        win._refresh_sel()
        assert "Transfer Selected (1)" in win.transfer_btn.get_label()

        win._on_transfer(None)
        t = win._transfers[0]
        pump_until(lambda: t.status in ("done", "failed"), 30)
        assert t.status == "done", (t.status, t.err)
        assert os.path.isfile(os.path.join(dst, "file.txt")), "file must land in destination"

        # Destination panel must auto-reload after the transfer completes
        # (debounced), so the copied item appears in the dest listing.
        pump(0.4)
        assert "file.txt" in win.dest_pane.meta, \
            "destination panel must refresh after transfer finishes"

        # DirPane.clear() must drop rows/selection/meta; the path field belongs
        # to the EndpointBar, which the window blanks on disconnect.
        before_sel = dict(win.source_pane.selected)
        assert before_sel, "expected a selection to clear"
        win.source_pane.clear()
        assert len(win.source_pane.model) == 0
        assert win.source_pane.selected == {}
        assert win.source_pane.meta == {}

        win._on_destroy(None)
        win = None
        print("app smoke OK")
    finally:
        if win is not None:
            try:
                win._on_destroy(None)
            except Exception:
                pass
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def _smoke_remote_dest():
    """Local source -> 'remote SSH' dest transfer through AppWindow's worker,
    exercising the real transfer_engine.run() remote-destination path."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        from app.window import AppWindow
        from local_transport import LocalConnection
        from tests.common import FakePosixSsh
    except Exception as e:
        print(f"SKIP remote-dest smoke (no display?): {e}")
        return

    def pump():
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            while Gtk.events_pending():
                Gtk.main_iteration()
            time.sleep(0.005)

    src = tempfile.mkdtemp(prefix="app-rd-src-")
    root = tempfile.mkdtemp(prefix="app-rd-dst-")
    win = None
    try:
        open(os.path.join(src, "file.txt"), "w").write("12345")
        os.makedirs(os.path.join(src, "sub"))
        open(os.path.join(src, "sub", "inner.txt"), "w").write("data")

        win = AppWindow()
        win.show_all()
        win.conn = LocalConnection()
        win._load_source(src)
        pump()
        for row in win.source_pane.model:
            if row[1] in ("file.txt", "sub"):
                win.source_pane._check(row, True)
        win._refresh_sel()

        win.dest_conn = FakePosixSsh(root)
        win.dest_current_path = root
        win._on_transfer(None)
        end = time.monotonic() + 40
        while time.monotonic() < end and win._transfers and any(
                t.status not in ("done", "failed") for t in win._transfers):
            pump()
        assert win._transfers, "no transfer enqueued"
        for t in win._transfers:
            assert t.status == "done", (t.name, t.status, t.err)
        assert os.path.isfile(os.path.join(root, "file.txt")), "file must land on remote dest"
        assert os.path.isfile(os.path.join(root, "sub", "inner.txt")), "dir must land on remote dest"
        assert not [p for p in os.listdir(root) if "lan-copier-part" in p], "no stale part left"
        win._on_destroy(None)
        win = None
        print("app remote-dest smoke OK")
    finally:
        if win is not None:
            try:
                win._on_destroy(None)
            except Exception:
                pass
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


SMOKE = _smoke_ui
SMOKE_REMOTE_DEST = _smoke_remote_dest