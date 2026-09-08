# Requirements: Destination Disk Space Display & Source Selection Size Hint

**Document Version:** 1.1
**Date:** 2026-09-08
**Status:** Implemented — transports (`disk_space`), `DirPane` `status_row` + lazy folder sizes, and the `AppWindow` status labels (hint + disk + post-copy preview) are in, with tests. See the review notes at the bottom of §15 for the three wording adjustments made during review.
**Target Systems:** `app/window.py`, `app/widgets/dirpane.py`, `app/widgets/endpoint.py`, `ssh_transport.py`, `local_transport.py`, `commands/posix.py`, `commands/powershell.py`, `commands/local.py`, `tests/`

---

## 1. Overview / Problem Statement

When copying files from source to destination, the user has no idea how much space the destination has. They cannot plan accordingly, and they only discover a "disk full" failure mid-transfer. Additionally, when selecting items in the source pane, there is no feedback about the total size of the selection or whether the destination can hold it.

This feature adds:

1. **Destination disk space display** — total and free space for the destination's current folder, shown in the destination pane's summary bar, with a post-copy preview when source items are selected.
2. **Source selection size hint** — total size and item count of the current source selection, shown in the source pane's summary bar, color-coded green/red based on whether the net copy fits in the destination's free space.
3. **Lazy recursive folder size calculation** — both panes calculate folder sizes in the background on populate (replacing the current `--` in the Size column), because accurate selection sizing and conflict accounting require recursive folder sizes on both sides.

---

## 2. Goals and Non-Goals

### Goals

1. Show the destination's total and free disk space in a friendly, human-readable format.
2. Show the user, before transferring, whether their current selection fits in the destination (color-coded feedback).
3. Show a post-copy preview on the destination pane ("After copy: X / Y free") so the user sees the impact of the transfer.
4. Account for overwrites: net space impact = selected source size − conflicting destination size.
5. Keep all queries non-blocking (background threads, `GLib.idle_add` UI updates) and stale-result-safe.
6. Reuse existing architecture: transport contract methods, `human_size`, `classify_items` states, the existing ↻ refresh button.

### Non-Goals

- **Source pane disk space display** — disk space is shown for the destination only.
- **Transfer-time space enforcement** — this feature does not pre-check or abort transfers based on space; it only informs the user.
- **Space reservation / staging guarantees** — no reservation of free space before a transfer.
- **Showing size in the Selected tab or Transfer button** — tab label and button text stay as-is (`Selected (N)`, `▶ Transfer Selected (N)`).
- **Caching folder sizes across navigations** — sizes are recalculated on each populate (see §16 Deferred).
- **Disk space display for the source pane** — explicitly out of scope per user decision.
- **Amber/marginal traffic-light states** — color coding is a simple binary (fits / doesn't fit).

---

## 3. Users / Actors

| Actor | Description |
|---|---|
| **End user** | Uses the app to browse local/SSH endpoints and copy files. The primary beneficiary of both displays. |
| **Source connection** | `LocalConnection` or `SSHConnection`; provides selected item sizes (recursive for folders). |
| **Destination connection** | `LocalConnection` or `SSHConnection`; provides disk space (total/free) and conflicting item sizes. |

---

## 4. Functional Requirements

### 4.1 Destination disk space display

| # | Requirement |
|---|---|
| F1 | The destination pane's summary bar shows a right-aligned label with the destination's disk space for the current folder's filesystem, formatted `X / Y free` (e.g., `12.3 GB / 256 GB free`), using `human_size` for both values. |
| F2 | The disk space label is a **separate label** from the existing left-aligned comparison-counts summary, but lives in the **same row** (a new horizontal `status_row` box inside `DirPane`). The existing summary keeps `xalign=0` and gains `hexpand=True`; the disk space label is `xalign=1`. |
| F3 | The disk space is queried: (a) when the destination connects, (b) on destination navigation, (c) on the existing ↻ refresh button, (d) after a transfer batch completes, (e) after a delete completes. |
| F4 | **Drill-down avoidance**: when the user navigates to a child folder of the last-queried path, the cached disk space result is reused without a new query. Local connections additionally compare `os.stat(path).st_dev` for exact filesystem identity; SSH connections use the path-prefix heuristic (new path starts with cached path + `/`). Any other navigation re-queries. |
| F5 | The disk space query runs in a background thread; the UI updates via `GLib.idle_add`. |
| F6 | When the destination is disconnected, the disk space label is hidden. |
| F7 | When the disk space query fails (SSH error, permission denied, network drop), the label shows `Disk space unavailable` and the error is logged to the Log tab (amber/warn level). The label is retried on the next navigation/refresh. This applies whether or not source items are selected — a failed query suppresses the post-copy preview (F9) as well. |
| F8 | The normal (no-selection) disk space label uses the default foreground color — no color coding. |

### 4.2 Post-copy preview (destination pane)

| # | Requirement |
|---|---|
| F9 | When the source has at least one selected item **and** the destination is connected, the destination's right label switches to `After copy: X / Y free`, where X = max(0, current free space − net needed space) (see §6.3); a red color signals the case where net > free. |
| F10 | The post-copy preview is color-coded: **green** if net needed space ≤ current free space (fits), **red** otherwise. |
| F11 | If the net needed space is negative (overwrites free more space than the copy uses), the preview shows a free-space value **larger** than the current free space and is colored green. |
| F12 | When the source selection is empty, the destination label reverts to the plain `X / Y free` form (F1). |
| F13 | While folder sizes are still calculating (selection size incomplete), the preview shows with a `~` prefix on the size and no color; it finalizes (color applied) when calculation completes. |

### 4.3 Source selection size hint

| # | Requirement |
|---|---|
| F14 | The source pane's summary bar shows a right-aligned label (same `status_row` layout as F2) with the current selection's total size and item count, formatted `Selected: 2.3 GB (1,247 files)`. |
| F15 | The item count matches the count used by the transfer button (`▶ Transfer Selected (N)`) — i.e., all selected entries (files and folders). |
| F16 | The hint is **hidden entirely** when nothing is selected. |
| F17 | The hint is color-coded: **green** if net needed space ≤ destination free space, **red** otherwise (same threshold as F10). |
| F18 | When the destination is **not connected**, the hint shows size and count only, with **no color coding**. The same applies when the destination is connected but the disk space query has failed or is still in flight — color requires a known free-space value. |
| F19 | While folder sizes are still calculating, the hint shows `Selected: ~2.3 GB (1,247 files)` with a spinner and no color; it finalizes when calculation completes. |
| F20 | The hint updates live as the user checks/unchecks items (reusing the existing `selection_changed` callback path). |
| F21a | **Impl note**: the item count in the hint always renders as `(N files)` (no singular form), matching the decided format in D10/F14. |

### 4.4 Lazy recursive folder size calculation (both panes)

| # | Requirement |
|---|---|
| F21 | On populate (`DirPane.set_items`), folder sizes are calculated recursively in background threads for **both** source and destination panes. |
| F22 | As each folder's size completes, the Size column for that row updates from `--` to the human-readable size (via `human_size`), and the raw `COL_SIZE` column is updated so sorting by size stays correct. |
| F23 | A small spinner is shown in the summary bar (next to the right label) while any folder size calculation is in flight. |
| F24 | Folder size calculation reuses the existing recursive size methods (`LocalConnection.stat_remote` / `SSHConnection.stat`), which already return `{"bytes", "files"}`. |
| F25 | If a folder's size calculation fails entirely (returns `None`) the Size column keeps the em-dash, the folder is marked `failed` (the hint stays provisional `~`), and the failure is logged (amber). A **partial** result (local `stat_remote` reports `partial` on unreadable subtrees; SSH is best-effort and never detects partial) shows `~1.2 GB` (tilde prefix) and is logged (amber). |
| F26 | Folder size calculations are bounded: a small worker pool (e.g., 2–4 concurrent calculations) prevents thread explosion on large listings. |

---

## 5. Behavioral Rules / Workflows

### 5.1 Summary bar layout (both panes)

```
DirPane (Gtk.Box VERTICAL)
├── controls row (filter + quick-select)          [existing]
├── Gtk.ScrolledWindow (tree)                     [existing]
└── status_row (Gtk.Box HORIZONTAL)               [NEW]
    ├── summary (Gtk.Label, xalign=0, hexpand=True)   [existing, moved into status_row]
    └── right_label (Gtk.Label, xalign=1)             [NEW]
```

- The `status_row` replaces the current direct packing of `self.summary` into `DirPane`.
- `right_label` content differs per pane:
  - **Source pane**: selection hint (F14–F20).
  - **Destination pane**: disk space / post-copy preview (F1–F13).

### 5.2 Disk space query lifecycle

```
connect / navigate (non-child) / refresh / transfer-complete / delete-complete
        │
        ▼
[background thread] query disk_space(current_path)
        │
        ▼
[GLib.idle_add] update right_label + cache {path, total, free}
```

- Drill-down navigation (child of cached path) → reuse cache, no query (F4).
- Non-child navigation / refresh / transfer-complete / delete-complete → re-query; the **previous cached value remains displayed** until the new result arrives (smooth transition, no flicker). If the new query fails, the label switches to `Disk space unavailable` (F7).
- Local: `shutil.disk_usage(path)` → `(total, used, free)`.
- SSH POSIX: `df -B1 --output=size,used,avail <path>` (GNU) / `df -k <path>` (macOS, parse KB → bytes).
- SSH Windows: PowerShell `Get-PSDrive` / `[IO.DriveInfo]` → `AvailableFreeSpace`, `TotalSize`.

### 5.3 Selection hint update lifecycle

```
check/uncheck item
        │
        ▼
recompute: Σ selected source sizes (recursive, may be in-flight)
         − Σ conflicting destination sizes (states "differ"/"same")
        │
        ▼
update right_label: "Selected: X (N files)" + color (green/red/none)
```

- While any folder size is in-flight, show `~` prefix + spinner, no color (F19).
- When all sizes complete, finalize text and color (F13/F19).
- Destination disconnected → no color (F18).

### 5.4 Swap (⇄) behavior

- On endpoint swap, both panes re-trigger their listing loads (existing `_remote_req`/`_dest_req` bump).
- The new destination pane re-queries disk space; both panes re-trigger folder size calculations.
- The generation counter (see §6.4) discards any in-flight results from before the swap.

---

## 6. Data / State / Business Rules

### 6.1 Disk space cache

Per destination pane, a single-entry cache:

```
disk_cache = {"path": str, "total": int, "free": int}
```

- Used for drill-down avoidance (F4).
- Invalidated on: connect, non-child navigation, refresh, transfer completion, delete completion, swap.

### 6.2 Folder size state

Per pane, per populate:

```
size_state = {
    "pending": {name: ...},   # folders still calculating
    "done": {name: {"bytes": int, "files": int}},
    "partial": {name: {"bytes": int, "partial": True}},
}
```

- `set_items` resets this state and starts calculations for all folders in the listing.
- Completion updates the model row (`COL_SIZE`, `COL_SIZE_TEXT`) and re-runs the summary computation.

### 6.3 Net needed space (conflict accounting)

```
net_needed = Σ size(selected source items) − Σ size(conflicting destination items)
```

- Conflicting destination items = destination items whose state (from `classify_items`) is `differ` or `same` and whose name matches a selected source item.
- `net_needed` may be negative (overwrite frees space) — handled gracefully (F11).
- Color rule: `net_needed ≤ free_space` → green; else red. Applies to both the source hint (F17) and the destination preview (F10).
- While any relevant folder size is in-flight, `net_needed` is provisional (`~` prefix, no color).

### 6.4 Generation counter (stale-result protection)

- Each pane keeps a monotonically increasing generation integer.
- Every background task (disk space query, folder size calculation) captures the generation at dispatch time and tags its result.
- Results are applied only if the captured generation still equals the pane's current generation; otherwise discarded.
- Generation is bumped on: navigation, refresh, swap, disconnect, connect.

---

## 7. UI / UX Requirements

| # | Requirement |
|---|---|
| U1 | Right-aligned labels must not overlap the left-aligned summary; the summary label expands (`hexpand`) to push the right label to the row's end. |
| U2 | Text must stay compact and readable at the default window width (1280 px); long paths or counts must not cause layout thrash. |
| U3 | The spinner (F23) is small and unobtrusive, shown only while calculations are in flight. |
| U4 | Color coding uses the existing log-tag color conventions where sensible (green = ok, red = bad); colors must be distinguishable on the default GTK theme. |
| U5 | Tooltips: the disk space label and selection hint should have tooltips explaining what they mean (e.g., "Free space on the destination filesystem", "Total size of selected items; green = fits in destination"). |
| U6 | No new buttons are added to the UI; the existing ↻ refresh button covers manual refresh (F3c). |

---

## 8. Interfaces / APIs / Integrations

### 8.1 New transport method: `disk_space(path)`

Both `LocalConnection` and `SSHConnection` gain a uniform method:

```
disk_space(path) -> {"total": int, "free": int} | None
```

- **Local** (`LocalConnection`): `shutil.disk_usage(path)` → `(total, used, free)`.
- **SSH POSIX** (`SSHConnection`): new command builders in `commands/posix.py`:
  - GNU/Linux: `df -B1 --output=size,used,avail <path>` (parse `size` and `avail`).
  - macOS/Darwin: `df -k <path>` (parse KB → bytes).
- **SSH Windows** (`SSHConnection`): new builder in `commands/powershell.py`:
  - `(Get-PSDrive -Name (Resolve-Path <path>).Drive.Name)` or `[IO.DriveInfo]` → `AvailableFreeSpace`, `TotalSize`.
- Returns `None` on failure (non-zero rc, parse error, network drop).
- Follows the existing pattern of `stat`/`exists`/`mkdir` (OS-type branch, `_run_cmd` execution, pure command builders).

### 8.2 Reused existing components

| Component | Usage |
|---|---|
| `human_size(n)` (`app/widgets/dirpane.py`) | Formatting all byte values. |
| `LocalConnection.stat_remote` / `SSHConnection.stat` | Recursive folder size calculation (F24). |
| `classify_items` (`app/window.py`) | Conflict states (`differ`/`same`) for net space accounting. Directories now compare recursive `(bytes, files)` too (see §4.4): a same-named folder whose sizes differ flips `same`→`differ` once both sides' lazy sizes land (`_reclassify_folder_state`), and never flips while either side's size is `failed`/`partial` (F25 / A9 — local `stat_remote` now flags `partial` on walk undercounts). |
| `DirPane.set_items` / `_notify_selection` / `selection_changed` | Populate hook and live selection updates. |
| `AppWindow._summarise` / `_recompute_states` | Existing summary computation; extended to drive the right labels. |
| ↻ refresh button (`EndpointBar`) | Manual disk space refresh (F3c). |
| `GLib.idle_add` | All UI updates from background threads. |
| Log tab (`log_warn` tag) | Disk space query failures and partial folder size warnings. |

### 8.3 No changes to

- `Transfer` object / transfer engine snapshot semantics.
- `_swap_endpoints` trio-swap logic (disk space is computed, not stored per-side state).
- Profiles schema, discovery, exporter.

---

## 9. Error Handling / Failure Recovery

| # | Scenario | Behavior |
|---|---|---|
| E1 | Disk space query fails (SSH error, permission denied, network drop) | Label shows `Disk space unavailable`; error logged to Log tab (amber); retried on next navigation/refresh (F7). |
| E2 | Folder size calculation fails / partial (permission denied on subfolder) | Size column shows `~1.2 GB`; error logged (amber); selection hint uses the partial value with `~` prefix (F25). |
| E3 | Destination disconnects mid-session | Disk space label hidden; source hint loses color (F18); generation counter discards in-flight results. |
| E4 | User navigates while calculations are in flight | Generation counter discards stale results; new populate starts fresh calculations (F26/§6.4). |
| E5 | User swaps endpoints while calculations are in flight | Same as E4; both panes re-trigger on the new listing loads. |
| E6 | Disk space query returns absurd values (e.g., 0 total) | Display as reported by the OS; no special-casing. |
| E7 | Selection changes while folder sizes are in-flight | Hint shows provisional `~` value; finalizes when calculations complete (F19). |

---

## 10. Security / Privacy

| # | Requirement |
|---|---|
| S1 | No new credentials or secrets are introduced; disk space queries use the existing connection/session. |
| S2 | Remote commands are built by the existing pure command builders (`commands/posix.py`, `commands/powershell.py`) with the same quoting discipline as `stat`/`exists` — no new shell-injection surface. |
| S3 | Disk space and folder size values are display-only; they are never written to profiles, logs beyond the existing Log tab, or any persistent store. |
| S4 | Permission-denied errors are surfaced as warnings, not crashes; no elevation or privilege changes are attempted. |

---

## 11. Performance / Scalability

| # | Requirement |
|---|---|
| P1 | All disk space queries and folder size calculations run off the GTK thread; the UI never blocks. |
| P2 | Folder size calculations are bounded to a small worker pool (2–4 concurrent) per pane (F26). |
| P3 | Drill-down navigation avoids redundant disk space queries (F4). |
| P4 | For SSH sources, recursive folder size calculation may be slow on large trees; the spinner + `~` prefix communicate progress without blocking. |
| P5 | Rapid check/uncheck of many items must not spawn unbounded work; the selection summary recomputes from already-known sizes and only waits on in-flight folder calculations. |
| P6 | No new per-item storage beyond the existing `meta`/`selected` dicts; folder sizes are stored in the model columns and the per-populate `size_state`. |

---

## 12. Persistence / State Management

| # | Requirement |
|---|---|
| M1 | No new persistent state. Disk space cache and folder size state are in-memory only, per pane, per populate. |
| M2 | The disk space cache is invalidated on connect, non-child navigation, refresh, transfer completion, delete completion, and swap (§6.1). |
| M3 | Folder size state is reset on every `set_items` (§6.2). |

---

## 13. Observability / Auditability

| # | Requirement |
|---|---|
| O1 | Disk space query failures and partial folder size calculations are logged to the Log tab (amber/warn level) with enough context (path, error) to diagnose. |
| O2 | Successful disk space queries are **not** logged (avoid noise); the display itself is the feedback. |
| O3 | The Log tab's existing color conventions are reused (`log_warn` for recoverable issues). |

---

## 14. Testing / Acceptance Criteria

### 14.1 Unit tests (extend `tests/`)

| # | Test |
|---|---|
| T1 | `commands/posix.py`: exact-string tests for the GNU `df` and macOS `df -k` command builders. |
| T2 | `commands/powershell.py`: exact-string test for the `Get-PSDrive`/`DriveInfo` builder. |
| T3 | `LocalConnection.disk_space`: returns `{"total", "free"}` for a real local path; returns `None` for an invalid path. |
| T4 | `SSHConnection.disk_space`: parses GNU `df -B1` output, macOS `df -k` output, and PowerShell output correctly (fake command runner). |
| T5 | `human_size` formatting of disk space values (already covered; extend if needed). |
| T6 | Net space accounting: source selection + conflicting dest items → correct `net_needed`, including negative net (overwrite frees space). |
| T7 | Generation counter: stale results (older generation) are discarded; current results applied. |
| T8 | Drill-down cache: child-path navigation reuses cache; sibling/parent/other navigation re-queries. |
| T9 | Partial folder size: `~` prefix applied on failure; summary uses partial value. |

### 14.2 UI behavior tests (extend `tests/test_app.py`)

| # | Test |
|---|---|
| T10 | Source pane: no selection → right label hidden; selection → `Selected: X (N files)` shown. |
| T11 | Source pane: destination connected → color applied (green/red); destination disconnected → no color. |
| T12 | Destination pane: right label shows `X / Y free`; with source selection shows `After copy: X / Y free`. |
| T13 | Destination pane: disk space query failure → `Disk space unavailable` shown. |
| T14 | Folder Size column: `--` replaced by human-readable size after calculation completes. |
| T15 | Spinner shown while calculations in flight, hidden when done. |

### 14.3 Acceptance criteria (manual)

| # | Criterion |
|---|---|
| A1 | Connect a local destination → summary bar shows `X / Y free` right-aligned. |
| A2 | Drill into a child folder → no visible delay/query (cached); navigate to a different mount → value updates. |
| A3 | Select items in source → source shows `Selected: X (N files)`; destination shows `After copy: X / Y free`; both green when it fits. |
| A4 | Select more than the free space → both turn red. |
| A5 | Overwrite scenario: replacing a larger file with a smaller one → preview shows increased free space, green. |
| A6 | Disconnect destination → source hint loses color; destination label hidden. |
| A7 | Kill the SSH connection mid-session → `Disk space unavailable` + Log tab entry; app stays responsive. |
| A8 | Run `python3 tests.py` — full suite green. |

---

## 15. Assumptions / Decisions

| # | Decision / Assumption |
|---|---|
| D1 | Disk space display is **destination-only** (user decision). |
| D2 | Color coding is a **simple binary** (green = fits, red = doesn't), no amber zone (user decision). |
| D3 | Conflict accounting is **accurate**: net = source − conflicting dest sizes (user decision). |
| D4 | Folder sizes are calculated lazily on populate for **both panes** (user decision, required by D3). |
| D5 | The selection hint format is `Selected: X (N files)` — simple, no explicit net/overwrite numbers (user decision); color conveys the net impact. |
| D6 | Manual refresh reuses the existing ↻ button (user decision). |
| D7 | Drill-down avoidance uses `st_dev` (local) and path-prefix (SSH) heuristics; both are heuristics and may occasionally re-query or reuse across a mount boundary — acceptable per user intent. |
| D8 | The app's local platform is Linux (per environment); SSH targets may be Linux/macOS/Windows. |
| D9 | `df`/`Get-PSDrive` output parsing follows the same OS-type branching pattern as existing `stat`/`exists` methods. |
| D10 | The item count in the hint counts all selected entries (files + folders), matching the transfer button's N. |

### Review adjustments (implemented as-documented — deviations from earlier wording)

| # | Adjustment |
|---|---|
| A9 | `stat_remote` gains a `partial` key **only when True** (additive; existing equality asserts on `{"bytes","files"}` keep passing). SSH `stat` never detects partial (find stderr is discarded). |
| A10 | Folder-size threading lives in `app/window.py` (daemon `threading.Thread` + `BoundedSemaphore(2)` per side), keeping `dirpane.py` a pure view. |
| A11 | Stale-result protection piggybacks on the existing `_remote_req`/`_dest_req` counters (also bumped on disconnect) instead of a dedicated generation integer — same semantics, fewer moving parts. |
| A12 | `human_size_compact()` strips a trailing `.0` so totals render `256 GB` (free space keeps one decimal: `12.3 GB / 256 GB free`). |
| A13 | The projected free space in the `After copy` preview is clamped to ≥ 0 (F9); red color signals `net > free`. |

---

## 16. Deferred or Future Scope

| # | Item |
|---|---|
| X1 | **Folder size caching across navigations** — recalculating on each populate is simpler; caching per-path could be added later if large-folder navigation proves slow. |
| X2 | **Transfer-time space enforcement** — pre-flight check or abort-on-low-space during transfer. |
| X3 | **Source pane disk space display** — explicitly out of scope per user decision; could be revisited. |
| X4 | **Amber/marginal traffic-light states** — deferred; binary color is the current decision. |
| X5 | **Space reservation / staging guarantees** — no reservation mechanism. |
| X6 | **Showing size in Selected tab / Transfer button** — deferred; user chose to keep as-is. |