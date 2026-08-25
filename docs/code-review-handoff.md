# lan-copier — Code Review Handoff (everything done, for a later detailed review)

**Status update (session 6):** Bars simplified again — each side is a single `EndpointBar` row *inside* its own side column (so the paned divider resizes bar + tree together): a display-only connection label + **one** green-capable `conn_btn` (theme `suggested-action`; no dropdown / ✎ / separate connect-disconnect / status dot). The `ConnectionDialog` popup is now the **single** place to pick This computer / a saved profile / a new SSH connection and to disconnect (`collect()` → `{mode:"local"|"ssh"|"disconnect"}`). DirPane filter/quick-select row moved **above** the tree. `app/widgets/endpoint.py` emits `connect(bar)` (bar forwarded), and `window._on_connect_clicked(bar)` routes local→folder-picker / ssh→`_do_connect` / disconnect. `_refresh_bars()`/`_on_connect_profile`/`_on_new_connection`/`_on_edit_connection` removed; `_side_profile` dict tracks each side's profile name. New tests: `test_endpoint_bar_single_button`, `test_connection_dialog_collect_modes`.
**Prepared:** 2026-08-24
**Status update (session 4):** The legacy UI and its parity test were **deleted** (archived under `docs/old/legacy-ui/`); the suite is now 100% on `app/`. Most follow-up items flagged in §2/footer were resolved in session 4 (dead code removed, `_autosave_profile` no-duplicate, `rp.join` for remote dest paths, selection preserved across reload, `_DARK_STATE_COLOR`/`state_color()` removed, sort-mirroring decision below). Read `implementation-continuation-plan.md` for the current authoritative state.
**Audience:** a future engineer doing a detailed code review of the current working state.
**Baseline before you start:** `python3 tests.py` → **ALL TESTS PASSED** (12 unit test fns in `test_app` + `app smoke` + `app remote-dest smoke`). There is **no git commit** — this is a commit-less working tree; `git status` shows many staged/untracked files. Do not rely on diff history; review the files directly. The legacy UI (`ui.py`, `panes.py`, root `profiles.py`, `tests/test_ui.py`) is archived under `docs/old/legacy-ui/`.

> Companion docs: `implementation-continuation-plan.md` (session handoff / leftover work — authoritative), `symmetric-endpoints-feature-plan.md` (original spec), `architecture-and-developer-guide.md`.

---

## 1. What was done across the whole task

1. **Transport/time correctness (Phase A)** — in the still-current low-level layers:
   - `SSHConnection.kind = "ssh"` class attr (ssh_transport.py:29) → fixed remote-DESTINATION being completely dead (`ui._dest_ssh()` used the hard attribute so `AttributeError` was silently caught in worker threads, nothing listed, and transfers misrouted through the local `copy()` path).
   - `ui._dest_ssh()` hardened with `getattr(..., "kind", None) == "ssh"` (still true in `AppWindow._dest_is_ssh` app/window.py:763).
   - **Windows listing fixed** (commands/powershell.py `list_dir`): single-quoted `'{0}`t{1}…'` emitted a literal `` `t `` (no TAB) so `_parse_ps_list` saw 1 field/line → empty list. Now: `[Console]::OutputEncoding = UTF8` + double-quoted `` "$isDir`t$isLink…" `` (real TAB) + try/catch root-error surfacing (`exit 1`). Matched by exact-string test in tests/test_commands.py.
   - `_parse_ps_list` (ssh_transport.py:420) now strips BOM and CRLF.
   - Dest navigation endpoint-aware (home/up use `home_dir()` + `rp.dirname(path, family)`), `_dest_connected` no longer wipes typed password/remember, friendly dialogs on failed remote listing.

2. **Profiles schema v2** (app/profiles.py): `{version:2, profiles:{name:{host,port,user,password,remember}}, last:{source_profile,dest_profile}}`. "This computer" never stored (`profiles.THIS`). No v1 migration; legacy file loads as empty v2 (locked decision). Atomic save with fsync+replace.

3. **Clean UI reimplementation** (the big work) — threw away the 2774-line `ui.py`/`panes.py`/root `profiles.py` monolith and built the new `app/` package:
   - `app/connections.py` — `ConnectionDialog` (single modal SSH editor: Host w/ discovery, Port, User, Password+Show, Remember w/ "(plaintext on disk)" hint, "Save as" prefilled `user@host`) + compact symmetric `ConnectionBar` ([TITLE] [▾ This computer / profiles / + New connection…] [Connect/Browse…/Disconnect] [✎] status).
   - `app/panels.py` — `DirPane`: the symmetric browse panel for both sides (path bar Up/Home/Refresh/Open/Export, filter, select-all/missing/changed/invert/folders/files/compare, sortable TreeModelSort w/ state column, checkbox selection, delete button, summary). Pure view; window supplies data.
   - `app/window.py` — `AppWindow(Gtk.Window)`: composes two bars + two panes in a Paned, a Notebook (Selected + Transfers tabs), a delete-progress stack page, a bottom log. Ports the ENTIRE legacy engine faithfully (compare, export, delete-with-progress, transfer worker + concurrency gate + ticker + conflict dialog, sort-state machinery, on-destroy main-loop drain).

4. **main.py** now imports `AppWindow` from `app.window.py` (the sole behavioural entry change).

5. **Tests**: new module `tests/test_app.py` (profiles v2 roundtrip + legacy-empty + classify/compare + an AppWindow smoke exercising local browse → select → transfer). Root `tests.py` gained the `("app", test_app)` group + calls `test_app.SMOKE()`.

---

## 2. File-by-file review guide (with anchors)

### Low-level (untouched transport — review for correctness & invariants)
- `ssh_transport.py` (~1550 lines) — review the endpoint contract (`key()`, family, os_type, exists/size/stat/tree, mkdir/rename/delete/unique_path/remote_part_path/sweep/rm_remote/spawn_ssh) via `commands/powershell.py` base64 `-EncodedCommand`, plus the new `kind`. Invariants to verify: islink-before-isdir, strip trailing `/` before `rm -rf --`, `shlex.quote`-never interpolated user paths only inside `commands/*.py`, part-file atomicity, UNSORTED-sort-guard, INT64 sort-key columns, `_parse_ps_list` BOM/CRLF.
- `local_transport.py`, `transfer_engine.py`, `commands/` builders (+ exact-string tests in tests/test_commands.py/test_engine.py), `discovery.py`, `tree_exporter.py` — unchanged wave-0/1 work; review for the endpoint contract uniformity + remote path `commands.paths` usage (never control-machine `os.path` on remote strings).

### `app/profiles.py` (New)
- `_fresh()`, `load()/save()`, `_normalize()`, `names()/get()/remember_side()`, `_path()` → `config/profiles.json`.
- Review point: normalize strictness (unknown keys dropped), password blanking when remember False, port int cast defaults 22; remember dedupe is in window, not the store.

### `appconnections.py` (New)  ← filename lowercase `connections.py`
- `ConnectionDialog`: `_build` grid (rows 0–5), `_discover_clicked`/`set_hosts` (dedupes via model text set), `collect()`.
- `ConnectionBar`: combo built from `[THIS]+names+[NEW_ROW]`; `active_profile` property; `set_connecting/set_connected/set_disconnected` → `_sync()` sets label/sensitivity.
- Review points: port read uses `get_value_as_int()`; "Show" reveal; collect returns None when host/user missing; the `(plaintext on disk)` hint wording; the combo active index math (names.index(active)+1).

### `app/panels.py` (New)
- `DirPane`: `COL_*` constants; `_build_pathbar/_build_model/_build_buttons`; `set_items/set_states/set_summary/set_delete_count/clear/_check/_select_pred/_sync_select_all`; `_sort_by/_sort_state/_compare_rows`; `_child_path`; `_visible_rows`; filter refilter; row-activated → goto dir.
- Review points: `set_items` clears `selected` every reload (so selection is lost on navigation — INTENTIONAL for now, mirrors legacy; noted as open question), model clear + prefix path math for root "/", `natural_key` stable tie-breakers, state column reuses sort id COL_STATE_SORT=0 (checkbox header not click-sortable, legacy-equivalent).
- BIG PASS on: the sort/filter path translation (`_child_path`), correctness of `select_all`/invert interacting with filtering (visible rows only), dirs-first ordering under sort.

- TODO(cleanup) markers? none here.

## app/window.py (New `AppWindow`)
- Lifecycle + state: `Transfer` model (`__slots__`), batch/done/failed, gate (threading.Condition) + `_max_parallel`, ask-lock/dialog conflict resolution, ticker, `_closed`, `_destroyed`.
- Profile/connect: `_refresh_bars`, `_discover_later`, `_params_for`, `_on_connect_profile`, `_on_new_connection`, `_on_edit_connection`, `_autosave_profile`, `_do_connect`, `_connected_side`, `_on_disconnect`.
- Navigate/load: `_on_source_navigate/_load_source/_source_loaded`, `_on_dest_navigate/_load_dest/_dest_loaded`, `_dest_is_ssh`, `_on_source_browse/_on_dest_browse/_choose_folder/_on_open_dest`.
- Compare: `_on_compare/_compare_abort/_compare_done/_dest_exists`.
- Export: `_start_export/_export_context/_choose_save_path/_export_depth_choices/_prompt_export_options/_run_export/_export_done`.
- Delete: `_confirm_delete`, `_on_delete_cancel_clicked`, `_run_delete`, `_delete_progress_update`, `_delete_finished`.
- Transfer: `_on_transfer/_enqueue/_policy/_worker/_gate_acquire/_gate_release/_on_parallel_changed/_set_running/_set_merging/_on_part/_on_bytes/_finish_transfer/_on_retry/_on_cancel_all/_on_clear_finished/_rebuild_trow/_update_transfer_controls/_ensure_ticker/_tick/_set_cell/_update_fraction/_update_progress`, plus `_on_transfers_press/_retry_for_path/_action_for_path/_on_pause/_on_remove/_cleanup_part`, conflict dialog `_on_ask_conflict`.
- Lifecycle/destroy: `_on_delete_event`, `_confirm_quit_delete`, `_confirm_quit`, `_final_quit`, `_on_destroy` (drains nested main loops via re-armed idle — subtle; review with care).

### Some follow-up items found today (fresh eyes should revisit each)
1. **Dead code in `app/window.py`**: `_is_remote` (def ~643) unused; `_error_idle` (~570) unused; `_on_disconnect_dest` (~1196) unused stub (bar disconnect points to `_on_disconnect`); `natural_key` imported at top but unused in window.py; `shutil` used only in `_cleanup_part`; `_glyphs` is always `{}` (legacy had cairo glyphs; the classic `_icon_cb` is not present — transfers use icon-name attributes). [cleanup deferred]
2. **Distinction from legacy**: the legacy glyph pipeline was dropped (fine), but confirm the transfers-tree pixbuf columns still render OK without `_render_glyphs` — the smoke passes, but visual sanity on a real window is worth a look.
3. **`_enqueue`** uses `self.notebook.page_num(self.transfers_page) else 1` guard — works because `transfers_page` is now set; could be simplified.
4. **`DirPane.clear()` missing + `_do_connect` worker exception + edit-rename dedupe — all FIXED this session** (see implementation-continuation-plan §2.2). Verify + add tests (smokes cover clear and remote-dest transfer).
5. **Edit-profile rename doesn't dedupe collisions** — now fixed (dedupes " Name 2" like `_autosave_profile`).
6. **Selection is dropped on every reload** (`set_items` clears `selected`), so a refresh during an active transfer selection drops checkmarks from the model — review whether this should preserve or be flagged for a future improvement (legacy cleared silently too). Note: **compare+filter is NOT a bug** — `_compare_done` iterates the base model, so hidden rows are still selected.

## 3. Cross-cutting review checklist (bootstraps a thorough pass)
- [ ] Thread safety: every worker thread writes UI only via `GLib.idle_add` (`_*_loaded/_done/_finished/_progress_update/_set_*, *_idle`).
- [ ] No UI writes from worker thread; no `conn.close()` while a worker uses conn (destroy path sets `_closed` and drains).
- [ ] Sort mirroring/U NSORT guard: window uses both panes now independent (no mirroring in AppWindow — verify the legacy sync was intentionally dropped: previously SRC→DST sort mirroring existed; the new DirPane doesn’t mirror). Decide whether to restore mirroring (open item).
- [ ] Atomicity + part files + stale/live part sweep in ssh_transport + transfer_engine (copy via part→os.replace / remote part dir → mv|per-entry merge).
- [ ] Symlink/`rm` guards: local delete islink-before-isdir; remote delete strips trailing `/`.
- [ ] `commands/*` builders never interpolate raw user paths outside those modules (shlex.quote / base64). Verify no new `f"...{path}..."` in the SSH/mkdir/rm lines.
- [ ] Sort key columns in models use `GObject.TYPE_INT64`.
- [ ] No hardcoded column indices: `COL_*` in panels.py, `SRC_*`/`DST_*` still in legacy ui.py only.
- [ ] `config/profiles.json` still written to project dir; `profiles.py` v2 helpers are consistent with `app/profiles.py` (only `app/profiles.py` is used by the app now).
- [ ] `os.path` never used on remote paths (use `rp.*`); `expand_remote` where a remote path may contain `~`.

## 4. Test coverage map (for review: are the seams tested?)
| Area | Tests |
|---|---|
| PowerShell builders exact strings | tests/test_commands.py (`test_cmd_powershell_builders`) |
| `_parse_ps_list` BOM/CRLF | tests/test_ssh.py `test_parse_ps_list_bom_crlf` |
| `kind` markers | tests/test_ssh.py `test_kind_marker`; tests/test_ui.py `test_ui_dest_ssh_real_connection` |
| POSIX builders + export + engine (run/move/part/stall) | tests/test_commands, test_engine, test_export |
| Local transport copy matrix | tests/test_local |
| legacy window parity | tests/test_ui.py (still runs against legacy ui.py; kept as safety net) |
| profiles v2 + legacy-empty + classify/compare | tests/test_app.py |
| AppWindow smoke (browse→transfer) | tests/test_app.py `SMOKE` (`_smoke_ui`) |
| AppWindow local→SSH-dest transfer (full worker+engine) | tests/test_app.py `SMOKE_REMOTE_DEST` (`_smoke_remote_dest`) |
**Gap:** no dedicated test for the new windows' SSH connect path (uses real transport, not fakable in CI without heavy mocks), delete progress worker, compare+filter interaction, or edit-profile rename. These need a human pass.

## 5. Cleanup checklist (DONE in session 4)
1. ~~Delete legacy `ui.py`, `panes.py`, root `profiles.py`~~ → **DONE**, archived under `docs/old/legacy-ui/`.
2. ~~Retire `tests/test_ui.py`~~ → **DONE**; its still-valuable assertions were folded into `tests/test_app.py` (`test_classify_items_full`, `test_color_palettes`).
3. **Prune dead helpers in `app/window.py`** → **DONE**: removed `_is_remote`, `_error_idle`, `_on_disconnect_dest`, `_sanitize`, `_glyphs`, the dead `src_stat` line; dropped the unused `natural_key` import. Also removed `_DARK_STATE_COLOR`/`state_color()` from `app/panels.py`.
4. **Sort mirroring between panes**: decided **not** to restore — the two DirPanes sort independently; the legacy cross-panel mirror was intentionally dropped (each side is a standalone browser). Left as-is.
5. ~~Update AGENTS.md + `docs/architecture-and-developer-guide.md`~~ → **DONE**.
6. Update `docs/symmetric-endpoints-feature-plan.md` §16 to reference the reimplementation + `app/` layout → **DONE** (below).

## 6. Key decisions already locked (do not relitigate)
- Passwords persisted only when "Remember" (plaintext) — tooltip warns.
- Per-side last profile/path only; last_selection** dropped.
- New connect auto-saves a profile (name prefilled user@host, editable); dedupe " Name 2".
- No v1 profile migration.
- Windows endpoints via PowerShell/tar.exe; symmetric primitives; remote-dest atomicity (part→mv/merge).
- Move: same-endpoint fast-path rename; else copy+delete-on-success.
- Symlink safety + atomic parts + UNSORTED guard + INT64 sort keys are non-negotiable invariants.

## 7. Direct callouts for the reviewer's first 30 minutes
- Start: `python3 tests.py` (expect green). The legacy UI is gone (archived in `docs/old/legacy-ui/`); the suite is 100% on `app/`.
- Read `app/profiles.py` (short) → `app/connections.py` → `app/panels.py` → skim `app/window.py` `_build_ui/_recompute_states/_worker/_enqueue/_on_ask_conflict/_on_destroy`.
- The transfer path (`_on_transfer → _enqueue → _worker → transfer_engine.run | conn.copy`) is the beating heart — verify it matches the engine's `run(dest, src,…)` signature (dest first) and that `t.dest_conn` routing is correct when dest is SSH.
- Confirm `main.py` imports `AppWindow`; there are no other legacy-UI references.