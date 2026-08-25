"""AppWindow: the clean, symmetric lan-copier window.

Two identical sides (SOURCE / DEST): each has a compact connection bar above a
shared DirPane tree. Compare/select, transfer, delete, and export ride on the
same symmetric model, and the whole thing is driven by this connection
state-machine.

This is a fresh reimplementation. Legacy `ui.py` stays as reference until
`AppWindow` is confirmed, then is removed.
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango

import os
import shutil
import threading
import time

import app.profiles as profiles
from app.widgets.dialog import ConnectionDialog, RESP_DISCONNECT
from app.widgets.endpoint import EndpointBar
from app.widgets.dirpane import DirPane, human_size
import commands.paths as rp
from discovery import discover
from local_transport import LocalConnection, dir_list, delete_local_item
from ssh_transport import (
    SSHConnection, POLICY_ASK, POLICY_OVERWRITE, POLICY_KEEP_BOTH, POLICY_SKIP,
)
import transfer_engine
import tree_exporter


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


def friendly_error_of(conn):
    err = getattr(conn, "last_error", None) or "Unknown error."
    fn = getattr(conn, "friendly_error", SSHConnection.friendly_error)
    return fn(err)


def markup_escape(text):
    """Escape a plain string for use in a Pango-markup label. Filenames and
    paths often contain `&` (e.g. "Willy.Wonka.&.the…mp4"); MessageDialog's
    format_secondary_text parses markup, so a raw ampersand would raise a
    Gtk-WARNING and drop the text."""
    return GLib.markup_escape_text(str(text))


def classify_items(remote_items, local_items):
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


POLICY_CHOICES = [
    ("Ask (smart)", POLICY_ASK),
    ("Overwrite", POLICY_OVERWRITE),
    ("Keep both", POLICY_KEEP_BOTH),
    ("Skip", POLICY_SKIP),
]
LIGHT_COLORS = {"missing": "#c62828", "differ": "#ef6c00", "conflict": "#ad1457",
                "same": "#2e7d32", "extra": "#1565c0",
                "running": "#1565c0", "done": "#2e7d32", "failed": "#c62828",
                "queued": "#757575", "skipped": "#757575", "cancelled": "#757575",
                "paused": "#f9a825"}
DARK_COLORS = {"missing": "#ff5252", "differ": "#ffab40", "conflict": "#ff4081",
               "same": "#81c784", "extra": "#40c4ff",
               "running": "#40c4ff", "done": "#81c784", "failed": "#ff5252",
               "queued": "#9e9e9e", "skipped": "#9e9e9e", "cancelled": "#9e9e9e",
               "paused": "#ffd54f"}
_TERMINAL = ("done", "skipped", "failed", "cancelled")


def _is_dark_theme():
    try:
        s = Gtk.Settings.get_default()
        if s is None:
            return False
        if s.get_property("gtk-application-prefer-dark-theme"):
            return True
        return "dark" in s.get_property("gtk-theme-name").lower()
    except Exception:
        return False


class Transfer:
    __slots__ = ("id", "name", "src", "dest", "batch", "is_dir", "dest_conn",
                 "method", "policy", "status", "part", "total", "files",
                 "files_done", "current", "last", "last_t", "speed", "eta",
                 "final", "err", "procs", "paused", "removed")

    def __init__(self, name, src, dest, batch=0, is_dir=False):
        self.id = None
        self.name = name
        self.src = src
        self.dest = dest
        self.batch = batch
        self.is_dir = is_dir
        self.dest_conn = None
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


class AppWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="LAN SSH File Copier")
        self.set_default_size(1280, 800)
        self.connect("destroy", self._on_destroy)
        self.connect("delete-event", self._on_delete_event)

        self.conn = None
        self.dest_conn = None
        self.current_path = None
        self.dest_current_path = None
        self._states = {}
        self._side_profile = {}

        self.batch = 0
        self.done = 0
        self.failed = 0
        self._transfers = []
        self._by_id = {}
        self._trow = {}
        self._next_id = 0
        self._ticker_id = None
        self._apply_all_choice = None
        self._ask_lock = threading.Lock()
        self._ask_dialog = None
        self._gate = threading.Condition()
        self._max_parallel = 1
        self._inflight = 0

        self._delete_in_progress = False
        self._delete_cancel = threading.Event()
        self._remote_req = 0
        self._dest_req = 0
        self._dest_reload_id = None
        self._closed = threading.Event()
        self._destroyed = False
        self._dialogs = []
        self._active_dialog = None
        self._hosts = []

        self._colors = DARK_COLORS if _is_dark_theme() else LIGHT_COLORS
        self.transfers_page = None

        self._build_ui()
        self._discover_later()

    # ======================================================================
    # UI build
    # ======================================================================

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_border_width(6)
        self.add(vbox)

        self.source_bar = EndpointBar("SOURCE", callbacks={
            "connect": self._on_connect_clicked,
            "navigate": self._on_navigate,
            "open": self._on_open,
            "browse": self._on_pick_folder,
            "export": self._on_export,
            "delete": self._on_delete,
        })
        self.dest_bar = EndpointBar("DESTINATION", callbacks={
            "connect": self._on_connect_clicked,
            "navigate": self._on_navigate,
            "open": self._on_open,
            "browse": self._on_pick_folder,
            "export": self._on_export,
            "delete": self._on_delete,
        })

        self.main_stack = Gtk.Stack()
        browser = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        side_paned = Gtk.Paned()
        side_paned.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.source_pane = DirPane(
            callbacks={"navigate": self._on_navigate,
                        "selection_changed": self._refresh_sel})
        self.dest_pane = DirPane(
            callbacks={"navigate": self._on_navigate,
                        "selection_changed": self._refresh_sel})
        self.source_pane.set_state_colors(self._colors)
        self.dest_pane.set_state_colors(self._colors)
        source_side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        source_side.pack_start(self.source_bar, False, False, 0)
        source_side.pack_start(self.source_pane, True, True, 0)
        dest_side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        dest_side.pack_start(self.dest_bar, False, False, 0)
        dest_side.pack_start(self.dest_pane, True, True, 0)
        # The ⇄ swap control lives in a narrow strip on the inner edge of the
        # destination column so it rides along with the paned divider (GTK3
        # Paned cannot host children inside its gutter).
        self.swap_btn = Gtk.Button(label="⇄")
        self.swap_btn.set_tooltip_text("Swap source ↔ destination "
                                       "(each side keeps its current folder)")
        self.swap_btn.connect("clicked", lambda b: self._on_swap_sides())
        swap_strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        swap_strip.pack_start(self.swap_btn, False, False, 4)
        dest_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        dest_wrap.pack_start(swap_strip, False, False, 0)
        dest_wrap.pack_start(dest_side, True, True, 0)
        side_paned.pack1(source_side, True, True)
        side_paned.pack2(dest_wrap, True, True)
        side_paned.set_position(600)

        self.notebook = Gtk.Notebook()
        self.sel_tab_lbl = Gtk.Label(label="Selected (0)")
        self.notebook.append_page(self._build_selected_page(), self.sel_tab_lbl)
        self.transfers_tab_lbl = Gtk.Label(label="Transfers (0)")
        self.transfers_page = self._build_transfers_page()
        self.notebook.append_page(self.transfers_page, self.transfers_tab_lbl)
        self.log_tab_lbl = Gtk.Label(label="Log")
        self.notebook.append_page(self._build_log_page(), self.log_tab_lbl)

        # Vertical drag handle: source/dest on top (absorbs all extra height),
        # the bottom notebook (Selected / Transfers / Log) fixed-small by
        # default but resizable by dragging the divider.
        self.bottom_vpaned = Gtk.Paned()
        self.bottom_vpaned.set_orientation(Gtk.Orientation.VERTICAL)
        self.bottom_vpaned.pack1(side_paned, True, True)
        self.bottom_vpaned.pack2(self.notebook, False, True)
        self._bottom_vpaned_set = False
        self.bottom_vpaned.connect("size-allocate", self._init_bottom_vpaned)
        browser.pack_start(self.bottom_vpaned, True, True, 0)
        browser.pack_start(Gtk.Label(
            label="Colours: red=missing · orange=size differs · magenta=file/folder clash · "
                  "green=same · blue=destination only", xalign=0), False, False, 0)

        self.main_stack.add_named(browser, "browser")
        self.main_stack.add_named(self._build_delete_progress_page(), "delete_progress")
        self.main_stack.set_visible_child_name("browser")
        vbox.pack_start(self.main_stack, True, True, 0)

    def _init_bottom_vpaned(self, paned, alloc):
        """On first layout, seat the bottom divider at a percentage of the
        available height so the defaults scale with window size: the source/dest
        area keeps ~70%, the bottom notebook (Selected/Transfers/Log) ~30%."""
        if self._bottom_vpaned_set or alloc.height <= 0:
            return
        self._bottom_vpaned_set = True
        paned.set_position(int(alloc.height * 0.70))

    def _build_log_page(self):
        self.log = Gtk.TextView()
        self.log.set_editable(False)
        self.log.set_cursor_visible(False)
        self.log.modify_font(Pango.FontDescription("monospace 9"))
        buf = self.log.get_buffer()
        buf.create_tag("log_err", foreground="#ff5555")
        buf.create_tag("log_warn", foreground="#e6a817")
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.log)
        return sw

    def _build_selected_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar = Gtk.Box(spacing=4)
        self.dest_label = Gtk.Label(label="Destination: ", xalign=0)
        bar.pack_start(self.dest_label, False, False, 0)
        clear = Gtk.Button(label="Clear all")
        clear.connect("clicked", lambda b: self._clear_selection())
        bar.pack_start(clear, False, False, 0)
        self.transfer_btn = Gtk.Button(label="▶ Transfer Selected (0)")
        self.transfer_btn.get_style_context().add_class("suggested-action")
        self.transfer_btn.connect("clicked", self._on_transfer)
        bar.pack_end(self.transfer_btn, False, False, 0)
        page.pack_start(bar, False, False, 0)
        self.sel_model = Gtk.ListStore(str, str, str)
        self.sel_tree = Gtk.TreeView(model=self.sel_model)
        self.sel_tree.append_column(Gtk.TreeViewColumn("Item", Gtk.CellRendererText(), text=0))
        self.sel_tree.append_column(Gtk.TreeViewColumn("Source", Gtk.CellRendererText(), text=1))
        self.sel_tree.append_column(Gtk.TreeViewColumn("To", Gtk.CellRendererText(), text=2))
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(120)
        sw.add(self.sel_tree)
        page.pack_start(sw, True, True, 0)
        return page

    def _build_transfers_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header = Gtk.Box(spacing=6)
        header.pack_start(Gtk.Label(label="Policy:"), False, False, 0)
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
        self.parallel_spin.connect("value-changed", self._on_parallel_changed)
        header.pack_start(self.parallel_spin, False, False, 0)
        self.summary_lbl = Gtk.Label(label="", xalign=0)
        header.pack_start(self.summary_lbl, False, False, 0)
        self.cancel_all_btn = Gtk.Button(label="✕ Cancel all")
        self.cancel_all_btn.connect("clicked", self._on_cancel_all)
        self.cancel_all_btn.set_visible(False)
        header.pack_end(self.cancel_all_btn, False, False, 0)
        self.clear_finished_btn = Gtk.Button(label="🗑 Clear finished")
        self.clear_finished_btn.connect("clicked", self._on_clear_finished)
        self.clear_finished_btn.set_visible(False)
        header.pack_end(self.clear_finished_btn, False, False, 0)
        page.pack_start(header, False, False, 0)

        self.transfers_model = Gtk.ListStore(int, str, str, int, str, str, str, str, bool, str, str, str)
        self.transfers_tree = Gtk.TreeView(model=self.transfers_model)
        self.transfers_tree.get_selection().set_mode(Gtk.SelectionMode.NONE)
        self.transfers_tree.set_tooltip_column(10)
        nr = Gtk.CellRendererText()
        nr.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_col = Gtk.TreeViewColumn("Item", nr, text=1)
        name_col.set_expand(True)
        name_col.set_min_width(160)
        self.transfers_tree.append_column(name_col)
        pr = Gtk.CellRendererProgress()
        self.transfers_tree.append_column(Gtk.TreeViewColumn("Progress", pr, value=3, text=2))
        sp = Gtk.CellRendererText()
        sp.set_property("xalign", 1.0)
        speed_col = Gtk.TreeViewColumn("Speed", sp, text=4)
        speed_col.set_alignment(1.0)
        speed_col.set_min_width(70)
        self.transfers_tree.append_column(speed_col)
        et = Gtk.CellRendererText()
        et.set_property("xalign", 1.0)
        eta_col = Gtk.TreeViewColumn("ETA", et, text=5)
        eta_col.set_alignment(1.0)
        eta_col.set_min_width(70)
        self.transfers_tree.append_column(eta_col)
        st = Gtk.CellRendererText()
        st_col = Gtk.TreeViewColumn("Status", st, text=6, foreground=7)
        st_col.add_attribute(st, "foreground-set", 8)
        st_col.set_min_width(60)
        self.transfers_tree.append_column(st_col)
        r = Gtk.CellRendererPixbuf()
        self._retry_col = Gtk.TreeViewColumn("↻", r)
        self._retry_col.set_min_width(28)
        self._retry_col.add_attribute(r, "icon-name", 9)
        self.transfers_tree.append_column(self._retry_col)
        a = Gtk.CellRendererPixbuf()
        self._action_col = Gtk.TreeViewColumn("⏸/✕", a)
        self._action_col.set_min_width(34)
        self._action_col.add_attribute(a, "icon-name", 11)
        self.transfers_tree.append_column(self._action_col)
        self.transfers_tree.connect("button-press-event", self._on_transfers_press)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(140)
        sw.add(self.transfers_tree)
        page.pack_start(sw, True, True, 0)
        return page

    def _build_delete_progress_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(40)
        self.delete_header_lbl = Gtk.Label(label="", xalign=0.5)
        page.pack_start(self.delete_header_lbl, False, False, 0)
        self.delete_progress_bar = Gtk.ProgressBar()
        self.delete_progress_bar.set_show_text(True)
        page.pack_start(self.delete_progress_bar, False, False, 0)
        self.delete_current_lbl = Gtk.Label(label="", xalign=0.5)
        self.delete_current_lbl.set_line_wrap(True)
        page.pack_start(self.delete_current_lbl, False, False, 0)
        self.delete_cancel_btn = Gtk.Button(label="✕ Cancel Remaining")
        self.delete_cancel_btn.connect("clicked", self._on_delete_cancel_clicked)
        page.pack_start(self.delete_cancel_btn, False, False, 0)
        return page

    # ======================================================================
    # Profiles & discovery
    # ======================================================================

    def _discover_later(self):
        def work():
            try:
                hosts = discover()
            except Exception:
                hosts = []
            GLib.idle_add(self._hosts_ready, hosts, None)
        threading.Thread(target=work, daemon=True).start()

    def _hosts_ready(self, hosts, dialog):
        if self._destroyed:
            return False
        self._hosts = list(hosts)
        if dialog is not None:
            dialog.set_hosts(hosts)
        return False

    def _dialog_discovery(self, dialog):
        def work():
            try:
                hosts = discover()
            except Exception:
                hosts = []
            GLib.idle_add(self._hosts_ready, hosts, dialog)
        threading.Thread(target=work, daemon=True).start()

    # ======================================================================
    # Connection controller
    # ======================================================================

    def _on_connect_clicked(self, bar):
        """Single connection entry point: open the popup to choose *what* this
        side points at (This computer / saved profile / new SSH), connect, or
        disconnect the current connection."""
        side = "source" if bar is self.source_bar else "dest"
        conn = self.conn if side == "source" else self.dest_conn
        connected = conn is not None
        store = profiles.load()
        pdata = {n: profiles.get(store, n) for n in profiles.names(store)}
        name = self._side_profile.get(side) if connected else None
        mode = "local" if (connected and getattr(conn, "kind", None) != "ssh") else "ssh"
        initial = {}
        if connected and mode == "ssh":
            initial = {
                "host": getattr(conn, "host", ""), "port": getattr(conn, "port", 22),
                "user": getattr(conn, "user", ""),
                "password": getattr(conn, "password", "") if getattr(conn, "remember", False) else "",
                "remember": bool(getattr(conn, "remember", False)),
            }
            if name is not None:
                initial["name"] = name
        dlg = ConnectionDialog(self, f"Connect {side.title()}", hosts=self._hosts,
                               profiles_data=pdata, initial=initial, name=name,
                               connected=connected, mode=mode)
        dlg.set_on_discover(self._dialog_discovery)
        self._active_dialog = dlg
        resp = dlg.run()
        data = dlg.collect()
        self._active_dialog = None
        dlg.destroy()
        if resp == RESP_DISCONNECT and connected:
            self._on_disconnect(bar)
            return
        if resp != Gtk.ResponseType.OK or not data:
            return
        if data["mode"] == "local":
            if side == "source":
                self._on_source_browse(bar)
            else:
                self._on_dest_browse(bar)
        else:
            self._do_connect(side, data["params"])

    def _autosave_profile(self, side, params, hostname=None):
        store = profiles.load()
        name = params.get("name")
        if not name:
            name = f"{params.get('user', '')}@{params.get('host', '')}"
        host = str(params.get("host", ""))
        port = int(params.get("port", 22) or 22)
        user = str(params.get("user", ""))
        remember = bool(params.get("remember"))
        password = params.get("password", "") if remember else ""
        # Identify the machine by its resolved hostname first (stable across IP
        # changes) and fall back to host+port+user for legacy/no-hostname rows.
        # This is the fix for reconnects that used to append "user@host 2".
        existing = profiles.find_profile(store, host, port, user, hostname or "")
        if existing:
            name = existing
        elif not hostname:
            base = name
            while name in store["profiles"]:
                i = 2
                while f"{base} {i}" in store["profiles"]:
                    i += 1
                name = f"{base} {i}"
                break
        store["profiles"][name] = {
            "host": host, "port": port, "user": user,
            "hostname": str(hostname or ""),
            "password": password,
            "remember": remember,
        }
        profiles.remember_side(store, side, name)
        # Collapse any legacy "name 2/3..." rows now that the hostname is known,
        # so duplicated entries (saved before hostname-aware identity) naturally
        # merge into a single profile.
        profiles.merge_duplicates(store)
        try:
            profiles.save(store)
        except OSError as e:
            self._log(f"Could not save profile: {e}")
        self._side_profile[side] = name
        self._log(f"Saved profile '{name}' for {side}")
        return name

    # ======================================================================
    # Dialogs / logging
    # ======================================================================

    def _append(self, text, tag=None):
        buf = self.log.get_buffer()
        end = buf.get_end_iter()
        if tag:
            buf.insert_with_tags_by_name(end, text + "\n", tag)
        else:
            buf.insert(end, text + "\n")
        if buf.get_line_count() > 1000:
            start = buf.get_iter_at_offset(0)
            end = buf.get_iter_at_line(buf.get_line_count() - 1000)
            buf.delete(start, end)

    @staticmethod
    def _log_tag(msg):
        """Pick a color tag for a log line by content: hard failures go red,
        recoverable soft problems orange, everything else stays the default."""
        low = msg.lower()
        if msg.startswith("FAILED:") or " error" in low or low.startswith("error"):
            return "log_err"
        if (low.startswith("could not") or "couldn't" in low or "failed" in low
                or low.startswith("retrying") or low.startswith("skip")
                or " not found" in low):
            return "log_warn"
        return None

    def _log(self, msg):
        self._append(f"{time.strftime('%H:%M:%S')}  {msg}", self._log_tag(msg))

    def _cmd_result(self, argv, rc, err):
        if self._destroyed:
            return False
        import shlex
        ts = time.strftime("%H:%M:%S")
        self._append(f"{ts}  $ {shlex.join(argv)}")
        if rc == 0:
            self._append(f"{ts}  → rc=0")
        else:
            detail = (err or "").strip().replace("\n", " | ")[:300]
            self._append(f"{ts}  → rc={rc}" + (f": {detail}" if detail else ""),
                         "log_warn")
        return False

    def _track_dialog(self, dlg):
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
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                                buttons=Gtk.ButtonsType.OK, text=title)
        if detail:
            dlg.format_secondary_text(markup_escape(detail))
        self._track_dialog(dlg)
        dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()
        return False

    def _friendly_dialog(self, conn, kind=Gtk.MessageType.ERROR):
        title, detail = friendly_error_of(conn)
        raw = getattr(conn, "last_error", None)
        if raw and raw not in detail:
            detail = f"{detail}\n\nRaw error:\n{raw}"
        self._show_error(f"{title} — {getattr(conn, 'host', '')}", detail, kind)

    # ======================================================================
    # Connect / disconnect
    # ======================================================================

    def _do_connect(self, side, params):
        host = str(params.get("host", "")).strip()
        user = str(params.get("user", "")).strip()
        password = params.get("password", "") or ""
        try:
            port = int(params.get("port", 22) or 22)
        except (TypeError, ValueError):
            port = 22
        if not host or not user:
            self._show_error(f"{side.title()} SSH: host and username are required.")
            return
        bar = self.source_bar if side == "source" else self.dest_bar
        bar.set_connecting(f"Connecting {side}…")

        def work():
            conn = None
            home = None
            hostname = host
            try:
                conn = SSHConnection(host, port, user, password)
                conn.on_command = lambda argv, rc, err: GLib.idle_add(self._cmd_done, argv, rc, err)
                home = conn.home_dir()
                try:
                    hostname = conn.hostname() or host
                except Exception:
                    hostname = host
            except Exception as e:
                if conn is None:
                    conn = SSHConnection.__new__(SSHConnection)
                    conn.host, conn.port, conn.user = host, port, user
                    conn.last_error = str(e)
            GLib.idle_add(self._connected_side, side, conn, home, params, hostname)
        threading.Thread(target=work, daemon=True).start()

    def _cmd_done(self, argv, rc, err):
        return self._cmd_result(argv, rc, err)

    def _connected_side(self, side, conn, home, params, hostname=None):
        if self._destroyed:
            return False
        bar = self.source_bar if side == "source" else self.dest_bar
        if not home:
            self._friendly_dialog(conn, Gtk.MessageType.WARNING)
            bar.set_disconnected("Connection failed")
            return False
        label = f"{conn.user}@{conn.host}"
        local = getattr(conn, "kind", None) != "ssh"
        if side == "source":
            self.conn = conn
            bar.set_connected(conn, label, is_local=local)
            self._log(f"Connected to {conn.user}@{conn.host}:{conn.port} — home: {home}")
            self._load_source(home)
        else:
            self.dest_conn = conn
            bar.set_connected(conn, label, is_local=local)
            self._log(f"Dest connected {conn.user}@{conn.host} — home: {home}")
            self._load_dest(home)
        self._autosave_profile(side, params, hostname)
        return False

    def _on_disconnect(self, bar):
        side = "source" if bar is self.source_bar else "dest"
        conn = self.conn if side == "source" else self.dest_conn
        if conn:
            conn.close()
        bar.set_path("")
        self._side_profile[side] = None
        if side == "source":
            self.conn = None
            self.current_path = None
            self.source_pane.clear()
            self.source_pane.set_controls_visible(False)
            if self.dest_conn is None:
                self.dest_pane.clear()
        else:
            self.dest_conn = None
            self.dest_current_path = None
            self.dest_pane.clear()
            self.dest_pane.set_controls_visible(False)
        bar.set_disconnected()
        self._recompute_states()
        self._refresh_sel()
        self._log("Disconnected")

    # ======================================================================
    # Side swap
    # ======================================================================

    def _transfer_or_delete_active(self):
        return self._delete_in_progress or any(
            t.status in ("queued", "running", "paused") for t in self._transfers)

    def _swap_endpoints(self):
        """Exchange the two sides' endpoint sessions. Per-side session state is
        exactly the (connection, current path, remembered profile) trio below —
        when adding a per-side field, swap it here too."""
        self.conn, self.dest_conn = self.dest_conn, self.conn
        self.current_path, self.dest_current_path = (
            self.dest_current_path, self.current_path)
        self._side_profile["source"], self._side_profile["dest"] = (
            self._side_profile.get("dest"), self._side_profile.get("source"))

    def _on_swap_sides(self):
        if self._transfer_or_delete_active():
            self._show_error(
                "Cannot swap while a transfer or deletion is in progress.")
            return
        n_source = len(self.source_pane.selected)
        n_dest = len(self.dest_pane.selected)
        if (n_source or n_dest) and not self._confirm_swap(n_source, n_dest):
            return
        # Invalidate any in-flight listing first: its completion callback
        # drops stale request ids, so a pre-swap response lands nowhere.
        self._remote_req += 1
        self._dest_req += 1
        self._swap_endpoints()
        self.source_pane.clear()
        self.dest_pane.clear()
        self._rebind_side("source")
        self._rebind_side("dest")
        self._refresh_sel()
        self._log("Swapped source and destination")

    def _rebind_side(self, side):
        """Re-render one side's bar/pane from its session state and reload the
        listing from the side's current folder. No re-handshake happens — an
        existing connection simply moves to the other slot."""
        conn = self.conn if side == "source" else self.dest_conn
        bar = self.source_bar if side == "source" else self.dest_bar
        pane = self.source_pane if side == "source" else self.dest_pane
        path = self.current_path if side == "source" else self.dest_current_path
        if conn is None:
            bar.set_path("")
            bar.set_disconnected()
            pane.clear()
            pane.set_controls_visible(False)
            return
        local = getattr(conn, "kind", None) != "ssh"
        label = "This computer" if local else f"{conn.user}@{conn.host}"
        bar.set_connected(conn, label, is_local=local)
        bar.set_path(path or "")
        pane.set_controls_visible(True)
        if side == "source":
            self._load_source(path or "~")
        else:
            self._load_dest(path or "~")

    def _confirm_swap(self, n_source, n_dest):
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, text="Swap source and destination?")
        counts = []
        if n_source:
            counts.append(f"{n_source} on the source side")
        if n_dest:
            counts.append(f"{n_dest} on the destination side")
        detail = ("The two connections exchange places; "
                  "each keeps its current folder.")
        if counts:
            detail += f"\n\nChecked items will be unchecked ({', '.join(counts)})."
        dlg.format_secondary_text(markup_escape(detail))
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Swap", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        self._track_dialog(dlg)
        dlg.show_all()
        r = dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()
        return r == Gtk.ResponseType.OK

    # ======================================================================
    # Load / navigate
    # ======================================================================

    def _on_navigate(self, widget, where, path=None):
        side = "dest" if (widget is self.dest_bar or widget is self.dest_pane) else "source"
        bar = self.source_bar if side == "source" else self.dest_bar
        conn = self.conn if side == "source" else self.dest_conn
        cur_path = self.current_path if side == "source" else self.dest_current_path
        if conn is None:
            self._show_error(f"Connect a {side} first.")
            return
        if where == "goto":
            cur = path or bar.path_entry.get_text().strip() or "~"
        else:
            cur = cur_path or bar.path_entry.get_text().strip() or "~"
        cur = cur or "~"
        target = None
        if getattr(conn, "kind", None) == "ssh":
            fam = conn.family
            cur = conn.expand_remote(cur) or cur
            if where == "up":
                base = cur.rstrip("/")
                if base:
                    p = rp.dirname(base, fam)
                    target = p if p and p != base else None
            elif where == "home":
                target = conn.home_dir() or cur
            else:
                target = cur
        else:
            cur = os.path.expanduser(cur)
            if where == "up":
                base = cur.rstrip("/")
                if base:
                    p = os.path.dirname(base)
                    target = p if p and p != base else None
            elif where == "home":
                target = os.path.expanduser("~")
            else:
                target = cur
        if target is None:
            return
        if side == "source":
            self._load_source(target)
        else:
            self._load_dest(target)

    def _load_source(self, path):
        if self.conn is None:
            return
        self._remote_req += 1
        req = self._remote_req

        def work():
            try:
                items = self.conn.list_dir(path)
            except Exception as e:
                items = None
                if hasattr(self.conn, "last_error"):
                    self.conn.last_error = str(e)
            GLib.idle_add(self._source_loaded, req, path, items)
        threading.Thread(target=work, daemon=True).start()

    def _source_loaded(self, req, path, items):
        if self._destroyed or req != self._remote_req:
            return False
        self.current_path = path
        self.source_bar.set_path(path)
        if items is None:
            self.source_pane.clear()
            self._refresh_sel()
            if getattr(self.conn, "kind", None) == "ssh":
                self._friendly_dialog(self.conn, Gtk.MessageType.WARNING)
            return False
        self.source_pane.set_items(items, path)
        self.source_pane.set_controls_visible(True)
        self._recompute_states()
        self._refresh_sel()
        return False

    def _load_dest(self, path=None):
        target = path or (self.dest_bar.path_entry.get_text().strip() or "~")
        self._dest_req += 1
        req = self._dest_req

        def work():
            try:
                if self._dest_is_ssh():
                    items = self.dest_conn.list_dir(target)
                else:
                    items = dir_list(target)
            except Exception as e:
                items = None
            GLib.idle_add(self._dest_loaded, req, target, items)
        threading.Thread(target=work, daemon=True).start()

    def _dest_loaded(self, req, path, items):
        if self._destroyed or req != self._dest_req:
            return False
        self.dest_current_path = path
        self.dest_bar.set_path(path)
        self.dest_label.set_text(f"Destination: {path}")
        if items is None:
            self.dest_pane.clear()
            self._refresh_sel()
            if self._dest_is_ssh():
                self._friendly_dialog(self.dest_conn, Gtk.MessageType.WARNING)
            return False
        self.dest_pane.set_items(items, path)
        self.dest_pane.set_controls_visible(True)
        self._recompute_states()
        self._refresh_sel()
        return False

    def _dest_is_ssh(self):
        return self.dest_conn is not None and getattr(self.dest_conn, "kind", None) == "ssh"

    def _on_pick_folder(self, bar=None):
        """📁 pick-folder action: route to the correct side. Empty for a remote
        (SSH) endpoint — the file chooser only works for this computer."""
        if bar is self.dest_bar:
            self._on_dest_browse(bar)
        else:
            self._on_source_browse(bar)

    def _on_dest_browse(self, bar=None, _dlg=None):
        if self._dest_is_ssh():
            return
        path = self._choose_folder("Choose destination folder",
                                   self.dest_bar.path_entry.get_text() or "~")
        if not path:
            return
        if not isinstance(self.dest_conn, LocalConnection):
            if self.dest_conn is not None:
                self.dest_conn.close()
            self.dest_conn = LocalConnection()
        self._side_profile["dest"] = None
        self.dest_bar.set_connected(self.dest_conn, "This computer", is_local=True)
        self._load_dest(path)

    def _on_source_browse(self, bar=None, _dlg=None):
        if getattr(self.conn, "kind", None) == "ssh":
            return
        path = self._choose_folder("Choose a local source folder",
                                   self.current_path or "~")
        if not path:
            return
        self.conn = LocalConnection()
        self._side_profile["source"] = None
        self.source_bar.set_connected(self.conn, "This computer", is_local=True)
        self._load_source(path)

    def _choose_folder(self, title, initial):
        if Gtk is None:
            return None
        dlg = Gtk.FileChooserDialog(
            title=title, transient_for=self, action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                     Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        base = os.path.expanduser(initial)
        if os.path.isdir(base):
            dlg.set_current_folder(base)
        self._track_dialog(dlg)
        result = dlg.run()
        path = dlg.get_filename() if result == Gtk.ResponseType.OK else None
        self._untrack_dialog(dlg)
        dlg.destroy()
        return path

    def _on_open(self, bar=None, _n=None):
        import subprocess
        import sys
        side = "dest" if bar is self.dest_bar else "source"
        cur_dir = self.dest_current_path if side == "dest" else self.current_path
        path = os.path.expanduser(cur_dir or "~")
        if not os.path.isdir(path):
            return
        try:
            opener = {"darwin": ["open", path],
                      "win32": ["explorer", path]}.get(sys.platform, ["xdg-open", path])
            subprocess.Popen(opener)
        except Exception as e:
            self._log(f"Could not open: {e}")

    def _recompute_states(self):
        src_items = [{"name": n, "is_dir": m["is_dir"], "size": m["size"]}
                     for n, m in self.source_pane.meta.items()]
        dst_items = [{"name": n, "is_dir": m["is_dir"], "size": m["size"]}
                     for n, m in self.dest_pane.meta.items()]
        states = classify_items(src_items, dst_items)
        self._states = states
        self.source_pane.set_states(states)
        self.dest_pane.set_states(states)
        self._summarise(src_items, dst_items, states)
        self._update_delete_buttons()

    def _summarise(self, src_items, dst_items, states):
        counts = {k: 0 for k in ("missing", "differ", "conflict", "same", "extra")}
        for s in states.values():
            counts[s] += 1
        def fmt(m):
            return " · ".join(f"{counts[k]} {k}" for k in m if counts[k])
        self.source_pane.set_summary(fmt(("missing", "differ", "conflict", "same")))
        self.dest_pane.set_summary(fmt(("missing", "differ", "conflict", "same", "extra")))

    # ======================================================================
    # Selection
    # ======================================================================

    def _refresh_sel(self):
        self.sel_model.clear()
        for path, info in sorted(self.source_pane.selected.items()):
            self.sel_model.append([info["name"], path, self._dest_join(info["name"])])
        n = len(self.source_pane.selected)
        self.transfer_btn.set_label(f"▶ Transfer Selected ({n})")
        self.sel_tab_lbl.set_text(f"Selected ({n})")
        self._update_delete_buttons()

    def _clear_selection(self):
        for row in self.source_pane.model:
            row[0] = False
        self.source_pane.selected.clear()
        self.source_pane._sync_select_all()
        self._refresh_sel()

    def _update_delete_buttons(self):
        blocked = self._transfer_or_delete_active()
        self.source_bar.set_delete_count(len(self.source_pane.selected), enabled=not blocked)
        self.dest_bar.set_delete_count(len(self.dest_pane.selected), enabled=not blocked)
        self.swap_btn.set_sensitive(not blocked)

    def _dest_exists(self, path):
        try:
            if self._dest_is_ssh():
                p = self.dest_conn.expand_remote(path) or path
                return bool(self.dest_conn.exists(p))
            return os.path.isdir(os.path.expanduser(path))
        except Exception:
            return False

    # ======================================================================
    # Export
    # ======================================================================

    def _on_export(self, bar=None):
        if bar is self.dest_bar:
            self._on_export_dest(bar)
        else:
            self._on_export_source(bar)

    def _on_export_source(self, pane=None):
        if self.conn is None:
            self._show_error("Connect a source first.")
            return
        self._start_export("source")

    def _on_export_dest(self, pane=None):
        dest = self.dest_current_path or "~"
        if not self._dest_exists(dest):
            self._show_error("Destination folder does not exist:", str(dest))
            return
        self._start_export("dest")

    def _start_export(self, panel):
        context = self._export_context(panel)
        if not context:
            return
        opts = self._prompt_export_options(context)
        if not opts:
            return
        suggested = tree_exporter.suggest_export_filename(
            context["host_info"]["host_name"], context["root_path"])
        default_path = os.path.join(os.path.expanduser(self.dest_current_path or "~"), suggested)
        out_path = self._choose_save_path("Save export YAML", default_path)
        if not out_path:
            return
        self._run_export(context, opts, out_path)

    def _export_context(self, panel):
        if panel == "source":
            if self.conn is None:
                return None
            host_info = (tree_exporter.describe_local_host()
                         if getattr(self.conn, "kind", None) != "ssh"
                         else tree_exporter.describe_remote_host(self.conn))
            return {"panel": "source", "conn": self.conn, "root_path": self.current_path,
                    "selected_paths": sorted(self.source_pane.selected), "host_info": host_info}
        root = self.dest_current_path or "~"
        host_info = (tree_exporter.describe_remote_host(self.dest_conn)
                     if self._dest_is_ssh() else tree_exporter.describe_local_host())
        return {"panel": "dest", "conn": self.dest_conn, "root_path": root,
                "selected_paths": sorted(self.dest_pane.selected), "host_info": host_info}

    def _choose_save_path(self, title, initial_path):
        dlg = Gtk.FileChooserDialog(
            title=title, transient_for=self, action=Gtk.FileChooserAction.SAVE,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                     Gtk.STOCK_SAVE, Gtk.ResponseType.OK))
        dlg.set_do_overwrite_confirmation(True)
        p = os.path.abspath(os.path.expanduser(initial_path))
        if os.path.isdir(os.path.dirname(p)):
            dlg.set_current_folder(os.path.dirname(p))
        dlg.set_current_name(os.path.basename(p))
        self._track_dialog(dlg)
        r = dlg.run()
        out = dlg.get_filename() if r == Gtk.ResponseType.OK else None
        self._untrack_dialog(dlg)
        dlg.destroy()
        return out

    @staticmethod
    def _export_depth_choices():
        return [("1", "Root level only"), ("2", "2 levels"), ("3", "3 levels"),
                ("4", "4 levels"), ("5", "5 levels"), ("full", "Full recursive")]

    @staticmethod
    def _export_depth_value(key):
        return None if key == "full" else int(key)

    def _prompt_export_options(self, context):
        panel = context["panel"]
        dlg = Gtk.Dialog(title=f"Export {panel.title()} Tree", transient_for=self, modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        "Choose File…", Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(8)
        box.pack_start(Gtk.Label(label=f"Host: {context['host_info']['host_display']}", xalign=0),
                       False, False, 0)
        pl = Gtk.Label(label=f"Path: {context['root_path']}", xalign=0)
        pl.set_line_wrap(True)
        box.pack_start(pl, False, False, 0)
        all_btn = Gtk.RadioButton.new_with_label_from_widget(None, "Entire directory")
        n = len(context["selected_paths"])
        sel_btn = Gtk.RadioButton.new_with_label_from_widget(all_btn, f"Selected items only ({n})")
        sel_btn.set_sensitive(n > 0)
        if n > 0:
            sel_btn.set_active(True)
        box.pack_start(all_btn, False, False, 0)
        box.pack_start(sel_btn, False, False, 0)
        depth = Gtk.ComboBoxText()
        for key, label in self._export_depth_choices():
            depth.append(key, label)
        depth.set_active_id("full")
        box.pack_start(depth, False, False, 0)
        dlg.show_all()
        self._track_dialog(dlg)
        r = dlg.run()
        scope = "selected" if sel_btn.get_active() and sel_btn.get_sensitive() else "all"
        key = depth.get_active_id() or "full"
        self._untrack_dialog(dlg)
        dlg.destroy()
        if r != Gtk.ResponseType.OK:
            return None
        return {"scope": scope, "depth_key": key,
                "max_depth": self._export_depth_value(key), "depth_label": key}

    def _run_export(self, context, opts, out_path):
        bar = self.source_bar if context["panel"] == "source" else self.dest_bar
        bar.set_export_sensitive(False)
        self._log(f"Exporting {context['panel']} tree from {context['root_path']}")

        def work():
            try:
                selected = context["selected_paths"] if opts["scope"] == "selected" else None
                # Use the connection captured at snapshot time: a side swap
                # while the export runs must not reroute it to another host.
                conn = context["conn"]
                if context["panel"] == "source" and getattr(conn, "kind", None) == "ssh":
                    doc = tree_exporter.export_remote_tree(
                        conn, out_path, context["panel"], context["root_path"],
                        scope=opts["scope"], max_depth=opts["max_depth"],
                        depth_label=opts["depth_label"], selected_paths=selected)
                else:
                    doc = tree_exporter.export_local_tree(
                        out_path, context["panel"], context["root_path"],
                        scope=opts["scope"], max_depth=opts["max_depth"],
                        depth_label=opts["depth_label"], selected_paths=selected)
                GLib.idle_add(self._export_done, bar, context, out_path, doc, None)
            except Exception as exc:
                GLib.idle_add(self._export_done, bar, context, out_path, None, exc)
        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, bar, context, out_path, doc, exc):
        if self._destroyed:
            return False
        bar.set_export_sensitive(True)
        if exc is not None:
            self._log(f"Export failed: {exc}")
            self._show_error("Export failed", str(exc))
            return False
        self._log(f"Export finished: {doc['meta'].get('total_entries', 0)} entries → {out_path}")
        self._show_error("Export complete", out_path, Gtk.MessageType.INFO)
        return False

    # ======================================================================
    # Delete
    # ======================================================================

    def _on_delete(self, bar=None):
        if bar is self.dest_bar:
            self._on_delete_dest(bar)
        else:
            self._on_delete_source(bar)

    def _on_delete_source(self, pane=None):
        if self._delete_in_progress:
            return
        items = [(p, i["name"], i.get("is_dir", False))
                 for p, i in sorted(self.source_pane.selected.items())]
        if not items:
            return
        if not self._confirm_delete("Source", items):
            return
        self._run_delete(items, side="source")

    def _on_delete_dest(self, pane=None):
        if self._delete_in_progress:
            return
        items = [(p, i["name"], i.get("is_dir", False))
                 for p, i in sorted(self.dest_pane.selected.items())]
        if not items:
            return
        if not self._confirm_delete("Destination", items):
            return
        self._run_delete(items, side="dest")

    def _confirm_delete(self, location, items):
        n = len(items)
        n_files = sum(1 for _, _, d in items if not d)
        n_dirs = n - n_files
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE,
                                text="Confirm Permanent Deletion")
        preview = "\n".join(f"• {name}" for _, name, _ in items[:6])
        if n > 6:
            preview += f"\n… and {n - 6} more"
        parts = []
        if n_files:
            parts.append(f"{n_files} file(s)")
        if n_dirs:
            parts.append(f"{n_dirs} folder(s)")
        dlg.format_secondary_text(markup_escape(
            f"Permanently delete {n} item(s) ({', '.join(parts)}) from {location}?\n\n"
            f"{preview}\n\n⚠️ This operation is permanent and cannot be undone."))
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Delete Permanently", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        ok = dlg.get_widget_for_response(Gtk.ResponseType.OK)
        if ok is not None:
            ok.get_style_context().add_class("destructive-action")
        self._track_dialog(dlg)
        dlg.show_all()
        r = dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()
        return r == Gtk.ResponseType.OK

    def _on_delete_cancel_clicked(self, btn):
        self._delete_cancel.set()
        btn.set_sensitive(False)

    def _run_delete(self, items, side):
        self._delete_in_progress = True
        self._delete_cancel = threading.Event()
        self._update_delete_buttons()
        self.delete_cancel_btn.set_sensitive(True)
        self.delete_header_lbl.set_text(f"Deleting {len(items)} item(s) from {side}…")
        self.delete_progress_bar.set_fraction(0.0)
        self.delete_current_lbl.set_text("")
        self.main_stack.set_visible_child_name("delete_progress")

        if side == "source":
            deleter = delete_local_item if getattr(self.conn, "kind", None) != "ssh" \
                else self.conn.delete_item
        else:
            deleter = self.dest_conn.delete if self._dest_is_ssh() else delete_local_item

        def work():
            done_paths, errors = [], []
            total = len(items)
            for i, (path, name, _) in enumerate(items):
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
        self.delete_progress_bar.set_fraction(i / total if total else 0)
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
                self.source_pane.selected.pop(p, None)
            self._refresh_sel()
            self._load_source(self.current_path)
        else:
            for p in done_paths:
                self.dest_pane.selected.pop(p, None)
            self._load_dest(self.dest_current_path)
        n_skip = total - len(done_paths) - len(errors)
        msg = f"Deletion finished: {len(done_paths)} deleted"
        if errors:
            msg += f", {len(errors)} failed"
        if n_skip:
            msg += f", {n_skip} skipped"
        self._log(msg)
        if errors:
            self._show_error(f"{len(errors)} of {total} item(s) could not be deleted",
                             "\n".join(f"{n}: {e}" for n, e in errors[:10]))
        self._update_delete_buttons()
        return False

    def _on_disconnect_dest(self, bar):
        pass

    # ======================================================================
    # Transfer
    # ======================================================================

    def _dest_join(self, name):
        """Build a destination path for `name` using the endpoint's family
        (control-machine os.path is never correct for a remote dest)."""
        dest = self.dest_current_path or ""
        if self._dest_is_ssh():
            return rp.join(self.dest_conn.family, dest or "/", name)
        return os.path.join(dest, name)

    def _on_transfer(self, btn=None):
        if self.conn is None:
            self._show_error("Not connected.")
            return
        if self._delete_in_progress:
            self._show_error("Cannot start a transfer while a deletion is in progress.")
            return
        if not self.source_pane.selected:
            self._show_error("Select at least one item on the source side.")
            return
        dest = self.dest_current_path or ""
        if self._dest_is_ssh():
            exists = self._dest_exists(dest or "~")
            if not exists:
                self._show_error("The destination folder does not exist or is unreachable",
                                 str(dest or "~"))
                return
        elif not os.path.isdir(os.path.expanduser(dest)):
            self._show_error("The destination folder does not exist",
                             str(dest or "~"))
            return
        self._apply_all_choice = None
        transfers = []
        for path, info in sorted(self.source_pane.selected.items()):
            t = Transfer(info["name"], path, self._dest_join(info["name"]),
                         self.batch, is_dir=info.get("is_dir", False))
            if self.dest_conn is not None:
                t.dest_conn = self.dest_conn
            transfers.append(t)
        self._log(f"Starting {len(transfers)} transfer(s)")
        for t in transfers:
            self._enqueue(t)

    def _enqueue(self, t):
        t.policy = self._policy()
        t.id = self._next_id
        self._next_id += 1
        self._transfers.append(t)
        self._by_id[t.id] = t
        self._trow[t.id] = self.transfers_model.get_path(self.transfers_model.append([
            t.id, t.name, "waiting…", 0, "", "", "queued", self._colors["queued"],
            True, None, "", "edit-delete"]))
        self.transfers_tab_lbl.set_text(f"Transfers ({len(self._transfers)})")
        self._update_transfer_controls()
        self.notebook.set_current_page(
            self.notebook.page_num(self.transfers_page) if hasattr(self, "transfers_page") else 1)
        threading.Thread(target=self._worker, args=(t,), daemon=True).start()

    def _policy(self):
        i = self.policy_combo.get_active()
        return POLICY_ASK if i < 0 else POLICY_CHOICES[i][1]

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
                st = self.conn.stat_remote(t.src)
                t.total = st["bytes"] if st else None
                t.files = st["files"] if st else None
                t.method = "tar" if t.is_dir else "scp"
                if t.dest_conn is not None and getattr(t.dest_conn, "kind", None) == "ssh":
                    status, detail = transfer_engine.run(
                        t.dest_conn, self.conn, t.src, t.dest, policy=t.policy, method=t.method,
                        on_ask=self._on_ask_conflict,
                        on_part=lambda p: GLib.idle_add(self._on_part, t, p),
                        on_bytes=lambda b, f: GLib.idle_add(self._on_bytes, t, b, f),
                        proc_sink=t.procs,
                        on_finish=lambda: GLib.idle_add(self._set_merging, t))
                else:
                    status, detail = self.conn.copy(
                        t.src, t.dest, policy=t.policy, method=t.method,
                        on_ask=self._on_ask_conflict,
                        on_part=lambda p: GLib.idle_add(self._on_part, t, p),
                        on_bytes=lambda b, f: GLib.idle_add(self._on_bytes, t, b, f),
                        proc_sink=t.procs,
                        on_finish=lambda: GLib.idle_add(self._set_merging, t))
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
        self.transfers_tab_lbl.set_text(f"Transfers ({len([x for x in self._transfers if x.status not in _TERMINAL])})")
        self._update_transfer_controls()
        if status in ("done", "skipped", "failed"):
            self._schedule_dest_reload()
        return False

    def _schedule_dest_reload(self):
        """Debounce re-listing the destination folder after transfers land so a
        parallel batch triggers a single reload shortly after the last one."""
        if self.dest_current_path is None:
            return
        if self._dest_reload_id is not None:
            GLib.source_remove(self._dest_reload_id)
        self._dest_reload_id = GLib.timeout_add(300, self._reload_dest_idle)

    def _reload_dest_idle(self):
        self._dest_reload_id = None
        if self._destroyed:
            return False
        if self.dest_current_path:
            self._load_dest(self.dest_current_path)
        return False

    def _on_retry(self, t):
        self._apply_all_choice = None
        t.batch = self.batch
        for attr in ("status", "part", "total", "files", "files_done", "current",
                     "last", "last_t", "speed", "eta", "final", "err", "method"):
            setattr(t, attr, None)
        t.status = "queued"
        t.current = 0
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
        doomed = {t.id for t in self._transfers if t.status in _TERMINAL}
        i = 0
        while i < len(self.transfers_model):
            if self.transfers_model[i][0] in doomed:
                self.transfers_model.remove(self.transfers_model.get_iter(i))
            else:
                i += 1
        self._transfers = [t for t in self._transfers if t.id not in doomed]
        for tid in doomed:
            self._by_id.pop(tid, None)
            self._trow.pop(tid, None)
        self._rebuild_trow()
        self.transfers_tab_lbl.set_text(f"Transfers ({len(self._transfers)})")
        self._update_transfer_controls()

    def _rebuild_trow(self):
        self._trow = {}
        for i in range(len(self.transfers_model)):
            row = self.transfers_model[i]
            self._trow[row[0]] = self.transfers_model.get_path(self.transfers_model.get_iter(i))

    def _update_transfer_controls(self):
        self.cancel_all_btn.set_visible(
            any(t.status in ("queued", "running", "paused") for t in self._transfers))
        self.clear_finished_btn.set_visible(
            any(t.status in _TERMINAL for t in self._transfers))
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

    def _update_fraction(self, t):
        if t.total:
            frac = min(t.current / t.total, 1.0)
            text = f"{frac * 100:.0f}% ({human_size(t.current)}/{human_size(t.total)})"
            if t.method == "tar" and t.files:
                text += f" · file {t.files_done}/{t.files}"
            self._set_cell(t, 2, text)
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
            self.conn.kill_procs(t.procs)
        doomed = {t.id}
        i = 0
        while i < len(self.transfers_model):
            if self.transfers_model[i][0] in doomed:
                self.transfers_model.remove(self.transfers_model.get_iter(i))
            else:
                i += 1
        self._transfers = [x for x in self._transfers if x.id not in doomed]
        for tid in doomed:
            self._by_id.pop(tid, None)
            self._trow.pop(tid, None)
        self._rebuild_trow()
        self.transfers_tab_lbl.set_text(f"Transfers ({len(self._transfers)})")
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

    # ======================================================================
    # Lifecycle
    # ======================================================================

    def _on_delete_event(self, widget, event):
        if self._delete_in_progress:
            return not self._confirm_quit_delete()
        if not any(t.status in ("queued", "running", "paused") for t in self._transfers):
            return False
        return not self._confirm_quit()

    def _confirm_quit_delete(self):
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, text="Deletion in progress")
        dlg.add_buttons("Keep running", Gtk.ResponseType.CANCEL,
                        "Quit anyway", Gtk.ResponseType.OK)
        dlg.format_secondary_text(markup_escape(
            "A deletion is currently running.\nQuitting now cancels the remaining deletions."))
        self._track_dialog(dlg)
        dlg.show_all()
        r = dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()
        if r == Gtk.ResponseType.OK:
            self._delete_cancel.set()
        return r == Gtk.ResponseType.OK

    def _confirm_quit(self):
        active = sum(1 for t in self._transfers if t.status in ("queued", "running", "paused"))
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, text="Transfers in progress")
        dlg.add_buttons("Keep running", Gtk.ResponseType.CANCEL,
                        "Quit anyway", Gtk.ResponseType.OK)
        dlg.format_secondary_text(markup_escape(
            f"{active} transfer(s) are queued or in progress.\n"
            "Quitting now cancels them and deletes partial files."))
        self._track_dialog(dlg)
        dlg.show_all()
        r = dlg.run()
        self._untrack_dialog(dlg)
        dlg.destroy()
        return r == Gtk.ResponseType.OK

    def _final_quit(self):
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
            if Gtk.main_level() > 0:
                Gtk.main_quit()
            GLib.idle_add(self._final_quit)