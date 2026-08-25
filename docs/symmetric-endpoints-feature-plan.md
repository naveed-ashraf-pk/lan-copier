# Feature Plan: Symmetric Any-Source → Any-Destination File Management

**Document Version:** 3.1
**Date:** 2026-08-23
**Status:** Approved design; **Wave 0 + Wave 1 core implemented & green** (transports symmetric, transfer engine + move, command builders, unified `os_type`, destination ConnectionBar & endpoint-aware dest). See §16 "Implementation Status".
**Target Systems:** `ui.py`, `ssh_transport.py`, `local_transport.py`, `profiles.py`, `discovery.py`, `tree_exporter.py`, `tests.py`, `docs/`

---

## 1. Summary & Intent

Today `lan-copier` is asymmetric: the **Source** can be this computer or an SSH host, but the **Destination is always a local folder on this machine** (which evidently runs Windows/Linux or both — the destination has always been local OS calls).

This change upgrades the app so **any supported endpoint can act as Source or Destination** over LAN, including **Windows SSH hosts as dual-role endpoints**. The app becomes a symmetric file manager across: Local (this PC), SSH (Linux/macOS), and SSH (Windows).

| Source \ Dest | This computer | SSH Linux/macOS | SSH Windows |
|---|---|---|---|
| This computer | ✅ (existing) | ✅ tar push (new) | ✅ tar push (new) |
| SSH Linux/macOS | ✅ (existing) | ✅ bridge (new) | ✅ bridge (new) |
| SSH Windows | ⚠️ restored to parity (new) | ✅ bridge (new) | ✅ bridge (new) |

### Goals
1. **Symmetric endpoints**: every operation (copy, compare, delete, move, rename, new folder, export) works between any two endpoints.
2. **Reuse & de-duplication**: one copy engine, one delete primitive set, one merge algorithm, one browser pane, one connection bar, one OS-detection path, one remote command builder.
3. **Long-term maintainability**: strict layering (UI → transfer engine → endpoints → command builders); transport is UI-free and engine-free of GTK; all remote commands are pure, testable strings.
4. **Zero user-facing data breakage**: profiles.json migrates backward-compatibly (schema v2); existing flows stay byte-identical.
5. **Polished UI**: symmetric per-pane connection bars, tooltips everywhere, `⇄ Swap` direction shortcut.

### Out of scope (explicitly)
- In-place ops beyond rename / new folder / delete / copy / move (no permissions UI, no drive/format management).
- IPv6 / SMB / FTP / NFS transport types.
- Same-endpoint **copy** fast-path (streaming bridge is fine; only **move** gets the rename fast-path, §5.3).
- **Same-host server-side copy optimization** — copying between two folders on the *same* remote host still bridges through the control machine (§4.3's SSH↔SSH legs), even though a single remote `cp -a`/`Copy-Item` would avoid the round trip entirely. Left as a documented future optimization; v1 favors one consistent code path over a special case.
- **Loopback SSH** (`127.0.0.1`/`localhost` profiles, seen in existing `config/profiles.json`) is treated as a normal SSH endpoint, not auto-detected as "local" — no behavior change from today.
- Linux/macOS→Windows **push** where the Windows host has no `tar.exe`: v1 detects and gives an actionable "install tar.exe" hint instead of silent breakage (§5.6).
- Advanced Windows semantics: reparse points/junctions are treated as regular links/best-effort; NTFS alternate data streams are ignored; paths beyond `MAX_PATH` (260 chars, no long-path opt-in) fail with a clear message (§5.9).
- The **"This computer" endpoint's own OS** is unconstrained by this plan (it already relies on the existing `local_transport.py`/stdlib calls and inherits whatever host-OS limitations exist there today, e.g. symlink-creation privileges when Python itself runs on Windows) — not a regression, not newly in scope.

### Breaking-change boundary (confirmed)
- **Internal (code): breaking.** Transport `copy()` API and destination-pane semantics change; tests are updated in the same change.
- **Data (profiles.json): not breaking.** Schema v2 migrates old flat profiles transparently (§7).
- **User-visible behavior: not breaking.** All existing flows keep working with byte-identical semantics.

---

## 2. Context & Current Architecture

### 2.1 What exists today
- **Transport**: `SSHConnection` (ssh_transport.py) owns the copy pipeline; `LocalConnection` (local_transport.py) **inherits** it and overrides network-touching methods + `_copy_scp`/`_copy_tar` with in-process logic. Pause = SIGSTOP on tracked subprocesses (SSH) / event-based `_check`+`_LocalAbort` (local).
- **OS awareness is fragmented**: `ssh_transport.stat_remote`/`tree_remote` read `uname -s` and branch Darwin/GNU (ssh_transport.py:305-447); `tree_exporter` has a **separate** `_remote_uname` (with Windows detection via `uname` failure → PowerShell) and its own `_export_uname` cache (tree_exporter.py:119-139). Windows hosts are already spoken to via `powershell -NoProfile -EncodedCommand <base64>` (tree_exporter.py:501-561), which sidesteps POSIX-shell quoting entirely — the same mechanism this plan reuses for the destination engine.
- **UI** (ui.py, 2610 lines): single SSH auth toolbar for source; destination pane is hardwired local (`_load_dest`→`dir_list`, `_on_compare`→`dir_tree`, dest delete→`delete_local_item`, dest export→`export_local_tree`, "Open destination"→`xdg-open`). Source/dest panes duplicate ~8 selection/toggle/sort/load methods.
- **Profiles**: flat `{name: {host, port, user, password?, last_source, last_dest, last_selection?}}`; "This computer" = `host:"local"`.
- **Discovery** (discovery.py): ARP + `/24` scan for SSH hosts; feeds only the source Host dropdown today.
- **Sort safety**: raw sort keys `GObject.TYPE_INT64`; the UNSORTED sort-column transition is never propagated (GTK3 `GtkTreeModelSort` segfault guard).

### 2.2 Gaps this plan closes
1. Destination side is 100% local-filesystem code → all endpoint-aware for local & remote (POSIX + Windows).
2. Windows hosts can be **sources** for export only; copy/compare/delete error out. The new PowerShell primitive layer (built for destinations) also **restores Windows sources to parity**.
3. OS detection duplicated (`_uname` vs `_export_uname`) → single shared, lazily-cached detection.
4. Remote commands are inline f-strings/unshaped → extracted into pure, unit-tested command builders.
5. Copy logic is entangled with connection objects → extracted into a pure `TransferEngine` (§4.2).

---

## 3. Requirements

### 3.1 Functional
| # | Requirement |
|---|---|
| F1 | Source and Destination each support: **This computer**, **SSH Linux/macOS**, **SSH Windows** (Win 10/11 + OpenSSH + `tar.exe`). |
| F2 | Copy any→any: policy (ask/overwrite/keep both/skip), parallel gate, pause/resume/cancel, stall detection; mode/mtime/symlinks preserved. |
| F3 | Compare (recursive + state coloring) across any endpoint pair. |
| F4 | Delete checked items on either endpoint (symlink-traversal-safe, protected-path-guarded, read-only retry locally, file-lock handling on Windows). |
| F5 | Move: transfer + delete-source-on-success (per item); same-endpoint atomic rename fast-path; EXDEV/own-subtree/dir-onto-existing fallbacks (§5.3). |
| F6 | Rename an item / create a folder on either endpoint (§5.5). |
| F7 | Export current tree from either endpoint (existing exporter; Windows path already works). |
| F8 | Profiles: per-side save/restore, backward-compatible v2, "This computer" default destination. |
| F9 | Discovery feeds both panes' host dropdowns. |
| F10 | UI: symmetric labeled connection bars, tooltips, `⇄ Swap`, mutual exclusion of transfers/deletes/renames, window-close guards retained. |

### 3.2 Non-functional
| # | Requirement |
|---|---|
| N1 | Zero third-party Python deps (stdlib + PyGObject). PowerShell/`tar.exe` are invoked over SSH, not Python deps. |
| N2 | All network/disk I/O off the GTK thread; UI updates only via `GLib.idle_add`; stale callbacks discarded via request counters. |
| N3 | Atomicity & crash safety preserved on every destination (§5.2). |
| N4 | No performance regression for existing flows; new remote flows use direct stream bridges (no local disk staging) except the same-endpoint move fast-path. |
| N5 | Sort-mirroring + UNSORTED segfault guard hold for both panes (dual-guard). |
| N6 | `python3 tests.py` stays self-contained; each remote command has an exact-string test. |
| N7 | Maintenance: pure command builders + fakes mean OS behavior is deterministically testable without real hosts; manual real-host validation is a recorded release gate (not a CI substitute). |

---

## 4. Architecture

### 4.1 Layering (strict, one-directional)

```
UI (CopierWindow → ConnectionBar, BrowserPane)
        │  never touches subprocess / ssh internals
        ▼
TransferEngine (pure engine: policy, part lifecycle, stream pump, stall, pause/cancel)
        │
        ▼
Endpoints (SSHConnection / LocalConnection)  ← implement uniform contract
        │
        ▼
Command Builders (pure, tested): posix.py, powershell.py, local.py
```

- **UI** never calls subprocess, ssh, or os-ops directly; it only calls endpoint-interface methods and the engine.
- **Endpoints** never import GTK and never embed raw command strings inline (except nothing — always via builders).
- **Command builders** are pure functions returning `argv`/script blobs; endpoints are thin executors.

### 4.2 TransferEngine (new module, e.g. `transfer_engine.py`)

Single `run(dest, src, src_path, dest_path, policy, method, on_ask, on_bytes, proc_sink, on_finish)`.

- **Staged part lifecycle on the destination side**: `dest.part_path(final)` → `dest.place(part, final)` (local: `os.replace`/`_merge_dir`; POSIX remote: `mv`/sh-merge; Windows remote: `tar.exe` extract to temp part dir → PowerShell per-entry merge). Live-parts set + stale sweep live on the destination endpoint. The part directory/file is always created via the destination's `mkdir`/create primitive **before** the extractor process is spawned — one explicit sequencing rule for every OS, not left implicit per-leg.
- **Leg dispatch** (single harness, byte-identical for existing flows - §4.3).
- **Conflict resolution** (endpoint-aware, one place): `dest.exists(final)`, `dest.size(final)`, sizes via `src.size(src_path)`; `dest.unique_path(final)`.
- **Progress/stall/pause**: the existing tar pump & stall clock; `proc_sink` is the single pause/cancel channel (see pause rule §4.4). Progress accounting happens **as bytes leave the source reader**, before they reach whatever extracts them — so progress/ETA is identical regardless of the destination's OS; no per-OS progress code is needed.
- **`Transfer` model wiring** (ui.py): `Transfer` gains `src_conn` + `dest_conn` (replacing the single `conn`) and `dest_path` (replacing the implicit local `dest`); the worker thread calls `engine.run(t.dest_conn, t.src_conn, t.src, t.dest_path, ...)`. Pause/remove/retry/cancel-all logic is otherwise unchanged (they already operate on `t.procs`/`proc_sink`, which is per-transfer and OS-agnostic).
- **Reduced test churn (compat shim)**: `SSHConnection.copy(...)`/`LocalConnection.copy(...)` are **kept** as thin wrappers over `engine.run(LocalConnection(), self, ...)` for the "pull to local destination" shape used by the majority of today's tests — so most existing Local→Local/SSH→Local tests need **no signature changes**; only tests that exercise a *remote destination* call `engine.run(...)` directly.
- **Same connection object as both source and destination**: fully supported (e.g. moving between two folders on the same remote host) — `proc_sink` is per-transfer, not per-connection, so pause/cancel never cross-contaminate two legs sharing one `SSHConnection`.

### 4.3 Copy flow / transfer legs

| Flow | Read leg | Dest finalization | Pause |
|---|---|---|---|
| SSH→Local | existing `scp -p -r` (file) / `tar` (dir), unchanged | `_place` (unchanged) | SIGSTOP (subprocs) — unchanged |
| Local→Local | existing in-process `_copy_scp`/`_copy_tar`, unchanged | `_place` (unchanged) | event-based `_check` — unchanged |
| Local→SSH(POSIX) | local `tar` reader subprocess | remote `mv` (file) / sh per-entry merge (dir) | SIGSTOP both procs |
| Local→SSH(Windows) | local `tar` reader subprocess | remote `tar.exe -xpf -` to part dir, PowerShell per-entry merge | SIGSTOP both procs |
| SSH(POSIX)→SSH(any) | remote `tar` reader subprocess (POSIX source) | same as Local→SSH per dest OS | SIGSTOP both procs |
| SSH(Windows) **source** → any | remote `tar.exe -cf - -C dir -- name` (bsdtar creates archives too, not just extracts) | same as above per dest OS | SIGSTOP both procs |

Only the **primitives** (list/stat/tree/delete/exists/size/unique_path/mkdir/rename/merge) need PowerShell on a Windows endpoint — the bulk data leg uses `tar.exe` symmetrically for both reading and writing, exactly like the POSIX legs. This removes an entire "invent a PowerShell tar equivalent" problem from the design.

### 4.4 Pause/cancel rule (one mechanism, explicit legs)

- **Every subprocess leg** is paused via SIGSTOP and resumed via SIGCONT through the shared `proc_sink` (both endpoints append to it; both implement `pause/resume/kill_procs`).
- **The in-process local leg** (LocalConnection) keeps its event-based `_check`/`_LocalAbort` — it has no subprocess. The earlier plan's idea to remove it is **revoked**: it's fast and tested and avoids SIGSTOP races in pure-local flows.
- `_mux_dead` retry is implemented on **both** sides; a dead master on either side triggers `_reset_master` + one retry. Stall detection covers both procs via the shared activity clock.

### 4.5 Unified endpoint contract (both classes implement)

| Method | Purpose | Local impl | SSH(Linux/mac) impl | SSH(Windows) impl |
|---|---|---|---|---|
| `kind` (attr) | `"local"` / `"ssh"` | — | — | — |
| `key()` | endpoint identity for equality/fast-path | `("local",)` | `("ssh", host, port, user)` | same as POSIX |
| `os_type` (cached) | detection, shared with exporter | `"ThisComputer"` | `"Linux"`/`"Darwin"` | `"Windows"` |
| `list_dir(path)` | children dicts | `dir_list` (moved to method) | `ls -la --` parse | PowerShell `Get-ChildItem` |
| `stat(path)` (alias `stat_remote`) | `{bytes, files}` | existing | existing (uses `os_type`) | PowerShell recursive sum |
| `tree(path)` (alias `tree_remote`) | `{rel: size}` | existing `dir_tree` | existing (uses `os_type`) | PowerShell recursive scan |
| `delete(path)` | `(ok, err)` | `delete_local_item` moved here (alias kept) | `rm -rf --` (unchanged) | `Remove-Item -Recurse -Force` + lock retry |
| `exists(path)` | bool | `os.path.lexists` | `[ -e ]` quoted | `Test-Path` |
| `size(path)` | int/None | existing `_local_size` | `stat -c %s`/`stat -f %z` | `(Get-Item).Length` |
| `unique_path(path)` | never-existing name | existing | shell loop `(name (n).ext)` | PS loop |
| `mkdir(path)` | create folder | `os.makedirs` | `mkdir -p --` | `New-Item -ItemType Directory` |
| `rename(src, dst)` | rename/move item | `os.rename` | `mv --` | `Move-Item` |
| `home_dir()`, `expand(path)` | home & `~` | existing | existing | :ps: `$env:USERPROFILE` |
| `part_path(final)` / `place(part, final)` / `sweep(part)` | dest-side staging & finalize | existing local | `mv` / sh-merge | `tar.exe` + PowerShell merge |
| `pause/resume/kill_procs/kill_all` | per-transfer pause | event-based | SIGSTOP | SIGSTOP |

Back-compat aliases (`stat_remote`, `tree_remote`, module-level `delete_local_item`/`dir_list`/`dir_tree`, **and `_uname`/`_export_uname` as read/write properties over the unified `os_type`**) are kept as thin shims for `tests.py`/`tree_exporter.py` and removed after the migration lands. This matters concretely: `tests.py` sets `c._uname = "Linux"` directly and asserts `conn._export_uname == "Darwin"` (tests.py:289, 1994) — both keep working unchanged as aliases.

### 4.6 Command Builders (new, pure — the reusability core)

- `commands/posix.py`: `shlex.quote`-based argv builders: `rm_rf`, `mv`, `mkdir_p`, `ls_la`, `find_stat`, `find_tree`, `tar_stream`, `tar_extract`, `exists`, `unique`, and the **sh per-entry merge blob**.
- `commands/powershell.py`: build a base64-encoded `-EncodedCommand` (UTF-16LE, reusing the exporter's encoding) fixed-function scripts: `list_dir`, `stat`, `tree`, `delete`, `exists`, `size`, `unique_path`, `mkdir`, `rename`, **per-entry merge**, and a `tar.exe` presence check.
- `commands/local.py`: thin wrappers for the local OS calls so the engine stays endpoint-agnostic.

**Rule**: every builder is a pure function returning exact `argv`/blob; each has an exact-string unit test; endpoints never interpolate user paths into remote strings outside builders (no f-string shell injection).

### 4.7 OS detection — single shared source of truth

- Add lazy, cached `SSHConnection.os_type`; values `Linux`/`Darwin`/`Windows`. Detection = `uname -s`; if it fails/empty → `Windows` (matches current exporter semantics). On Windows, `has_tar` is detected **lazily** — only right before the first transfer that needs it (§5.6), not as a connect-time gate (a Windows host with no `tar.exe` must still be browsable/deletable/renamable via the PowerShell primitives).
- `tree_exporter._remote_uname`/`_export_uname` and `ssh_transport._uname` **converge onto `os_type`** (single cache, exporter no longer runs its own detection; the exporter's Windows/`-EncodedCommand` code is unchanged and reused).

### 4.8 Remote path handling (new, correctness-critical)

**Gap found in review**: the app's control machine's `os.path` reflects *its own* OS, not the *remote endpoint's* OS. Naively using Python's `os.path.dirname`/`commonpath`/`join` on a Windows remote path string (e.g. `C:\Users\Bob\Documents`) while running the control app on Linux silently produces wrong results (colons/backslashes aren't POSIX separators). Every existing local-side helper that manipulates a path string (`_part_path`'s `os.path.basename`/`dirname`, `unique_path`'s `os.path.splitext`, subtree/prefix checks for the move guard) must **not** be reused as-is for a remote endpoint.

- New small `remote_paths.py` (or `commands/paths.py`) providing OS-family-aware `join`, `dirname`, `basename`, `splitext`, `is_subpath`, `normalize` for two families: **posix** (`/`, case-sensitive) and **windows** (`\` or `/`, case-insensitive, drive-letter aware).
- Every endpoint method that builds a part path, checks a move's own-subtree guard, or compares "same path" uses its **own** `os_type`-selected path-ops, never the calling machine's `os.path`.
- Local endpoint keeps using real `os.path` (it *is* the local OS).
- Consequence for §5.3 (move guard) and §5.9 (misc edge cases) is captured there directly.

---

## 5. Safety, Edge Cases, and Design Decisions

### 5.1 Symlink & protected-path safety (unchanged, endpoint-aware)
- Local: `islink()` before `isdir()`; read-only retry on NTFS mounts.
- Remote (POSIX): trailing slashes stripped before `rm -rf --`; protected-path guard on both endpoints for delete/move-source-delete/rename.
- Windows: `Remove-Item -Recurse -Force` does not follow reparse-point symlink dirs; protected-path guard against `C:\`, `\`, empty, drive-root, and the home dir.

### 5.2 Remote destination atomicity & merge
- **Part naming is uniform across every OS**: `.{basename}.lan-copier-part-{pid}-{n}` (dot-prefix works fine on NTFS too) — one naming rule, no OS-specific special case, per Design Rule §10.2.
- **POSIX**: stream to remote part in the same dir as `final` (same-FS atomic `mv` = file). Directory = temp part dir → **sh per-entry merge** (files replace same-named files; dirs recurse; nothing not overwritten is touched; symlinked dirs never traversed). Mirrors local `_place`'s dir-collision handling.
- **Windows**: `tar.exe -xpf - -C <part-dir>` (bsdtar accepts GNU tar streams; supports `tar` on the wire in both directions, §4.3). Then **PowerShell per-entry merge** (`Move-Item` semantics: file-over-file replace, dir-over-dir recurse, type-mismatch handled like local `_place`). Busy/locked files: retry loop (e.g. 5×300ms) then fail the item with a clear message; the temp part stays on disk for manual cleanup, is swept on next run. **`tar.exe`/PowerShell exit-code policy**: a nonzero exit is only treated as fatal when stderr matches a known-fatal pattern (e.g. "no such file", "access is denied", "cannot create"); benign ACL/permission-bit warnings from bsdtar on NTFS are tolerated (NTFS has no POSIX mode bits) — mirrors the existing `_clean_err`/`_mux_dead` pattern-matching approach, validated against the real Windows host.
- **Stale/live parts** live on the destination endpoint (`_live_parts` + sweep on next `run`), keyed like today's local set.
- **Remote fsync**: not performed (matches current caveat); crash safety from part+rename.

### 5.3 Move (transfer + delete-on-success; same-endpoint fast-path)
- Per-item: transfer → on success delete that source item. Cancel during delete → source kept, reported. Copy-ok-delete-fail → both copies exist, reported (never silent loss on either side).
- Fast-path when `dest.key() == src.key()`: file→(missing|replaceable file), or dir→missing name: single atomic `os.replace`/remote `mv` (`Move-Item` on Windows). 
  - dir→existing dir, or cross-filesystem `EXDEV`/`cross-device`: fall back to the copy+delete bridge (per-entry merge semantics).
- Own-subtree guard: refused when `dest_path` is under `src_path` on the same endpoint, computed via **that endpoint's own path-ops** (§4.8) — never the control machine's `os.path` (a Windows remote path compared with POSIX rules would misdetect or miss the guard). Windows comparisons are case-insensitive and drive-letter-aware.
- Source==dest path (same endpoint, same normalized path): no-op — and this guard applies to plain **copy** too, not just move (a copy onto its own exact source path is refused/no-op rather than silently corrupting via self-overwrite).
- Source-delete-after-move uses protected-path guards.
- Windows same-endpoint fast-path checks same-volume (drive-letter/root comparison before attempting; on failure, `Move-Item`'s own error triggers the copy+delete fallback).

### 5.4 Conflict handling (endpoint-aware)
`dest.exists(final)` / `dest.size(final)` / `src.size(src_path)`. Policies identical: `skipped` (exists + skip / ask-same-size), `keep_both` (`dest.unique_path`), `overwrite`, `ask` (size compare, apply-all, cancel). Conflict dialog copy says **Source / Destination**.

### 5.5 Rename & New folder
- Rename (selected item, either endpoint): guarded prompt → reject empty/`/`/`.`/`..`/protected → `rename()`.
- New folder (at browsed path): guarded prompt → `mkdir()` → refresh pane.
- Rename updates `self.selected`/`self.dest_selected` keys (path-keyed dicts) and refreshes the pane.
- Blocked while transfers/deletes/other renames run (mutual exclusion, §5.8).

### 5.6 Windows destination — required remote tooling
- Requires Windows 10/11 + OpenSSH server + `tar.exe` (present by default ≥1803). `has_tar` is detected **lazily, once, cached** the first time a transfer touches that endpoint (not a connect-time gate — browsing/deleting/renaming a Windows host never requires `tar.exe`). If missing at that point, the transfer is refused with an actionable "install/enable tar.exe (Windows 10 1803+/11 ship it; check `tar --version` in a remote shell) or upgrade Windows" message, surfaced through the existing `friendly_error`/`_show_error` dialog pattern (extended with Windows-specific hints: missing `tar.exe`, locked file, PowerShell execution disabled).
- All remote interaction via `powershell -NoProfile -EncodedCommand <b64>` (encapsulates quoting; matches exporter). Path strings passed to PowerShell are left in the endpoint's native form (Windows path-ops from §4.8 build them); no assumption that forward slashes are always safe.

### 5.7 Windows **source** parity (restored)
The same PowerShell primitives (list/stat/tree/delete/exists/size/mkdir/rename) are used for a **Windows source**, restoring copy/compare/delete for Windows hosts at parity with POSIX sources. The bulk data leg itself does **not** need PowerShell: bsdtar (`tar.exe`) can both create (`-cf -`) and extract (`-xpf -`) tar streams, so a Windows source uses the exact same tar-bridge leg as a POSIX source (§4.3) — one engine, one stream format, only the small primitive layer is OS-specific.

### 5.8 Mutual exclusion & close guards
- Deletion/transfer mutual exclusion extended to rename + new folder.
- Window-close veto while transfers/deletes/renames run is retained; a move mid-delete covered by the delete veto.

### 5.9 Misc edge cases
- Trailing slashes normalized on remote paths for delete/move/finalize, using each endpoint's own path-ops (§4.8) — POSIX `rstrip("/")`, Windows trims both `\` and `/`.
- Paths with spaces/quotes/shell chars only ever go through builders (shlex.quote / EncodedCommand).
- Empty selection, zero-length files, hidden files, very deep trees, unreadable subfolders (compare/export fail loudly, unchanged).
- `~` expansion routed through endpoint `expand()`.
- Two profiles describing the same endpoint compare equal via `key()`.
- Windows path comparisons (own-subtree guard, same-path guard, `key()`-adjacent path checks) are **case-insensitive and drive-letter-normalized** — `C:\Data` and `c:\data\` must be recognized as the same location.
- Windows `MAX_PATH` (260 chars unless long-path support is enabled on that host) is a known limitation: an item whose destination path would exceed it fails with a clear message rather than a cryptic one; not worked around in v1.
- Part naming is the same PID+counter scheme on every OS (§5.2) — no separate Windows-only naming rule.
- `tar` stream from any reader (Local/POSIX/Windows) is plain GNU-compatible `tar`; bsdtar (Windows) and GNU tar (Linux) both read/write it interchangeably — validated against the real hosts available for this project.

---

## 6. UI Redesign

### 6.1 `ConnectionBar` widget (per pane; labeled SOURCE / DESTINATION)
- Endpoint profile dropdown + Save current + Delete.
- Type toggle: `This computer` | `SSH`.
- SSH group: Host combo (auto-discovered), Port, User, Password, Remember, Connect/Browse/Disconnect, status.
- Destination-only: "Open in file manager" — enabled only for local endpoints.
- On connect the bar shows the detected host type (`Linux`/`macOS`/`Windows`) and, on Windows, `tar.exe` availability.
- Tooltips on every field/button (password "Remember" warns plaintext; host refresh hints; etc.).

### 6.2 `BrowserPane` widget (single implementation, both sides)
Consolidates the duplicated source/destination logic: column layout via **one constants set** (`COL_*` + back-compat aliases `SRC_*`/`DST_*`), filter box, toggle-all/invert/select-missing/changed/folders/files, recursive compare, state coloring, tooltip column, delete-button row, refresh/up, sort-mirror.
- The Destination tab is now a full `BrowserPane` (adds a second `TreeModelFilter`+`TreeModelSort`).
- **Dual UNSORTED guard**: both directions; `_child_path`/`_select_counterpart`/sort-mirror generalized to take a pane.

### 6.3 Layout & selection
- Right pane keeps notebook **[Destination | Selected | Transfers]**.
- `⇄ Swap` swaps source/destination (profiles, endpoints, paths, selections).
- Selected tab renders remote targets as `user@host:path`; transfers show "from → to" for both endpoints.
- Legend + status line mention both panes.

### 6.4 UI/functionality separation
- All stateful operation logic (deleter resolver, export routing, move/rename orchestration) lives in small UI helper methods that call endpoint-interface methods; the widgets only wire signals to them. No `isinstance(SshConnection)` special-casing in the widgets — endpoint type is exposed via `kind`/`os_type` only at the connection-bar level.
- Both connections' `on_command` callback is wired to the same console-log sink at connect time (mirrors today's single wiring in `_on_connect`), so the log shows commands from either side of a transfer.

---

## 7. Profiles — Schema v2 (backward compatible)

```json
{ "version": 2,
  "source": { "mac-book": {...}, "This computer": {...} },
  "dest":   { "mac-book": {...}, "This computer": {...} } }
```
- Old flat `{name: entry}` files load with all entries preserved under `source`; `dest` defaults to `{}`; a `"This computer"` entry is created in `dest` if absent.
- Per side: `_active_profile`, last path, last selection restore — mirroring today's source behavior.
- `last_dest` (a local path today) migrates to `dest["This computer"].last_dest`.
- Loader accepts v1/v2; always writes v2; atomic save (existing `profiles.save`). Corrupt JSON → `{}` → v2 defaults (same tolerance as today).

---

## 8. Discovery
- A single scan result is kept in one shared set on the window; both panes' Host combos refresh from it after any scan completes. Concurrent refreshes from both bars are harmless (the second scan's results just merge/dedupe into the same set); no new protocol work.

---

## 9. Tree Export
- Dest export picks `export_remote_tree` (already Windows-aware) when the destination endpoint is SSH; else `export_local_tree`.
- Default save path: local home/Downloads (adjusted fallback when destination is remote).
- `describe_remote_host`/`describe_local_host` reuse `os_type` (drop exporter's private detection).

---

## 10. Design & Code Rules (Best Practices, enforceable)

1. **Layering**: UI → engine → endpoints → builders; imports respect the direction; endpoints never import GTK; UI never runs subprocess/os-ops.
2. **Single source of truth**: one `os_type`, one policy resolver, one merge algorithm (two emitters), one part-lifecycle, one pause mechanism (`proc_sink`).
3. **Command builders are pure & tested**: no inline remote strings outside `commands/`; each builder has an exact-argv test.
4. **Naming**: uniform verbs across endpoints (`list/stat/tree/delete/exists/size/unique_path/mkdir/rename/move/part_path/place/sweep`). Aliases only for back-compat, removed after migration.
5. **Return conventions**: destructive ops → `(ok, err)`; queries → value or `None` (with `last_error` set); exceptions only for programmer errors/invariants — never for expected remote failures.
6. **Threading**: every I/O in a worker thread; UI writes strictly via `GLib.idle_add`; request counters discard stale results; parallelism via `threading.Condition` gate; cancel-all via batch counter; `proc_sink` is the pause/cancel channel shared by both endpoints.
7. **No magic indices**: `COL_*` named constants + aliases; sort keys `GObject.TYPE_INT64`; UNSORTED guard applied in both panes, never propagated.
8. **Small functions, single responsibility**: target < ~50 lines; early returns; descriptive names; no duplication across source/dest in UI (`BrowserPane`/`ConnectionBar`).
9. **Safety invariants immutable**: atomic replace / part files, protected-path guards, symlink-traversal-safe deletes, per-entry merge (never bulk delete siblings), shlex/base64 quoting everywhere.
10. **Testability**: builders + fakes cover OS branches deterministically; local legs run on temp dirs; a real-host manual validation run is a release gate (Windows + one POSIX host).
11. **Migration discipline**: profiles upgrade in-place & idempotently; breaking internal API lands with its test updates in the same commit; `os_type` is cached per-connection and never re-detected mid-session.

---

## 11. Test Plan

### 11.1 Kept as-is (zero regression)
- Local→Local & SSH→Local copy matrices, parsers (`_parse_ls`, `_ls_epoch`, `stat`, `tree`), sort keys/UNSORTED guard, export utilities, local delete symlink/read-only/protected.

### 11.2 Updated for the new API
- Because `copy()` is kept as a compat shim (§4.2), the **majority** of today's Local→Local/SSH→Local transfer tests need **no signature changes**. Only tests that need a remote *destination* call `engine.run(...)` directly; low-level tests (`_place`, `_merge_dir`, `_sweep_stale`, part naming, `unique_path`) are extended with endpoint-aware variants rather than rewritten.

### 11.3 New coverage
| Area | Tests |
|---|---|
| Builders | exact strings: `posix.*`, `powershell.*` (incl. `-EncodedCommand` blob), `local.*`; quoting of spaces/quotes/shell chars; base64 round-trip |
| Remote primitives | `exists`, `size`, `unique_path`, `mkdir`, `rename` on POSIX & PowerShell fakes |
| Local→SSH | file/dir via tar, policies, part+rename (POSIX) & `tar.exe`+PS merge (Windows fake), symlink preservation, cleanup on failure |
| SSH→SSH | same + dual-side mux-dead retry + dual STOP/CONT pause/cancel |
| Move | per-item, fast-path (file→new, dir→new, EXDEV), dir-onto-existing fallback, own-subtree guard, copy-ok-delete-fail reporting, protected refusal |
| Rename / New folder | local + remote (POSIX & Windows fakes), selection-key update, protected-name rejection |
| Profiles migration | v1 flat → v2, data preserved, dest default, `last_dest` migration |
| OS detection | `os_type` converge (Linux/Darwin/Windows/ThisComputer), exporter reuses cache |
| UI smoke (extended) | dual-pane filter/sort/toggle, swap, remote-target render, export save-path fallback, UNSORTED guard both directions, tar.exe-missing hint |

---

## 12. Documentation Updates
- `docs/architecture-and-developer-guide.md` + `AGENTS.md`: new invariants (streaming bridge, remote part+rename, per-entry merge both emitters, unified `os_type`, command builders required, schema v2, symmetric UI, move fast-path, Windows dest/source parity, pause rule).
- This document is the reference spec.

---

## 13. Build Order — Phased Waves

The original single-track order risked a big-bang change spanning transport rewrite + Windows engine + full UI redesign + Move/Rename/Mkdir at once. Restructured into **independently shippable waves**; every wave ends with `python3 tests.py` green, and Wave 1 alone already delivers the primary ask (symmetric Local/POSIX file management) even if Wave 2 (Windows) hits a delay.

**Wave 0 — Foundations (low-risk, no behavior change)**
1. `commands/posix.py`, `commands/local.py` builders + exact-string tests (Windows builders arrive in Wave 2).
2. Unified `os_type` (+ `_uname`/`_export_uname` aliases, §4.5/§4.7) and `key()` on both endpoint classes; exporter converges onto `os_type`.
3. New primitives for Local + POSIX only: `exists`, `size`, `unique_path`, `mkdir`, `rename`, `delete` (method + module aliases).
4. `copy()` compat shim in place (§4.2) — **zero behavior change**; full suite green throughout this wave.

**Wave 1 — POSIX/Local symmetric transport + UI (the core ask)**
5. `TransferEngine` driving SSH→Local & Local→Local (unchanged legs) plus the two **new** legs: Local→SSH(POSIX), SSH→SSH(POSIX) (tar-bridge, remote part/`mv`/sh-merge, part-dir-before-extract sequencing, remote path-ops §4.8).
6. Transport tests for the two new POSIX flows: policies, pause/cancel on both procs, dual-side mux-dead retry, stale-part sweep.
7. `ConnectionBar` + `BrowserPane` widgets; symmetric panes; `⇄ Swap`; remote-target rendering (`user@host:path`); dual UNSORTED guard.
8. Move (copy+delete + same-endpoint fast-path, own-subtree/EXDEV guards) + Rename + New folder — POSIX + Local only.
9. Profiles v2 migration; discovery wired to both bars (shared set); export routing/save-path fallback.
10. UI smoke tests updated for dual panes (POSIX-only).
11. **Wave 1 checkpoint**: any Local/POSIX-SSH source ↔ any Local/POSIX-SSH destination is fully usable and shippable on its own.

**Wave 2 — Windows endpoints (destination + restored source parity)**
12. `commands/powershell.py` builders (`-EncodedCommand`) + exact-string/base64-encoding tests.
13. Windows primitives (`list/stat/tree/delete/exists/size/unique_path/mkdir/rename`) + lazy `has_tar` detection (§5.6).
14. Windows destination finalization (`tar.exe` extract + PowerShell per-entry merge + locked-file retry + exit-code tolerance policy).
15. Windows **source** data leg (`tar.exe -cf -`, §4.3/§5.7) + primitive parity.
16. Transport tests with Windows fakes (11.3 Windows rows).
17. UI: host-type/`tar.exe` status indicators; Windows-specific `friendly_error` hints.
18. **Wave 2 checkpoint**: manual validation against one real Windows SSH host and one real POSIX host (both available) before calling this wave done.

**Wave 3 — Polish & docs**
19. `docs/architecture-and-developer-guide.md` + `AGENTS.md` updated to match the shipped design.
20. Final full-suite run; this document's status updated to reflect what shipped vs. deferred.

---

## 14. Risks & Open Items
| Risk | Mitigation |
|---|---|
| sh / PowerShell merge bug → data loss | Minimal scripts, quoting everywhere, unit tests on both emitters, and real-host validation gate; never bulk-delete siblings. |
| Windows `tar.exe` absent / bsdtar quirks | `has_tar` detection + hint; bsdtar parses GNU tar streams; real-host pass. |
| Windows file-locking during merge/delete | Retry-loop then clear failure; part kept for manual cleanup; swept next run. |
| Two-connection pause/cancel bookkeeping | Single shared `proc_sink`; both endpoints same contract; tests for both-stop. |
| UNSORTED segfault with two sort models | Symmetric guard + tests; constant aliases keep existing smoke compiling. |
| Evolution of OS detection (Windows Server, WSL) | `os_type` detection handles unknown as Windows (existing exporter behavior); WSL is Linux. |
| Large SSH→SSH trees | Direct bridge, bounded memory; stall on both procs. |

- **Open**: whether move-delete needs its own confirmation dialog (default: reuse delete confirm, prefixed "move").
- **Open**: whether `⇄ Swap` should also swap an in-flight transfer list (default: swap endpoint config/paths/selections only; transfers keep original direction).
- **Open**: Windows `tar.exe`-to-stderr progress noise (suppressed in v1 like current drain; tolerated per the exit-code policy in §5.2).

---

## 15. Acceptance Criteria (Definition of Done)

Concrete, testable statements per functional requirement — used as the go/no-go checklist for each wave.

| Req | Done when… |
|---|---|
| F1 | Connecting either pane to This-computer / POSIX SSH / Windows SSH shows the correct detected type; a Windows destination shows `tar.exe` status once a transfer is attempted (not at connect time). |
| F2 | A file and a directory copy correctly for Local→SSH(POSIX), SSH(POSIX)→SSH(POSIX) (Wave 1), and Local→SSH(Windows), SSH(Windows)→Local (Wave 2) — mtime/mode preserved, all 4 policies exercised in tests, pause/resume/cancel verified on both legs of each flow. |
| F3 | Recursive compare produces identical missing/differ/conflict/same/extra classification for every endpoint pair (unit-tested against fakes, no real hosts needed). |
| F4 | Deleting a checked folder containing a symlink never touches the symlink's target, on Local, POSIX-remote, and Windows-remote (fake-verified; Windows confirmed on the real host). |
| F5 | A same-endpoint move of a file is verified (via test assertion) to spawn **no** transfer stream — only a rename call; a cross-endpoint move deletes the source only after the destination write succeeds; a forced destination failure leaves the source provably untouched. |
| F6 | Renaming an item updates any existing selection-dict entry for that path; New Folder appears after refresh; both rejected with a clear message while a transfer/delete is active. |
| F7 | Export produces a valid YAML for This-computer, POSIX, and Windows sources/destinations, reusing the unified `os_type` (no exporter-only detection path left). |
| F8 | Loading an existing v1 `profiles.json` produces a v2 structure with 100% of prior data (hosts, passwords, last paths) visible in the UI with zero user action required. |
| F9 | One discovery scan (triggered from either bar) populates both panes' host dropdowns. |
| F10 | Every connection-bar control and pane button has a non-empty tooltip; `⇄ Swap` inverts source/destination state; window-close during an active transfer/delete/rename is still vetoed. |

**Wave-level gates**: Wave 0/1 close only when `python3 tests.py` is green with no Windows-dependent code paths touched; Wave 2 closes only after the manual real-host validation pass (§13) succeeds against both an available Windows host and a POSIX host.

---

## 16. Implementation Status (updated 2026-08-24 — post-cleanup)

**⚠️ Read `docs/implementation-continuation-plan.md` first for the current, authoritative, self-contained handoff.**

**Shipped (implemented + unit/smoke tested):**
- `commands/` builders (posix, powershell, local, paths) with exact-string tests; `commands/paths.py` used for all remote-path joins.
- Uniform endpoint contract on `SSHConnection`/`LocalConnection`; unified `os_type` (+ aliases); exporter converges on `os_type`; `kind` markers on both transports.
- `transfer_engine.py`: `run(dest, src, …)` for every endpoint pair + `move(…)` fast-path/guards.
- **Clean `app/` UI** (replaces the deleted legacy `ui.py`): `app/window.py` (`AppWindow`), `app/connections.py` (`ConnectionBar`+`ConnectionDialog`), `app/panels.py` (`DirPane`), `app/profiles.py` (schema v2). Destination routing via `_dest_is_ssh()`; remote dest paths via `rp.join(family,…)`.
- Tests split into `tests/` with thin `python3 tests.py` runner; `app` group + two GTK smokes exercise the UI.

**Resolved (was deferred):**
- Remote-destination dead (missing `kind`) — fixed.
- Windows listing dead (PowerShell TAB/UTF-8/root-error) — fixed.
- Dest navigation/paths used control `os.path` — now endpoint-aware via `rp`.
- Legacy connection UI + separate dest bar → compact symmetric `ConnectionBar`/`ConnectionDialog`.
- Profiles v1 → schema v2 (no migration).
- Legacy `ui.py`/`panes.py`/`profiles.py`/`tests/test_ui.py` **deleted** (archived under `docs/old/legacy-ui/`).

**Known / deferred (needs real hardware):**
- Windows **source**→local copy uses a POSIX `tar` command in `ssh_transport._copy_tar` (no Windows branch); the symmetric **destination** path uses `tar.exe`. Validate on a real Windows host.
- Real-host pass on Mac + Windows (browse/transfer/compare/delete/export, non-ASCII filenames) — session 3's harness already covered local + SSH-dest behavior without a network host.
