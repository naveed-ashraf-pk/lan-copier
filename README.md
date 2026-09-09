# Lan Copier

A lightweight GTK 3 desktop app for comparing, transferring, and managing files
between local and remote (SSH) machines on a LAN.

## Features

- **Symmetric endpoints** — any side can be source or destination (local, SSH Linux/macOS, SSH Windows via PowerShell)
- **Dual-pane browser** — side-by-side file trees with live comparison (missing / changed / same / extra / conflict)
- **Parallel transfers** — 1–8 concurrent transfers with progress, pause, resume, cancel
- **Conflict resolution** — Ask (smart), Overwrite, Keep both, Skip
- **Safe deletion** — checked-item-only deletion with protected-path guards, symlink safety, and NTFS resilience
- **LAN discovery** — auto-scan subnet for SSH hosts
- **Tree export** — YAML snapshot of any source/destination tree
- **Side swap** — flip source and destination in one click

## Screenshots

![Main window](docs/images/screenshot1.png)

![File comparison](docs/images/screenshot2.png)

## Requirements

- Python 3.6+
- GTK 3.0 + PyGObject (`gi.repository`)
- OpenSSH (`ssh`, `scp`, `ssh-keygen`) in `$PATH`
- Linux, macOS, or Windows (with OpenSSH + bundled `tar.exe`)

## Installation

    git clone https://github.com/<you>/lan-copier.git
    cd lan-copier
    python3 main.py

No `pip install` — zero third-party Python dependencies.

## Usage

    python3 main.py

1. Click **Connect** on either side to pick "This computer" or an SSH endpoint
2. Browse and check files to transfer
3. Hit **Copy** or **Move**

## Project Structure

    main.py                 Entry point — launches app.window.AppWindow
    app/
      window.py             AppWindow: composes both side columns + notebook and
                            hosts the connection / transfer / delete / export engine
      profiles.py           Saved SSH profiles (schema v3)
      widgets/
        endpoint.py         EndpointBar (connect button + path + nav + actions)
        dirpane.py          Dual-pane file tree with comparison states
        dialog.py           ConnectionDialog (the single connect dialog)
    ssh_transport.py        SSHConnection — OpenSSH multiplexing + endpoint contract
    local_transport.py      LocalConnection — local filesystem operations
    transfer_engine.py      Parallel copy/move engine
    tree_exporter.py        YAML tree snapshot exporter
    discovery.py            LAN subnet scanner
    commands/               Command builders (POSIX, PowerShell, local, paths)
    config/                 Gitignored profile store (config/profiles.json)
    tests/                  Test suite (one file per module, run by tests.py)
    docs/                   Architecture guide, session handoffs, archived legacy UI
                            (old/legacy-ui/)

## UI & Behavior Notes

- **Layout**: each side is one `EndpointBar` *inside* its side column, directly above
  its `DirPane`, so the horizontal paned divider resizes bar + tree together. The
  `DirPane` filter/quick-select row (Missing / Changed / Invert / Folders / Files)
  sits above the tree.
- **Connecting**: the `ConnectionDialog` popup is the single place to connect, change,
  or disconnect either side — This computer, a saved profile, or a new SSH endpoint.
  Each `EndpointBar` shows a display-only connection label plus one `conn_btn` that
  turns green when connected. The "Open" and "Pick folder" actions appear only for a
  connected **local** endpoint (both sides); pick-folder opens a local file chooser.
- **Side swap (⇄)**: swaps the two endpoint sessions wholesale — each side's
  (connection, current path, remembered profile) trio travels together. It confirms
  and clears any checked items first, and disables while transfers/deletes run.
- **Status bar (per pane)**: left comparison-counts summary + spinner + a right label.
  The source pane shows `Selected: X (N files)` (green/red vs. the destination's free
  space); the destination shows `X / Y free`, `After copy: …` when items are selected,
  or `Disk space unavailable`.
- **Folder sizes**: the Size column is filled lazily in the background after each
  listing. Directories compare by recursive `(bytes, files)` — a same-named folder
  starts provisionally `same` and flips to `differ` once both panes' sizes land.
  Failed/partial size results keep `same` (never flip on an undercount).
- **Sorting**: every column header (Name/Size/Type/Modified/State) is click-sortable;
  Size and Modified sort by their raw 64-bit values, folders always first.
- **Log coloring**: the Log tab colors messages — hard failures (`FAILED:`/`Error`)
  red, recoverable soft issues (`Could not`, `Retrying`, `Skipped`, non-zero `rc`)
  amber.

## Testing

    python3 tests.py

## License

MIT
