"""DirPane: the compact, pure-view file tree shared by the SOURCE and the
DESTINATION panels.

Everything here is a view: it holds the model, the checkbox selection, and the
comparison-state text. It has no connection or path-bar responsibilities — the
merged per-side action bar (`endpoint.EndpointBar`) owns the path entry, the
navigation buttons, and the file actions, and the window supplies async loaders
and fills items through `set_items`.

Callbacks:
    navigate(pane, where, path=None)   where in {'goto'}; double-click on a dir
                                       passes its full path so the window loads it
    selection_changed()                any checkbox change (window rebuilds tabs)
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GObject, Pango
import re


# Model columns (named; never hardcode a bare integer).
COL_CHECK = 0
COL_NAME = 1
COL_SIZE_TEXT = 2
COL_TYPE = 3
COL_MTIME_TEXT = 4
COL_IS_DIR = 5
COL_PATH = 6
COL_SIZE = 7
COL_MTIME = 8

# The "State" header column reuses sort id 0 (the checkbox column) with a
# custom sort func — the checkbox header itself is not click-sortable.
COL_STATE_SORT = 0

STATE_PRIO = {"missing": 0, "differ": 1, "conflict": 2, "same": 3, "extra": 4}

_STATE_COLOR = {
    "missing": "#c62828", "differ": "#ef6c00", "conflict": "#ad1457",
    "same": "#2e7d32", "extra": "#1565c0",
}


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def natural_key(name):
    return tuple((1, int(part)) if part.isdigit() else (0, part.lower())
                 for part in re.split(r"(\d+)", name))


class DirPane(Gtk.Box):
    def __init__(self, callbacks=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        callbacks = callbacks or {}
        self.navigate_cb = callbacks.get("navigate")
        self.selection_cb = callbacks.get("selection_changed")

        self.selected = {}      # full path -> {"name","is_dir"}
        self.meta = {}          # name -> {"is_dir","size"}
        self.states = {}        # name -> state keyword
        self._state_colors = dict(_STATE_COLOR)

        self._build_model()

    # -- construction -------------------------------------------------------

    def _build_model(self):
        self.model = Gtk.ListStore(
            bool, str, str, str, str, bool, str,
            GObject.TYPE_INT64, GObject.TYPE_INT64)
        self.filter = self.model.filter_new()
        self._filter_text = ""
        self.filter.set_visible_func(self._filter_visible)
        self.sort = Gtk.TreeModelSort(model=self.filter)
        self.tree = Gtk.TreeView(model=self.sort)
        self.tree.set_tooltip_column(COL_PATH)

        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_toggled)
        self.tree.append_column(Gtk.TreeViewColumn("", toggle, active=COL_CHECK))

        nr = Gtk.CellRendererText()
        nr.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_col = Gtk.TreeViewColumn("Name", nr)
        name_col.set_cell_data_func(nr, self._name_cb)
        name_col.set_expand(True)
        name_col.set_sort_column_id(COL_NAME)
        self.tree.append_column(name_col)

        size_col = Gtk.TreeViewColumn("Size", Gtk.CellRendererText(), text=COL_SIZE_TEXT)
        size_col.set_sort_column_id(COL_SIZE_TEXT)
        self.tree.append_column(size_col)

        type_col = Gtk.TreeViewColumn("Type", Gtk.CellRendererText(), text=COL_TYPE)
        type_col.set_sort_column_id(COL_TYPE)
        self.tree.append_column(type_col)

        mtime_col = Gtk.TreeViewColumn("Modified", Gtk.CellRendererText(), text=COL_MTIME_TEXT)
        mtime_col.set_sort_column_id(COL_MTIME_TEXT)
        self.tree.append_column(mtime_col)

        sr = Gtk.CellRendererText()
        st_col = Gtk.TreeViewColumn("State", sr)
        st_col.set_cell_data_func(sr, self._state_cb)
        st_col.set_sort_column_id(COL_STATE_SORT)
        self.tree.append_column(st_col)

        for cid in (COL_NAME, COL_SIZE_TEXT, COL_TYPE, COL_MTIME_TEXT):
            self.sort.set_sort_func(
                cid, lambda m, a, b, _d=None, c=cid: self._sort_by(m, a, b, c))
        self.sort.set_sort_func(COL_STATE_SORT, self._sort_state)
        self.tree.connect("row-activated", self._on_row_activated)

        # The filter + quick-select row sits above the tree; the summary label
        # below it.
        self.controls = self._build_controls()
        self.pack_start(self.controls, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tree)
        self.pack_start(sw, True, True, 0)

        self.summary = Gtk.Label(label="", xalign=0)
        self.pack_start(self.summary, False, False, 0)

    def _build_controls(self):
        box = Gtk.Box(spacing=4)
        box.pack_start(Gtk.Label(label="Filter:"), False, False, 0)
        self.filter_entry = Gtk.Entry()
        self.filter_entry.set_hexpand(True)
        self.filter_entry.set_placeholder_text("filter file names…")
        self.filter_entry.connect("changed", self._on_filter_changed)
        box.pack_start(self.filter_entry, True, True, 0)

        self.select_all_btn = Gtk.ToggleButton(label="Select all")
        self.select_all_btn.connect("toggled", self._on_select_all)
        box.pack_start(self.select_all_btn, False, False, 0)

        self.select_missing_btn = Gtk.Button(label="Missing")
        self.select_missing_btn.set_tooltip_text("Check items missing from the other side")
        self.select_missing_btn.connect("clicked", lambda b: self._select_pred(
            lambda n: self.states.get(n) == "missing"))
        box.pack_start(self.select_missing_btn, False, False, 0)

        self.select_changed_btn = Gtk.Button(label="Changed")
        self.select_changed_btn.set_tooltip_text("Check items missing / size-differing / conflicted")
        self.select_changed_btn.connect("clicked", lambda b: self._select_pred(
            lambda n: self.states.get(n) in ("missing", "differ", "conflict")))
        box.pack_start(self.select_changed_btn, False, False, 0)

        self.invert_btn = Gtk.Button(label="Invert")
        self.invert_btn.set_tooltip_text("Flip checked/unchecked for visible items")
        self.invert_btn.connect("clicked", lambda b: self._on_invert())
        box.pack_start(self.invert_btn, False, False, 0)

        self.folders_btn = Gtk.Button(label="Folders")
        self.folders_btn.connect("clicked", lambda b: self._select_pred(self._is_dir_name))
        box.pack_start(self.folders_btn, False, False, 0)

        self.files_btn = Gtk.Button(label="Files")
        self.files_btn.connect("clicked", lambda b: self._select_pred(lambda n: not self._is_dir_name(n)))
        box.pack_start(self.files_btn, False, False, 0)
        return box

    def set_controls_visible(self, visible):
        self.controls.set_visible(visible)

    # -- data population ----------------------------------------------------

    def set_items(self, items, base_path):
        prefix = base_path.rstrip("/") + "/" if base_path != "/" else "/"
        prev_selected = set(self.selected)
        self.model.clear()
        self.meta.clear()
        self.selected = {}
        for it in sorted(items, key=lambda i: (not i["is_dir"], i["name"].lower())):
            full = prefix + it["name"]
            self.meta[it["name"]] = {"is_dir": it["is_dir"], "size": it["size"]}
            self.model.append([
                False, it["name"],
                "—" if it["is_dir"] else human_size(it.get("size", 0)),
                "Folder" if it["is_dir"] else ("Link" if it.get("is_link") else "File"),
                it.get("mtime", ""), it["is_dir"], full,
                it.get("size", 0), int(it.get("mtime_epoch", 0) or 0)])
        if prev_selected:
            for row in self.model:
                if row[COL_PATH] in prev_selected:
                    self._restore_check(row)
        self._sync_select_all()

    def clear(self):
        """Drop every row, the selection, states, and summary text."""
        self.model.clear()
        self.meta.clear()
        self.states.clear()
        self.selected = {}
        self.summary.set_text("")
        self._sync_select_all()

    def set_state_colors(self, colors):
        self._state_colors = dict(colors or _STATE_COLOR)
        self.tree.queue_draw()

    def set_states(self, states):
        self.states = states or {}
        self.tree.queue_draw()

    def set_summary(self, text):
        self.summary.set_text(text)

    # -- renderers ----------------------------------------------------------

    def _apply_fg(self, cell, name):
        state = self.states.get(name)
        if state in self._state_colors:
            cell.set_property("foreground", self._state_colors[state])
            cell.set_property("foreground-set", True)
        else:
            cell.set_property("foreground-set", False)

    def _name_cb(self, col, cell, model, it, *a):
        name = model.get_value(it, COL_NAME)
        cell.set_property("text", name)
        self._apply_fg(cell, name)

    def _state_cb(self, col, cell, model, it, *a):
        name = model.get_value(it, COL_NAME)
        state = self.states.get(name)
        if not state or state not in self._state_colors:
            cell.set_property("text", "")
            cell.set_property("foreground-set", False)
            return
        cell.set_property("text", state)
        cell.set_property("foreground", self._state_colors[state])
        cell.set_property("foreground-set", True)

    # -- sorting ------------------------------------------------------------

    def _sort_by(self, model, a, b, kind):
        da = bool(model.get_value(a, COL_IS_DIR))
        db = bool(model.get_value(b, COL_IS_DIR))
        order = self.sort.get_sort_column_id()[1]
        if da != db:
            dirs_first = order != Gtk.SortType.DESCENDING
            return -1 if da == dirs_first else 1
        na = model.get_value(a, COL_NAME)
        nb = model.get_value(b, COL_NAME)
        if kind == COL_SIZE_TEXT:
            ka, kb = model.get_value(a, COL_SIZE), model.get_value(b, COL_SIZE)
        elif kind == COL_TYPE:
            ka, kb = model.get_value(a, COL_TYPE), model.get_value(b, COL_TYPE)
        elif kind == COL_MTIME_TEXT:
            ka, kb = model.get_value(a, COL_MTIME), model.get_value(b, COL_MTIME)
        else:
            ka, kb = na, nb
        if ka != kb:
            return -1 if ka < kb else 1
        ta, tb = natural_key(na), natural_key(nb)
        if ta != tb:
            return -1 if ta < tb else 1
        return 0

    def _sort_state(self, model, a, b, *x):
        na = model.get_value(a, COL_NAME)
        nb = model.get_value(b, COL_NAME)
        pa = STATE_PRIO.get(self.states.get(na), 9)
        pb = STATE_PRIO.get(self.states.get(nb), 9)
        if pa != pb:
            return -1 if pa < pb else 1
        return -1 if na.lower() < nb.lower() else (1 if na.lower() > nb.lower() else 0)

    # -- selection ----------------------------------------------------------

    def _child_path(self, path):
        if isinstance(path, str):
            path = Gtk.TreePath.new_from_string(path)
        c = self.sort.convert_path_to_child_path(path)
        if c is not None:
            path = c
        c = self.filter.convert_path_to_child_path(path)
        return c if c is not None else path

    def _visible_rows(self):
        for i, row in enumerate(self.model):
            it = self.model.get_iter((i,))
            if it is not None and self.filter.convert_child_iter_to_iter(it) is not None:
                yield row

    def _check(self, row, on):
        full = row[COL_PATH]
        row[COL_CHECK] = on
        if on:
            self.selected[full] = {"name": row[COL_NAME], "is_dir": row[COL_IS_DIR]}
        else:
            self.selected.pop(full, None)
        self._notify_selection()

    def _restore_check(self, row):
        """Re-add a row to `selected` without re-notifying (used during a reload
        repopulation; the window refreshes the Selected tab anyway)."""
        row[COL_CHECK] = True
        self.selected[row[COL_PATH]] = {"name": row[COL_NAME], "is_dir": row[COL_IS_DIR]}

    def _notify_selection(self):
        if self.selection_cb:
            try:
                self.selection_cb()
            except Exception:
                pass

    def _on_toggled(self, renderer, path):
        row = self.model[self._child_path(path)]
        self._check(row, not bool(row[COL_CHECK]))

    def _on_select_all(self, btn):
        on = bool(btn.get_active())
        for row in self.model:
            row[COL_CHECK] = on
            if on:
                self.selected[row[COL_PATH]] = {"name": row[COL_NAME], "is_dir": row[COL_IS_DIR]}
            else:
                self.selected.pop(row[COL_PATH], None)
        self._notify_selection()
        self._sync_select_all()

    def _sync_select_all(self):
        all_on = bool(len(self.model)) and all(r[COL_CHECK] for r in self.model)
        if all_on != bool(self.select_all_btn.get_active()):
            hid = self.select_all_btn.handler_block_by_func(self._on_select_all)
            try:
                self.select_all_btn.set_active(all_on)
            finally:
                self.select_all_btn.handler_unblock_by_func(self._on_select_all)

    def _on_invert(self):
        for row in self._visible_rows():
            self._check(row, not bool(row[COL_CHECK]))
        self._sync_select_all()

    def _select_pred(self, pred):
        for row in self._visible_rows():
            if pred(row[COL_NAME]):
                self._check(row, True)
        self._sync_select_all()

    def _is_dir_name(self, name):
        info = self.meta.get(name)
        return bool(info and info["is_dir"])

    # -- filter / nav -------------------------------------------------------

    def _filter_visible(self, model, it, *a):
        if not self._filter_text:
            return True
        return self._filter_text in model.get_value(it, COL_NAME).lower()

    def _on_filter_changed(self, entry):
        self._filter_text = entry.get_text().strip().lower()
        self.filter.refilter()

    def _on_row_activated(self, tree, path, col):
        row = self.model[self._child_path(path)]
        if bool(row[COL_IS_DIR]) and self.navigate_cb:
            self.navigate_cb(self, "goto", row[COL_PATH])