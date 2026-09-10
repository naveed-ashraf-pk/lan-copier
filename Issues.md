# Issues & Improvements Tracker

## Open Issues

## Planned Improvements

- **[UI] Redesign bottom panel layout** — Show the selection panel and transfer panel side by side (both visible simultaneously). Move logs to a separate tab.

- **[Arch] Introduce state machine for `windows.py`** — Currently manages UI state with ad-hoc booleans (`self.removed`, etc.). A proper state machine would improve maintainability and reduce state-related bugs.

## Resolved

- ~~**[UI] Add "Select Same" button** — The selection toolbar has "Select All" and "Missing" options, but no way to select files that are *same* on both sides.~~

- ~~**[SSH] Windows disk space shows "info not available"** — When connecting to a Windows destination, disk space info was unavailable despite folder size calculation working.~~

- **[Transfer] Speed/ETA reflects read speed, not write** — Progress bar shows read throughput; when writes are slower, the UI hangs in "merging" state and the OS prompts to force-kill. Eventually completes.

- **[UI] Disk space not refreshing** — Available disk space on the destination panel does not update after transfers, refresh, or file deletion. Requires navigating to a different folder to see updated values.
