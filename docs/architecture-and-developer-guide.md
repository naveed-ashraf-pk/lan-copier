# `lan-copier`: Architecture & Developer Knowledge Base

This document provides a comprehensive, high-level guide for developers and AI agents working on `lan-copier`. It outlines the system architecture, core workflows, UI patterns, safety mechanisms, and critical codebase conventions.

---

## 1. System Overview & Core Philosophy

`lan-copier` is a high-performance, lightweight GTK 3 desktop application for comparing, synchronizing, transferring, and managing files between:
1. **Remote SSH Source $\rightarrow$ Remote SSH / Local Destination** (browsing over OpenSSH multiplexing).
2. **Local Source $\rightarrow$ Local / Remote SSH Destination** (serverless folder-to-folder copy on the same machine, or pushed over SSH).

Any supported endpoint — This computer, SSH (Linux/macOS), or SSH (Windows, via PowerShell + `tar.exe`) — can act as source *or* destination.

### Core Principles
- **Zero Third-Party Python Dependencies**: Uses Python 3 standard library (`subprocess`, `shlex`, `shutil`, `os`, `stat`, `threading`, `json`) + `PyGObject` (`gi.repository.Gtk`, `GLib`, `Pango`, `Gdk`, `GObject`).
- **Strict Atomicity & Crash Safety**: File transfers use PID-tagged `.lan-copier-part-*` files, fsync before replacement, and atomic renames (`os.replace` / entry-by-entry directory merges). Remote destinations stage into a same-directory part dir then `mv` (file) or **per-entry merge** (dir); Windows destinations stream via the bundled `tar.exe`.
- **Transport Symmetry**: `LocalConnection` inherits from `SSHConnection` to share the identical copy pipeline, part-file sweeping, stall-detection, and conflict handling logic.
- **Responsive UI via Background Threading**: All network, disk I/O, directory walks, and deletions execute in worker threads, communicating back to the GTK main loop exclusively via `GLib.idle_add`.

---

## 2. Codebase Structure & Component Responsibilities

| File | Primary Role & Responsibilities |
|---|---|
| `main.py` | Entry point. Initializes GTK, configures dark/light theme preference, and launches `app.window.AppWindow`. |
| `app/window.py` | **Current UI** (`AppWindow`): composes two side columns (each = one `EndpointBar` above a `DirPane`) in a horizontal Paned, a **vertical Paned** above a Notebook (Selected + Transfers + Log tabs), and a delete-progress stack page. Hosts the entire connection state-machine and the transfer/export/delete engine. The single `_on_connect_clicked(bar)` opens the `ConnectionDialog` popup for connect/change/disconnect of either side. Destination paths for a remote dest are built with `commands.paths.join(family, ...)`; selection changes cascade to the Selected tab + delete buttons; dest auto-reloads after transfers. |
| `app/widgets/endpoint.py` | `EndpointBar`: the **merged single-row bar** per side — a display-only connection label (no dropdown) + one popup-driven `conn_btn` (turns green `suggested-action` when connected), the path field + nav (`⬆ ⌂ ↻`), `📂` open (local only, both sides), `⤓` export (with a running-states sensitivity), `🗑` delete (live count). Path + actions hide until a side is connected. Each bar sits *inside* its own side column above the `DirPane`, so the horizontal paned divider resizes bar + tree together. |
| `app/widgets/dirpane.py` | `DirPane`: symmetric browse **tree** shared by both sides (filter + quick-select `Missing`/`Changed`/`Invert`/`Folders`/`Files` **above the tree**, colourised TreeModelSort treeview with checkboxes, state column, summary). Pure view — the window supplies items/states; the path bar lives on `EndpointBar`. |
| `app/widgets/dialog.py` | `ConnectionDialog`: the single modal popup for connecting/changing/disconnecting either side — connection combo (`This computer` / saved profiles / `+ New…`), SSH editor with LAN discovery + password/remember + "Save as", and a `Disconnect` button when already connected. `collect()` → `{mode: local|ssh|disconnect, params}`. |
| `app/profiles.py` | **Schema v3** store (`config/profiles.json`): `{version:3, profiles:{name:{host,port,user,hostname,password,remember}}, last:{source_profile,dest_profile}}`. Profiles are matched by the **resolved remote hostname** first (`find_profile`), then `host+port+user`; `merge_duplicates` collapses legacy "name 2/3..." rows. "This computer" (`profiles.THIS`) is never stored. |
| `ssh_transport.py` | `SSHConnection` (now with `kind="ssh"`). Manages OpenSSH master control sockets, remote shell escaping (`shlex.quote`/PowerShell `-EncodedCommand`), directory listing/stat, copy pipelines (`scp` & `tar`), remote staging parts + merge/rename, OS detection (`os_type`), resolved `hostname()` (profile identity), endpoint primitives (`exists`/`size`/`unique_path`/`mkdir`/`rename`/`delete`), and remote deletions. |
| `local_transport.py` | `LocalConnection` class (`kind="local"`); fails back to the local filesystem for every endpoint primitive. |
| `transfer_engine.py` | Pure engine: `run(dest, src, ...)` drives copy for any endpoint pair and `move(...)` implements move (copy + delete-on-success, same-endpoint rename fast-path). Remote destinations stream tar → staging part → `mv`/per-entry merge. |
| `commands/posix.py` · `commands/powershell.py` · `commands/local.py` · `commands/paths.py` | Pure, unit-tested command builders (POSIX & PowerShell exact strings incl. the real-TAB UTF-8 PowerShell `list_dir`, local fs wrappers, OS-family path ops). All remote strings live here; endpoints never interpolate paths directly. |
| `discovery.py` | Subnet LAN scanner. Uses background thread workers and socket connects on port 22 to auto-discover active SSH hosts on the local network. |
| `tree_exporter.py` | YAML tree snapshot exporter for local/POSIX/Windows sources and destinations. |
| `tests/` (root `tests.py`) | Self-contained plain-assert test suite, one file per module (`test_commands`, `test_ssh`, `test_local`, `test_engine`, `test_export`, `test_app`) + shared `common.py` fixtures. Run with `python3 tests.py`. The `app` suite (profiles v2, classify, _autosave_profile dedupe, color palettes) plus two GTK smokes (local & remote-dest AppWindow transfers) exercise the UI. |
| `docs/` | Handoffs (`implementation-continuation-plan.md`, `code-review-handoff.md`), architecture guide, legacy feature specs + archived legacy UI (`old/legacy-ui/`), feature plan (`symmetric-endpoints-feature-plan.md`). |

---

## 3. Key Features & Workflows

### 3.1 Dual-Pane Comparative Browser
- **Source Panel (Left)**: Displays remote SSH directory or local source folder. Supports path entry, filter text, file/folder navigation, and checkboxes for transfer selection.
- **Destination Panel (Right)**: Displays target local folder with tabs:
  - *Destination*: Live view of destination files, comparison states, checkboxes for destination item deletion.
  - *Selected*: Queue of source items ready to copy with their targeted destination paths.
  - *Transfers*: Active, queued, paused, and finished transfer jobs with progress, transfer rate, ETA, and controls.
- Both panes are the same `DirPane` widget (app/widgets/dirpane.py); the modern window (app/window.py) is symmetric by construction through two `EndpointBar`s + two `DirPane`s.
- **Comparative State Engine (`classify_items`)**:
  Computes differences between source and destination direct children:
  - `missing` (Red): Exists only in source (needs copying).
  - `differ` (Orange): Exists in both, but file size differs.
  - `conflict` (Magenta): Type mismatch (file on one side, directory on the other).
  - `same` (Green): Identical size / directory presence on both sides.
  - `extra` (Blue): Exists only in destination (informational / candidate for cleanup).
- The recursive **deep-compare action is removed**; only the per-folder states above feed the row colours and the quick-select buttons (`Missing`/`Changed`/`Folders`/`Files`).
- **Side swap (⇄)**: a strip button on the inner edge of the destination column (rides with the paned divider) exchanges the two sides' endpoint sessions wholesale via `_swap_endpoints`. Per-side session state is exactly the **(connection, current path, remembered profile) trio** and travels as a unit — paths stay with their connection, no reconnect happens, each side just reloads its folder from its new slot (`_rebind_side`). The swap is guarded by `_transfer_or_delete_active()` (button disabled while transfers/deletes run), confirms-and-clears checked items on either side, and bumps `_remote_req`/`_dest_req` first so any in-flight listing is dropped by the request-id checks. Exports capture their connection into the context at snapshot time (`context["conn"]`), so a swap while an export runs cannot reroute it.

### 3.2 Parallel Transfer Pipeline & Conflict Resolution
- **Transfer Engine**: Supports concurrency control (1–8 parallel transfers via `SpinButton` & `threading.Condition` gate).
- **Transfer Methods**:
  - `scp`: Single regular files copied in 1 MiB chunks for smooth pausing and cancelling.
  - `tar`: Recursive directory streams piped over SSH or generated locally. Symlinks are preserved as symlinks (never traversed).
- **Conflict Policies**:
  - `Ask (smart)`: Same-size files skip silently; differing sizes prompt modal dialog (`Skip`, `Replace`, `Keep both`, `Cancel`).
  - `Overwrite`: Replaces destination atomically.
  - `Keep both`: Automatically renames with incremental suffix (`filename (1).ext`).
  - `Skip`: Leaves destination untouched.

### 3.3 Safe Permanent Item Deletion
- **Strictly Checked Selection**: Operates exclusively on checked rows (`self.selected` on Source, `self.dest_selected` on Destination).
- **Dedicated Progress View (`Gtk.Stack`)**: Flips the UI from `browser` to `delete_progress` during deletion, rendering a progress bar, active item label, and a "✕ Cancel Remaining" button.
- **Mutual Exclusion**: Deletions are blocked when transfers are running/queued/paused; transfers and navigation are inaccessible while deletion runs.
- **Window Close Guard**: `delete-event` prompts confirmation if transfers or deletions are in-flight.

---

## 4. Critical Conventions, Rules & Gotchas

When extending or maintaining this codebase, adhere strictly to the following conventions:

### 4.1 TreeView & Model Column Constants
Never use hardcoded integer column indices. Always use named constants:
- **Source Model (`self.model`)**:
  `SRC_CHECK (0), SRC_NAME (1), SRC_SIZE_TEXT (2), SRC_TYPE (3), SRC_MTIME_TEXT (4), SRC_IS_DIR (5), SRC_PATH (6), SRC_SIZE (7), SRC_MTIME (8)` (9 columns total).
- **Destination Model (`self.dest_model`)**:
  `DST_CHECK (0), DST_NAME (1), DST_SIZE_TEXT (2), DST_TYPE (3), DST_MTIME_TEXT (4), DST_PATH (5), DST_IS_DIR (6), DST_TOOLTIP (7), DST_STATE (8), DST_SIZE (9), DST_MTIME (10)` (11 columns total).
- **Sort Key Columns (64-bit)**:
  `SRC_SIZE`, `SRC_MTIME`, `DST_SIZE`, `DST_MTIME` must be stored as `GObject.TYPE_INT64` (gint64) to prevent 32-bit overflow crashes on epoch timestamps past 2038 or multi-gigabyte files.

### 4.2 Cross-Panel Sort Mirroring & GTK Segfault Protection
- `SORT_SRC_TO_DST` and `SORT_DST_TO_SRC` map corresponding sort columns between source and destination.
- **Never propagate `Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID`** across models: GTK 3's `GtkTreeModelSort` has an internal bug that segfaults if rows are inserted after an unsorted transition. Always guard against `UNSORTED` and unknown column IDs before syncing.
- Folders always sort before files in both ascending and descending order using `_compare_rows()`.

### 4.3 Symlink & Filesystem Safety
- **Never Dereference Symlinks on Delete**: When unlinking local items (`delete_local_item`), always check `os.path.islink()` **before** `os.path.isdir()`. Calling `shutil.rmtree` on a directory symlink would destroy the target folder's contents.
- **Remote SSH Symlink Deletion**: Always strip trailing slashes (`clean_path.rstrip("/")`) before executing `rm -rf -- <quoted_path>` over SSH. A trailing slash forces `rm` to dereference and traverse the symlink directory.
- **Protected Path Guards**: All deletion methods reject root (`/`), home (`~`, `/home/user`), empty strings (`""`), and relative tokens (`.`, `..`).
- **NTFS / Permissions Resilience**: On Windows/NTFS mounts, files with read-only attributes cause `EACCES`/`EPERM` upon deletion. Local deletion implements an `onerror` chmod fallback (`stat.S_IWUSR | stat.S_IRUSR`) before unlinking.

### 4.4 Threading & GTK Event Loop
- **Thread Safety**: Never touch GTK widgets or `Gtk.ListStore` models directly from worker threads. Always dispatch UI updates and callbacks via `GLib.idle_add(func, *args)`.
- **Dialog Destruction**: Modal dialogs must be tracked with `self._track_dialog(dlg)` and untracked with `self._untrack_dialog(dlg)` so `_on_destroy` can cleanly close them without hanging nested loops on quit.
- **Process Cancellation**: SSH pipelines track process PIDs in `proc_sink` lists; cancellations trigger `kill_procs()` to terminate children and sweep incomplete part files.

---

## 5. Testing & Verification

The test suite in `tests.py` runs without `pytest` or external dependencies:

```bash
python3 tests.py
```

### Key Test Coverage Areas:
- **Transports**: SSH command escaping, connection lifecycle, directory parsing, epoch sorting, stream pipeline failures, retry triggers.
- **Local Transports**: Local copying with overwrite/skip/keep_both, permission failures, symlink preservation, part-file cleanup.
- **Deletion Engine**: Regular file/directory deletion, symlink protection, read-only NTFS recovery, protected path rejection, remote command quoting.
- **UI & Interaction Smoke Tests**: Concurrency spinner, tree sorting mirror, destination checkboxes, selection clearing, progress stack transitions, window-close vetoing.
