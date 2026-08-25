# Implementation Specification & Test Plan: Source & Destination Tree Export

**Document Version:** 1.0  
**Date:** 2026-08-22  
**Status:** Finalized Design & Build Plan  
**Target Systems:** `lan-copier` UI (`ui.py`), local transport (`local_transport.py`), SSH transport (`ssh_transport.py`), export utility (`tree_exporter.py`), and test suite (`tests.py`)

---

## 1. Executive Summary & Objectives

This feature adds a structured export capability for the current **Source** or **Destination** panel so users can save a directory snapshot and compare it outside the application.

### Core goals
1. Export from **either panel** using the panel's current path.
2. Support **local** paths and **remote SSH** paths across Linux, macOS, and Windows SSH hosts.
3. Export enough metadata for robust external comparison while keeping the output compact and deterministic.
4. Allow users to choose **scope** and **depth** before exporting.
5. Keep all scan work off the GTK thread and place the export logic in a **separate utility module**.

### Recommended output shape
The export format should be **YAML** with a **flat, lexicographically sorted `entries` map keyed by relative path**. This is the best fit for AI agents and diff tools because it avoids nested traversal overhead and produces stable, easy-to-compare output.

---

## 2. User Experience & Workflow

### 2.1 Entry Points
- Add an **Export** button to the **Source** panel toolbar.
- Add an **Export** button to the **Destination** panel toolbar.
- Both buttons open the same modal flow, pre-filled with the active panel context.

### 2.2 Export Options Modal

When the user clicks Export, open a modal dialog that captures:

1. **Target context**
   - Host name and IP.
   - Current panel path.
   - Panel label: Source or Destination.

2. **Scope**
   - `Entire directory`
   - `Selected items only (<N>)`
   - If there are no checked items for that panel, disable the selected-only option.

3. **Depth**
   - `Root level only`
   - `2 levels`
   - `3 levels`
   - `4 levels`
   - `5 levels`
   - `Full recursive`

Depth semantics:
- `Root level only` exports direct children of the selected root path or selected item roots.
- Numbered levels include nested descendants up to that depth.
- `Full recursive` has no depth cap.

4. **Save path**
   - Save chooser with a suggested file name:
     `<host_name>-<folder_name>-<YYYYMMDD-HHMMSS>.yaml`

### 2.3 Completion Behavior
- Perform export in a background thread.
- Keep the UI responsive.
- Log start and completion messages.
- Show success or error dialogs once finished.

---

## 3. Data Schema

### 3.1 Top-Level YAML Structure

```yaml
meta:
  panel: source
  host_name: workstation
  host_ip: 192.168.1.10
  host_display: workstation (192.168.1.10)
  root_path: /home/user/project
  exported_at: "2026-08-22 15:30:22"
  scope: all
  depth: full
  total_entries: 12
  total_files: 8
  total_directories: 4
  total_size_bytes: 403456

entries:
  "src/":
    type: directory
    modified: "2026-08-22 14:10:00"
    created: "2026-08-20 11:00:00"
    mode: "0755"
  "src/index.js":
    type: file
    size_bytes: 15360
    modified: "2026-08-22 10:15:00"
    created: "2026-08-20 11:05:00"
    mode: "0644"
```

### 3.2 Entry Fields

Required fields:
- `type`: `file`, `directory`, `symlink`, or `other`
- `modified`: normalized timestamp string when available
- `created`: normalized timestamp string when available

Conditional fields:
- `size_bytes`: for files and other non-directory entries
- `mode`: permission bits when available cross-platform
- `symlink_target`: when the entry is a symlink

### 3.3 Cross-OS Timestamp Notes
- Linux may not expose true birth time via portable standard tools.
- For consistency:
  - use birth time when available
  - otherwise fall back to metadata change time / creation-like timestamp available from the platform
- The export code should clearly normalize whatever is available into the `created` field.

---

## 4. Architecture

### 4.1 New Utility Module: `tree_exporter.py`

Responsibilities:
- collect host metadata
- scan local trees
- scan remote trees over SSH
- apply scope and depth filtering
- generate deterministic YAML
- write the export file

Suggested functions:

```python
def describe_local_host(): ...
def describe_remote_host(conn): ...
def collect_local_entries(root_path, max_depth=None): ...
def collect_remote_entries(conn, root_path, max_depth=None): ...
def build_export_document(panel, host_info, root_path, scope, depth, entries): ...
def dump_yaml_document(doc): ...
def export_tree(panel, host_info, root_path, collector, out_path, scope, depth): ...
```

### 4.2 Remote Scanning Strategy

The remote scanner must support Linux, macOS, and Windows over SSH. The remote OS is
detected once per connection (`uname -s` over SSH, cached on the connection) and the
probe order is dispatched accordingly:

1. **POSIX (Linux / macOS / Darwin)**
   - Try a small remote **Python** script first (`python3`, then `python`). The script is
     a clean multi-line payload carried to the host as a **base64 blob** invoked via a
     single-statement bootstrap (`import base64,sys;exec(base64.b64decode(sys.argv[1]))`),
     which is immune to remote shell quoting, newline mangling, and "def after semicolon"
     syntax errors.
   - If Python is absent, fall back to native `find`:
     - **macOS (Darwin)**: BSD `find` + `stat -f '%N\t%HT\t%p\t%z\t%m\t%B\t%Y'`
     - **Linux / GNU**: `find -printf '%y\t%m\t%s\t%T@\t%C@\t%p\t%l\n'`
2. **Windows (uname fails / unknown)**: PowerShell `-EncodedCommand` first, then the
   Python attempt as a last resort.

This ensures consistent metadata shape across hosts, correct handling of spaces and
special characters, macOS birth times (`st_birthtime`), octal modes, symlink targets,
and avoids GNU-only flags on macOS.

### 4.3 Remote Hostname Pre-fetch

Before showing the export modal, the UI calls `tree_exporter.describe_remote_host(conn)`,
which resolves the machine's real hostname (cached on the connection). This makes the
suggested export file name start with the host name (e.g. `MacBook-Pro-Torrents-....yaml`)
instead of the raw IP the user connected to.

### 4.3 UI Integration

`ui.py` should:
- add Export buttons to both panels
- collect selected paths for the active panel
- open the options dialog
- launch background export threads
- handle success/error completion in the GTK main loop

---

## 5. Scope & Depth Rules

### 5.1 Entire Directory
- Root path is the panel's current path.
- Entries are exported relative to that root.

### 5.2 Selected Items Only
- Use the checked rows already tracked by the panel.
- Export the selected top-level items and their descendants subject to the chosen depth.
- Relative paths remain rooted at the current panel path so output stays comparable to full exports.

### 5.3 Depth Calculation
- `Root level only` means depth `1` from the chosen root.
- Numbered options cap recursion to that integer.
- `Full recursive` means unlimited depth.

---

## 6. Output Format Rationale

YAML is the right default because:
- it is readable by humans
- it is compact enough for AI agents
- it preserves a stable mapping shape for deterministic comparison

The flat `entries` map is preferred over nested trees because:
- diffs are cleaner
- AI agents can compare exact paths directly
- there are no metadata-vs-child key collisions
- token usage is lower than deep nesting

---

## 7. Testing Plan

### 7.1 Utility Tests
- YAML output is deterministic and sorted.
- Local collection handles:
  - files
  - directories
  - symlinks
  - depth limits
  - selected-items scope
- Host metadata formatting handles:
  - host name only
  - host name plus IP

### 7.2 Remote Tests
- Remote Python output parser handles returned JSON lines or payload structure.
- Windows-like, Linux-like, and macOS-like metadata normalization is covered with fakes.

### 7.3 UI-Adjacent Tests
- Suggested file name uses host name first.
- Export option normalization maps modal values to depth integers / unlimited.
- Panel selection extraction feeds the exporter correctly.

### 7.4 Regression Expectations
- No blocking GTK calls on worker threads.
- No change to transfer or delete behavior.
- Existing tests must continue to pass.

---

## 8. Implementation Sequence

1. Add `docs/export-feature-plan.md`.
2. Create `tree_exporter.py` with deterministic data collection and YAML serialization.
3. Add remote host description and remote scan support, preferring remote Python for cross-OS parity.
4. Add the export modal and Export buttons to both panels.
5. Add tests for document generation, depth handling, and utility behavior.
6. Run `python3 tests.py` and fix regressions.

---

## 9. Non-Goals for Initial Version

- Checksums or hashing.
- Alternate export formats beyond YAML.
- Incremental export updates.
- Inline diffing inside the app.

These can be added later if external comparison workflows show a concrete need.
