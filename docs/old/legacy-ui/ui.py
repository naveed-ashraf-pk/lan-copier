# TODO(cleanup): LEGACY FILE. Superseded by app/window.py (AppWindow) + the
# app/ package (connections.py, panels.py, profiles.py). main.py now imports
# app.window.AppWindow. Keep this file only as reference until the new window
# is confirmed, then delete ui.py, panes.py and this legacy root profiles.py.
# Do NOT add new logic here.

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango, Gdk, GObject

import os
import re
import shlex
import shutil
import sys
import threading
import time

import profiles
import commands.paths as rp
from discovery import discover
from local_transport import LocalConnection, dir_list, dir_tree, delete_local_item
from ssh_transport import (
    SSHConnection, POLICY_ASK, POLICY_OVERWRITE, POLICY_KEEP_BOTH, POLICY_SKIP,
)
import tree_exporter

POLICY_CHOICES = [
    ("Ask (smart)", POLICY_ASK),
    ("Overwrite", POLICY_OVERWRITE),
    ("Keep both", POLICY_KEEP_BOTH),
    ("Skip", POLICY_SKIP),
]

LOCAL_PROFILE = "This computer"

# Model column indexes for the source (self.model) and destination
# (self.dest_model) listings; named so the sort/load code never repeats
# bare numbers. The last two columns of each model carry raw sort keys
# (exact byte size and epoch mtime) that are never displayed.
SRC_CHECK, SRC_NAME, SRC_SIZE_TEXT, SRC_TYPE, SRC_MTIME_TEXT, \
    SRC_IS_DIR, SRC_PATH, SRC_SIZE, SRC_MTIME = range(9)
DST_CHECK, DST_NAME, DST_SIZE_TEXT, DST_TYPE, DST_MTIME_TEXT, DST_PATH, \
    DST_IS_DIR, DST_TOOLTIP, DST_STATE, DST_SIZE, DST_MTIME = range(11)


def friendly_error_of(conn):
    """Best-effort friendly message for any transport object. Connections
    (SSHConnection, LocalConnection, test fakes) expose friendly_error()
    and last_error; fall back to the SSH renderer so a missing method can
    never crash an error dialog."""
    err = getattr(conn, "last_error", None) or "Unknown error."
    fn = getattr(conn, "friendly_error", SSHConnection.friendly_error)
    return fn(err)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(sec):
    sec = int(sec)
    if sec < 0:
        return "—"
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def compare_trees(remote, local):
    """Split a remote file tree against a local one into missing paths and
    size-differing paths, plus the top-level names covering them."""
    missing = {rel for rel in remote if rel not in local}
    diff = {rel for rel in remote if rel in local and remote[rel] != local[rel]}
    top = {rel.split("/", 1)[0] for rel in missing | diff}
    return missing, diff, top


STATE_PRIO = {"missing": 0, "differ": 1, "conflict": 2, "same": 3, "extra": 4}

LIGHT_COLORS = {
    "missing": "#c62828", "differ": "#ef6c00", "conflict": "#ad1457",
    "same": "#2e7d32", "extra": "#1565c0",
    "running": "#1565c0", "done": "#2e7d32", "failed": "#c62828",
    "queued": "#757575", "skipped": "#757575", "cancelled": "#757575",
    "paused": "#f9a825",
}

DARK_COLORS = {
    "missing": "#ff5252", "differ": "#ffab40", "conflict": "#ff4081",
    "same": "#81c784", "extra": "#40c4ff",
    "running": "#40c4ff", "done": "#81c784", "failed": "#ff5252",
    "queued": "#9e9e9e", "skipped": "#9e9e9e", "cancelled": "#9e9e9e",
    "paused": "#ffd54f",
}

TRANSFER_COLOR_KEYS = ("running", "done", "failed", "queued", "skipped",
                       "cancelled", "paused")


def _is_dark_theme():
    """Best-effort detection of a dark GTK theme; safe when no display or
    settings are available."""
    try:
        s = Gtk.Settings.get_default()
        if s is None:
            return False
        if s.get_property("gtk-application-prefer-dark-theme"):
            return True
        return "dark" in s.get_property("gtk-theme-name").lower()
    except Exception:
        return False


def classify_items(remote_items, local_items):
    """Compare the direct children of two directories by name.

    remote_items / local_items: lists of {"name", "is_dir", "size"} dicts.
    Returns {name: state} where state is one of:
      missing  - only on the remote side (will be copied)
      differ   - both sides, different size (will overwrite)
      conflict - a file on one side, a folder on the other
      same     - both sides, same size (or both folders)
      extra    - only on the local/destination side (informational)
    Name matching is exact (case-sensitive)."""
    remote = {it["name"]: it for it in remote_items}
    local = {it["name"]: it for it in local_items}
    states = {}
    for name in set(remote) | set(local):
        r, l = remote.get(name), local.get(name)
        if r is None:
            states[name] = "extra"
        elif l is None:
            states[name] = "missing"
        elif r["is_dir"] != l["is_dir"]:
            states[name] = "conflict"
        elif r["is_dir"]:
            states[name] = "same"
        elif r["size"] == l["size"]:
            states[name] = "same"
        else:
            states[name] = "differ"
    return states


class Transfer:
    def __init__(self, name, src, dest, batch, conn, is_dir=False):
        self.id = None
        self.name = name
        self.src = src
        self.dest = dest
        self.batch = batch
        self.conn = conn
        self.is_dir = is_dir
        self.method = None
        self.policy = POLICY_ASK
        self.status = "queued"
        self.part = None
        self.total = None
        self.files = None
        self.files_done = None
        self.current = 0
        self.last = 0
        self.last_t = None
        self.speed = 0.0
        self.eta = None
        self.final = None
        self.err = ""
        self.procs = []
        self.paused = False
        self.removed = False


class CopierWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="LAN SSH File Copier")
        self.set_default_size(1120, 740)
        self.connect("destroy", self._on_destroy)
        self.connect("delete-event", self._on_delete_event)
        self.conn = None
        self.dest_conn = None
        self.dest_profile_name = None
        self.current_path = "~"
        self.dest_current_path = None
        self._remote_meta = {}
        self._dest_meta = {}
        self._states = {}
        self.selected = {}
        self.dest_selected = {}
        self._delete_in_progress = False
        self._delete_cancel = threading.Event()
        self.batch = 0
        self.done = 0
        self.failed = 0
        self._transfers = []
        self._ticker_id = None
        self._apply_all_choice = None
        self._ask_lock = threading.Lock()
        self._gate = threading.Condition()
        self._max_parallel = 1
        self._inflight = 0
        self._next_id = 0
        self._trow = {}
        self._by_id = {}
        self._active_profile = None
        self._restore_selection = None
        self._dest_req = 0
        self._remote_req = 0
        self._closed = threading.Event()
        self._destroyed = False
        self._ask_dialog = None
        self._dialogs = []
        self._syncing_sort = False
        self.model_sort = None
        self._colors = DARK_COLORS if _is_dark_theme() else LIGHT_COLORS
        self._glyphs = self._render_glyphs()
        self._build_ui()
        self._load_profiles_ui()
        self._discover_hosts()

    def _render_glyphs(self):
        """Render the action/retry glyphs into pixbufs ourselves so the UI
        never depends on the icon theme or its image loaders (a missing
        theme icon makes GTK fall back to image-missing.png, whose loader
        can hard-abort the app under FD pressure). Falls back to {} when
        cairo is unavailable; the columns then use theme icons instead."""
        try:
            import cairo
            gi.require_version("PangoCairo", "1.0")
            from gi.repository import PangoCairo
        except Exception:
            return {}
        out = {}
        for name, glyph in (("media-playback-pause", "⏸"),
                            ("media-playback-start", "▶"),
                            ("edit-delete", "✕"),
                            ("view-refresh", "↻")):
            try:
                surf = cairo.ImageSurface(cairo.Format.ARGB32, 20, 20)
                ctx = cairo.Context(surf)
                layout = PangoCairo.create_layout(ctx)
                layout.set_text(glyph, -1)
                layout.set_font_description(Pango.FontDescription("sans 13"))
                w, h = layout.get_pixel_size()
                ctx.move_to((20 - w) / 2.0, (20 - h) / 2.0)
                ctx.set_source_rgba(0.1, 0.1, 0.1, 1.0)
                PangoCairo.show_layout(ctx, layout)
                out[name] = Gdk.pixbuf_get_from_surface(surf, 0, 0, 20, 20)
            except Exception:
                return {}
        return out

    def _icon_cb(self, col, cell, model, it, key):
        cell.set_property("pixbuf", self._glyphs.get(model.get_value(it, key)))

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_border_width(8)
        self.add(vbox)
        self._build_connection(vbox)
        self._build_dest_connection(vbox)
        self.main_stack = Gtk.Stack()
        vbox.pack_start(self.main_stack, True, True, 0)
        browser_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        paned = Gtk.Paned()
        paned.set_orientation(Gtk.Orientation.HORIZONTAL)
        browser_box.pack_start(paned, True, True, 0)
        self._build_remote_pane(paned)
        self._build_local_pane(paned)
        paned.set_position(640)
        legend = Gtk.Label(
            label="Compare colors: red = missing in destination · orange = size differs "
                  "· magenta = file/folder conflict · green = same · blue = destination only",
            xalign=0)
        browser_box.pack_start(legend, False, False, 0)
        self.main_stack.add_named(browser_box, "browser")
        self.main_stack.add_named(self._build_delete_progress_page(), "delete_progress")
        self.main_stack.set_visible_child_name("browser")
        self._build_bottom(vbox)
        self.model_sort.connect("sort-column-changed", self._on_source_sort_changed)
        self.dest_model.connect("sort-column-changed", self._on_dest_sort_changed)
        self._log("Ready. Scan hosts or type an address to connect.")

    def _build_delete_progress_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(40)
        title = Gtk.Label(label="🗑 Deleting Items")
        title.get_style_context().add_class("title-3")
        page.pack_start(title, False, False, 0)
        self.delete_header_lbl = Gtk.Label(label="", xalign=0.5)
        page.pack_start(self.delete_header_lbl, False, False, 0)
        self.delete_progress_bar = Gtk.ProgressBar()
        self.delete_progress_bar.set_show_text(True)
        page.pack_start(self.delete_progress_bar, False, False, 0)
        self.delete_current_lbl = Gtk.Label(label="", xalign=0.5)
        self.delete_current_lbl.set_line_wrap(True)
        page.pack_start(self.delete_current_lbl, False, False, 0)
        cancel_box = Gtk.Box(halign=Gtk.Align.CENTER)
        self.delete_cancel_btn = Gtk.Button(label="✕ Cancel Remaining")
        self.delete_cancel_btn.connect("clicked", self._on_delete_cancel_clicked)
        cancel_box.pack_start(self.delete_cancel_btn, False, False, 0)
        page.pack_start(cancel_box, False, False, 0)
        return page

    def _build_connection(self, vbox):
        grid = Gtk.Grid(column_spacing=6, row_spacing=4)
        vbox.pack_start(grid, False, False, 0)

        grid.attach(Gtk.Label(label="Source:"), 0, 0, 1, 1)
        self.source_combo = Gtk.ComboBoxText()
        self.source_combo.append_text("SSH")
        self.source_combo.append_text("This computer")
        self.source_combo.set_active(0)
        self.source_combo.set_tooltip_text(
            "SSH: browse a folder on a remote machine. "
            "This computer: compare/copy two local folders with no server.")
        self.source_combo.connect("changed", self._on_source_changed)
        grid.attach(self.source_combo, 1, 0, 1, 1)

        # The connect/browse button must NOT live inside the SSH auth box:
        # in local mode that box is hidden, and the user still needs the
        # button (labelled "Browse…") to pick a source folder.
        self.connect_btn = Gtk.Button(label="Connect")
        self.connect_btn.connect("clicked", self._on_connect)
        grid.attach(self.connect_btn, 2, 0, 1, 1)

        # SSH auth widgets are wrapped in a box so local mode can hide them
        # all at once.
        auth = Gtk.Box(spacing=6)
        auth.pack_start(Gtk.Label(label="Host:"), False, False, 0)
        self.host_combo = Gtk.ComboBoxText.new_with_entry()
        self.host_combo.get_child().set_placeholder_text("host or IP (auto-discovered)")
        self.host_combo.get_child().set_width_chars(18)
        auth.pack_start(self.host_combo, False, False, 0)
        refresh_hosts = Gtk.Button(label="↻")
        refresh_hosts.set_tooltip_text("Rescan LAN for SSH hosts")
        refresh_hosts.connect("clicked", self._discover_hosts)
        auth.pack_start(refresh_hosts, False, False, 0)
        auth.pack_start(Gtk.Label(label="Port:"), False, False, 0)
        self.port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port_spin.set_value(22)
        auth.pack_start(self.port_spin, False, False, 0)
        auth.pack_start(Gtk.Label(label="User:"), False, False, 0)
        self.user_entry = Gtk.Entry()
        self.user_entry.set_width_chars(12)
        auth.pack_start(self.user_entry, False, False, 0)
        auth.pack_start(Gtk.Label(label="Password:"), False, False, 0)
        self.pass_entry = Gtk.Entry()
        self.pass_entry.set_visibility(False)
        self.pass_entry.set_width_chars(12)
        auth.pack_start(self.pass_entry, False, False, 0)
        self.remember = Gtk.CheckButton(label="Remember")
        self.remember.set_tooltip_text("Store password with saved profile (plaintext)")
        auth.pack_start(self.remember, False, False, 0)
        grid.attach(auth, 3, 0, 7, 1)
        self.auth_box = auth

        self.local_hint = Gtk.Label(label="Pick the source folder with Browse…", xalign=0)
        self.local_hint.set_visible(False)
        grid.attach(self.local_hint, 3, 0, 7, 1)

        self.status_label = Gtk.Label(label="Not connected", xalign=0)
        grid.attach(self.status_label, 10, 0, 2, 1)

        prof = Gtk.Box(spacing=6)
        prof.pack_start(Gtk.Label(label="Profile:"), False, False, 0)
        self.profile_combo = Gtk.ComboBoxText()
        self.profile_combo.connect("changed", self._on_profile_changed)
        prof.pack_start(self.profile_combo, False, False, 0)
        save_btn = Gtk.Button(label="Save current")
        save_btn.connect("clicked", self._on_save_profile)
        prof.pack_start(save_btn, False, False, 0)
        del_btn = Gtk.Button(label="Delete")
        del_btn.connect("clicked", self._on_del_profile)
        prof.pack_start(del_btn, False, False, 0)
        hint = Gtk.Label(label="Discovered hosts appear in the Host dropdown.", xalign=0)
        prof.pack_start(hint, False, False, 0)
        grid.attach(prof, 0, 1, 12, 1)
        self.profile_box = prof

    def _build_remote_pane(self, paned):
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        nav = Gtk.Box(spacing=4)
        self.path_entry = Gtk.Entry()
        self.path_entry.set_text("~")
        self.path_entry.set_placeholder_text("remote path (Enter to go)")
        self.path_entry.connect("activate", self._on_path_enter)
        self.up_btn = Gtk.Button(label="⬆ Up")
        self.up_btn.connect("clicked", self._on_up)
        self.refresh_btn = Gtk.Button(label="↻ Refresh")
        self.refresh_btn.connect("clicked", lambda b: self._load_remote())
        self.src_export_btn = Gtk.Button(label="⤓ Export")
        self.src_export_btn.set_tooltip_text("Export the current source tree to YAML")
        self.src_export_btn.connect("clicked", self._on_export_source_clicked)
        for w in (self.path_entry, self.up_btn, self.refresh_btn,
                  self.src_export_btn):
            nav.pack_start(w, w is self.path_entry, True, 0)
        left.pack_start(nav, False, False, 0)

        filter_box = Gtk.Box(spacing=4)
        filter_box.pack_start(Gtk.Label(label="Filter:"), False, False, 0)
        self.filter_entry = Gtk.Entry()
        self.filter_entry.set_placeholder_text("name contains…")
        self.filter_entry.connect("changed", self._on_filter_changed)
        filter_box.pack_start(self.filter_entry, True, True, 0)
        left.pack_start(filter_box, False, False, 0)

        actions = Gtk.Box(spacing=4)
        self.toggle_all_btn = Gtk.ToggleButton(label="Select all")
        self.toggle_all_btn.connect("toggled", self._on_toggle_all)
        self.select_missing_btn = Gtk.Button(label="Select missing")
        self.select_missing_btn.set_tooltip_text(
            "Check every visible item with state 'missing' (adds to selection)")
        self.select_missing_btn.connect("clicked", self._on_select_missing)
        self.select_changed_btn = Gtk.Button(label="Select changed")
        self.select_changed_btn.set_tooltip_text(
            "Check every visible item that is missing, size-differing or "
            "conflicting (adds to selection)")
        self.select_changed_btn.connect("clicked", self._on_select_changed)
        self.invert_btn = Gtk.Button(label="Invert")
        self.invert_btn.set_tooltip_text("Flip checked/unchecked for visible items")
        self.invert_btn.connect("clicked", self._on_invert)
        self.folders_btn = Gtk.Button(label="Folders")
        self.folders_btn.set_tooltip_text("Check every visible folder (adds to selection)")
        self.folders_btn.connect("clicked", self._on_select_folders)
        self.files_btn = Gtk.Button(label="Files")
        self.files_btn.set_tooltip_text("Check every visible file (adds to selection)")
        self.files_btn.connect("clicked", self._on_select_files)
        self.compare_btn = Gtk.Button(label="⇄ Select missing (recursive)")
        self.compare_btn.set_tooltip_text(
            "Walk the whole tree and check everything missing or size-differing "
            "at any depth (adds to selection)")
        self.compare_btn.connect("clicked", self._on_compare)
        for w in (self.toggle_all_btn, self.select_missing_btn,
                  self.select_changed_btn, self.invert_btn, self.folders_btn,
                  self.files_btn, self.compare_btn):
            actions.pack_start(w, False, False, 0)
        left.pack_start(actions, False, False, 0)

        self.model = Gtk.ListStore(
            bool, str, str, str, str, bool, str,
            GObject.TYPE_INT64, GObject.TYPE_INT64)
        self.model_filter = self.model.filter_new()
        self._filter_text = ""
        self.model_filter.set_visible_func(self._filter_visible)
        self.model_sort = Gtk.TreeModelSort(model=self.model_filter)
        self.tree = Gtk.TreeView(model=self.model_sort)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_row_toggled)
        self.tree.append_column(Gtk.TreeViewColumn("", toggle, active=0))
        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_col = Gtk.TreeViewColumn("Name", name_renderer)
        name_col.set_cell_data_func(name_renderer, self._name_cb)
        name_col.set_expand(True)
        name_col.set_sort_column_id(1)
        self.tree.append_column(name_col)
        size_col = Gtk.TreeViewColumn("Size", Gtk.CellRendererText(), text=SRC_SIZE_TEXT)
        size_col.set_sort_column_id(2)
        self.tree.append_column(size_col)
        type_col = Gtk.TreeViewColumn("Type", Gtk.CellRendererText(), text=SRC_TYPE)
        type_col.set_sort_column_id(3)
        self.tree.append_column(type_col)
        mtime_col = Gtk.TreeViewColumn("Modified", Gtk.CellRendererText(), text=SRC_MTIME_TEXT)
        mtime_col.set_sort_column_id(4)
        self.tree.append_column(mtime_col)
        st_renderer = Gtk.CellRendererText()
        st_col = Gtk.TreeViewColumn("State", st_renderer)
        st_col.set_cell_data_func(st_renderer, self._state_cb)
        st_col.set_sort_column_id(0)
        self.model_sort.set_sort_func(0, self._sort_state_remote)
        for cid in (1, 2, 3, 4):
            # GTK passes user_data as a 4th positional arg, so bind the
            # column id via a default rather than the c parameter.
            self.model_sort.set_sort_func(
                cid, lambda m, a, b, _d=None, c=cid: self._sort_remote(m, a, b, c))
        self.tree.append_column(st_col)
        self.tree.set_tooltip_column(6)
        self.tree.connect("row-activated", self._on_row_activated)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tree)
        left.pack_start(sw, True, True, 0)
        self.remote_summary_lbl = Gtk.Label(label="", xalign=0)
        left.pack_start(self.remote_summary_lbl, False, False, 0)
        self.src_delete_btn = Gtk.Button(label="🗑 Delete Checked (0)")
        self.src_delete_btn.get_style_context().add_class("destructive-action")
        self.src_delete_btn.set_sensitive(False)
        self.src_delete_btn.connect("clicked", self._on_delete_source_clicked)
        left.pack_start(self.src_delete_btn, False, False, 0)
        paned.add1(left)

    def _build_local_pane(self, paned):
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        dest_box = Gtk.Box(spacing=4)
        self.dest_entry = Gtk.Entry()
        default_dest = os.path.expanduser("~/Downloads")
        if not os.path.isdir(default_dest):
            default_dest = os.path.expanduser("~")
        self.dest_entry.set_text(default_dest)
        dest_box.pack_start(self.dest_entry, True, True, 0)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._on_browse_dest)
        home = Gtk.Button(label="⌂")
        home.set_tooltip_text("Home folder")
        home.connect("clicked", self._on_dest_home)
        up = Gtk.Button(label="⬆")
        up.set_tooltip_text("Up one level")
        up.connect("clicked", self._on_dest_up)
        self.dest_refresh_btn = Gtk.Button(label="↻")
        self.dest_refresh_btn.set_tooltip_text("Reload destination folder")
        self.dest_refresh_btn.connect("clicked", self._load_dest)
        self.dest_export_btn = Gtk.Button(label="⤓ Export")
        self.dest_export_btn.set_tooltip_text("Export the current destination tree to YAML")
        self.dest_export_btn.connect("clicked", self._on_export_dest_clicked)
        dest_box.pack_start(browse, False, False, 0)
        dest_box.pack_start(up, False, False, 0)
        dest_box.pack_start(self.dest_refresh_btn, False, False, 0)
        dest_box.pack_start(home, False, False, 0)
        dest_box.pack_start(self.dest_export_btn, False, False, 0)
        right.pack_start(dest_box, False, False, 0)

        self.notebook = Gtk.Notebook()
        right.pack_start(self.notebook, True, True, 0)

        dest_page = self._build_dest_page()
        self.dest_tab_lbl = Gtk.Label(label="Destination")
        self.notebook.append_page(dest_page, self.dest_tab_lbl)

        sel_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sel_page.pack_start(Gtk.Label(
            label="Destination applies to newly selected items", xalign=0), False, False, 0)

        self.sel_model = Gtk.ListStore(str, str, str, str, bool)
        self.sel_tree = Gtk.TreeView(model=self.sel_model)
        item_renderer = Gtk.CellRendererText()
        item_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.sel_tree.append_column(Gtk.TreeViewColumn("Item", item_renderer, text=0))
        src_renderer = Gtk.CellRendererText()
        src_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.sel_tree.append_column(Gtk.TreeViewColumn("Source", src_renderer, text=1))
        dest_renderer = Gtk.CellRendererText()
        dest_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        dest_col = Gtk.TreeViewColumn("Will copy to", dest_renderer, text=2)
        dest_col.add_attribute(dest_renderer, "foreground", 3)
        dest_col.add_attribute(dest_renderer, "foreground-set", 4)
        self.sel_tree.append_column(dest_col)
        self.sel_tree.set_tooltip_column(2)
        sel_sw = Gtk.ScrolledWindow()
        sel_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sel_sw.add(self.sel_tree)
        sel_page.pack_start(sel_sw, True, True, 0)

        sel_btns = Gtk.Box(spacing=4)
        clear_btn = Gtk.Button(label="Clear all")
        clear_btn.connect("clicked", self._on_clear_sel)
        set_all = Gtk.Button(label="Set all → current")
        set_all.set_tooltip_text("Point every selected item at the destination above")
        set_all.connect("clicked", self._on_set_all_dest)
        open_dest = Gtk.Button(label="Open destination")
        open_dest.connect("clicked", self._on_open_dest)
        sel_btns.pack_start(clear_btn, False, False, 0)
        sel_btns.pack_start(set_all, False, False, 0)
        sel_btns.pack_start(open_dest, False, False, 0)
        sel_page.pack_start(sel_btns, False, False, 0)

        self.transfer_btn = Gtk.Button(label="▶ TRANSFER SELECTED (0)")
        self.transfer_btn.get_style_context().add_class("suggested-action")
        self.transfer_btn.connect("clicked", self._on_transfer)
        sel_page.pack_start(self.transfer_btn, False, False, 0)
        self.sel_tab_lbl = Gtk.Label(label="Selected (0)")
        self.notebook.append_page(sel_page, self.sel_tab_lbl)

        tpage = self._build_transfers_page()
        self.transfers_tab_lbl = Gtk.Label(label="Transfers (0)")
        self.notebook.append_page(tpage, self.transfers_tab_lbl)
        self.transfers_page = tpage
        self.notebook.set_current_page(0)
        paned.add2(right)

        self.dest_entry.connect("changed", lambda e: self._refresh_sel())
        self.dest_entry.connect("activate", lambda e: self._load_dest())
        self._load_dest()

    # -- destination endpoint (this computer or an SSH host) ----------------

    def _dest_ssh(self):
        return self.dest_conn is not None and getattr(self.dest_conn, "kind", None) == "ssh"

    def _on_dest_connect(self, bar):
        host = bar.host_combo.get_child().get_text().strip()
        user = bar.user_entry.get_text().strip()
        password = bar.pass_entry.get_text()
        if not host or not user or not password:
            self._show_error("Destination SSH: host, username and password are required.")
            return
        bar.set_connecting("Connecting destination…")
        self.status_label.set_text("Connecting destination…")

        def work():
            conn = SSHConnection(host, bar.port_spin.get_value_as_int(), user, password)
            conn.on_command = lambda argv, rc, err: GLib.idle_add(self._cmd_result, argv, rc, err)
            home = conn.home_dir()
            GLib.idle_add(self._dest_connected, conn, home)

        threading.Thread(target=work, daemon=True).start()

    def _dest_connected(self, conn, home):
        if self._destroyed:
            return False
        if not home:
            self._friendly_dialog(conn, Gtk.MessageType.WARNING)
            self.dest_bar.set_disconnected("Connection failed")
            return False
        self.dest_conn = conn
        self.dest_bar.set_connected(conn, f"{conn.user}@{conn.host} — {home}")
        if not self.dest_entry.get_text().strip():
            self.dest_entry.set_text(home)
        self._load_dest()
        return False

    def _disconnect_dest(self):
        if self.dest_conn:
            self.dest_conn.close()
            self.dest_conn = None
        self.dest_bar.set_disconnected()
        self._load_dest()

    def _dest_scan_hosts(self, bar):
        self._scan_dest_hosts = bar

        def work():
            hosts = discover()
            GLib.idle_add(self._dest_hosts_ready, hosts)

        threading.Thread(target=work, daemon=True).start()

    def _dest_hosts_ready(self, hosts):
        if self._destroyed:
            return False
        bar = getattr(self, "_scan_dest_hosts", None)
        self._scan_dest_hosts = None
        if bar is not None:
            bar.set_hosts(hosts)
        return False

    def _on_dest_bar_action(self, bar, action, value):
        if action == "save":
            name = self._prompt("Save destination profile", "Profile name:")
            if name:
                self.profiles[name] = bar.profile_payload()
                profiles.save(self.profiles)
                bar.set_profile_list(list(self.profiles), active=name)
                self._log(f"Saved destination profile '{name}'")
        elif action == "profile" and value:
            p = self.profiles.get(value)
            if p:
                bar.apply_profile(p)

    def _dest_tree(self, path):
        return dir_tree(path) if not self._dest_ssh() else self.dest_conn.tree(path)

    def _dest_tree_ok(self, path):
        if not self._dest_ssh():
            return os.path.isdir(path)
        return bool(self.dest_conn.exists(path) and self.dest_conn.is_dir(path))

    def _build_transfers_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header = Gtk.Box(spacing=6)
        header.pack_start(Gtk.Label(label="If exists:"), False, False, 0)
        self.policy_combo = Gtk.ComboBoxText()
        for label, _ in POLICY_CHOICES:
            self.policy_combo.append_text(label)
        self.policy_combo.set_active(0)
        self.policy_combo.set_tooltip_text(
            "Ask (smart): same size = skip silently, different = ask once per batch")
        header.pack_start(self.policy_combo, False, False, 0)
        header.pack_start(Gtk.Label(label="Parallel:"), False, False, 0)
        self.parallel_spin = Gtk.SpinButton.new_with_range(1, 8, 1)
        self.parallel_spin.set_value(1)
        self.parallel_spin.set_tooltip_text(
            "Max items copied at once (1 = strictly one by one). Applies immediately: "
            "raising starts queued items, lowering throttles only new starts.")
        self.parallel_spin.connect("value-changed", self._on_parallel_changed)
        self._on_parallel_changed(self.parallel_spin)
        header.pack_start(self.parallel_spin, False, False, 0)
        self.summary_lbl = Gtk.Label(label="", xalign=0)
        header.pack_start(self.summary_lbl, False, False, 0)
        self.cancel_all_btn = Gtk.Button(label="✕ Cancel all")
        self.cancel_all_btn.connect("clicked", self._on_cancel_all)
        self.cancel_all_btn.set_visible(False)
        self.clear_finished_btn = Gtk.Button(label="🗑 Clear finished")
        self.clear_finished_btn.connect("clicked", self._on_clear_finished)
        self.clear_finished_btn.set_visible(False)
        header.pack_end(self.clear_finished_btn, False, False, 0)
        header.pack_end(self.cancel_all_btn, False, False, 0)
        page.pack_start(header, False, False, 0)

        self.transfers_model = Gtk.ListStore(int, str, str, int, str, str, str, str, bool, str, str, str)
        self.transfers_tree = Gtk.TreeView(model=self.transfers_model)
        self.transfers_tree.get_selection().set_mode(Gtk.SelectionMode.NONE)
        self.transfers_tree.set_tooltip_column(10)
        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_col = Gtk.TreeViewColumn("Item", name_renderer, text=1)
        name_col.set_expand(True)
        name_col.set_min_width(160)
        self.transfers_tree.append_column(name_col)
        prog_renderer = Gtk.CellRendererProgress()
        prog_col = Gtk.TreeViewColumn("Progress", prog_renderer, value=3, text=2)
        prog_col.set_min_width(200)
        self.transfers_tree.append_column(prog_col)
        speed_renderer = Gtk.CellRendererText()
        speed_renderer.set_property("xalign", 1.0)
        speed_col = Gtk.TreeViewColumn("Speed", speed_renderer, text=4)
        speed_col.set_alignment(1.0)
        speed_col.set_min_width(80)
        self.transfers_tree.append_column(speed_col)
        eta_renderer = Gtk.CellRendererText()
        eta_renderer.set_property("xalign", 1.0)
        eta_col = Gtk.TreeViewColumn("ETA", eta_renderer, text=5)
        eta_col.set_alignment(1.0)
        eta_col.set_min_width(70)
        self.transfers_tree.append_column(eta_col)
        st_renderer = Gtk.CellRendererText()
        st_col = Gtk.TreeViewColumn("Status", st_renderer, text=6, foreground=7)
        st_col.add_attribute(st_renderer, "foreground-set", 8)
        st_col.set_min_width(60)
        self.transfers_tree.append_column(st_col)
        retry_renderer = Gtk.CellRendererPixbuf()
        retry_col = Gtk.TreeViewColumn("↻", retry_renderer)
        retry_col.set_min_width(28)
        if self._glyphs:
            retry_col.set_cell_data_func(retry_renderer, self._icon_cb, 9)
        else:
            retry_col.add_attribute(retry_renderer, "icon-name", 9)
        self.transfers_tree.append_column(retry_col)
        self._retry_col = retry_col
        action_renderer = Gtk.CellRendererPixbuf()
        action_col = Gtk.TreeViewColumn("⏸/✕", action_renderer)
        action_col.set_min_width(34)
        if self._glyphs:
            action_col.set_cell_data_func(action_renderer, self._icon_cb, 11)
        else:
            action_col.add_attribute(action_renderer, "icon-name", 11)
        self.transfers_tree.append_column(action_col)
        self._action_col = action_col
        self.transfers_tree.connect("button-press-event", self._on_transfers_press)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.transfers_tree)
        page.pack_start(sw, True, True, 0)
        return page

    def _build_dest_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        actions = Gtk.Box(spacing=4)
        self.dest_toggle_all_btn = Gtk.ToggleButton(label="Select all")
        self.dest_toggle_all_btn.connect("toggled", self._on_dest_toggle_all)
        self.dest_invert_btn = Gtk.Button(label="Invert")
        self.dest_invert_btn.set_tooltip_text("Flip checked/unchecked for visible items")
        self.dest_invert_btn.connect("clicked", self._on_dest_invert)
        self.dest_select_extras_btn = Gtk.Button(label="Select extras")
        self.dest_select_extras_btn.set_tooltip_text(
            "Check every item that only exists in the destination (blue 'extra' state)")
        self.dest_select_extras_btn.connect("clicked", self._on_dest_select_extras)
        for w in (self.dest_toggle_all_btn, self.dest_invert_btn, self.dest_select_extras_btn):
            actions.pack_start(w, False, False, 0)
        page.pack_start(actions, False, False, 0)

        self.dest_model = Gtk.ListStore(
            bool, str, str, str, str, str, bool, str, int,
            GObject.TYPE_INT64, GObject.TYPE_INT64)
        self.dest_tree = Gtk.TreeView(model=self.dest_model)
        dtoggle = Gtk.CellRendererToggle()
        dtoggle.connect("toggled", self._on_dest_row_toggled)
        self.dest_tree.append_column(Gtk.TreeViewColumn("", dtoggle, active=DST_CHECK))
        dname_renderer = Gtk.CellRendererText()
        dname_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        dname_col = Gtk.TreeViewColumn("Name", dname_renderer)
        dname_col.set_cell_data_func(dname_renderer, self._dest_name_cb)
        dname_col.set_expand(True)
        dname_col.set_sort_column_id(DST_NAME)
        self.dest_tree.append_column(dname_col)
        dsize_col = Gtk.TreeViewColumn("Size", Gtk.CellRendererText(), text=DST_SIZE_TEXT)
        dsize_col.set_sort_column_id(DST_SIZE_TEXT)
        self.dest_tree.append_column(dsize_col)
        dtype_col = Gtk.TreeViewColumn("Type", Gtk.CellRendererText(), text=DST_TYPE)
        dtype_col.set_sort_column_id(DST_TYPE)
        self.dest_tree.append_column(dtype_col)
        dmtime_col = Gtk.TreeViewColumn("Modified", Gtk.CellRendererText(), text=DST_MTIME_TEXT)
        dmtime_col.set_sort_column_id(DST_MTIME_TEXT)
        self.dest_tree.append_column(dmtime_col)
        dst_renderer = Gtk.CellRendererText()
        dst_col = Gtk.TreeViewColumn("State", dst_renderer)
        dst_col.set_cell_data_func(dst_renderer, self._dest_state_cb)
        dst_col.set_sort_column_id(DST_STATE)
        self.dest_model.set_sort_func(DST_STATE, self._sort_state_dest)
        for cid in (DST_NAME, DST_SIZE_TEXT, DST_TYPE, DST_MTIME_TEXT):
            self.dest_model.set_sort_func(
                cid, lambda m, a, b, _d=None, c=cid: self._sort_dest(m, a, b, c))
        self.dest_tree.append_column(dst_col)
        self.dest_tree.set_tooltip_column(DST_TOOLTIP)
        self.dest_tree.connect("row-activated", self._on_dest_row_activated)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.dest_tree)
        page.pack_start(sw, True, True, 0)
        self.dest_summary_lbl = Gtk.Label(label="", xalign=0)
        page.pack_start(self.dest_summary_lbl, False, False, 0)
        self.dest_delete_btn = Gtk.Button(label="🗑 Delete Checked (0)")
        self.dest_delete_btn.get_style_context().add_class("destructive-action")
        self.dest_delete_btn.set_sensitive(False)
        self.dest_delete_btn.connect("clicked", self._on_delete_dest_clicked)
        page.pack_start(self.dest_delete_btn, False, False, 0)
        return page

    def _build_bottom(self, vbox):
        vbox.pack_start(Gtk.Separator(), False, False, 0)
        self.log = Gtk.TextView()
        self.log.set_editable(False)
        self.log.set_cursor_visible(False)
        self.log.modify_font(Pango.FontDescription("monospace 9"))
        log_sw = Gtk.ScrolledWindow()
        log_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_sw.set_min_content_height(110)
        log_sw.add(self.log)
        vbox.pack_start(log_sw, False, False, 0)

    def _append(self, text):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")
        if buf.get_line_count() > 1000:
            start = buf.get_iter_at_offset(0)
            end = buf.get_iter_at_line(buf.get_line_count() - 1000)
            buf.delete(start, end)

    def _log(self, msg):
        self._append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _cmd_result(self, argv, rc, err):
        if self._destroyed:
            return False
        ts = time.strftime("%H:%M:%S")
        self._append(f"{ts}  $ {shlex.join(argv)}")
        if rc == 0:
            self._append(f"{ts}  → rc=0")
        else:
            detail = (err or "").strip().replace("\n", " | ")[:300]
            self._append(f"{ts}  → rc={rc}" + (f": {detail}" if detail else ""))
        return False

    def _track_dialog(self, dlg):
        """Register a transient dialog so _on_destroy can close it. This is
        what makes 'quit' work even while a modal dlg.run() nested loop is
        blocking the main window."""
        self._dialogs.append(dlg)
        return dlg

    def _untrack_dialog(self, dlg):
        try:
            self._dialogs.remove(dlg)
        except ValueError:
            pass

    def _close_dialogs(self):
        for dlg in self._dialogs:
            try:
                dlg.destroy()
            except Exception:
                pass
        self._dialogs = []

    def _show_error(self, title, detail="", kind=Gtk.MessageType.ERROR):
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=kind,
            buttons=Gtk.ButtonsType.OK, text=title,
        )
        if detail:
            dlg.format_secondary_text(detail)
        self._track_dialog(dlg)
        dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()

    def _friendly_dialog(self, conn, kind=Gtk.MessageType.ERROR):
        title, detail = friendly_error_of(conn)
        raw = getattr(conn, "last_error", None)
        if raw and raw not in detail:
            detail = f"{detail}\n\nRaw error:\n{raw}"
        self._show_error(f"{title} — {getattr(conn, 'host', '')}", detail, kind)

    def _prompt(self, title, label_text, initial=""):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(6)
        box.set_border_width(8)
        box.pack_start(Gtk.Label(label=label_text), False, False, 0)
        entry = Gtk.Entry()
        entry.set_text(initial)
        box.pack_start(entry, False, False, 0)
        dlg.show_all()
        self._track_dialog(dlg)
        result = dlg.run()
        text = entry.get_text()
        self._untrack_dialog(dlg)
        dlg.destroy()
        return text if result == Gtk.ResponseType.OK else None

    def _choose_save_path(self, title, initial_path):
        dlg = Gtk.FileChooserDialog(
            title=title, transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                     Gtk.STOCK_SAVE, Gtk.ResponseType.OK),
        )
        dlg.set_do_overwrite_confirmation(True)
        initial_path = os.path.abspath(os.path.expanduser(initial_path))
        parent = os.path.dirname(initial_path)
        if os.path.isdir(parent):
            dlg.set_current_folder(parent)
        dlg.set_current_name(os.path.basename(initial_path))
        self._track_dialog(dlg)
        result = dlg.run()
        path = dlg.get_filename()
        self._untrack_dialog(dlg)
        dlg.destroy()
        return path if result == Gtk.ResponseType.OK else None

    def _prompt_export_options(self, panel, root_path, selected_count, host_info):
        dlg = Gtk.Dialog(title=f"Export {panel.title()} Tree", transient_for=self, modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        "Choose File…", Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(8)
        box.pack_start(Gtk.Label(label=f"Host: {host_info['host_display']}", xalign=0), False, False, 0)
        path_lbl = Gtk.Label(label=f"Path: {root_path}", xalign=0)
        path_lbl.set_line_wrap(True)
        box.pack_start(path_lbl, False, False, 0)

        scope_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scope_box.pack_start(Gtk.Label(label="Scope:", xalign=0), False, False, 0)
        all_btn = Gtk.RadioButton.new_with_label_from_widget(None, "Entire directory")
        selected_btn = Gtk.RadioButton.new_with_label_from_widget(
            all_btn, f"Selected items only ({selected_count})")
        selected_btn.set_sensitive(selected_count > 0)
        if selected_count > 0:
            selected_btn.set_active(True)
        scope_box.pack_start(all_btn, False, False, 0)
        scope_box.pack_start(selected_btn, False, False, 0)
        box.pack_start(scope_box, False, False, 0)

        depth_box = Gtk.Box(spacing=6)
        depth_box.pack_start(Gtk.Label(label="Depth:", xalign=0), False, False, 0)
        depth_combo = Gtk.ComboBoxText()
        for key, label in self._export_depth_choices():
            depth_combo.append(key, label)
        depth_combo.set_active_id("full")
        depth_box.pack_start(depth_combo, False, False, 0)
        box.pack_start(depth_box, False, False, 0)

        dlg.show_all()
        self._track_dialog(dlg)
        result = dlg.run()
        scope = "selected" if selected_btn.get_active() and selected_btn.get_sensitive() else "all"
        depth_key = depth_combo.get_active_id() or "full"
        self._untrack_dialog(dlg)
        dlg.destroy()
        if result != Gtk.ResponseType.OK:
            return None
        return {
            "scope": scope,
            "depth_key": depth_key,
            "max_depth": self._export_depth_value(depth_key),
            "depth_label": depth_key,
        }

    def _build_dest_connection(self, vbox):
        from panes import ConnectionBar
        self.dest_bar = ConnectionBar(
            "DESTINATION", local_default=True,
            callbacks={
                "connect": self._on_dest_connect,
                "browse": lambda _b: self._on_browse_dest(None),
                "disconnect": lambda _b: self._disconnect_dest(),
                "open": lambda _b: self._on_open_dest(None),
                "scan": self._dest_scan_hosts,
                "action": self._on_dest_bar_action,
            },
            show_open=True)
        vbox.pack_start(self.dest_bar, False, False, 0)

    def _discover_hosts(self, *args):
        self.status_label.set_text("Scanning LAN for SSH hosts…")

        def work():
            hosts = discover()
            GLib.idle_add(self._hosts_ready, hosts)

        threading.Thread(target=work, daemon=True).start()

    def _hosts_ready(self, hosts):
        if self._destroyed:
            return False
        if hosts:
            self.status_label.set_text(f"Found {len(hosts)} host(s) — select from dropdown")
        else:
            self.status_label.set_text("No hosts found — type the address manually")
        model = self.host_combo.get_model()
        existing = {model[i][0] for i in range(len(model))} if model is not None else set()
        for h in hosts:
            if h not in existing:
                self.host_combo.append_text(h)

    def _load_profiles_ui(self):
        self.profiles = profiles.load()
        self.profile_combo.remove_all()
        for name in self.profiles:
            self.profile_combo.append_text(name)

    def _on_profile_changed(self, combo):
        name = combo.get_active_text()
        if name and name in self.profiles:
            p = self.profiles[name]
            self.host_combo.get_child().set_text(p.get("host", ""))
            try:
                port = int(p.get("port", 22))
            except (TypeError, ValueError):
                port = 22
            self.port_spin.set_value(port)
            self.user_entry.set_text(p.get("user", ""))
            if p.get("password"):
                self.pass_entry.set_text(p["password"])
                self.remember.set_active(True)

    def _on_save_profile(self, btn):
        name = self._prompt("Save profile", "Profile name:")
        if not name:
            return
        data = {
            "host": self.host_combo.get_child().get_text().strip(),
            "port": self.port_spin.get_value_as_int(),
            "user": self.user_entry.get_text().strip(),
        }
        if self.remember.get_active():
            data["password"] = self.pass_entry.get_text()
        self.profiles[name] = data
        profiles.save(self.profiles)
        self._load_profiles_ui()
        self.profile_combo.set_active(list(self.profiles).index(name))
        self._log(f"Saved profile '{name}'")

    def _on_del_profile(self, btn):
        name = self.profile_combo.get_active_text()
        if name and name in self.profiles:
            del self.profiles[name]
            profiles.save(self.profiles)
            self._load_profiles_ui()
            self._log(f"Deleted profile '{name}'")

    def _on_connect(self, btn):
        if self.conn:
            self._disconnect()
            return
        if self._local_mode():
            self._pick_local_source()
            return
        host = self.host_combo.get_child().get_text().strip()
        user = self.user_entry.get_text().strip()
        password = self.pass_entry.get_text()
        if not host or not user or not password:
            self._show_error("Host, username and password are required.")
            return
        self.connect_btn.set_sensitive(False)
        self.status_label.set_text("Connecting…")

        def work():
            conn = SSHConnection(host, self.port_spin.get_value_as_int(), user, password)
            conn.on_command = lambda argv, rc, err: GLib.idle_add(self._cmd_result, argv, rc, err)
            home = conn.home_dir()
            GLib.idle_add(self._connected, conn, home)

        threading.Thread(target=work, daemon=True).start()

    def _local_mode(self):
        return self.source_combo.get_active_text() == "This computer"

    def _apply_source_ui(self):
        """Keep the toolbar consistent with the active mode and connection
        state: always show the connect/browse button, hide SSH auth fields in
        local mode, and show the local hint only while locally disconnected
        (once connected the status label shows the folder instead)."""
        local = self._local_mode()
        connected = self.conn is not None
        self.auth_box.set_visible(not local)
        self.profile_box.set_visible(not local)
        self.local_hint.set_visible(local and not connected)
        if local:
            self.connect_btn.set_label("Disconnect" if connected else "Browse…")
        else:
            self.connect_btn.set_label("Disconnect" if connected else "Connect")

    def _on_source_changed(self, combo):
        """Switch between SSH and local source. Tear down any active source
        first, then show/hide the SSH auth widgets. In local mode a saved
        session folder is auto-restored, mirroring how SSH restores the last
        source folder on connect."""
        if self.conn:
            self._disconnect()
        self._apply_source_ui()
        if self._local_mode():
            saved = self._saved_local_source()
            if saved:
                self._connect_local(saved, restore_selection=True)
            else:
                self.status_label.set_text("Not connected")

    def _disconnect(self):
        self.batch += 1
        self._apply_all_choice = None
        self.conn.close()
        self.conn = None
        self.model.clear()
        self.selected.clear()
        self._refresh_sel()
        self._apply_source_ui()
        self.status_label.set_text("Not connected")
        self._log("Disconnected")

    def _saved_local_source(self):
        name = getattr(self, "_active_profile", None)
        if name and name in self.profiles:
            p = self.profiles[name].get("last_source_local")
            if p and os.path.isdir(p):
                return p
        return None

    def _pick_local_source(self, path=None):
        """Pick a source folder. Accepts an explicit path for tests/automation;
        otherwise shows a folder chooser."""
        if path is None:
            dlg = Gtk.FileChooserDialog(
                title="Choose source folder", transient_for=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
                buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                         Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
            cur = os.path.expanduser(self.current_path)
            if os.path.isdir(cur):
                dlg.set_current_folder(cur)
            self._track_dialog(dlg)
            if dlg.run() != Gtk.ResponseType.OK:
                self._untrack_dialog(dlg)
                dlg.destroy()
                return
            path = dlg.get_filename()
            self._untrack_dialog(dlg)
            dlg.destroy()
        self._connect_local(path)

    def _ensure_local_profile(self):
        """Local mode persists its session through the profile store too, but
        a user who never used SSH has no profile yet. Create a 'This computer'
        profile on first use so last_source_local / last_selection_local are
        saved and restored like the SSH ones."""
        name = getattr(self, "_active_profile", None)
        if name and name in self.profiles:
            return
        fixed = LOCAL_PROFILE
        if fixed not in self.profiles:
            self.profiles[fixed] = {"host": "local", "port": 0, "user": ""}
            try:
                profiles.save(self.profiles)
            except OSError as e:
                self._log(f"Could not save local profile: {e}")
        self._active_profile = fixed
        if self.profile_combo.get_active_text() != fixed:
            self._load_profiles_ui()
            self.profile_combo.set_active(list(self.profiles).index(fixed))

    def _connect_local(self, path, restore_selection=False):
        conn = LocalConnection()
        self.conn = conn
        self.current_path = path
        self._apply_source_ui()
        self.status_label.set_text(f"Local source — {path}")
        self._log(f"Local source: {path}")
        self._ensure_local_profile()
        if restore_selection:
            name = getattr(self, "_active_profile", None)
            if name and name in self.profiles:
                self._restore_selection = self.profiles[name].get("last_selection_local") or None
        self._load_remote()

    def _connected(self, conn, home):
        if self._destroyed:
            return False
        self.connect_btn.set_sensitive(True)
        if home:
            self.conn = conn
            self.current_path = home
            self._apply_source_ui()
            self.status_label.set_text(f"Connected — {conn.user}@{conn.host}")
            self._log(f"Connected to {conn.user}@{conn.host}:{conn.port} — home: {home}")
            self._activate_profile(conn)
            self._load_remote()
        else:
            self.status_label.set_text("Connection failed")
            self._log(f"Connection failed: {friendly_error_of(conn)}")
            self._friendly_dialog(conn)

    def _activate_profile(self, conn):
        """Save/update the profile used for this connection (creates one
        named user@host:port when nothing matches) and restore the last
        session state (source folder, destination, selection) from it."""
        name = self.profile_combo.get_active_text()
        if not name or name not in self.profiles:
            for cand, p in self.profiles.items():
                if (p.get("host") == conn.host and p.get("port") == conn.port
                        and p.get("user") == conn.user):
                    name = cand
                    break
            else:
                name = f"{conn.user}@{conn.host}:{conn.port}"
        p = dict(self.profiles.get(name) or {})
        changed = False
        for key, val in (("host", conn.host), ("port", conn.port), ("user", conn.user)):
            if p.get(key) != val:
                p[key] = val
                changed = True
        pw = self.pass_entry.get_text()
        if self.remember.get_active() and p.get("password") != pw:
            p["password"] = pw
            changed = True
        self.profiles[name] = p
        self._active_profile = name
        if changed:
            profiles.save(self.profiles)
            self._log(f"Updated profile '{name}'")
        if self.profile_combo.get_active_text() != name:
            self._load_profiles_ui()
            self.profile_combo.set_active(list(self.profiles).index(name))
        if p.get("last_source"):
            self.current_path = p["last_source"]
        self._restore_selection = p.get("last_selection") or None
        if p.get("last_dest"):
            self.dest_entry.set_text(p["last_dest"])
            self._load_dest()

    def _save_profile_state(self):
        """Persist the current session (source folder, destination, selected
        items) into the active profile. Called on transfer start and on
        selection clear; the file is only rewritten when something changed.
        SSH and local sources are stored under different keys so switching
        modes never clobbers the other mode's last location."""
        name = getattr(self, "_active_profile", None)
        if not name or name not in self.profiles:
            return
        p = self.profiles[name]
        if self._local_mode():
            new = {
                "last_source_local": self.current_path,
                "last_dest": self._current_dest(),
                "last_selection_local": {path: dict(info) for path, info in self.selected.items()},
            }
        else:
            new = {
                "last_source": self.current_path,
                "last_dest": self._current_dest(),
                "last_selection": {path: dict(info) for path, info in self.selected.items()},
            }
        changed = False
        for k, v in new.items():
            if p.get(k) != v:
                p[k] = v
                changed = True
        if changed:
            try:
                profiles.save(self.profiles)
            except OSError as e:
                self._log(f"Could not save profile state: {e}")

    def _load_remote(self, *args):
        if not self.conn:
            return
        self.refresh_btn.set_sensitive(False)
        self._remote_req += 1
        req = self._remote_req

        def work():
            try:
                items = self.conn.list_dir(self.current_path)
            except Exception as e:
                items = None
                if hasattr(self.conn, "last_error"):
                    self.conn.last_error = str(e)
            GLib.idle_add(self._remote_loaded, req, items)

        threading.Thread(target=work, daemon=True).start()

    def _remote_loaded(self, req, items):
        if self._destroyed:
            return False
        if req != self._remote_req:
            return False
        self.refresh_btn.set_sensitive(True)
        self.path_entry.set_text(self.current_path)
        self.model.clear()
        if items is None:
            self._remote_meta = {}
            self._apply_states()
            self._friendly_dialog(self.conn, Gtk.MessageType.WARNING)
            return
        self._remote_meta = {
            it["name"]: {"is_dir": it["is_dir"], "size": it["size"]} for it in items}
        restore = self._restore_selection
        self._restore_selection = None
        for it in sorted(items, key=lambda i: (not i["is_dir"], i["name"].lower())):
            try:
                row = self.model.append([
                    it["path"] in self.selected,
                    it["name"],
                    "—" if it["is_dir"] else human_size(it["size"]),
                    "Folder" if it["is_dir"] else ("Link" if it["is_link"] else "File"),
                    it["mtime"],
                    it["is_dir"],
                    it["path"],
                    it.get("size", 0),
                    int(it.get("mtime_epoch", 0) or 0),
                ])
            except Exception as exc:
                # One malformed row must not abort the whole listing (which
                # would also skip the state pass below and leave the panel
                # uncolored). Log and skip it.
                self._log(f"skipping unrenderable item {it.get('name')!r}: {exc}")
                continue
            if restore and it["path"] in restore:
                info = restore[it["path"]]
                self.model[row][0] = True
                self.selected[it["path"]] = {
                    "name": it["name"],
                    "dest": info.get("dest") or self._current_dest(),
                    "is_dir": it["is_dir"],
                }
        if restore:
            self._refresh_sel()
        self._apply_states()

    def _on_row_activated(self, tree, path, col):
        it = self.model[self._child_path(path)]
        self._select_counterpart(self.dest_tree, self.dest_model, it[1])
        if it[5]:
            self.current_path = it[6]
            self._load_remote()

    def _on_path_enter(self, entry):
        p = entry.get_text().strip()
        if p:
            self.current_path = p
            self._load_remote()

    def _on_up(self, btn):
        parent = os.path.dirname(self.current_path.rstrip("/"))
        if not parent:
            return
        self.current_path = parent
        self._load_remote()

    def _load_dest(self, *args):
        path = self.dest_entry.get_text().strip()
        if not path:
            return
        self.dest_refresh_btn.set_sensitive(False)
        self._dest_req += 1
        req = self._dest_req
        if path != self.dest_current_path:
            self.dest_selected.clear()

        def work():
            if self._dest_ssh():
                try:
                    items = self.dest_conn.list_dir(path)
                except Exception as e:
                    items = None
                    if hasattr(self.dest_conn, "last_error"):
                        self.dest_conn.last_error = str(e)
            else:
                items = dir_list(path)
            GLib.idle_add(self._dest_loaded, req, path, items)

        threading.Thread(target=work, daemon=True).start()

    def _dest_loaded(self, req, path, items):
        if self._destroyed:
            return False
        if req != self._dest_req:
            return False
        self.dest_refresh_btn.set_sensitive(True)
        self.dest_current_path = path
        self.dest_model.clear()
        if items is None:
            self._dest_meta = {}
            self._apply_states()
            self.dest_summary_lbl.set_text("Destination folder not found or not accessible")
            self._update_delete_buttons()
            if self._dest_ssh():
                self._friendly_dialog(self.dest_conn, Gtk.MessageType.ERROR)
            return
        self._dest_meta = {
            it["name"]: {"is_dir": it["is_dir"], "size": it["size"]} for it in items}
        for it in sorted(items, key=lambda i: (not i["is_dir"], i["name"].lower())):
            try:
                self.dest_model.append([
                    it["path"] in self.dest_selected,
                    it["name"],
                    "—" if it["is_dir"] else human_size(it["size"]),
                    "Folder" if it["is_dir"] else ("Link" if it["is_link"] else "File"),
                    it["mtime"],
                    it["path"],
                    it["is_dir"],
                    "",
                    9,
                    it.get("size", 0),
                    int(it.get("mtime_epoch", 0) or 0),
                ])
            except Exception as exc:
                self._log(f"skipping unrenderable destination item {it.get('name')!r}: {exc}")
                continue
        self._apply_states()
        self._sync_dest_toggle_all()
        self._update_delete_buttons()

    def _on_dest_home(self, btn):
        if self._dest_ssh():
            home = self.dest_conn.home_dir()
            if home:
                self.dest_entry.set_text(home)
        else:
            self.dest_entry.set_text(os.path.expanduser("~"))
        self._load_dest()

    def _on_dest_up(self, btn):
        raw = self.dest_current_path or self.dest_entry.get_text().strip()
        if self._dest_ssh():
            cur = self.dest_conn.expand_remote(raw) or raw
            parent = rp.dirname(cur.rstrip("/"), self.dest_conn.family)
        else:
            cur = os.path.expanduser(raw)
            parent = os.path.dirname(cur.rstrip("/"))
        if not parent or parent == cur.rstrip("/"):
            return
        self.dest_entry.set_text(parent)
        self._load_dest()

    def _on_dest_row_activated(self, tree, path, col):
        it = self.dest_model[path]
        self._select_counterpart(self.tree, self.model, it[DST_NAME])
        if it[DST_IS_DIR]:
            self.dest_entry.set_text(it[DST_PATH])
            self._load_dest()

    def _select_counterpart(self, tree, model, name):
        if not name:
            return
        tm = tree.get_model()
        name_col = 1 if (tm is self.model or tm is self.model_filter
                         or tm is self.model_sort) else DST_NAME
        for i, row in enumerate(tm):
            if row[name_col] == name:
                tree.get_selection().select_path((i,))
                return

    def _name_cb(self, col, cell, model, it, *a):
        name = model.get_value(it, 1)
        cell.set_property("text", name)
        state = self._states.get(name)
        if state:
            cell.set_property("foreground", self._colors[state])
            cell.set_property("foreground-set", True)
        else:
            cell.set_property("foreground-set", False)

    def _state_cb(self, col, cell, model, it, *a):
        name = model.get_value(it, 1)
        state = self._states.get(name)
        cell.set_property("text", state or "")
        if state:
            cell.set_property("foreground", self._colors[state])
            cell.set_property("foreground-set", True)
        else:
            cell.set_property("foreground-set", False)

    def _dest_name_cb(self, col, cell, model, it, *a):
        name = model.get_value(it, DST_NAME)
        cell.set_property("text", name)
        state = self._states.get(name)
        if state:
            cell.set_property("foreground", self._colors[state])
            cell.set_property("foreground-set", True)
        else:
            cell.set_property("foreground-set", False)

    def _dest_state_cb(self, col, cell, model, it, *a):
        name = model.get_value(it, DST_NAME)
        state = self._states.get(name)
        cell.set_property("text", state or "")
        if state:
            cell.set_property("foreground", self._colors[state])
            cell.set_property("foreground-set", True)
        else:
            cell.set_property("foreground-set", False)

    def _sort_state_remote(self, model, a, b, *a2):
        na = model.get_value(a, SRC_NAME)
        nb = model.get_value(b, SRC_NAME)
        pa = STATE_PRIO.get(self._states.get(na), 9)
        pb = STATE_PRIO.get(self._states.get(nb), 9)
        if pa != pb:
            return -1 if pa < pb else 1
        return -1 if na.lower() < nb.lower() else (1 if na.lower() > nb.lower() else 0)

    def _sort_state_dest(self, model, a, b, *a2):
        pa = model.get_value(a, DST_STATE)
        pb = model.get_value(b, DST_STATE)
        na = model.get_value(a, DST_NAME)
        nb = model.get_value(b, DST_NAME)
        if pa != pb:
            return -1 if pa < pb else 1
        return -1 if na.lower() < nb.lower() else (1 if na.lower() > nb.lower() else 0)

    # -- sorting -----------------------------------------------------------

    # Sort column ids on the source (self.model_sort) and destination
    # (self.dest_model) models, mapped so one panel mirrors the other.
    # Source: 0=state, 1=name, 2=size, 3=type, 4=modified.
    # Dest:   DST_NAME, DST_SIZE_TEXT, DST_TYPE, DST_MTIME_TEXT, DST_STATE.
    SORT_SRC_TO_DST = {0: DST_STATE, 1: DST_NAME, 2: DST_SIZE_TEXT,
                       3: DST_TYPE, 4: DST_MTIME_TEXT}
    SORT_DST_TO_SRC = {v: k for k, v in SORT_SRC_TO_DST.items()}
    UNSORTED = Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID
    _SRC_COL_KIND = {1: "name", 2: "size", 3: "type", 4: "mtime"}
    _DST_COL_KIND = {DST_NAME: "name", DST_SIZE_TEXT: "size",
                     DST_TYPE: "type", DST_MTIME_TEXT: "mtime"}

    @staticmethod
    def _natural_key(name):
        """Natural sort key: numbers compare numerically, the rest
        case-insensitively, so 'file2' < 'file10' while 'A.txt' < 'b.txt'.
        Every segment is wrapped so int and str keys never collide."""
        return tuple((1, int(part)) if part.isdigit() else (0, part.lower())
                     for part in re.split(r"(\d+)", name))

    def _sort_remote(self, model, a, b, col):
        # `model` is the TreeModelFilter under model_sort (sort funcs receive
        # the child model); the sort state lives on the sort model itself.
        order = self.model_sort.get_sort_column_id()[1]
        return self._compare_rows(
            model, a, b, order, self._SRC_COL_KIND.get(col),
            SRC_NAME, SRC_IS_DIR, SRC_SIZE, SRC_MTIME, SRC_TYPE)

    def _sort_dest(self, model, a, b, col):
        order = self.dest_model.get_sort_column_id()[1]
        return self._compare_rows(
            model, a, b, order, self._DST_COL_KIND.get(col),
            DST_NAME, DST_IS_DIR, DST_SIZE, DST_MTIME, DST_TYPE)

    def _compare_rows(self, model, a, b, order, kind, name_col, is_dir_col,
                      size_col, mtime_col, type_col):
        """Shared row comparison: folders always sort before files, then the
        chosen column's raw key, then the natural name as a stable
        tie-breaker (a non-deterministic sort makes GTK rows jump around).
        GTK reverses custom sort results for descending order, which would
        also flip the 'folders first' primary key; the current `order` is
        therefore compared against so folders stay grouped above files in
        both directions and only the column key follows the arrow."""
        da = bool(model.get_value(a, is_dir_col))
        db = bool(model.get_value(b, is_dir_col))
        if da != db:
            dirs_first = order != Gtk.SortType.DESCENDING
            return -1 if da == dirs_first else 1
        na = model.get_value(a, name_col)
        nb = model.get_value(b, name_col)
        if kind == "name":
            ka, kb = self._natural_key(na), self._natural_key(nb)
        elif kind == "size":
            ka, kb = model.get_value(a, size_col), model.get_value(b, size_col)
        elif kind == "type":
            ka, kb = model.get_value(a, type_col), model.get_value(b, type_col)
        else:  # "mtime"
            ka, kb = model.get_value(a, mtime_col), model.get_value(b, mtime_col)
        if ka != kb:
            return -1 if ka < kb else 1
        tka, tkb = self._natural_key(na), self._natural_key(nb)
        if tka != tkb:
            return -1 if tka < tkb else 1
        return 0

    def _on_source_sort_changed(self, model):
        if self._syncing_sort:
            return
        col, order = model.get_sort_column_id()
        if col is None or col == self.UNSORTED or col not in self.SORT_SRC_TO_DST:
            # Never propagate 'unsorted': GTK3's GtkTreeModelSort segfaults
            # when rows are inserted after a sorted->unsorted transition,
            # and the UI cannot produce UNSORTED anyway (header clicks only
            # cycle ascending/descending). The other panel simply keeps its
            # current sort, and both start life unsorted.
            return
        other = self.SORT_SRC_TO_DST[col]
        self._syncing_sort = True
        try:
            self.dest_model.set_sort_column_id(other, order)
        finally:
            self._syncing_sort = False

    def _on_dest_sort_changed(self, model):
        if self._syncing_sort:
            return
        col, order = model.get_sort_column_id()
        if col is None or col == self.UNSORTED or col not in self.SORT_DST_TO_SRC:
            return
        other = self.SORT_DST_TO_SRC[col]
        self._syncing_sort = True
        try:
            self.model_sort.set_sort_column_id(other, order)
        finally:
            self._syncing_sort = False

    def _apply_states(self):
        remote_items = [{"name": n, "is_dir": m["is_dir"], "size": m["size"]}
                        for n, m in self._remote_meta.items()]
        local_items = [{"name": n, "is_dir": m["is_dir"], "size": m["size"]}
                       for n, m in self._dest_meta.items()]
        self._states = classify_items(remote_items, local_items)
        for i, row in enumerate(self.dest_model):
            prio = STATE_PRIO.get(self._states.get(row[DST_NAME]), 9)
            if row[DST_STATE] != prio:
                row[DST_STATE] = prio
        self.tree.queue_draw()
        self.dest_tree.queue_draw()
        self._update_state_summaries()

    def _update_state_summaries(self):
        counts = {"missing": 0, "differ": 0, "conflict": 0, "same": 0, "extra": 0}
        for s in self._states.values():
            counts[s] += 1

        def fmt(part):
            return " · ".join(f"{counts[k]} {k}" for k in part if counts[k])

        self.remote_summary_lbl.set_text(
            fmt(("missing", "differ", "conflict", "same")))
        self.dest_summary_lbl.set_text(
            fmt(("missing", "differ", "conflict", "same", "extra")))

    def _on_row_toggled(self, renderer, path):
        it = self.model[self._child_path(path)]
        full = it[6]
        if it[0]:
            it[0] = False
            self.selected.pop(full, None)
        else:
            it[0] = True
            self.selected[full] = {"name": it[1], "dest": self._current_dest(), "is_dir": it[5]}
        self._refresh_sel()

    def _child_path(self, path):
        """Translate a tree-view path (possibly sort/filter coordinates) into
        a path into self.model. The toggle renderer delivers paths as
        strings, while row-activated delivers TreePath objects; accept both."""
        if isinstance(path, str):
            path = Gtk.TreePath.new_from_string(path)
        if self.model_sort is not None:
            child = self.model_sort.convert_path_to_child_path(path)
            if child is not None:
                path = child
        if self.model_filter is not None:
            child = self.model_filter.convert_path_to_child_path(path)
            if child is not None:
                return child
        return path

    def _filter_visible(self, model, it, *a):
        if not self._filter_text:
            return True
        return self._filter_text in model.get_value(it, 1).lower()

    def _on_filter_changed(self, entry):
        self._filter_text = entry.get_text().strip().lower()
        self.model_filter.refilter()

    def _visible_rows(self):
        """Yield (index, row) for every row currently visible through the
        filter. TreeModelFilter is read-only, so writes go to self.model."""
        for i, row in enumerate(self.model):
            it = self.model.get_iter((i,))
            if it is not None and self.model_filter.convert_child_iter_to_iter(it) is not None:
                yield i, row

    def _select_rows(self, pred):
        for _, row in self._visible_rows():
            if pred(row):
                row[0] = True
                self.selected[row[6]] = {
                    "name": row[1], "dest": self._current_dest(), "is_dir": row[5]}
        self._sync_toggle_all()
        self._refresh_sel()

    def _on_select_missing(self, btn):
        self._select_rows(lambda row: self._states.get(row[1]) == "missing")

    def _on_select_changed(self, btn):
        self._select_rows(
            lambda row: self._states.get(row[1]) in ("missing", "differ", "conflict"))

    def _on_select_folders(self, btn):
        self._select_rows(lambda row: row[5])

    def _on_select_files(self, btn):
        self._select_rows(lambda row: not row[5])

    def _on_invert(self, btn):
        for _, row in self._visible_rows():
            full = row[6]
            if row[0]:
                row[0] = False
                self.selected.pop(full, None)
            else:
                row[0] = True
                self.selected[full] = {
                    "name": row[1], "dest": self._current_dest(), "is_dir": row[5]}
        self._sync_toggle_all()
        self._refresh_sel()

    def _on_toggle_all(self, btn):
        on = btn.get_active()
        for row in self.model:
            row[0] = on
            if on:
                self.selected[row[6]] = {"name": row[1], "dest": self._current_dest(), "is_dir": row[5]}
            else:
                self.selected.pop(row[6], None)
        self._refresh_sel()

    def _sync_toggle_all(self):
        all_on = bool(len(self.model)) and all(row[0] for row in self.model)
        if all_on != self.toggle_all_btn.get_active():
            hid = self.toggle_all_btn.handler_block_by_func(self._on_toggle_all)
            try:
                self.toggle_all_btn.set_active(all_on)
            finally:
                self.toggle_all_btn.handler_unblock_by_func(self._on_toggle_all)

    # -- destination checkbox selection ------------------------------------

    def _on_dest_row_toggled(self, renderer, path):
        it = self.dest_model[path]
        full = it[DST_PATH]
        if it[DST_CHECK]:
            it[DST_CHECK] = False
            self.dest_selected.pop(full, None)
        else:
            it[DST_CHECK] = True
            self.dest_selected[full] = {"name": it[DST_NAME], "is_dir": it[DST_IS_DIR]}
        self._update_delete_buttons()

    def _on_dest_toggle_all(self, btn):
        on = btn.get_active()
        for row in self.dest_model:
            row[DST_CHECK] = on
            if on:
                self.dest_selected[row[DST_PATH]] = {
                    "name": row[DST_NAME], "is_dir": row[DST_IS_DIR]}
            else:
                self.dest_selected.pop(row[DST_PATH], None)
        self._update_delete_buttons()

    def _sync_dest_toggle_all(self):
        all_on = bool(len(self.dest_model)) and all(row[DST_CHECK] for row in self.dest_model)
        if all_on != self.dest_toggle_all_btn.get_active():
            hid = self.dest_toggle_all_btn.handler_block_by_func(self._on_dest_toggle_all)
            try:
                self.dest_toggle_all_btn.set_active(all_on)
            finally:
                self.dest_toggle_all_btn.handler_unblock_by_func(self._on_dest_toggle_all)

    def _on_dest_invert(self, btn):
        for row in self.dest_model:
            full = row[DST_PATH]
            if row[DST_CHECK]:
                row[DST_CHECK] = False
                self.dest_selected.pop(full, None)
            else:
                row[DST_CHECK] = True
                self.dest_selected[full] = {"name": row[DST_NAME], "is_dir": row[DST_IS_DIR]}
        self._sync_dest_toggle_all()
        self._update_delete_buttons()

    def _on_dest_select_extras(self, btn):
        for row in self.dest_model:
            if self._states.get(row[DST_NAME]) == "extra":
                row[DST_CHECK] = True
                self.dest_selected[row[DST_PATH]] = {
                    "name": row[DST_NAME], "is_dir": row[DST_IS_DIR]}
        self._sync_dest_toggle_all()
        self._update_delete_buttons()

    def _update_delete_buttons(self):
        """Delete buttons reflect the checked count on each side and are
        disabled while a transfer or a deletion is already in progress."""
        transfers_active = any(
            t.status in ("queued", "running", "paused") for t in self._transfers)
        blocked = transfers_active or self._delete_in_progress
        n_src = len(self.selected)
        n_dst = len(self.dest_selected)
        self.src_delete_btn.set_label(f"🗑 Delete Checked ({n_src})")
        self.src_delete_btn.set_sensitive(n_src > 0 and not blocked)
        self.src_delete_btn.set_tooltip_text(
            "Cannot delete items while transfers are active" if transfers_active else "")
        self.dest_delete_btn.set_label(f"🗑 Delete Checked ({n_dst})")
        self.dest_delete_btn.set_sensitive(n_dst > 0 and not blocked)
        self.dest_delete_btn.set_tooltip_text(
            "Cannot delete items while transfers are active" if transfers_active else "")

    # -- deletion -----------------------------------------------------------

    def _on_delete_source_clicked(self, btn):
        if self._delete_in_progress:
            return
        items = [(path, info["name"], info.get("is_dir", False))
                 for path, info in sorted(self.selected.items())]
        if not items:
            return
        if self._local_mode():
            location = f"Local Source ({self.current_path})"
        else:
            location = f"Source ({self.conn.user}@{self.conn.host}:{self.current_path})" \
                if self.conn else "Source"
        if not self._confirm_delete(location, items):
            return
        self._run_delete(items, side="source")

    def _on_delete_dest_clicked(self, btn):
        if self._delete_in_progress:
            return
        items = [(path, info["name"], info.get("is_dir", False))
                 for path, info in sorted(self.dest_selected.items())]
        if not items:
            return
        if self._dest_ssh():
            location = (f"Destination ({self.dest_conn.user}@{self.dest_conn.host}:"
                        f"{self.dest_current_path or self._current_dest()})")
        else:
            location = f"Destination ({self.dest_current_path or self._current_dest()})"
        if not self._confirm_delete(location, items):
            return
        self._run_delete(items, side="dest")

    def _confirm_delete(self, location, items):
        n = len(items)
        n_files = sum(1 for _, _, is_dir in items if not is_dir)
        n_dirs = n - n_files
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Confirm Permanent Deletion",
        )
        preview = "\n".join(f"• {name}" for _, name, _ in items[:6])
        if n > 6:
            preview += f"\n… and {n - 6} more"
        parts = []
        if n_files:
            parts.append(f"{n_files} file(s)")
        if n_dirs:
            parts.append(f"{n_dirs} folder(s)")
        summary = f"Permanently delete {n} item(s) ({', '.join(parts)}) from {location}?"
        dlg.format_secondary_text(
            f"{summary}\n\n{preview}\n\n"
            "⚠️ This operation is permanent and cannot be undone.")
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Delete Permanently", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        ok_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
        if ok_btn is not None:
            ok_btn.get_style_context().add_class("destructive-action")
        self._track_dialog(dlg)
        dlg.show_all()
        resp = dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _on_delete_cancel_clicked(self, btn):
        self._delete_cancel.set()
        btn.set_sensitive(False)

    def _run_delete(self, items, side):
        self._delete_in_progress = True
        self._delete_cancel = threading.Event()
        self._update_delete_buttons()
        self.delete_cancel_btn.set_sensitive(True)
        self.delete_header_lbl.set_text(f"Deleting {len(items)} item(s) from "
                                        f"{'Source' if side == 'source' else 'Destination'}…")
        self.delete_progress_bar.set_fraction(0.0)
        self.delete_current_lbl.set_text("")
        self.main_stack.set_visible_child_name("delete_progress")

        if side == "source":
            deleter = (delete_local_item if self._local_mode()
                       else self.conn.delete_item)
        else:
            deleter = (self.dest_conn.delete if self._dest_ssh()
                       else delete_local_item)

        def work():
            done_paths = []
            errors = []
            total = len(items)
            for i, (path, name, is_dir) in enumerate(items):
                if self._delete_cancel.is_set():
                    break
                GLib.idle_add(self._delete_progress_update, i, total, name)
                ok, err = deleter(path)
                if ok:
                    done_paths.append(path)
                else:
                    errors.append((name, err))
            GLib.idle_add(self._delete_finished, side, done_paths, errors, total)

        threading.Thread(target=work, daemon=True).start()

    def _delete_progress_update(self, i, total, name):
        if self._destroyed:
            return False
        frac = i / total if total else 0
        self.delete_progress_bar.set_fraction(frac)
        self.delete_progress_bar.set_text(f"{i}/{total}")
        self.delete_current_lbl.set_text(name)
        return False

    def _delete_finished(self, side, done_paths, errors, total):
        if self._destroyed:
            return False
        self._delete_in_progress = False
        self.delete_progress_bar.set_fraction(1.0)
        self.main_stack.set_visible_child_name("browser")

        if side == "source":
            for p in done_paths:
                self.selected.pop(p, None)
            self._refresh_sel()
            self._save_profile_state()
            self._load_remote()
        else:
            for p in done_paths:
                self.dest_selected.pop(p, None)
            self._load_dest()

        n_done = len(done_paths)
        n_skipped = total - n_done - len(errors)
        msg = f"Deletion finished: {n_done} deleted"
        if errors:
            msg += f", {len(errors)} failed"
        if n_skipped:
            msg += f", {n_skipped} skipped (cancelled)"
        self._log(msg)
        if errors:
            detail = "\n".join(f"{name}: {err}" for name, err in errors[:10])
            self._show_error(f"{len(errors)} of {total} item(s) could not be deleted", detail)
        self._update_delete_buttons()
        return False

    def _on_compare(self, btn):
        if not self.conn:
            self._show_error("Not connected.")
            return
        dest = self._current_dest()
        if not self._dest_tree_ok(dest):
            self._show_error("Destination folder does not exist or is not accessible:", dest)
            return
        btn.set_sensitive(False)
        self.status_label.set_text("Comparing…")
        remote_path = self.current_path

        def work():
            try:
                remote = self.conn.tree_remote(remote_path)
                local = self._dest_tree(dest)
            except Exception as e:
                GLib.idle_add(self._compare_abort, btn, e)
                return
            GLib.idle_add(self._compare_done, btn, remote_path, dest, remote, local)

        threading.Thread(target=work, daemon=True).start()

    def _compare_abort(self, btn, exc):
        if self._destroyed:
            return False
        btn.set_sensitive(True)
        self.status_label.set_text("Compare failed")
        self._log(f"Compare failed: {exc}")

    def _compare_done(self, btn, remote_path, dest, remote, local):
        if self._destroyed:
            return False
        btn.set_sensitive(True)
        if remote is None:
            self.status_label.set_text("Compare failed")
            self._friendly_dialog(self.conn, Gtk.MessageType.WARNING)
            return
        missing, diff, top = compare_trees(remote, local)
        for row in self.model:
            if row[1] in top:
                row[0] = True
                self.selected[row[6]] = {"name": row[1], "dest": dest, "is_dir": row[5]}
        self._sync_toggle_all()
        self._refresh_sel()
        self.status_label.set_text(
            f"Compare: {len(missing)} missing · {len(diff)} size-differing "
            f"→ {len(self.selected)} selected · {len(remote)} remote files")
        self._log(f"Compare {remote_path} vs {dest}: {len(missing)} missing, "
                  f"{len(diff)} size-different, selected {len(self.selected)} "
                  f"of {len(self.model)}")

    def _on_export_source_clicked(self, btn):
        if not self.conn:
            self._show_error("Not connected.")
            return
        self._start_export("source")

    def _on_export_dest_clicked(self, btn):
        dest = self.dest_current_path or self._current_dest()
        if not dest or not os.path.isdir(dest):
            self._show_error("Destination folder does not exist:", dest or "")
            return
        self._start_export("dest")

    def _start_export(self, panel):
        context = self._export_context(panel)
        if not context:
            return
        opts = self._prompt_export_options(
            context["panel"], context["root_path"], len(context["selected_paths"]),
            context["host_info"])
        if not opts:
            return
        suggested = tree_exporter.suggest_export_filename(
            context["host_info"]["host_name"], context["root_path"])
        default_path = os.path.join(self._current_dest(), suggested)
        out_path = self._choose_save_path("Save export YAML", default_path)
        if not out_path:
            return
        self._run_export(context, opts, out_path)

    def _export_context(self, panel):
        if panel == "source":
            if not self.conn:
                return None
            if self._local_mode():
                host_info = tree_exporter.describe_local_host()
            else:
                host_info = tree_exporter.describe_remote_host(self.conn)
            return {
                "panel": "source",
                "root_path": self.current_path,
                "selected_paths": sorted(self.selected),
                "host_info": host_info,
            }
        if self._dest_ssh():
            host_info = tree_exporter.describe_remote_host(self.dest_conn)
        else:
            host_info = tree_exporter.describe_local_host()
        return {
            "panel": "dest",
            "root_path": self.dest_current_path or self._current_dest(),
            "selected_paths": sorted(self.dest_selected),
            "host_info": host_info,
        }

    @staticmethod
    def _export_depth_choices():
        return [
            ("1", "Root level only"),
            ("2", "2 levels"),
            ("3", "3 levels"),
            ("4", "4 levels"),
            ("5", "5 levels"),
            ("full", "Full recursive"),
        ]

    @staticmethod
    def _export_depth_value(depth_key):
        return None if depth_key == "full" else int(depth_key)

    def _run_export(self, context, opts, out_path):
        self.src_export_btn.set_sensitive(False)
        self.dest_export_btn.set_sensitive(False)
        self._log(f"Exporting {context['panel']} tree from {context['root_path']} to {out_path}")

        def work():
            try:
                selected = context["selected_paths"] if opts["scope"] == "selected" else None
                if context["panel"] == "source" and not self._local_mode():
                    doc = tree_exporter.export_remote_tree(
                        self.conn, out_path, context["panel"], context["root_path"],
                        scope=opts["scope"], max_depth=opts["max_depth"],
                        depth_label=opts["depth_label"], selected_paths=selected)
                else:
                    doc = tree_exporter.export_local_tree(
                        out_path, context["panel"], context["root_path"],
                        scope=opts["scope"], max_depth=opts["max_depth"],
                        depth_label=opts["depth_label"], selected_paths=selected)
                GLib.idle_add(self._export_done, context, out_path, doc, None)
            except Exception as exc:
                GLib.idle_add(self._export_done, context, out_path, None, exc)

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, context, out_path, doc, exc):
        if self._destroyed:
            return False
        self.src_export_btn.set_sensitive(True)
        self.dest_export_btn.set_sensitive(True)
        if exc is not None:
            self._log(f"Export failed: {exc}")
            self._show_error("Export failed", str(exc))
            return False
        total = doc["meta"].get("total_entries", 0)
        self._log(f"Export finished: {total} entries written to {out_path}")
        self._show_error("Export complete", out_path, Gtk.MessageType.INFO)
        return False

    def _current_dest(self):
        raw = self.dest_entry.get_text().strip()
        if self._dest_ssh():
            return self.dest_conn.expand_remote(raw) or raw
        return os.path.expanduser(raw)

    def _refresh_sel(self):
        self.sel_model.clear()
        cur = self._current_dest()
        for path, info in sorted(self.selected.items()):
            name = info["name"]
            target = os.path.join(info["dest"], name)
            differs = info["dest"] != cur
            self.sel_model.append([name, path, target, "grey" if differs else None, differs])
        self.transfer_btn.set_label(f"▶ TRANSFER SELECTED ({len(self.selected)})")
        self._update_tab_labels()
        self._update_delete_buttons()

    def _on_set_all_dest(self, btn):
        dest = self._current_dest()
        for info in self.selected.values():
            info["dest"] = dest
        self._refresh_sel()

    def _on_clear_sel(self, btn):
        self.selected.clear()
        for row in self.model:
            row[0] = False
        self.toggle_all_btn.set_active(False)
        self._refresh_sel()
        self._save_profile_state()

    def _on_browse_dest(self, btn):
        if self._dest_ssh():
            self._show_error("Destination is a remote host",
                             "Browse is only available for 'This computer' destinations. "
                             "Type a remote path in the bar instead, or switch the "
                             "destination type to 'This computer'.")
            return
        dlg = Gtk.FileChooserDialog(
            title="Choose destination folder", transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK),
        )
        cur = os.path.expanduser(self.dest_entry.get_text().strip())
        if os.path.isdir(cur):
            dlg.set_current_folder(cur)
        self._track_dialog(dlg)
        if dlg.run() == Gtk.ResponseType.OK:
            self.dest_entry.set_text(dlg.get_filename())
            self._load_dest()
        self._untrack_dialog(dlg)
        dlg.destroy()

    def _on_open_dest(self, btn):
        if self._dest_ssh():
            self._show_error("Destination is a remote host",
                             "'Open in file manager' only works for local destinations.")
            return
        dest = os.path.expanduser(self.dest_entry.get_text().strip())
        if not os.path.isdir(dest):
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        GLib.spawn_command_line_async(opener + " " + shlex.quote(dest))

    def _on_transfer(self, btn):
        if not self.conn:
            self._show_error("Not connected.")
            return
        if self._delete_in_progress:
            self._show_error("Cannot start a transfer while a deletion is in progress.")
            return
        if not self.selected:
            self._show_error("Select at least one item on the remote side.")
            return
        missing = []
        if not self._dest_ssh():
            missing = sorted({os.path.join(os.path.expanduser(info["dest"]), info["name"])
                              for info in self.selected.values()
                              if not os.path.isdir(os.path.expanduser(info["dest"]))})
        if missing:
            self._show_error("Some destination folders do not exist:", "\n".join(missing))
            return
        self._apply_all_choice = None
        transfers = [
            Transfer(info["name"], path,
                     os.path.join(os.path.expanduser(info["dest"]), info["name"]),
                     self.batch, self.conn, is_dir=info.get("is_dir", False))
            for path, info in sorted(self.selected.items())
        ]
        if self.dest_conn is not None:
            for t in transfers:
                t.dest_conn = self.dest_conn
        self._log(f"Starting {len(transfers)} transfer(s)")
        for t in transfers:
            self._enqueue(t)
        self._save_profile_state()

    def _enqueue(self, t):
        t.policy = self._policy()
        t.id = self._next_id
        self._next_id += 1
        self._transfers.append(t)
        self._by_id[t.id] = t
        self._trow[t.id] = self.transfers_model.get_path(self.transfers_model.append([
            t.id, t.name, "waiting…", 0, "", "", "queued", self._colors["queued"], True, None, "",
            "edit-delete"]))
        self._update_tab_labels()
        self._update_transfer_controls()
        self.notebook.set_current_page(self.notebook.page_num(self.transfers_page))
        threading.Thread(target=self._worker, args=(t,), daemon=True).start()

    def _policy(self):
        i = self.policy_combo.get_active()
        if i < 0:
            return POLICY_ASK
        return POLICY_CHOICES[i][1]

    def _worker(self, t):
        self._gate_acquire()
        try:
            if t.removed:
                return
            if t.batch != self.batch:
                GLib.idle_add(self._finish_transfer, t, "cancelled")
                return
            GLib.idle_add(self._set_running, t)
            try:
                st = t.conn.stat_remote(t.src)
                t.total = st["bytes"] if st else None
                t.files = st["files"] if st else None
                t.method = "tar" if t.is_dir else "scp"
                dest_conn = getattr(t, "dest_conn", None)
                if dest_conn is not None and getattr(dest_conn, "kind", None) == "ssh":
                    import transfer_engine
                    status, detail = transfer_engine.run(
                        dest_conn, t.conn, t.src, t.dest, policy=t.policy,
                        method=t.method, on_ask=self._on_ask_conflict,
                        on_part=lambda p: GLib.idle_add(self._on_part, t, p),
                        on_bytes=lambda b, f: GLib.idle_add(self._on_bytes, t, b, f),
                        proc_sink=t.procs,
                        on_finish=lambda: GLib.idle_add(self._set_merging, t),
                    )
                else:
                    status, detail = t.conn.copy(
                        t.src, t.dest, policy=t.policy, method=t.method,
                        on_ask=self._on_ask_conflict,
                        on_part=lambda p: GLib.idle_add(self._on_part, t, p),
                        on_bytes=lambda b, f: GLib.idle_add(self._on_bytes, t, b, f),
                        proc_sink=t.procs,
                        on_finish=lambda: GLib.idle_add(self._set_merging, t),
                    )
            except Exception as e:
                status, detail = "failed", str(e)
            if t.removed:
                GLib.idle_add(self._cleanup_part, t)
                return
            if t.batch != self.batch:
                status, detail = "cancelled", ""
            GLib.idle_add(self._finish_transfer, t, status, detail)
        finally:
            self._gate_release()

    def _gate_acquire(self):
        with self._gate:
            while self._inflight >= self._max_parallel:
                self._gate.wait()
            self._inflight += 1

    def _gate_release(self):
        with self._gate:
            self._inflight -= 1
            self._gate.notify_all()

    def _on_parallel_changed(self, spin):
        with self._gate:
            self._max_parallel = int(spin.get_value())
            self._gate.notify_all()

    def _set_running(self, t):
        if self._destroyed or t.removed:
            return False
        t.status = "running"
        self._set_cell(t, 2, "starting…")
        self._set_cell(t, 6, "…")
        self._set_cell(t, 7, self._colors["running"])
        self._set_cell(t, 11, "media-playback-pause")
        self._ensure_ticker()
        return False

    def _set_merging(self, t):
        if self._destroyed or t.removed:
            return False
        self._set_cell(t, 2, "merging…")
        return False

    def _on_part(self, t, part):
        if self._destroyed:
            return False
        t.part = part
        return False

    def _on_bytes(self, t, total, files):
        if self._destroyed:
            return False
        t.current = total
        t.files_done = files
        self._update_fraction(t)
        return False

    def _finish_transfer(self, t, status, detail=""):
        if self._destroyed:
            return False
        t.status = status
        self._set_cell(t, 11, None)
        if status == "done":
            t.final = detail
            self._set_cell(t, 3, 100)
            self._set_cell(t, 2, "done")
            self._set_cell(t, 6, "✓")
            self._set_cell(t, 7, self._colors["done"])
            self.done += 1
            self._log(f"Copied: {t.src} → {detail}")
        elif status == "skipped":
            self._set_cell(t, 3, 100)
            self._set_cell(t, 2, "skipped")
            self._set_cell(t, 6, "⊘")
            self._set_cell(t, 7, self._colors["skipped"])
            self.done += 1
            self._log(f"Skipped: {t.name} — {detail}")
        elif status == "failed":
            t.err = detail
            self._set_cell(t, 3, 0)
            self._set_cell(t, 2, "failed")
            self._set_cell(t, 6, "✗")
            self._set_cell(t, 7, self._colors["failed"])
            self._set_cell(t, 9, "view-refresh")
            self.failed += 1
            self._log(f"FAILED: {t.src} → {t.dest} ({detail.strip()})")
        else:
            self._set_cell(t, 3, 0)
            self._set_cell(t, 2, "cancelled")
            self._set_cell(t, 6, "—")
            self._set_cell(t, 7, self._colors["cancelled"])
            self._set_cell(t, 9, "view-refresh")
            self._log(f"Cancelled: {t.name}")
        self._cleanup_part(t)
        t.procs = []
        self._set_cell(t, 4, "")
        self._set_cell(t, 5, "")
        if status == "done":
            self._set_cell(t, 10, f"Copied to:\n{t.final}")
        elif status == "failed":
            self._set_cell(t, 10, t.err)
        else:
            self._set_cell(t, 10, f"{status} — {t.name}")
        self.summary_lbl.set_text(f"{self.done} done · {self.failed} failed")
        self._update_tab_labels()
        self._update_transfer_controls()
        return False

    def _on_retry(self, t):
        self._apply_all_choice = None
        t.batch = self.batch
        t.status = "queued"
        t.part = None
        t.total = None
        t.files = None
        t.files_done = None
        t.current = 0
        t.last = 0
        t.last_t = None
        t.speed = 0.0
        t.eta = None
        t.final = None
        t.err = ""
        t.method = None
        self._set_cell(t, 2, "waiting…")
        self._set_cell(t, 3, 0)
        self._set_cell(t, 4, "")
        self._set_cell(t, 5, "")
        self._set_cell(t, 6, "queued")
        self._set_cell(t, 7, self._colors["queued"])
        self._set_cell(t, 9, None)
        self._set_cell(t, 10, "")
        self._set_cell(t, 11, "edit-delete")
        self._log(f"Retrying: {t.name}")
        self._update_transfer_controls()
        threading.Thread(target=self._worker, args=(t,), daemon=True).start()

    def _on_cancel_all(self, btn):
        self.batch += 1
        self._apply_all_choice = None
        if self.conn:
            self.conn.kill_all()
        if self.dest_conn:
            self.dest_conn.kill_all()
        self._log("Cancelling all transfers…")

    def _on_clear_finished(self, btn):
        terminal = ("done", "skipped", "failed", "cancelled")
        doomed = {t.id for t in self._transfers if t.status in terminal}
        i = 0
        while i < len(self.transfers_model):
            row = self.transfers_model[i]
            if row[0] in doomed:
                self.transfers_model.remove(self.transfers_model.get_iter(i))
            else:
                i += 1
        self._transfers = [t for t in self._transfers if t.id not in doomed]
        for tid in doomed:
            self._by_id.pop(tid, None)
            self._trow.pop(tid, None)
        self._rebuild_trow()
        self._update_tab_labels()
        self._update_transfer_controls()

    def _rebuild_trow(self):
        self._trow = {}
        for i in range(len(self.transfers_model)):
            row = self.transfers_model[i]
            self._trow[row[0]] = self.transfers_model.get_path(
                self.transfers_model.get_iter(i))

    def _update_tab_labels(self):
        self.sel_tab_lbl.set_text(f"Selected ({len(self.selected)})")
        self.transfers_tab_lbl.set_text(f"Transfers ({len(self._transfers)})")

    def _update_transfer_controls(self):
        self.cancel_all_btn.set_visible(
            any(x.status in ("queued", "running", "paused") for x in self._transfers))
        self.clear_finished_btn.set_visible(
            any(x.status in ("done", "skipped", "failed", "cancelled") for x in self._transfers))
        self._update_delete_buttons()

    def _ensure_ticker(self):
        if self._ticker_id is None:
            self._ticker_id = GLib.timeout_add(500, self._tick)

    def _tick(self):
        if self._destroyed:
            self._ticker_id = None
            return False
        active = [t for t in self._transfers if t.status == "running"]
        if not active:
            self._ticker_id = None
            return False
        now = time.monotonic()
        for t in active:
            try:
                self._update_progress(t, now)
            except Exception:
                pass
        return True

    def _set_cell(self, t, col, value):
        path = self._trow.get(t.id)
        if path is None:
            return
        try:
            self.transfers_model[path][col] = value
        except (IndexError, ValueError):
            self._trow.pop(t.id, None)

    def _progress_text(self, t, frac):
        text = f"{frac * 100:.0f}% ({human_size(t.current)}/{human_size(t.total)})"
        if t.method == "tar" and t.files:
            text += f" · file {t.files_done}/{t.files}"
        return text

    def _update_fraction(self, t):
        if t.total:
            frac = min(t.current / t.total, 1.0)
            self._set_cell(t, 2, self._progress_text(t, frac))
            self._set_cell(t, 3, max(0, int(frac * 100)))
        else:
            self._set_cell(t, 3, 0)
            self._set_cell(t, 2, "working…")

    def _update_progress(self, t, now):
        if t.method != "tar":
            if not t.part or not os.path.exists(t.part):
                return
            try:
                t.current = os.path.getsize(t.part)
            except OSError:
                return
        self._update_fraction(t)
        if t.last_t is None:
            t.last = t.current
            t.last_t = now
        else:
            dt = now - t.last_t
            if dt >= 0.5:
                t.speed = (t.current - t.last) / dt
                t.last = t.current
                t.last_t = now
                self._set_cell(t, 4, human_size(t.speed) + "/s")
                if t.total and t.speed > 0:
                    t.eta = (t.total - t.current) / t.speed
                    self._set_cell(t, 5, human_time(t.eta))
                else:
                    self._set_cell(t, 5, "—")

    def _on_transfers_press(self, tree, event):
        if event.button != 1:
            return False
        result = tree.get_path_at_pos(int(event.x), int(event.y))
        if result is None:
            return False
        path, col, _, _ = result
        if col is self._retry_col:
            self._retry_for_path(path)
            return True
        if col is self._action_col:
            self._action_for_path(path)
            return True
        return False

    def _retry_for_path(self, path):
        it = self.transfers_model[path]
        if it[9] is None:
            return
        t = self._by_id.get(it[0])
        if t is not None:
            self._on_retry(t)

    def _action_for_path(self, path):
        it = self.transfers_model[path]
        if it[11] is None:
            return
        t = self._by_id.get(it[0])
        if t is None:
            return
        if it[11] in ("media-playback-pause", "media-playback-start"):
            self._on_pause(t)
        elif it[11] == "edit-delete":
            self._on_remove(t)

    def _on_pause(self, t):
        if self._destroyed or t.removed:
            return
        if t.status == "running":
            t.paused = True
            t.status = "paused"
            if self.conn:
                self.conn.pause(t.procs)
            self._set_cell(t, 2, "paused")
            self._set_cell(t, 6, "paused")
            self._set_cell(t, 7, self._colors["paused"])
            self._set_cell(t, 11, "media-playback-start")
        elif t.status == "paused":
            t.paused = False
            t.status = "running"
            if self.conn:
                self.conn.resume(t.procs)
            self._update_fraction(t)
            self._set_cell(t, 6, "…")
            self._set_cell(t, 7, self._colors["running"])
            self._set_cell(t, 11, "media-playback-pause")
            self._ensure_ticker()

    def _on_remove(self, t):
        if t.status not in ("queued", "paused"):
            return
        t.removed = True
        if self.conn:
            # also drops any pause bookkeeping for this sink, so procs that
            # were paused before they existed never freeze mid-spawn
            self.conn.kill_procs(t.procs)
        doomed = {t.id}
        i = 0
        while i < len(self.transfers_model):
            row = self.transfers_model[i]
            if row[0] in doomed:
                self.transfers_model.remove(self.transfers_model.get_iter(i))
            else:
                i += 1
        self._transfers = [x for x in self._transfers if x.id not in doomed]
        for tid in doomed:
            self._by_id.pop(tid, None)
            self._trow.pop(tid, None)
        self._rebuild_trow()
        self._update_tab_labels()
        self._update_transfer_controls()
        self._log(f"Removed from list: {t.name}")

    def _cleanup_part(self, t):
        if t.part and os.path.exists(t.part):
            if os.path.isdir(t.part) and not os.path.islink(t.part):
                threading.Thread(target=shutil.rmtree, args=(t.part,),
                                 kwargs={"ignore_errors": True}, daemon=True).start()
            else:
                try:
                    os.remove(t.part)
                except OSError:
                    pass

    def _on_ask_conflict(self, final, remote_size, local_size):
        with self._ask_lock:
            if self._apply_all_choice:
                return self._apply_all_choice
            if self._closed.is_set():
                return None
            result = []
            done = threading.Event()

            def show():
                dlg = Gtk.Dialog(title="File already exists", transient_for=self, modal=True)
                dlg.add_button("Cancel item", 4)
                dlg.add_button("Skip", 1)
                dlg.add_button("Keep both", 2)
                dlg.add_button("Replace", 3)
                dlg.set_default_response(1)
                box = dlg.get_content_area()
                box.set_spacing(6)
                box.set_border_width(10)
                rs = human_size(remote_size) if remote_size is not None else "unknown"
                ls = human_size(local_size) if local_size is not None else "unknown"
                box.pack_start(Gtk.Label(
                    label=f"<b>{os.path.basename(final)}</b> already exists", use_markup=True),
                    False, False, 0)
                box.pack_start(Gtk.Label(label=f"Remote: {rs}\nLocal: {ls}", xalign=0),
                               False, False, 0)
                apply_all = Gtk.CheckButton(label="Apply to all remaining conflicts")
                box.pack_start(apply_all, False, False, 0)
                self._ask_dialog = dlg
                self._track_dialog(dlg)
                dlg.show_all()
                resp = dlg.run()
                self._untrack_dialog(dlg)
                self._ask_dialog = None
                choice = {1: POLICY_SKIP, 2: POLICY_KEEP_BOTH, 3: POLICY_OVERWRITE}.get(resp)
                if apply_all.get_active() and choice:
                    self._apply_all_choice = choice
                try:
                    dlg.destroy()
                except Exception:
                    pass
                result.append(choice)
                done.set()

            GLib.idle_add(show)
            while not done.is_set() and not self._closed.is_set():
                done.wait(0.2)
            return result[0] if result else None

    def _on_delete_event(self, widget, event):
        """Window close: veto it while transfers or a deletion are active
        unless the user confirms. Return True to keep the window, False to
        let it close."""
        if self._delete_in_progress:
            return not self._confirm_quit_delete()
        if not any(t.status in ("queued", "running", "paused")
                   for t in self._transfers):
            return False
        return not self._confirm_quit()

    def _confirm_quit_delete(self):
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
            text="Deletion in progress",
        )
        dlg.add_buttons("Keep running", Gtk.ResponseType.CANCEL,
                        "Quit anyway", Gtk.ResponseType.OK)
        dlg.format_secondary_text(
            "A deletion is currently running.\n"
            "Quitting now will cancel the remaining deletions.")
        self._track_dialog(dlg)
        dlg.show_all()
        resp = dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            self._delete_cancel.set()
        return resp == Gtk.ResponseType.OK

    def _confirm_quit(self):
        active = sum(1 for t in self._transfers
                     if t.status in ("queued", "running", "paused"))
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
            text="Transfers in progress",
        )
        dlg.add_buttons("Keep running", Gtk.ResponseType.CANCEL,
                        "Quit anyway", Gtk.ResponseType.OK)
        dlg.format_secondary_text(
            f"{active} transfer(s) are queued or in progress.\n"
            "Quitting now cancels them and deletes partial files.")
        self._track_dialog(dlg)
        dlg.show_all()
        resp = dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _final_quit(self):
        """Drain every remaining main-loop level without spinning.

        gtk_main_quit() only flags the *innermost* running loop, and
        Gtk.main_level() does not drop until that loop actually unwinds, so
        a 'while Gtk.main_level() > 0: Gtk.main_quit()' loop spins forever
        while the current dispatch is still on the stack. Instead re-arm an
        idle source: each invocation is dispatched by whichever loop is
        innermost at the time, flags it, and the next invocation walks the
        outer levels one by one until none are left."""
        if Gtk.main_level() > 0:
            Gtk.main_quit()
            return True
        return False

    def _on_destroy(self, widget):
        if self._destroyed:
            return
        self._destroyed = True
        self._closed.set()
        self.batch += 1
        try:
            self._close_dialogs()
            self._ask_dialog = None
            if self.conn:
                self.conn.close()
            if self.dest_conn:
                self.dest_conn.close()
            for t in self._transfers:
                if t.status in ("queued", "running", "paused"):
                    self._cleanup_part(t)
        finally:
            # Flag the innermost loop once (a while-loop here would spin:
            # the level only drops when the loop unwinds). Nested loops are
            # flagged one at a time by the re-armed _final_quit idle source.
            if Gtk.main_level() > 0:
                Gtk.main_quit()
            GLib.idle_add(self._final_quit)
