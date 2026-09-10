# lan-copier — Implementation Plan + Session Handoff (compacted)

**Date:** 2026-09-09 (session 9: transfer-panel progress fixes — 99%-until-done bar cap, working `merging…` state with per-entry merge progress, SSH-dest method normalization; sessions 5–8 history below)
**Status:** clean reimplementation of the UI layer is DONE and green. Legacy `ui.py`/`panes.py`/`profiles.py`/`tests/test_ui.py` have been **deleted** and backed up under `docs/old/legacy-ui/`. **Session 5** fixed duplicate-profile stacking (identity is the resolved remote hostname, schema v3). **Session 6** simplified the bars: each side is a single `EndpointBar` row *inside* its own side column above the `DirPane` (a display-only endpoint label + one popup-driven `conn_btn` that turns green when connected — no dropdown/edit/disconnect buttons), the `ConnectionDialog` popup is now the single place to pick This computer / a saved profile / a new SSH connection and to disconnect, and the DirPane filter/quick-select row moved back **above** the tree. **Session 7** added the ⇄ side swap (endpoint sessions exchange wholesale, see §2.2d) and fixed the export worker to use its snapshot connection. **Session 8** added destination disk-space display + source selection-size hint + lazy folder sizes (see §2.2e; spec: `docs/disk-space-and-selection-hint.requirements.md`). This doc is the **single authoritative handoff**: read it in any new session, then `python3 tests.py` to verify the baseline. Everything below is self-contained.

**Sibling docs:** `symmetric-endpoints-feature-plan.md` (original feature spec, §§1–15), `architecture-and-developer-guide.md`, `AGENTS.md`.

---

## 0. TL;DR — one paragraph

`lan-copier` talks symmetrically between THIS Computer / SSH-Linux-macOS / SSH-Windows for file management (browse, compare, select, transfer, delete, export). The big symmetric feature (transport, transfer engine, move, command builders) is implemented and green. The legacy 2774-line `ui.py` monolith was thrown away and reimplemented cleanly into the `app/` package (`profiles` v3, `widgets/` endpoint bar + dialog + dirpane, `window` AppWindow). **The legacy files are now deleted** and archived under `docs/old/legacy-ui/`. `main.py` launches `AppWindow` from `app/window.py`. Full suite is green.

---

## 1. Current state — verified green this session

Run `python3 tests.py` → **ALL TESTS PASSED** (132 unit test fns + `app smoke` + `app remote-dest smoke`).

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
    profiles.json              (schema v3)
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
- **Row colors unreadable on dark theme**: `DirPane` now has `set_state_colors()`; `AppWindow` feeds it `LIGHT_COLORS`/`DARK_COLORS` (from `app/window.py`) so dark themes use the brighter palette (`missing` #ff8a80, `extra` #40c4ff, …).
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
- **Windows SSH *source* copy — FIXED**: `ssh_transport._copy_tar` was issuing a POSIX `tar -C` command with no Windows (`tar.exe`) branch, so a Windows source → local dest folder transfer failed (`'LC_ALL' is not recognized...`). Now both `_copy_tar` and `transfer_engine._reader_cmd` share a single source of truth, `SSHConnection._tar_read_cmd(remote_path)`, which derives parent/name with the endpoint family and branches on `_os_windows()` (`ps_cmd.tar_read` tar.exe via `-EncodedCommand` vs `posix_cmd.tar_read_remote`). Regression: `test_copy_tar_windows_*` in tests/test_ssh.py.

### 2.2e Session-8: destination disk space + source selection size hint (feature)

Full spec: `docs/disk-space-and-selection-hint.requirements.md` (status → Implemented). Summary:

- **Transport**: new `disk_space(path)` on the endpoint contract → `{"total","free"}` or `None`. Local: `shutil.disk_usage`; SSH POSIX: `df -B1 --output=size,used,avail` (GNU) / `df -k` (macOS); SSH Windows: `[IO.DriveInfo]`. Builders in `commands/posix.py` (`df_gnu`/`df_darwin`) and `commands/powershell.py` (`disk_space`). Tests: `test_cmd_*`, `test_disk_space_parse` (test_ssh), `test_local_disk_space`.
- **`DirPane`**: the bottom summary is now a `status_row` horizontal box — left `summary` (hexpand) + `spinner` + `right_label` (`xalign=1`, optional pango color). New view methods: `set_right_label(text, color)`, `set_busy(active)` (spinner), `set_folder_size(name, st, failed)` (updates `COL_SIZE`/`COL_SIZE_TEXT`/`meta`/`folder_sizes`; `~` prefix for partial, em-dash kept on total failure), `folder_paths()`. New `human_size_compact()` (strips `.0 `). `clear()` resets the new state.
- **`AppWindow`**:
  - `_start_folder_size_calc(side)` — per-pane lazy recursive folder sizes after each listing (daemon `threading.Thread` + `BoundedSemaphore(2)` per side), tagged with the side's `_remote_req`/`_dest_req` so navigation/swap/disconnect drop stale results; results land in `_folder_size_landed` → `set_folder_size` + spinner + label refresh. Local-fallback via `commands.local.stat_bytes_files` when a dest paths through with no connection object.
  - `_update_status_labels()` → `_update_source_hint()` (source right label: `Selected: X (N files)`, green/red vs. dest free; `~` + no color while sizes unknown or dest free unknown) and `_update_dest_space()` (dest right label: `X / Y free`, or `After copy: X / Y free` when items are selected, or `Disk space unavailable` on query failure). Colors use the existing `self._colors` palette (`done`/`failed`).
  - **Net accounting** `_selection_totals()`: `net = Σ selected source sizes − Σ conflicting dest sizes` (states `same`/`differ`), negative allowed (overwrites free space → green), clamping projected free ≥ 0.
  - `_dest_disk_space(req)` / `_query_disk` / `_disk_queried` + `_reuse_disk_cache()` — cached dest disk space, queried on every dest listing load (connect/navigate/refresh/transfer-complete/delete-complete) **except strict child drill-downs** (SSH prefix rule; local additionally requires matching `st_dev`). Same-path reloads re-query. Cache + failure flag cleared on disconnect and swap.
  - `_on_disconnect` now also bumps `_remote_req`/`_dest_req` (stale calc results on a cleared pane).
- **Tests**: `test_dirpane_status_row`, `test_dirpane_folder_size_update`, `test_selection_hint_and_dest_preview`, `test_selection_hint_no_dest_color`, `test_dest_disk_label_failure`, `test_disk_cache_reuse`, `test_folder_size_in_model_window`, `test_net_conflict_accounting` (all in `tests/test_app.py`).

Open/notes (for review): SSH folder-size partial detection is still not implemented (doc F25) — `find` stderr is discarded, so a partially-failed remote walk reports a clean (undercounted) `{bytes, files}`. Local `stat_remote` now flags `partial` (see §2.2f). New real-host validation for `df`/`IO.DriveInfo` output parsing belongs in the normal real-host pass (§2.1).

### 2.2f Session-8 follow-up: size-aware folder comparison

Folder states now consider recursive sizes (spec §4.4 + `classify_items` row):
- `classify_items` compares same-named dirs by `(size, files)` via `.get(..., 0)` (directories only; files stay size-only). At listing time folder sizes are unknown (0/0) so dirs start `same`.
- `_reclassify_folder_state(name)` runs from `_folder_size_landed`: once *both* panes' `folder_sizes[name]` exist and are non-`failed`/non-`partial`, flips the shared `_states` + both summaries to `differ` when `(bytes, files)` disagree; identical stays `same`. Failed/partial sizes keep the conservative `same` (never flip on an undercount).
- `LocalConnection.stat_remote` now flags `partial: True` when a file `getsize` fails or `os.walk` hits an unreadable dir (closes the agreed-but-deferred A9). SSH already surfaced failure as `None`→`failed`. Folder byte-count heuristic can't distinguish equal-size different-content folders (tree hashing out of scope).
- Tests: `test_folder_state_by_size`, `test_folder_state_failed_is_conservative` (window), `test_local_stat_remote_partial` (local), `test_classify_items_full` dir cases.

### 2.2g Session-9: transfer-panel progress fixes (A+B+C+D)

User-reported: the Speed/ETA bar tracked *read* throughput and froze on a long
`merging…` state worth a "not responding" scare, especially when a fast reader
fed a slow writer. Analysis: the pump's read count is pipe-throttled to ≈ the
write rate during streaming (so the read/write split is mostly theoretical), but
the bar hit 100% at reader EOF while the extractor/merge still ran, and the
ticker clobbered the `merging…` label within 500 ms. Fixed in the window + local
transport; the documented read-side accounting (`symmetric-endpoints-feature-plan`
§4.4) is kept intact as the streaming measure.

- **A — bar never claims done early**: `_update_fraction` caps the live bar at
  0.99 while `status == "running"`; only `_finish_transfer` shows 100% (on
  `done`/`skipped`). A full bar now unambiguously means "finished".
- **B — `merging…` state actually works**: `Transfer.merging` flag set by
  `_set_merging` (from `on_finish`). `_update_fraction` updates only the bar
  column while merging (never the text/speed/ETA); `_update_progress` returns
  early so speed/ETA stay cleared during placement. `_finish_transfer` and
  `_on_retry` reset the flag.
- **C — merge-phase progress (local dest)**: new `on_merge(done, total)` callback
  threaded through `copy → _copy_legacy → _place → _merge_dir` (files-count
  semantics, ~0.2 s throttle + a forced final `(total, total)` call, matching the
  `on_bytes` pattern). The window shows `merging… N/M`. Remote-dest placement is a
  single opaque remote command (no per-entry visibility) → static `merging…`.
- **D — SSH-dest method normalization**: single-file transfers to an SSH dest are
  streamed via the tar bridge but were tagged `method="scp"`, so the ticker took
  the local-`getsize(part)` branch and **never computed speed/ETA**. `_worker` now
  sets `t.method = "tar"` for every SSH-destination transfer (they always stream),
  restoring speed/ETA and the `· file N/M` counter there.
- Tests: `test_merge_dir_reports_progress`, `test_place_fresh_target_skips_merge_callback`
  (local), `test_transfer_progress_cap_and_merging` (window), plus the remote-dest
  smoke asserts `t.method == "tar"`. Transport plumbing is backward-compatible
  (`on_merge` defaults to `None`; the older `_merge_dir(part, final)` calls still work).
- Open for real-host pass (§2.1): confirm whether the OS "wait or kill"
  prompt is purely a frozen-progress perception issue or a true main-thread stall
  (lifecycle tracing found no main-thread blocking in the transfer path).

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
from app.widgets.dialog import ConnectionDialog, NEW_ROW
from app.widgets.dirpane import DirPane, human_size, human_size_compact, natural_key, COL_*
from app.widgets.endpoint import EndpointBar
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

**Next session minimal actions:** `git status`/`git log` (commits exist on `main`; keep new commits focused), `python3 tests.py`, then 2.1 → 2.3.