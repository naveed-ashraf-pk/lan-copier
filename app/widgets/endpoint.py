"""EndpointBar: the merged single-row bar for one side (SOURCE / DESTINATION).

Layout (disconnected):
    [TITLE]  Not connected        [Connect…]

Layout (connecting):
    [TITLE]  Connecting…          [Connecting…]   (button disabled)

Layout (connected — path + actions appear in the same row):
    [TITLE]  This computer  [Connected]  [/path/folder]  [📂][⬆][⌂][↻] [↗][⤓][🗑 (N)]
    [TITLE]  bob@macbook    [Connected]  [/home/bob]     [⬆][⌂][↻]    [⤓][🗑 (N)]

The endpoint is a display-only label (no dropdown): connecting / changing a
connection always goes through the popup dialog, opened by the single
`conn_btn`. That button turns green (theme `suggested-action`) when connected.
The "📂 pick folder" (file chooser) and "↗ open in file manager" actions are
only shown for a connected local (This computer) endpoint; they appear on both
sides. The pick-folder action is the first button after the path entry so
users can pick a folder instead of typing it.

This is a pure view: it emits callbacks and never talks to the network. The
window owns the connection state machine and feeds label/state in.
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango


class EndpointBar(Gtk.Box):
    """Merged per-side tool bar. Callbacks (receive the bar):
        connect(bar)             primary button → open the popup dialog
        disconnect(bar)          user asked to disconnect (from the dialog)
        navigate(bar, where, path=None)   up/home/refresh/goto
        open(bar)                📂 open folder in the file manager (local only)
        browse(bar)              📁 pick a local folder with a file chooser (local only)
        export(bar)              ⤓ export this tree to YAML
        delete(bar)              🗑 delete the checked items
    """

    _GREEN_CLASS = "suggested-action"

    def __init__(self, title, callbacks=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        callbacks = callbacks or {}
        self._connect = callbacks.get("connect")
        self._navigate = callbacks.get("navigate")
        self._open = callbacks.get("open")
        self._browse = callbacks.get("browse")
        self._export = callbacks.get("export")
        self._delete = callbacks.get("delete")
        self.conn = None
        self._local_connected = False
        self.browse_btn = None
        self._build(title)
        self.set_disconnected()

    def _build(self, title):
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(f"<b>{title}</b>")
        self.pack_start(lbl, False, False, 0)

        self.conn_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.pack_start(self.conn_label, False, False, 0)

        self.conn_btn = Gtk.Button(label="Connect…")
        self.conn_btn.set_tooltip_text(
            "Connect this side, or change which machine/path this side points to.")
        self.conn_btn.connect("clicked", self._on_connect)
        self.pack_start(self.conn_btn, False, False, 0)

        # --- connected-only row (path + navigation + actions) ---------------
        self.row = Gtk.Box(spacing=4)
        self.path_entry = Gtk.Entry()
        self.path_entry.set_hexpand(True)
        self.path_entry.set_placeholder_text("path (Enter to navigate)")
        self.path_entry.set_width_chars(18)
        self.path_entry.connect("activate", lambda e: self._nav("goto"))
        self.row.pack_start(self.path_entry, True, True, 0)

        self.browse_btn = Gtk.Button(label="📂")
        self.browse_btn.set_tooltip_text("Pick a folder on this computer with a file chooser")
        self.browse_btn.connect("clicked", lambda b: self._emit(self._browse, self))
        self.row.pack_start(self.browse_btn, False, False, 0)

        self.up_btn = Gtk.Button(label="⬆")
        self.up_btn.set_tooltip_text("Go up one level")
        self.up_btn.connect("clicked", lambda b: self._nav("up"))
        self.row.pack_start(self.up_btn, False, False, 0)

        self.home_btn = Gtk.Button(label="⌂")
        self.home_btn.set_tooltip_text("Go to the home folder")
        self.home_btn.connect("clicked", lambda b: self._nav("home"))
        self.row.pack_start(self.home_btn, False, False, 0)

        self.refresh_btn = Gtk.Button(label="↻")
        self.refresh_btn.set_tooltip_text("Refresh this folder")
        self.refresh_btn.connect("clicked", lambda b: self._nav("refresh"))
        self.row.pack_start(self.refresh_btn, False, False, 0)

        self.open_btn = Gtk.Button(label="↗")
        self.open_btn.set_tooltip_text("Open this folder in your file manager")
        self.open_btn.connect("clicked", lambda b: self._emit(self._open, self))
        self.row.pack_start(self.open_btn, False, False, 0)

        self.export_btn = Gtk.Button(label="⤓")
        self.export_btn.set_tooltip_text("Export this tree to a YAML manifest")
        self.export_btn.connect("clicked", lambda b: self._emit(self._export, self))
        self.row.pack_start(self.export_btn, False, False, 0)

        self.delete_btn = Gtk.Button(label="🗑 Delete (0)")
        self.delete_btn.get_style_context().add_class("destructive-action")
        self.delete_btn.set_tooltip_text("Delete the checked items")
        self.delete_btn.set_sensitive(False)
        self.delete_btn.connect("clicked", lambda b: self._emit(self._delete, self))
        self.row.pack_start(self.delete_btn, False, False, 0)

        self.pack_start(self.row, True, True, 0)

    @staticmethod
    def _emit(fn, *args):
        if callable(fn):
            fn(*args)

    def _on_connect(self, *_):
        if callable(self._connect):
            self._connect(self)

    # -- public -------------------------------------------------------------

    def set_connecting(self, msg="Connecting…"):
        self.conn_label.set_text(msg)
        self.conn_btn.set_label(msg)
        self.conn_btn.set_sensitive(False)
        self._drop_green()

    def set_connected(self, conn, label, is_local=False):
        self.conn = conn
        self._local_connected = bool(is_local)
        self.conn_label.set_text(label)
        self.conn_btn.set_label("Connected")
        self.conn_btn.set_tooltip_text("Change the connection for this side…")
        self.conn_btn.set_sensitive(True)
        self._make_green()
        self.row.set_visible(True)
        self.open_btn.set_visible(bool(is_local))
        self.browse_btn.set_visible(bool(is_local))

    def set_disconnected(self, label="Not connected"):
        self.conn = None
        self._local_connected = False
        self.conn_label.set_text(label)
        self.conn_btn.set_label("Connect…")
        self.conn_btn.set_tooltip_text("Connect this side, or change which "
                                       "machine/path this side points to.")
        self.conn_btn.set_sensitive(True)
        self._drop_green()
        self.row.set_visible(False)
        self.open_btn.set_visible(False)
        self.browse_btn.set_visible(False)

    def set_path(self, text):
        self.path_entry.set_text(text or "")

    def set_delete_count(self, n, enabled=True):
        self.delete_btn.set_label(f"🗑 Delete ({n})")
        self.delete_btn.set_sensitive(bool(enabled) and n > 0)

    def set_export_sensitive(self, sensitive):
        self.export_btn.set_sensitive(bool(sensitive))

    def set_status_text(self, label):
        """Set the connected label (used by loading helpers that want to show
        "— <current path>"). Keeps the green button."""
        if self.conn is not None:
            self._make_green()
        self.conn_label.set_text(label)

    # -- internal -----------------------------------------------------------

    def _make_green(self):
        self.conn_btn.get_style_context().add_class(self._GREEN_CLASS)

    def _drop_green(self):
        self.conn_btn.get_style_context().remove_class(self._GREEN_CLASS)

    def _nav(self, where, path=None):
        if self._navigate:
            self._navigate(self, where, path)