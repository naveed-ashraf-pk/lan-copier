# AI Agent Quick Start: `lan-copier`

Read **`docs/implementation-continuation-plan.md`** first (session handoff / current
state), then **`docs/architecture-and-developer-guide.md`** for architecture and
conventions. UI behavior and project layout live in `README.md` (in sync with the
code — prefer it over this file for descriptive detail).

## Commands
- **Test**: `python3 tests.py` is the only test command. The root runner calls each
  `tests/*.py` module's `ALL_TESTS`, then two AppWindow smoke tests. Add a test for
  new behavior in the matching module and expose it via `ALL_TESTS`. No
  lint/typecheck step is configured — plain asserts only.
- **Fixtures**: `tests/common.py` has `FakePosixSsh` (an `SSHConnection` with
  `kind="ssh"` driving real `sh -c`) — reuse it for engine/transport tests.

## Architecture
- **Transports are symmetric**: `LocalConnection` subclasses `SSHConnection`; both
  implement the same endpoint contract (`list_dir`, `stat`/`tree`, `delete`, `exists`,
  `size`, `unique_path`, `mkdir`, `rename`, `disk_space`, `key`). A new primitive must
  exist on both.
- **Remote paths**: on SSH endpoints, build/manipulate path strings with
  `commands/paths.py` helpers (family-aware `join`/`dirname`/`basename`) — never the
  control machine's `os.path` on a remote path (Windows `C:\…` splits wrong).

## Invariants
- **Stack**: Python 3 stdlib + PyGObject (`Gtk 3.0`). No 3rd-party dependencies.
- **Threading**: network/disk I/O runs in background threads; every UI update must
  go through `GLib.idle_add`.
- **Columns**: use the named `COL_*` constants in `app/widgets/dirpane.py`; never
  hardcode integer column indices.
- **Sort keys**: raw sort-key columns are `GObject.TYPE_INT64` (64-bit) to avoid
  2038-epoch / large-file overflow.
- **Sort sync**: never sync `Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID` across tree
  models (GTK3 TreeModelSort segfault on subsequent inserts).
- **Deletes**: check `os.path.islink()` before `os.path.isdir()` locally; strip
  trailing slashes (`clean_path.rstrip("/")`) before remote `rm -rf --` so symlinked
  targets are never traversed.
- **Atomicity**: transfers write PID-tagged `.lan-copier-part-*` files before atomic
  `os.replace`; remote destinations stage a same-directory part then `mv`/per-entry
  merge.
- **Progress**: a running transfer's bar never shows 100% until `_finish_transfer`
  marks it done; the merge/placement phase drives `on_merge(done, total)` (local
  dest only — remote placement is one opaque command).
- **Side swap**: per-side session state is exactly the **(connection, current path,
  remembered profile) trio** in `AppWindow._swap_endpoints` — any new per-side field
  must be swapped there too.


## Doc maintenance
- Any feature/behavioral/invariant change updates the docs briefly, in the same
  change (README for descriptive/UI facts, this file for invariants and commands).
- If related docs and code diverge after your changes, update the doc rather than leaving stale text.