# Problem: System wedge caused by interrupted lan-copier copy on NTFS3

Date: 2026-08-20
Status: system recovered (rebooted); leftover cleanup done; minimal lan-copier app fix applied
Scope: system-level investigation of `/mnt/data` (ntfs3) + lan-copier bug analysis

## TL;DR

A `lan-copier` copy of a folder tree to `/mnt/data` (NTFS3 kernel driver) **completed
successfully**, but a leftover hidden part-folder was then **trashed through Files (nautilus/GVFS)**,
and that trash move **hung inside the kernel** (uninterruptible D-state). A later lan-copier run
then hung too, sweeping the leftover part-folder with a synchronous, unguarded delete. The leaked
lock permanently wedged 23 `nautilus` worker threads, which:

- made the *"File operations, 3 operations active"* popup reappear on every Files window close,
- made the shutdown dialog show *"Files — Trashing Files"*,
- blocked suspend and reboot (`Freezing user space processes failed`), so rebooting "did nothing".

The copy itself is NOT lost — the final folder with the full 788 MB movie exists.
The leftovers were an empty part-folder and two orphaned `.trashinfo` markers (now removed).

Note: lan-copier's code does **not** use the trash API anywhere (verified: no gio/send2trash).
The `.trashinfo` markers were written by nautilus/GVFS when the leftover part-folder was trashed
through the Files GUI.

## System environment

| Item | Value |
|---|---|
| OS | Ubuntu 26.04 (Resolute) |
| Kernel | `7.0.0-29-generic` |
| Desktop | GNOME Shell (`--mode=ubuntu`), nautilus `1:50.0-0ubuntu2` |
| Target disk | `/mnt/data` = `/dev/nvme0n1p6`, **ntfs3** kernel driver |
| fstab | `UUID=76EF2667769FDDED /mnt/data ntfs3 nosuid,nodev,nofail,x-gvfs-show 0 0` |
| Active opts | `rw,nosuid,nodev,relatime,uid=0,gid=0,acl,iocharset=utf8,prealloc` (note `prealloc`) |
| Other ntfs3 | `/run/media/naveed-ashraf/Windows-SSD` = `/dev/nvme0n1p3` (`prealloc` too) |

## Timeline (2026-08-20)

| Time (PKT) | Event |
|---|---|
| 09:34 | `lan-copier` (python3, PID 11453) starts; becomes `Thread-157` owner |
| 09:50 | Hidden part-folder created: `Entertainment/Movies/.The Wind Rises (2013) 720p BRRiP x264 AAC [Team Nanban].lan-copier-part-11453-40` |
| 10:03 | Final folder completed: `.../The Wind Rises .../` with full `788380593`-byte mp4 + srt + subs |
| 10:47 / 10:49 | Leftover part-folder trashed through Files (nautilus/GVFS) → two dot-prefixed `.trashinfo` markers written; the trash move hangs in ntfs3 and leaks a directory lock |
| 16:25 | Suspend attempt: `Freezing user space processes failed after 20.007 seconds (24 tasks refusing to freeze)` — includes `Thread-157` (tgid 11453, blocked in `unlinkat`) and nautilus `pool-N`/`nautilus-search` threads (blocked in `getdents64`) |
| 16:31:58 | Kernel hung-task warnings: `task:nautilus-search state:D` (multiple PIDs), `task:pool-N state:D` |
| 16:42 | ntfs3 auto-repair on Windows-SSD: `Correct links count -> 2` (multiple inodes) |
| 17:00+ | `lan-copier` is a zombie (`[python3] <defunct>`); nautilus keeps 23 threads permanently D-state |
| 17:24 | Cleanup: both orphaned `.trashinfo` removed OK; `rm -rf` of the empty part-folder timed out (40 s, exit 124) but its deferred I/O completed — part-folder confirmed gone |

## Symptoms reported

1. Popup on closing Files: **"File operations, 3 operations active"** — reappears on every reopen/close.
2. Shutdown/reboot dialog: **"Files — Trashing Files"**; after confirming, nothing happens.
3. Suspend/reboot hangs; `systemd-inhibit` shows only normal inhibitors (no lan-copier).
4. `loginctl list-inhibitors` is an unknown command on this build (use `loginctl list-sessions` / `systemd-inhibit`).

## Root-cause chain

1. Copy #1 (09:34–10:03) completed; `_merge_dir`'s `os.rmdir(part)` failed silently on the
   **ntfs3** driver and the error was swallowed (`except OSError: pass`) → an empty hidden
   part-folder was left behind. **lan-copier bug: silent rmdir swallow.**
2. The leftover part-folder was trashed through Files (nautilus/GVFS) at 10:47/10:49 → GVFS wrote
   two dot-prefixed `.trashinfo` markers ("in-progress"); the trash move **hung in `unlinkat`**
   on ntfs3 and leaked a directory lock. The part-folder never moved; only the markers stayed.
3. At 16:25 a later lan-copier run reached `_sweep_stale`, which **synchronously** deleted the
   stale part-folder (`shutil.rmtree` → `unlinkat`) — no timeout, no watchdog. It hit the same
   leaked lock and the app thread went D-state (`Thread-157`, tgid 11453). **lan-copier bug:
   unguarded synchronous cleanup.**
4. nautilus's own trash/search operations on the wedged directory also went D-state (`pool-N`,
   `nautilus-search`); 23 threads never made progress (utime/stime static over 2 s samples,
   > 40 min).
5. D-state threads cannot be killed (SIGKILL is deferred). gnome-session therefore waits forever
   for nautilus to exit → the reboot dialog confirms but nothing happens. Suspend failed the same
   way (`Freezing user space processes failed`, 24 tasks refusing to freeze).

Underlying trigger: the **ntfs3 kernel driver** leaks an inode lock when an `unlinkat`/`rmdir`
is interrupted — see `filesystem.md` for observations and options.

## Evidence (key commands/outputs)

```console
# Frozen tasks at 16:25 (suspend attempt failed)
Freezing user space processes failed after 20.007 seconds (24 tasks refusing to freeze)
task:Thread-157 (_wo state:D ... pid:26481 tgid:11453 ppid:5898   # lan-copier, blocked in vfs_unlink/unlinkat
task:pool-47         state:D ... pid:41257 tgid:27060 ppid:5898    # nautilus worker, iterate_dir/getdents64
task:nautilus-search state:D ...                                    # many more

# Current wedge (17:05)
for t in /proc/27060/task/*/stat: 23 threads in state D, wchan=iterate_dir, 0 CPU progress.

# Zombie remains
11453 Zl  [python3] <defunct>     # lan-copier
32171 Z   [bwrap]  <defunct>      # its sandbox child

# Mount options (active)
/dev/nvme0n1p6 ntfs3 rw,nosuid,nodev,relatime,uid=0,gid=0,acl,iocharset=utf8,prealloc
```

## Leftover artifacts (on /mnt/data only)

| Artifact | Path | Size | Status |
|---|---|---|---|
| Empty part-folder | `/mnt/data/Entertainment/Movies/.The Wind Rises (2013) 720p BRRiP x264 AAC [Team Nanban].lan-copier-part-11453-40/` | 4096 (empty dir) | **removed** (data-safe; copy complete) |
| Orphaned trashinfo #1 | `/mnt/data/.Trash-1000/info/.The Wind Rises (2013) 720p BRRiP x264 AAC [Team Nanban].lan-copier-part-11453-40.trashinfo` | 180 B | **removed** |
| Orphaned trashinfo #2 | `/mnt/data/.Trash-1000/info/.2.The Wind Rises ... .lan-copier-part-11453-40.trashinfo` | 180 B | **removed** |

Cleanup performed 2026-08-20 ~17:24 via plain `rm` (not Files/gvfs). Both `.trashinfo` files
removed instantly; the `rm -rf` on the part-folder hit the intermittent ntfs3 hang and timed
out after 40 s, but the deferred D-state I/O completed and the folder is gone. After cleanup,
`/mnt/data/.Trash-1000/info/` is empty and the FS responds to new reads again.

The dot-prefix on the `.trashinfo` names marks the trash operation as **in-progress** to
GVFS/nautilus — that is what drives the "Trashing Files" dialog and the ghost operations.

## Notes — multiple past copy attempts / hidden leftovers

- The app has been used for **multiple copy attempts over time**; some failed mid-way, leaving
  hidden part-files/part-dirs (`<name>.lan-copier-part-<pid>-<n>`) behind.
- Only `/mnt/data` was ever used with lan-copier (home + Windows-SSD were not).
- A full sweep of `/mnt/data` for `*lan-copier*` did not complete (NTFS hang is intermittent —
  the sweep itself timed out), so **an exhaustive leftover scan still needs to run** once the FS
  is stable. Debris may include: hidden part-files, empty part-dirs, orphaned `.trashinfo` markers.

## Filesystem health observations

- Read-only traversal is mostly fine: full `find /mnt/data -type d` returned 12661 dirs;
  Windows-SSD returned 50516 dirs — both instantly.
- But the wedge is **intermittent**: a later `find /mnt/data -name '*lan-copier*'` hung (> 90 s,
  exit 124). The affected inode/lock is still in play.

## lan-copier bugs — analysis & fix status

Verified: the app uses plain `os.remove`/`os.rmdir`/`shutil.rmtree`/`os.replace`; it **never**
uses the trash API. The `.trashinfo` markers came from nautilus/GVFS trashing the leftover
part-folder through the GUI.

| # | Bug | Fixed? |
|---|---|---|
| 1 | `_merge_dir` swallowed `os.rmdir(part)` failure (`except OSError: pass`) → empty part-folder left behind, silently | **FIXED** (rmdir → rmtree fallback; reports if still present) |
| 2 | `_remove` swallowed every OSError → cleanup failures invisible | **FIXED** (reports to `self.last_error` + stderr) |
| 3 | `_sweep_stale` deletes stale parts **synchronously with no timeout/watchdog**; a hung FS wedges the app thread in D-state | NOT FIXED (deferred — bigger change) |
| 4 | A hung FS call is invisible to the UI; if the app is killed mid-hang it dies as a zombie (can't be reaped until I/O returns) | NOT FIXED (deferred, depends on #3) |
| 5 | Dot-prefixed part names sit next to `final`, so nautilus shows them as hidden files users may trash; also collides with gvfs trash-in-progress semantics | NOT FIXED (deferred — consider a dedicated temp dir) |

## Recovery — DONE

1. **DONE** Deleted the empty part-folder and the two orphaned `.trashinfo` markers using plain
   `rm` (NOT Files/gvfs), each with a timeout guard. The part-folder rm timed out but its I/O
   completed in the background.
2. **DONE** Rebooted (user). Post-boot sanity check: no D-state processes, no lan-copier zombies,
   fresh nautilus, no failed units, both NTFS mounts responsive, trash empty.
3. **TODO** Run the full leftover sweep on `/mnt/data` for `*lan-copier*` once the FS is stable
   (a previous sweep timed out mid-way; the NTFS hang was intermittent).
4. **TODO** Verify in normal use: no "operations active" popup, no "Trashing Files" dialog.

## Prevention / hardening (documented only — no system changes taken)

See **`filesystem.md`** for concise observations, problems and suggestions about the ntfs3
mounts (prealloc, ntfs-3g, noatime, chkdsk, detection). Decision pending.