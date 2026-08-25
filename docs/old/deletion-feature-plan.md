# Implementation Specification & Test Plan: Source & Destination Item Deletion

**Document Version:** 2.0  
**Date:** 2026-08-22  
**Status:** Finalized Design & Test Specification  
**Target Systems:** `lan-copier` UI (`ui.py`), Local transport (`local_transport.py`), SSH transport (`ssh_transport.py`), and Test Suite (`tests.py`)

---

## 1. Executive Summary & Objectives

The `lan-copier` application enables comparison, queuing, and parallel atomic transfers between a Source (remote SSH or local folder) and a Destination (local folder).

This specification details the addition of a robust, permanent **Item Deletion** capability for both the **Source** and **Destination** panels.

### Key Tenets:
1. **Strictly Checked Selection**: Deletion applies *only* to items explicitly checked via checkboxes (no implicit fallback to highlighted/focused row).
2. **Symmetrical Panel Experience**: Destination panel receives checkboxes (`dest_model`), selection dictionaries (`dest_selected`), and quick-selection controls (`Select all`, `Invert`, `Select extras`), mirroring the Source panel.
3. **Permanent Removal & Safety**: Files and directories are permanently deleted (`rm -rf` / `os.unlink` / `shutil.rmtree`). Symlinks are unlinked without traversing target directories. Protected paths (`/`, `~`, `""`, `.`, `..`) are rejected.
4. **Dedicated Deletion Progress View (`Gtk.Stack`)**: During deletion, the dual-pane browser is swapped with a progress card, preventing accidental user interactions while displaying real-time item status, a progress bar ($i/N$), and cancellation support.
5. **Mutual Exclusion**: Deletion is blocked when transfers are running/queued/paused; transfers/compares are blocked when a deletion is in progress.
6. **Zero Regressions**: Model sort mirroring, compare state coloring, profile persistence, and error dialogs are preserved.

---

## 2. UI/UX Architecture & Layout Specifications

### 2.1 Main Browser Layout (`browser` Stack Page)

```
+-----------------------------------------------------------------------------------------------+
| [Source: SSH / Local] [Connect/Browse] [Host/Port/User/Pass] [Profile: ...]                   |
+-----------------------------------------------------------------------------------------------+
|  SOURCE PANEL                                       |  DESTINATION PANEL                      |
|  [Path: ~/dir/...] [⬆ Up] [↻ Refresh]               |  [Path: /dest/...] [Browse] [⌂] [⬆] [↻] |
|  [Filter: ...]                                      |  [Tabs: Destination | Selected | Trans] |
|  [Select all] [Missing] [Changed] [Invert]...       |  [Select all] [Invert] [Select extras]  |
|  +------------------------------------------------+ |  +------------------------------------+ |
|  | [x] Name          Size   Type    State         | |  | [x] Name       Size   Type   State | |
|  | [ ] file1.txt     10 KB  File    missing       | |  | [ ] file1.txt  10 KB  File   same  | |
|  | [x] dir1          —      Folder  differ        | |  | [x] old_doc    5 KB   File   extra | |
|  +------------------------------------------------+ |  +------------------------------------+ |
|  [Summary: 2 missing · 1 differ]                    |  [Summary: 1 extra · 1 same]            |
|  [🗑 Delete Checked (1)]                            |  [🗑 Delete Checked (1)]                |
+-----------------------------------------------------------------------------------------------+
| Compare colors: red = missing · orange = size differs · magenta = conflict · green = same ...  |
+-----------------------------------------------------------------------------------------------+
| Console Log ($ rm ..., rc=0)                                                                  |
+-----------------------------------------------------------------------------------------------+
```

#### A. Source Panel Controls:
- **Delete Button**: Placed at the bottom of the left pane: `🗑 Delete Checked (<N>)`.
- **Button Sensitivity**: Enabled only when $N > 0$ items are checked in `self.selected`, and no active transfer or deletion is running.
- **Button Action**: Triggers modal confirmation for items in `self.selected`.

#### B. Destination Panel Controls:
- **Checkboxes**: Checkbox column (`DST_CHECK = 0`) added to `self.dest_tree` / `self.dest_model`.
- **Selection Toolbar**: Added above destination tree: `[Select all]`, `[Invert]`, `[Select extras]` (selects items in `extra` state).
- **Delete Button**: Placed at the bottom of the destination pane: `🗑 Delete Checked (<N>)`.
- **Button Sensitivity**: Enabled only when $N > 0$ items are checked in `self.dest_selected`, and no active transfer or deletion is running.
- **Selection State Invalidation**: Checked destination items in `self.dest_selected` automatically clear when the destination directory changes.

### 2.2 Confirmation Dialog

Before any deletion executes, a modal dialog appears:
- **Title**: `Confirm Permanent Deletion`
- **Location Context**:
  - For Source: `Source (user@host:/path)` or `Local Source (/path)`
  - For Destination: `Destination (/path)`
- **Summary**: `Permanently delete <N> item(s) (<F> file(s), <D> folder(s))?`
- **Item Preview**: Clean list showing up to 6 item names (with `... and <X> more` if $>6$).
- **Warning**: `⚠️ This operation is permanent and cannot be undone.`
- **Buttons**:
  - `[ Cancel ]` (Default focused, `Gtk.ResponseType.CANCEL`)
  - `[ Delete Permanently ]` (`Gtk.ResponseType.OK`, styled with CSS class `destructive-action`)

### 2.3 Dedicated Deletion Progress View (`delete_progress` Stack Page)

To prevent UI interference and display real-time feedback:

```
+-----------------------------------------------------------------------------------------------+
|                                                                                               |
|                                     🗑 Deleting Items                                         |
|                     Deleting 4 of 12 items from Destination...                                |
|                                                                                               |
|                             [==================>       ] 33%                                  |
|                                                                                               |
|                    Current: /mnt/data/Downloads/large_video.mkv                      /         |
|                                                                                               |
|                              [ ✕ Cancel Remaining ]                                           |
|                                                                                               |
+-----------------------------------------------------------------------------------------------+
```

- **Stack Switching**: `self.main_stack.set_visible_child_name("delete_progress")`.
- **Progress Card Widgets**:
  - Header Label: Target panel & overall summary.
  - Progress Bar: Fractional progress $i / N$ and percentage.
  - Active Item Label: Displays the path/filename currently being deleted.
  - Cancel Button: `[ ✕ Cancel Remaining ]` — sets `self._delete_cancel_event`, cleanly stopping the batch after the current item finishes.
- **Automatic Return**: Once finished, switches back to `browser` stack child, reloads the affected directory, cleans selection caches, recomputes compare states, and reports errors if any occurred.

---

## 3. Data Model Schema & Sort Synchronization

### 3.1 Model Column Indices

To maintain clean symmetry between Source and Destination panels:

```python
# Source Model Columns (self.model)
SRC_CHECK, SRC_NAME, SRC_SIZE_TEXT, SRC_TYPE, SRC_MTIME_TEXT, \
    SRC_IS_DIR, SRC_PATH, SRC_SIZE, SRC_MTIME = range(9)

# Destination Model Columns (self.dest_model)
DST_CHECK, DST_NAME, DST_SIZE_TEXT, DST_TYPE, DST_MTIME_TEXT, \
    DST_PATH, DST_IS_DIR, DST_TOOLTIP, DST_STATE, DST_SIZE, DST_MTIME = range(11)
```

### 3.2 Sort Column Mapping & TreeModelSort

Sort column IDs for bidirectional mirroring between `self.model_sort` and `self.dest_model`:

| Visual Column | Source Model Sort ID | Destination Model Sort ID |
|---|---|---|
| **State** | `0` | `8` (`DST_STATE`) |
| **Name** | `1` | `1` (`DST_NAME`) |
| **Size** | `2` | `2` (`DST_SIZE_TEXT`) |
| **Type** | `3` | `3` (`DST_TYPE`) |
| **Modified** | `4` | `4` (`DST_MTIME_TEXT`) |

```python
SORT_SRC_TO_DST = {0: 8, 1: 1, 2: 2, 3: 3, 4: 4}
SORT_DST_TO_SRC = {8: 0, 1: 1, 2: 2, 3: 3, 4: 4}
```

All callbacks (`_dest_name_cb`, `_dest_state_cb`, `_sort_dest`, `_sort_state_dest`, `_on_dest_row_activated`, `_apply_states`) use these explicit constants.

---

## 4. Backend Transport & Filesystem Removal Specifications

### 4.1 Local Deletion (`LocalConnection` & Local Destination)

```python
def delete_local_item(path):
    """Safely delete a local file, folder, or symlink permanently.
    Never traverses directory symlinks (always unlinks). Handles read-only
    permissions on NTFS/Windows mounts gracefully."""
    path = os.path.expanduser(path)
    clean_path = path.rstrip("/")
    if clean_path in ("", "/", os.path.expanduser("~")):
        return False, f"Protected path cannot be deleted: {path}"
    
    if not os.path.lexists(path):
        return True, ""  # Already deleted
        
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        else:
            def _handle_readonly(func, fpath, exc_info):
                try:
                    os.chmod(fpath, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
                    func(fpath)
                except Exception as ex:
                    raise ex
            shutil.rmtree(path, onerror=_handle_readonly)
        return True, ""
    except Exception as e:
        return False, str(e)
```

### 4.2 Remote Deletion over SSH (`SSHConnection`)

```python
def delete_item(self, remote_path):
    """Safely delete a file, directory, or symlink on remote host.
    Uses shlex.quote and trailing slash stripping to prevent symlink traversal."""
    if not self._ensure_master():
        return False, "SSH connection not active"
        
    remote_path = self.expand_remote(remote_path)
    clean_path = remote_path.rstrip("/")
    if clean_path in ("", "/", self.home_dir().rstrip("/")):
        return False, f"Protected path cannot be deleted: {remote_path}"
        
    # Strip trailing slash so rm deletes the symlink itself if path is a symlink
    quoted = shlex.quote(clean_path)
    cmd = ["ssh"] + self._opts() + [self.target, f"rm -rf -- {quoted}"]
    rc, out, err = self._run_cmd(cmd, timeout=60)
    if rc != 0:
        self.last_error = self._clean_err(err)
        return False, self.last_error
    return True, ""
```

---

## 5. End-to-End Flows, Use Cases & Expected Outcomes

The following structured test matrix defines all user flows and expected automated verification outcomes.

---

### Use Case 1: Source Panel Checked Items Deletion (SSH Remote)

* **Pre-conditions**: Connected via SSH to `user@host`. Source folder contains `file1.txt` (File), `folderA/` (Folder), `link1` (Symlink).
* **Steps**:
  1. User checks checkboxes for `file1.txt` and `folderA/`.
  2. Source delete button shows `🗑 Delete Checked (2)` and is enabled.
  3. User clicks `🗑 Delete Checked (2)`.
  4. Modal confirmation dialog appears showing target host, path, 2 items listed, and red "Delete Permanently" button.
  5. User confirms deletion.
  6. Main window switches to `delete_progress` stack page.
  7. Worker calls `SSHConnection.delete_item` for each item.
  8. Progress bar updates from 0% to 50% to 100%.
  9. Main window switches back to `browser` page.
* **Expected Outcomes**:
  - `file1.txt` and `folderA/` are removed from the remote system.
  - `self.selected` is cleared of the deleted paths.
  - Remote directory reloads automatically via `_load_remote()`.
  - Console log prints: `$ rm -rf -- ... → rc=0`.
  - Compare state colors update across both panels.

---

### Use Case 2: Destination Panel Checked Items Deletion (Local)

* **Pre-conditions**: Destination folder `/mnt/data/Downloads` contains `old.iso`, `cache/`, `symlink_dir -> /other`.
* **Steps**:
  1. User checks checkboxes for `old.iso` and `symlink_dir`.
  2. Destination delete button shows `🗑 Delete Checked (2)` and is enabled.
  3. User clicks `🗑 Delete Checked (2)`.
  4. Confirmation dialog appears showing `Destination: /mnt/data/Downloads` and preview of items.
  5. User confirms deletion.
  6. Progress view appears and displays `Deleting 1 of 2: old.iso` then `Deleting 2 of 2: symlink_dir`.
* **Expected Outcomes**:
  - `old.iso` is removed.
  - `symlink_dir` symlink is removed; the target `/other` directory is **completely intact**.
  - `self.dest_selected` is cleared.
  - Destination panel reloads via `_load_dest()`.
  - State indicators update (any `extra` or `differ` states recalculate).

---

### Use Case 3: Quick Selection Controls in Destination Panel

* **Pre-conditions**: Destination folder contains 5 files, 2 of which are marked `extra` (blue).
* **Steps**:
  1. User clicks `Select extras` above the destination list.
  2. The 2 `extra` files have their checkboxes checked; `self.dest_selected` length becomes 2.
  3. User clicks `Invert`.
  4. The other 3 files are checked; the 2 `extra` files are unchecked.
  5. User clicks `Select all`.
  6. All 5 files are checked.
* **Expected Outcomes**:
  - Destination delete button label reflects `🗑 Delete Checked (5)`.
  - Toggling checkboxes updates `self.dest_selected` accurately regardless of sort order or active filter.

---

### Use Case 4: Deletion with Active Search Filter

* **Pre-conditions**: Panel contains `photo1.jpg`, `photo2.jpg`, `video1.mp4`.
* **Steps**:
  1. User types `photo` in filter entry. List shows only `photo1.jpg` and `photo2.jpg`.
  2. User clicks `Select all` (checks both visible photos).
  3. User clears filter entry. List shows all 3 items, with `photo1.jpg` and `photo2.jpg` checked.
  4. User clicks `🗑 Delete Checked (2)` and confirms.
* **Expected Outcomes**:
  - Only `photo1.jpg` and `photo2.jpg` are deleted.
  - `video1.mp4` remains untouched and unchecked.

---

### Use Case 5: Partial Deletion Failure (Permissions / Locked Files)

* **Pre-conditions**: Batch of 3 files selected: `fileA.txt` (normal), `locked.txt` (read-only directory / permission denied), `fileB.txt` (normal).
* **Steps**:
  1. User initiates and confirms deletion of all 3 files.
  2. Worker deletes `fileA.txt` successfully.
  3. Worker encounters `Permission denied` on `locked.txt`; records error and proceeds.
  4. Worker deletes `fileB.txt` successfully.
  5. Deletion finishes; view returns to browser.
* **Expected Outcomes**:
  - `fileA.txt` and `fileB.txt` are deleted and removed from selection.
  - `locked.txt` remains on disk and remains checked in the list.
  - Error dialog pops up: *"1 of 3 items could not be deleted"* listing `locked.txt: Permission denied`.
  - Console log displays error with details.

---

### Use Case 6: Cancellation Mid-Batch

* **Pre-conditions**: User selects 50 items for deletion.
* **Steps**:
  1. User confirms deletion.
  2. Progress view shows `Deleting 5 of 50...`.
  3. User clicks `[ ✕ Cancel Remaining ]`.
  4. Worker completes item 5 and detects cancellation event.
* **Expected Outcomes**:
  - Items 1–5 are deleted.
  - Items 6–50 are skipped and not deleted.
  - Progress view switches back to browser.
  - Panel reloads; log notes: *"Deletion cancelled by user: 5 deleted, 45 skipped"*.

---

### Use Case 7: Mutual Exclusion with Transfers

* **Scenario A: Deletion attempted while Transfer is active**
  - **State**: A transfer is running or queued in the Transfers tab.
  - **Outcome**: Both Source and Destination Delete buttons are **disabled** (`sensitive=False`) with tooltip: *"Cannot delete items while transfers are active"*.
* **Scenario B: Transfer / Compare attempted while Deletion is active**
  - **State**: Deletion is executing in the `delete_progress` stack view.
  - **Outcome**: Dual-pane browser is hidden; transfer buttons, compare button, and navigation are inaccessible.

---

### Use Case 8: Critical Path Protection

* **Steps**:
  - Attempting to pass `""`, `"/"`, `"~"`, `"/home/user"`, `"."`, or `".."` to `delete_item` or `delete_local_item`.
* **Outcome**:
  - Operation immediately returns `False, "Protected path cannot be deleted"`. No filesystem or remote command is executed.

---

### Use Case 9: Transfer Selection & Profile State Invalidation

* **Pre-conditions**: Item `/r/movie.mkv` is checked in Source panel, added to `self.selected`, and saved in `profiles.json` under `last_selection`.
* **Steps**:
  1. User deletes `/r/movie.mkv` via Source delete button.
* **Expected Outcomes**:
  - `/r/movie.mkv` is removed from `self.selected` and `self.sel_model`.
  - Transfer button label updates to `▶ TRANSFER SELECTED (0)`.
  - Active profile state is saved (`_save_profile_state()`) so `/r/movie.mkv` is removed from `last_selection`.

---

### Use Case 10: Window Close Guard during Active Deletion

* **Steps**:
  1. User attempts to close window (`delete-event`) while a deletion worker is running.
* **Expected Outcomes**:
  - Modal prompt appears: *"Deletion in progress. Quitting now will cancel remaining deletions. Quit anyway?"*
  - If declined, window stays open and deletion completes.
  - If confirmed, cancellation event is set and window closes.

---

## 6. Automated Testing Plan (`tests.py`)

All use cases will be verified via automated assertions in `tests.py` without requiring external testing frameworks:

1. **`test_local_delete_file_and_dir()`**:
   - Creates temporary directory with files and subdirectories.
   - Verifies `delete_local_item` deletes files and recursive directories.
2. **`test_local_delete_symlink_safety()`**:
   - Creates target folder `/tmp/target_dir/secret.txt` and symlink `/tmp/dest_dir/link -> /tmp/target_dir`.
   - Executes `delete_local_item` on the symlink.
   - Asserts symlink is removed and `/tmp/target_dir/secret.txt` is intact.
3. **`test_local_delete_readonly_ntfs_emulation()`**:
   - Creates read-only file (`chmod 0400`) in temporary directory.
   - Executes `delete_local_item`; verifies fallback chmod removes it cleanly without error.
4. **`test_remote_delete_command_escaping()`**:
   - Tests `SSHConnection.delete_item` with special characters (`"file with $spaces & 'quotes'.txt"`).
   - Verifies command formulation and proper quoting.
5. **`test_protected_path_rejection()`**:
   - Asserts `delete_local_item` and `SSHConnection.delete_item` reject `/`, `~`, `""`, `.`.
6. **`test_ui_dest_checkboxes_and_sort_sync()`**:
   - Tests `DST_CHECK` toggle events in `dest_model`.
   - Tests `Select all`, `Invert`, and `Select extras`.
   - Verifies sort sync across both panels (`SORT_SRC_TO_DST` and `SORT_DST_TO_SRC`) with the 11-column `dest_model`.
7. **`test_ui_deletion_progress_stack_and_selection_sync()`**:
   - Simulates deletion of checked source and destination items.
   - Verifies stack switching to `delete_progress` and back to `browser`.
   - Verifies purging from `self.selected`, `self.sel_model`, and `profiles.json`.
8. **`test_ui_mutual_exclusion()`**:
   - Asserts delete buttons become insensitive when `Transfer(status="running")` exists.
