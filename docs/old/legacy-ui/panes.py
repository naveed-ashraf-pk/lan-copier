# TODO(cleanup): LEGACY FILE. Superseded by app/panels.py (DirPane) and
# app/connections.py (ConnectionBar + ConnectionDialog). Only the legacy
# tests/test_ui.py smoke still imports this. Delete after the new window is
# confirmed and the legacy test file is retired.

"""Reusable symmetric UI widgets for lan-copier.

- `ConnectionBar`: a labeled connection toolbar (SOURCE / DESTINATION) —
  endpoint type toggle (This computer / SSH), profile dropdown, host/port/
  user/password SSH group, connect/browse/disconnect, LAN discovery refresh,
  status label, and (destination-only) an "open in file manager" action that
  is limited to local endpoints.
- `BrowserPane`: a file-browsing panel used for both the source and the
  destination so the two sides are symmetric by construction: path bar,
  filter, quick-select buttons, sortable/colorised TreeView, per-row
  checkboxes, selection counter, delete button.

Both widgets stay UI-only: they call endpoint-interface methods handed in by
the window via callbacks, keeping UI and functionality separated.
"""

import os
import time

from gi.repository import Gtk, GObject, Pango

# canonical column layout shared by both panes (with back-compat aliases)
COL_CHECK = 0
COL_NAME = 1
COL_SIZE_TEXT = 2
COL_TYPE = 3
COL_MTIME_TEXT = 4
COL_IS_DIR = 5
COL_PATH = 6
COL_SIZE = 7
COL_MTIME = 8

SRC_CHECK, SRC_NAME, SRC_SIZE_TEXT, SRC_TYPE, SRC_MTIME_TEXT, \
    SRC_IS_DIR, SRC_PATH, SRC_SIZE, SRC_MTIME = (COL_CHECK, COL_NAME, COL_SIZE_TEXT,
                                                COL_TYPE, COL_MTIME_TEXT, COL_IS_DIR,
                                                COL_PATH, COL_SIZE, COL_MTIME)
DST_CHECK, DST_NAME, DST_SIZE_TEXT, DST_TYPE, DST_MTIME_TEXT, DST_IS_DIR, \
    DST_PATH, DST_SIZE, DST_MTIME = (COL_CHECK, COL_NAME, COL_SIZE_TEXT, COL_TYPE,
                                     COL_MTIME_TEXT, COL_IS_DIR, COL_PATH, COL_SIZE,
                                     COL_MTIME)

LOCAL_PROFILE = "This computer"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def sorted_by_dir(items):
    return sorted(items, key=lambda i: (not i["is_dir"], i["name"].lower()))


class ConnectionBar(Gtk.Box):
    """Generic per-pane connection toolbar. Callbacks:

      connect(bar)         — user asked to connect an SSH endpoint
      browse(bar)          — user asked to Browse a local folder
      disconnect(bar)      — user asked to disconnect
      open(bar)            — user asked to open the local folder in a manager
      scan(bar)            — user asked to rescan the LAN
      action(bar, action, *args) — e.g. ("profile", name), ("save", None)
    """

    def __init__(self, title, local_default=False, callbacks=None, show_open=False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.title = title
        callbacks = callbacks or {}
        self._on_connect = callbacks.get("connect")
        self._on_disconnect = callbacks.get("disconnect")
        self._on_browse = callbacks.get("browse")
        self._on_open = callbacks.get("open")
        self._on_scan = callbacks.get("scan") or (lambda *a: None)
        self._on_action = callbacks.get("action")
        self.conn = None
        self._has_open = bool(show_open and self._on_open)
        self.profiles = {}
        self._active_profile = None
        self._build(local_default)

    # -- build ---------------------------------------------------------------

    def _build(self, local_default):
        lbl = Gtk.Label(label=f"<b>{self.title}</b>", use_markup=True, xalign=0)
        self.pack_start(lbl, False, False, 0)

        self.profile_combo = Gtk.ComboBoxText()
        self.profile_combo.set_tooltip_text(
            "Saved profiles for this endpoint. The profile used here is "
            "remembered independently for the source and destination sides.")
        self.profile_combo.connect("changed", self._changed)
        self.pack_start(self.profile_combo, False, False, 0)

        save_btn = Gtk.Button(label="Save")
        save_btn.set_tooltip_text("Save the current connection as a named profile")
        save_btn.connect("clicked", lambda *_a: self._action("save", None))
        self.pack_start(save_btn, False, False, 0)

        self.type_combo = Gtk.ComboBoxText()
        self.type_combo.set_tooltip_text(
            "Endpoint type: 'This computer' (no server) or 'SSH' to a LAN host. "
            "Both source and destination can be either.")
        self.type_combo.append_text("This computer")
        self.type_combo.append_text("SSH")
        self.type_combo.set_active(1 if not local_default else 0)
        self.type_combo.connect("changed", lambda *_a: self._sync_ui())
        self.pack_start(self.type_combo, False, False, 0)

        self.conn_btn = Gtk.Button(label="Connect")
        self.conn_btn.set_tooltip_text(
            "Connect this endpoint. In 'This computer' mode the button is "
            "'Browse…' to pick a local folder.")
        self.conn_btn.connect("clicked", self._conn_clicked)
        self.pack_start(self.conn_btn, False, False, 0)

        self.ssh_box = Gtk.Box(spacing=4)
        self.ssh_box.pack_start(Gtk.Label(label="Host:"), False, False, 0)
        self.host_combo = Gtk.ComboBoxText.new_with_entry()
        self.host_combo.get_child().set_placeholder_text("host or IP")
        self.host_combo.get_child().set_width_chars(14)
        self.host_combo.set_tooltip_text(
            "SSH host. LAN-discovered hosts appear here automatically; you can "
            "also type an address or hostname by hand.")
        self.ssh_box.pack_start(self.host_combo, False, False, 0)
        scan_btn = Gtk.Button(label="↻")
        scan_btn.set_tooltip_text("Rescan the LAN for SSH hosts")
        scan_btn.connect("clicked", lambda *_a: self._on_scan(self))
        self.ssh_box.pack_start(scan_btn, False, False, 0)
        self.ssh_box.pack_start(Gtk.Label(label="Port:"), False, False, 0)
        self.port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port_spin.set_value(22)
        self.port_spin.set_tooltip_text("SSH port of the remote server (default 22)")
        self.ssh_box.pack_start(self.port_spin, False, False, 0)
        self.ssh_box.pack_start(Gtk.Label(label="User:"), False, False, 0)
        self.user_entry = Gtk.Entry()
        self.user_entry.set_width_chars(9)
        self.user_entry.set_tooltip_text("Username of the SSH account on that host")
        self.ssh_box.pack_start(self.user_entry, False, False, 0)
        self.ssh_box.pack_start(Gtk.Label(label="Password:"), False, False, 0)
        self.pass_entry = Gtk.Entry()
        self.pass_entry.set_visibility(False)
        self.pass_entry.set_width_chars(9)
        self.pass_entry.set_tooltip_text(
            "SSH password. Only stored on disk when 'Remember' is checked "
            "(plaintext in profiles.json).")
        self.ssh_box.pack_start(self.pass_entry, False, False, 0)
        self.remember = Gtk.CheckButton(label="Remember")
        self.remember.set_tooltip_text("Store the password with this profile (plaintext)")
        self.ssh_box.pack_start(self.remember, False, False, 0)
        self.pack_start(self.ssh_box, False, False, 0)

        self.status_lbl = Gtk.Label(label="Not connected", xalign=0)
        self.status_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.pack_start(self.status_lbl, True, True, 0)

        if self._has_open:
            self.open_btn = Gtk.Button(label="Open")
            self.open_btn.set_tooltip_text(
                "Open the current local folder in your file manager "
                "(only for 'This computer').")
            self.open_btn.connect("clicked", lambda *_a: self._on_open(self))
            self.pack_start(self.open_btn, False, False, 0)
        else:
            self.open_btn = None

        self._sync_ui()

    # -- public API ----------------------------------------------------------

    def is_local(self):
        return self.type_combo.get_active_text() == "This computer"

    def set_connecting(self, msg="Connecting…"):
        self.conn_btn.set_sensitive(False)
        self.status_lbl.set_text(msg)

    def set_connected(self, conn, label):
        self.conn = conn
        self.status_lbl.set_text(label)
        self._sync_ui()

    def set_disconnected(self, label="Not connected"):
        self.conn = None
        self.status_lbl.set_text(label)
        self._sync_ui()

    def apply_profile(self, p):
        """(Re)fill the auth widgets from a profile dict."""
        if self.is_local():
            return
        if p.get("host") is not None:
            self.host_combo.get_child().set_text(str(p.get("host", "")))
        try:
            port = int(p.get("port", 22))
        except (TypeError, ValueError):
            port = 22
        self.port_spin.set_value(port)
        self.user_entry.set_text(str(p.get("user", "")))
        pw = p.get("password") or ""
        self.pass_entry.set_text(pw)
        self.remember.set_active(bool(pw))

    def profile_payload(self):
        """A dict saving the current SSH widgets (host/port/user/password)."""
        return {
            "host": self.host_combo.get_child().get_text().strip(),
            "port": self.port_spin.get_value_as_int(),
            "user": self.user_entry.get_text().strip(),
            "password": self.pass_entry.get_text() if self.remember.get_active() else "",
        }

    def set_hosts(self, hosts):
        model = self.host_combo.get_model()
        existing = {model[i][0] for i in range(len(model))} if model is not None else set()
        current = self.host_combo.get_child().get_text()
        for h in hosts:
            if h and h not in existing:
                self.host_combo.append_text(h)
        if current:
            self.host_combo.get_child().set_text(current)

    def set_profile_list(self, profiles, active=None):
        self._active_profile = active
        self.profile_combo.handler_block_by_func(self._changed)
        try:
            self.profile_combo.remove_all()
            for name in profiles:
                self.profile_combo.append_text(name)
            if active is not None and active in profiles:
                self.profile_combo.set_active(list(profiles).index(active))
            elif profiles:
                self.profile_combo.set_active(0)
        finally:
            self.profile_combo.handler_unblock_by_func(self._changed)

    @property
    def active_profile(self):
        return self.profile_combo.get_active_text()

    # -- internal -----------------------------------------------------------

    def _changed(self, *_a):
        name = self.active_profile
        self._active_profile = name
        self._action("profile", name)

    def _action(self, name, value):
        if self._on_action:
            self._on_action(self, name, value)

    def _conn_clicked(self, *_a):
        if self.conn is not None:
            if self._on_disconnect:
                self._on_disconnect(self)
            return
        if self.is_local():
            if self._on_browse:
                self._on_browse(self)
            return
        if self._on_connect:
            self._on_connect(self)

    def _sync_ui(self):
        local = self.is_local()
        for w in self.ssh_box.get_children():
            w.set_visible(not local)
        connected = self.conn is not None
        self.conn_btn.set_sensitive(True)
        if connected:
            self.conn_btn.set_label("Disconnect")
        elif local:
            self.conn_btn.set_label("Browse…")
        else:
            self.conn_btn.set_label("Connect")
        if self.open_btn is not None:
            self.open_btn.set_sensitive(local and connected)


class BrowserPane(Gtk.Box):
    """Symmetric file-browsing pane for one endpoint.
    """