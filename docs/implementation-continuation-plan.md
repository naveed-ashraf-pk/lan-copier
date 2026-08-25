# lan-copier — Implementation Plan + Session Handoff (compacted)

**Date:** 2026-08-25 (session 7: ⇄ side swap + export snapshot fixes; sessions 5–6 history below)
**Status:** clean reimplementation of the UI layer is DONE and green. Legacy `ui.py`/`panes.py`/`profiles.py`/`tests/test_ui.py` have been **deleted** and backed up under `docs/old/legacy-ui/`. **Session 5** fixed duplicate-profile stacking (identity is the resolved remote hostname, schema v3). **Session 6** simplified the bars: each side is a single `EndpointBar` row *inside* its own side column above the `DirPane` (a display-only endpoint label + one popup-driven `conn_btn` that turns green when connected — no dropdown/edit/disconnect buttons), the `ConnectionDialog` popup is now the single place to pick This computer / a saved profile / a new SSH connection and to disconnect, and the DirPane filter/quick-select row moved back **above** the tree. **Session 7** added the ⇄ side swap (endpoint sessions exchange wholesale, see §2.2d) and fixed the export worker to use its snapshot connection. This doc is the **single authoritative handoff**: read it in any new session, then `python3 tests.py` to verify the baseline. Everything below is self-contained.

**Sibling docs:** `symmetric-endpoints-feature-plan.md` (original feature spec, §§1–15), `architecture-and-developer-guide.md`, `AGENTS.md`.

---

## 0. TL;DR — one paragraph

`lan-copier` talks symmetrically between THIS Computer / SSH-Linux-macOS / SSH-Windows for file management (browse, compare, select, transfer, delete, export). The big symmetric feature (transport, transfer engine, move, command builders) is implemented and green. The legacy 2774-line `ui.py` monolith was thrown away and reimplemented cleanly into the `app/` package (`profiles` v3, `widgets/` endpoint bar + dialog + dirpane, `window` AppWindow). **The legacy files are now deleted** and archived under `docs/old/legacy-ui/`. `main.py` launches `AppWindow` from `app/window.py`. Full suite is green.

---

## 1. Current state — verified green this session

Run `python3 tests.py` → **ALL TESTS PASSED** (12 unit test fns + `app smoke` + `app remote-dest smoke`).

### 1.1 Repository layout (clean, post-cleanup)
```
main.py                      → from app.window import AppWindow (launches the UI)
app/
  __init__.py
  profiles.py                profiles schema v3 (import app.profiles as profiles)
  widgets/
    endpoint.py              EndpointBar (merged single-row bar + status dot)
    dialog.py                ConnectionDialog
    dirpane.py               DirPane (tree-only browse panel)
  window.py                  AppWindow(Gtk.Window) — composes the bars/panes + engine
ssh_transport.py             SSHConnection (kind="ssh") — transport
local_transport.py           LocalConnection(kind="local")
transfer_engine.py           symmetric transfer engine + move()
commands/                    posix / powershell / local / paths builders
discovery.py, tree_exporter.py
tests.py                     root runner (GROUPS: commands/ssh/local/engine/export/app)
tests/
  test_commands.py test_ssh.py test_local.py test_engine.py test_export.py
  test_app.py                profiles v2 + classify + AppWindow smokes
config/
  profiles.json              (schema v2)
docs/
  implementation-continuation-plan.md   ← THIS FILE (handoff)
  symmetric-endpoints-feature-plan.md   ← original spec (cross-check at end)
  architecture-and-developer-guide.md
  old/legacy-ui/             DELETED legacy ui.py/panes.py/profiles.py/test_ui.py (archived)
  old/…                      historical feature-plan docs
```
There is **no `test_ui.py`** and no legacy UI modules anymore — the suite is 100% on `app/`.

### 1.2 What Phase A fixed (all still active in the new code)
- **kind = "ssh"** added to `SSHConnection` (ssh_transport.py:29). `_dest_ssh()` uses `getattr(...,"kind",None)=="ssh"`. Without it remote dest listing/transfer misrouted. (test in test_ssh + test_ui.)
- **PowerShell `list_dir`**: emits real TAB via double-quoted backtick string + `[Console]::OutputEncoding = UTF8`; root-path error now exits 1 (not silently empty). `_parse_ps_list` strips BOM/CRLF. (commands/powershell.py, ssh_transport.py.)
- **Dest navigation is endpoint-aware**: `_on_dest_up`/`_on_dest_home`/`_current_dest` use `conn.expand_remote` + `rp.dirname(path, family)` on remote. In the new window this logic is in `AppWindow._on_dest_navigate` / `_on_source_navigate`.
- `_dest_connected` no longer wipes the typed password / remember.
- Friendly error dialogs on failed remote listing.

### 1.3 The new architecture — read before touching UI
- **`app/profiles.py`** — schema v3:
  ```
  {"version":3,
   "profiles": {name: {host, port, user, hostname, password, remember}},
   "last": {source_profile, dest_profile}}
  ```
  "This computer" is a constant (`profiles.THIS`), never stored. **No v1 migration** — a legacy file loads as empty v3 (per decision). `hostname` = resolved remote machine name, the **primary profile identity** (`find_profile` matches hostname first, then `host+port+user`; `merge_duplicates` collapses legacy "name 2/3..." rows). Helpers: `_fresh()`, `load()`, `save()`, `names()`, `get()`, `remember_side()`, `find_profile()`, `merge_duplicates()`.
- **`app/widgets/`** —
  - `dialog.ConnectionDialog`: the **single popup** to choose/connect/change/disconnect either side — a connection combo (`This computer` / saved profiles / `+ New…`), SSH editor (host combo w/ discovery, port, user, password + "Show", remember "(plaintext on disk)", "Save as" prefilled `user@host`), and a **Disconnect** button when the side is already connected. `collect()` → `{mode:"local"|"ssh", params}` or `{mode:"disconnect"}`.
  - `endpoint.EndpointBar`: the **merged single-row bar** per side: `<TITLE> Not connected [Connect…]` — a display-only connection label + **one** green-capable `conn_btn` (no dropdown, no ✎, no separate connect/disconnect). Once connected: `[label] [Connected] [path] [⬆][⌂][↻] [📂 local][⤓][🗑 (N)]`. Clicking the button always opens the `ConnectionDialog`. Callbacks: connect(bar) / navigate(bar,where,path) / open / export / delete. Methods: `set_connecting/set_connected(conn,label,is_local)/set_disconnected`, `set_path`, `set_delete_count`, `set_export_sensitive`, `set_status_text`. The bar sits *inside* its side column above the `DirPane`, so the paned divider resizes bar + tree together.
  - `dirpane.DirPane`: the browse **tree** (no path bar). Filter + quick-select `Missing/Changed/Invert/Folders/Files` row sits **above** the tree; summary label below. Holds `model`/`filter`/`sort` TreeView, checkbox selection (`selected`), `meta`, `states`; methods `set_items/set_states/set_summary/clear/_check/_select_pred/set_controls_visible`. Column constants `COL_*`. Callbacks: navigate(pane,"goto",path), selection_changed.
- **`app/window.py`** — `AppWindow(Gtk.Window)` composes two side columns (each = one `EndpointBar` above a `DirPane`) in a Paned, a `Notebook` with Selected + Transfers + Log tabs, delete-progress stack page. Hosts ALL the engine: the single `_on_connect_clicked(bar)` opens the `ConnectionDialog` and routes local→folder-picker / ssh→`_do_connect` / disconnect, `_autosave_profile` keeps hostname-dedup (schema v3), plus export, delete-with-progress, transfer worker + gate + ticker + conflict dialog, sort sync, on-destroy drain. The recursive deep-compare is **removed** (only per-folder `classify_items` states + quick-select remain). `Transfer` model is local to window.py.

### 1.4 Decisions already locked (do not relitigate)
- Passwords plaintext only when "Remember" checked; tooltip warns.
- Per side: remember only the last profile + last path. **Drop last_selection restore.**
- New SSH connect → auto-save as profile (name prefilled `user@host`, editable in dialog), remembered for that side.
- No backward-compat for profiles v1 (throw away).
- Windows endpoints supported via PowerShell + tar.exe; symmetric primitives; remote-dest atomic (part→mv/merge).
- Move: fast-path rename same-endpoint; else copy+delete-on-success.
- Symlink safety (islink before isdir; strip trailing slash before rm), atomic parts everywhere, UNSORTED sort guard, INT64 sort keys — all invariants carried over verbatim.

---

## 2. Leftover work (open items) — what a future session must do

### 2.1 Wait-for-user / realistic-hardware validation (CANNOT automate here)
1. **Real-host pass on Mac + Windows** (user has real hosts). For each of:
   - Mac as SOURCE → local dest; Mac as DEST → local source.
   - Windows as SOURCE → dest; Windows as DEST → source.
   Run the app (`python3 main.py`), connect via the new dialog, then: browse, Up/Home, select+transfer a file AND a folder, compare, delete, export. Record failures → fix + loop.
2. **Confirm the new UI = done** → then the legacy files are deleted (see 2.3).

### 2.2 Code-level gaps fixed this session
Fixed (all active in the new code + regression-tested):
- **`DirPane.clear()` was missing** yet called by `AppWindow` on disconnect / failed listing (`_on_disconnect`/`_source_loaded`/`_dest_loaded`). It silently did nothing (Python attribute error was swallowed in the idle callback), leaving stale rows/selection/meta on-screen after a failed load or disconnect. Added `DirPane.clear()` (app/panels.py) to drop rows/selected/meta/states + blank the path bar + zero the delete count. Regression: `app smoke` now calls it.
- **`_do_connect` worker could raise** (`SSHConnection(...)` or `home_dir()` throwing) straight into the background thread, leaving the bar stuck "Connecting…" with no error dialog. Wrapped in try/except → synthesizes a conn with `last_error` → `_connected_side` shows the friendly dialog and resets the bar.
- **`_on_edit_connection` rename collision** — editing a profile to a name that already exists silently overwrote the target profile. Now dedupes with " Name 2" (mirrors `_autosave_profile`).
- **Tests**: `tests/test_app.py` gained an `app remote-dest smoke` (`SMOKE_REMOTE_DEST`) that drives a local→SSH-dest transfer through `AppWindow._worker`/`transfer_engine.run` (FakePosixSsh) — covers the SSH-destination path in context. Wired into `tests.py`.

### 2.2b Session-3 fixes (user-reported UI issues — all verified)
- **Selection did not cascade**: toggling a checkbox never notified the window, so the Selected page stayed empty and the Delete button stayed disabled. `DirPane._check` now fires a `selection_changed` callback (wired to `_refresh_sel` in `AppWindow`), which rebuilds the Selected tab, the transfer counter, and both delete buttons. Verified headless: check→delete enabled + Selected shows 1; uncheck→both revert.
- **Folder drill-down broken**: `_on_source_navigate`/`_on_dest_navigate` used the *old* `current_path` for "goto", so double-clicking/Enter re-loaded the same folder (a "flash"). Now "goto" uses the entered path; "up"/"home"/"refresh" keep using the stored path. Verified: drill into `sub1` → shows its children; Up returns to parent; same for the dest side.
- **Bottom panels not resizable**: the Notebook (Selected/Transfers) and the log TextView were packed with `expand=True`, so extra window height shrank the source/dest panes. Now the browser box is a **vertical `Gtk.Paned`** (`bottom_vpaned`): child1 = horizontal source|dest paned (`resize=True` — absorbs all extra height), child2 = notebook (`resize=False`, draggable divider). Default divider seat = 70% of height (set once on first `size-allocate`), so it scales with window size. Verified headless: notebook grows 219→273px as the window grows, and source/dest keep the default majority.
- **Log moved into the notebook** as a third tab ("Log") alongside Selected/Transfers (user asked: no separate bottom log strip). The single vertical paned divider now drags the whole bottom notebook.
- **Destination panel auto-reloads after transfers finish** (user asked): `_finish_transfer` schedules a debounced (300 ms) `_load_dest(self.dest_current_path)` for `done`/`skipped`/`failed`, so copied items and compare states appear without a manual refresh. Verified in `app smoke` + harness: after transfer, `dest_pane.meta` contains the copied file+dir.
- **Row colors unreadable on dark theme**: `DirPane` now has `set_state_colors()`; `AppWindow` feeds it `LIGHT_COLORS`/`DARK_COLORS` (from `app/window.py`) so dark themes use the brighter palette (`missing` #ff5252, `extra` #40c4ff, …).
- **Legacy UI retired from the test suite**: `tests.py` no longer imports/runs `tests/test_ui.py` — the parity net is gone, the suite is 100% on `app/`. Ported its still-valuable assertions into `test_app.py` (`test_classify_items_full`, `test_color_palettes`) and re-pointed `test_local.test_dir_list` at `local_transport`.

### 2.2c Session-4 critical-review fixes (code cleanup + hardening)
- **Legacy code DELETED**: `ui.py`, `panes.py`, root `profiles.py`, and `tests/test_ui.py` are removed and archived under `docs/old/legacy-ui/`. `tests.py` has no `ui` group; `tests/` has no `test_ui.py`.
- **`_autosave_profile` no-duplicate fix**: reconnecting to an already-saved endpoint now refreshes that profile instead of stacking `"user@host 2"` duplicates every connect (previously each reconnect created one). Matches an existing name by host+port+user. Regression: `test_autosave_profile_no_duplicate`.
- **Remote-path invariant (SSH destination) fixed**: `_dest_join()` builds destination paths with `rp.join(conn.family, ...)` for a remote dest (and `os.path.join` locally); used by both `_on_transfer` and `_refresh_sel`. Previously remote dest paths were joined with the control machine's `os.path`.
- **Browse sets connection state**: `_on_source_browse`/`_on_dest_browse` now reflect the browsed local folder so the bar's button shows "Disconnect" (it was stuck on "Browse…").
- **`_on_transfer` clearer errors**: replaced the misleading "Some destination folders do not exist" list with a single clear "The destination folder does not exist / is unreachable" message; SSH dest existence is checked via `_dest_exists` (was always bypassed).
- **Dead code pruned**: removed `_sanitize`, `_glyphs`, `_error_idle`, `_is_remote`, `_on_disconnect_dest` stub, and the dead `src_stat` probe line in `_worker`; removed unused `natural_key` import and `_DARK_STATE_COLOR`/`state_color()` in panels.py.
- **`DirPane` hardening**: `set_items` now preserves the checkbox selection across reloads by full path (so the destination auto-reload after transfer does not drop checks); `_on_select_all` batches to a single `_notify_selection()`; `_compare_done` now calls `_sync_select_all()` after bulk-checking.

### 2.2d Session-7: ⇄ side swap (feature) + export fixes
- **Side swap feature**: a `⇄` button in a narrow strip on the inner edge of the destination column (inside the paned child so it rides with the divider; GTK3 Paned cannot host children in its gutter). `_on_swap_sides` → guard (`_transfer_or_delete_active`, same predicate that gates delete buttons; sensitivity wired in `_update_delete_buttons`) → confirm-and-clear if either pane has checks (`_confirm_swap`) → bump `_remote_req`/`_dest_req` (drops in-flight listings) → `_swap_endpoints` swaps the **(conn, path, profile) trio** — per-side session state is exactly that trio; extend it there when adding fields → clear both panes → `_rebind_side` per side re-renders bar/pane from session state and reloads (no reconnect; partial connections allowed, emptied side gets standard disconnected visuals). Paths travel **with** their connection.
- **Export swap-safety fix**: `_export_context` now snapshots `context["conn"]`; `_run_export`'s worker uses it instead of reading `self.conn`/`self.dest_conn` mid-thread (a side swap during an export previously rerouted the export to the other host).
- **Export success bugfix**: `_run_export` passed an undefined `pane` to `_export_done` on the success path (NameError swallowed by the except) — every successful export reported "Export failed". Now passes `bar`.
- Tests: `test_swap_sides_travels_with_conn`, `test_swap_confirm_and_clear`, `test_swap_partial_moves_single_connection`, `test_swap_blocked_while_transfers_active` (all headless in `tests/test_app.py`).

Open/notes (not bugs, for review):
- **`compare + filter`**: `_compare_done` iterates the base model (not the filtered view), so hidden rows ARE included in the selection; the "compare skips hidden rows" note in the older docs was inaccurate. No change needed.
- **Windows SSH *source* copy** (`ssh_transport._copy_tar`, line ~972) still issues a POSIX `tar -C` command with no Windows (`tar.exe`) branch. The symmetric **destination** path is correct (tar.exe). Needs a real-Windows source→dest run; if it fails, port `_copy_tar` to branch on `_os_windows()` (use `commands/powershell.py tar_read/tar_extract`) exactly like `transfer_engine._spawn_reader/_spawn_extractor` already do.

### 2.3 Cleanup — DONE (session 4)
Legacy `ui.py`, `panes.py`, root `profiles.py`, `tests/test_ui.py` deleted and archived under `docs/old/legacy-ui/` for reference. Remaining cleanup (only if desired, low priority): fold the `app/__init__.py` docstring to describe the package.

---

## 3. Reconfirmation items (things marked "verify later" in code / plan)

1. **Delete confirmation for remote-delete of symlinks on Windows** — the transport (`ssh_transport.SSHConnection.delete`) already strips trailing slashes and checks islink; the new UI passes through; still needs the real-Windows run to confirm.
2. **`suggest_export_filename`** default path uses the *dest side current folder*; on a fresh session with no dest path set the save dialog defaults to `~`. Fine.
3. **Non-ASCII filename round trip on Windows** — we set `[Console]::OutputEncoding UTF8` and parse as text; confirm on a real Windows box with a non-ASCII file name.
4. **Profile password round trip** — `_params_for` re-uses `password` from stored profile; if user unchecks "Remember" on an existing profile the stored password is blanked — intended, but confirm UX is acceptable.
5. **Auto-save on every connect** — now reuses/updates a profile matching the same host+port+user (fixed in session 4); no more suffix duplicates. A user-edited "Save as" name is preserved unless it collides with a different endpoint, in which case " Name 2" is appended.

---

## 4. Build order for a future session (if continuing from scratch)

1. Read this file + `docs/symmetric-endpoints-feature-plan.md` + `docs/architecture-and-developer-guide.md`.
2. `python3 tests.py` → expect green.
3. Do real-host validation (2.1), fix + add regression tests for anything that breaks.
4. If new feature work: follow the plan doc waves; always keep `python3 tests.py` green.

---

## 5. Interface/import cheat-sheet (quick reference)

```
from app.window import AppWindow            # main.py entry
from app.connections import ConnectionBar, ConnectionDialog, NEW_ROW
from app.panels import DirPane, human_size, natural_key, state_color, COL_*
from app import profiles as profiles        # profiles.THIS, load/save/names/get/remember_side
from ssh_transport import SSHConnection, POLICY_*
from local_transport import LocalConnection, dir_list, dir_tree, delete_local_item
import transfer_engine   # run(dest,src,...), move(...)
import tree_exporter     # export_local_tree / export_remote_tree, describe_*_host, suggest_export_filename
from discovery import discover
```
`tests` package: `tests/common.py` has `FakePosixSsh` (an SSHConnection with `kind="ssh"` over real `sh -c`) used by engine tests — reuse it for any new engine/transport tests.

---

## 6. Test discipline (kept green every change)
- `python3 tests.py` — the only test command.
- Each module in `tests/` exposes `ALL_TESTS`; the root runner calls them then `ui smoke` + `app smoke`.
- New behavior → add a test in the matching module; mark message-context assertions, don't hardcode line numbers.

---

**Next session minimal actions:** `git status` (no commits yet — everything is staged/untracked in the working tree; there is intentionally **no git baseline**), `python3 tests.py`, then 2.1 → 2.3.