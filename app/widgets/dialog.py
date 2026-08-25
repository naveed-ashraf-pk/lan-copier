"""ConnectionDialog: the modal endpoint editor — the single place to choose and
connect either side.

A clean dialog for choosing *what* a source/destination points at:
  - "This computer"  → local folder (SSH fields dimmed)
  - a saved profile  → prefilled SSH endpoint
  - "+ New…"         → blank SSH endpoint (host, port, user, password, remember,
                       save-as name) with live LAN discovery

When the side is already connected the dialog also offers a Disconnect action.

Pure widget: it emits no I/O — the window wires discovery via
`set_on_discover`. `run()` the dialog, then call `collect()` after OK.

collect() → dict:
    {"mode": "disconnect"}                       when the user chose Disconnect
    {"mode": "local"}                            "This computer" → folder picker
    {"mode": "ssh", "params": {host, port, user,
                                password, remember, name}}      an SSH endpoint
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import app.profiles as profiles

NEW_ROW = "+ New connection…"

RESP_DISCONNECT = 2


class ConnectionDialog(Gtk.Dialog):
    def __init__(self, parent, title, hosts=(), profiles_data=None, initial=None,
                 name=None, connected=False, mode="ssh"):
        super().__init__(title=title, transient_for=parent, modal=True,
                         destroy_with_parent=True)
        initial = initial or {}
        self.set_default_size(480, -1)
        self._on_discover = None
        self._profiles = profiles_data or {}
        self._holding = False
        self._fields = {}          # combo kind -> {"host","port","user",...}
        self._ssh_boxes = []
        self._kind = None
        self._initial_mode = mode
        self._build(hosts, initial, name, connected)

    def _build(self, hosts, initial, name, connected):
        box = self.get_content_area()
        box.set_spacing(4)
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)

        grid.attach(self._lbl("Connection"), 0, 0, 1, 1)
        self.kind = Gtk.ComboBoxText()
        self.kind.append_text(profiles.THIS)            # index 0
        for pname in self._profiles:
            self.kind.append_text(pname)
        if name is None:
            self.kind.append_text(NEW_ROW)
        self.kind.connect("changed", self._on_kind_changed)
        grid.attach(self.kind, 1, 0, 1, 1)

        grid.attach(self._lbl("Host"), 0, 1, 1, 1)
        host_box = Gtk.Box(spacing=4)
        self.host = Gtk.ComboBoxText.new_with_entry()
        self.host.set_entry_text_column(0)
        for h in hosts:
            if h:
                self.host.append_text(str(h))
        self.host.get_child().set_placeholder_text("host or IP")
        self.host.get_child().set_width_chars(26)
        self.host.set_tooltip_text(
            "SSH host or IP. Discovered hosts appear in the dropdown.")
        host_box.pack_start(self.host, True, True, 0)
        self.discover = Gtk.Button(label="↻")
        self.discover.set_tooltip_text("Rescan the LAN for SSH hosts")
        self.discover.connect("clicked", self._discover_clicked)
        host_box.pack_start(self.discover, False, False, 0)
        grid.attach(host_box, 1, 1, 1, 1)
        self._ssh_boxes.append(host_box)

        grid.attach(self._lbl("Port"), 0, 2, 1, 1)
        self.port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port.set_value(22)
        self.port.set_tooltip_text("SSH port (default 22)")
        grid.attach(self.port, 1, 2, 1, 1)
        self._ssh_boxes.append(grid)

        grid.attach(self._lbl("User"), 0, 3, 1, 1)
        self.user = Gtk.Entry()
        self.user.set_placeholder_text("username")
        self.user.set_width_chars(26)
        self.user.set_tooltip_text("SSH account username on the remote host")
        grid.attach(self.user, 1, 3, 1, 1)
        self._ssh_boxes.append(grid)

        grid.attach(self._lbl("Password"), 0, 4, 1, 1)
        pw_box = Gtk.Box(spacing=4)
        self.password = Gtk.Entry()
        self.password.set_visibility(False)
        self.password.set_width_chars(20)
        pw_box.pack_start(self.password, True, True, 0)
        self.reveal = Gtk.CheckButton(label="Show")
        self.reveal.connect("toggled",
                            lambda b: self.password.set_visibility(b.get_active()))
        pw_box.pack_start(self.reveal, False, False, 0)
        grid.attach(pw_box, 1, 4, 1, 1)
        self._ssh_boxes.append(pw_box)

        if initial.get("password"):
            self.password.set_text(str(initial["password"]))
            self.remember = Gtk.CheckButton(label="Update saved password on this computer")
        else:
            self.remember = Gtk.CheckButton(label="Remember password on this computer")
        self.remember.set_tooltip_text(
            "Store the password in the local profile (plaintext). Unchecked "
            "connections are never persisted beyond the session.")
        self.remember.set_active(bool(initial.get("remember")))
        note = Gtk.Label(label="(plaintext on disk)", xalign=0)
        note.get_style_context().add_class("dim-label")
        row = Gtk.Box(spacing=4)
        row.pack_start(self.remember, False, False, 0)
        row.pack_start(note, False, False, 0)
        grid.attach(row, 1, 5, 1, 1)
        self._ssh_boxes.append(row)

        grid.attach(self._lbl("Save as"), 0, 6, 1, 1)
        self.name = Gtk.Entry()
        self.name.set_width_chars(26)
        prefill = name or f"{initial.get('user', '')}@{initial.get('host', '')}"
        self.name.set_text(str(initial.get("name", "")) or prefill)
        self.name.set_tooltip_text(
            "Profile name. The connection is saved under this name for the "
            "current side after it connects successfully.")
        grid.attach(self.name, 1, 6, 1, 1)
        self._ssh_boxes.append(grid)

        box.pack_start(grid, True, True, 0)
        note = Gtk.Label(label="The connection is saved automatically when it connects.",
                         xalign=0)
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        if connected:
            self.add_button("Disconnect", RESP_DISCONNECT)
        self.add_button("Connect", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        # Select the row matching the current/requested state, then apply it.
        self._select_initial(initial, name, connected)
        self.show_all()

    # -- kind switching -----------------------------------------------------

    def _select_initial(self, initial, name, connected):
        """Figure out which combo row to activate and prefill its fields."""
        local = self._initial_mode == "local" or (
            connected and not (initial.get("host") or name in self._profiles))
        if local:
            self._set_kind_active(profiles.THIS)
            return
        if name in self._profiles:
            self._set_kind_active(name)
            return
        if name is not None and name != profiles.THIS:
            # connected to a profile not in the current list: fall to New with
            # its values preserved so the user can re-save it.
            self._fields[NEW_ROW] = {
                "host": initial.get("host", ""), "port": initial.get("port", 22),
                "user": initial.get("user", ""), "password": initial.get("password", ""),
                "remember": initial.get("remember", False), "name": name,
            }
            self._append_new_row()
            self._set_kind_active(NEW_ROW)
            return
        self._append_new_row()
        self._set_kind_active(NEW_ROW)

    def _append_new_row(self):
        cur = [self.kind.get_model()[i][0] for i in range(len(self.kind.get_model()))]
        if NEW_ROW not in cur:
            self.kind.append_text(NEW_ROW)

    def _set_kind_active(self, text):
        model = self.kind.get_model()
        for i in range(len(model)):
            if model[i][0] == text:
                self._holding = True
                try:
                    self.kind.set_active(i)
                finally:
                    self._holding = False
                self._apply_kind(text)

    def _on_kind_changed(self, combo):
        if self._holding:
            return
        self._apply_kind(combo.get_active_text())

    def _apply_kind(self, kind):
        if self._kind is not None and self._kind != kind:
            self._save_fields(self._kind)
        self._kind = kind
        local = kind in (None, profiles.THIS)
        if local:
            f = self._fields.get(profiles.THIS) or {}
        elif kind == NEW_ROW:
            f = self._fields.get(NEW_ROW) or {}
        else:
            f = self._fields.get(kind) or self._profiles.get(kind) or {}
        self._set_fields(f)
        if not local and kind != NEW_ROW and not self.name.get_text():
            # A saved profile has no stored name; default the Save-as row to it.
            self.name.set_text(kind)
        for w in self._ssh_boxes:
            w.set_sensitive(not local)
        self.host.set_sensitive(not local)
        self.discover.set_sensitive(not local)
        if local:
            self.name.set_text(profiles.THIS)

    def _save_fields(self, kind):
        self._fields[kind] = {
            "host": self.host.get_child().get_text(),
            "port": self.port.get_value_as_int(),
            "user": self.user.get_text(),
            "password": self.password.get_text(),
            "remember": self.remember.get_active(),
            "name": self.name.get_text(),
        }

    def _set_fields(self, f):
        f = f or {}
        self.host.get_child().set_text(str(f.get("host", "")))
        self.port.set_value(int(f.get("port", 22) or 22))
        self.user.set_text(str(f.get("user", "")))
        self.password.set_text(str(f.get("password", "")))
        self.remember.set_active(bool(f.get("remember")))
        self.name.set_text(str(f.get("name", "")))

    @staticmethod
    def _lbl(text):
        lbl = Gtk.Label(label=text, xalign=0)
        if text:
            lbl.set_markup(f"<b>{text}</b>")
        return lbl

    # -- discovery ----------------------------------------------------------

    def _discover_clicked(self, *_a):
        if callable(self._on_discover):
            self._on_discover(self)

    def set_on_discover(self, fn):
        """fn(dialog) — ask the window to scan the LAN and call set_hosts."""
        self._on_discover = fn or (lambda *a: None)

    def set_hosts(self, hosts):
        model = self.host.get_model()
        existing = {model[i][0] for i in range(len(model))} if model else set()
        for h in hosts:
            if h and h not in existing:
                self.host.append_text(str(h))

    # -- result -------------------------------------------------------------

    def collect(self):
        kind = self._kind or self.kind.get_active_text()
        if kind == profiles.THIS:
            return {"mode": "local"}
        if kind == NEW_ROW:
            self._save_fields(NEW_ROW)
            f = self._fields[NEW_ROW]
            if not f.get("host") or not f.get("user"):
                return None
            return {"mode": "ssh", "params": self._params_from(f)}
        f = self._profiles.get(kind) or {}
        self._fields[kind] = {
            "host": self.host.get_child().get_text(), "port": self.port.get_value_as_int(),
            "user": self.user.get_text(), "password": self.password.get_text(),
            "remember": self.remember.get_active(), "name": self.name.get_text(),
        }
        if not self._fields[kind]["host"] or not self._fields[kind]["user"]:
            return None
        return {"mode": "ssh", "params": self._params_from(self._fields[kind])}

    @staticmethod
    def _params_from(f):
        return {
            "host": f["host"].strip(),
            "port": f["port"],
            "user": f["user"].strip(),
            "password": f["password"] if f["remember"] else "",
            "remember": bool(f["remember"]),
            "name": f.get("name", "").strip(),
        }