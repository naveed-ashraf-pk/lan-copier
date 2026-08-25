# Filesystem notes — ntfs3 mounts (observations, problems, suggestions)

Date: 2026-08-20
Status: **documentation only — no actions taken** (decision pending)

## Observed state

| Mount | Source | FSTYPE | Active options |
|---|---|---|---|
| `/mnt/data` | `/dev/nvme0n1p6` | ntfs3 | `rw,nosuid,nodev,relatime,uid=0,gid=0,acl,iocharset=utf8,prealloc` |
| `/run/media/naveed-ashraf/Windows-SSD` | `/dev/nvme0n1p3` | ntfs3 | `rw,nosuid,nodev,relatime,uid=1000,gid=1000,acl,iocharset=utf8,prealloc` |

fstab (note: `prealloc` is active even though not written in fstab — it is ntfs3's default):
`/dev/disk/by-uuid/76EF2667769FDDED /mnt/data ntfs3 nosuid,nodev,nofail,x-gvfs-show 0 0`

Kernel: `7.0.0-29-generic` (Ubuntu 26.04).

## Observations / problems

1. **Intermittent I/O hangs (delete path).** `unlinkat`/`rmdir` on `/mnt/data` hung in D-state,
   leaking a directory lock; later `readdir` on the affected inode blocked forever. A full `find`
   on the mount worked instantly, then a later `find` hung (> 90 s). Hang is not deterministic.
2. **`prealloc` default-on.** Confirmed in the ntfs3 module (`Opt_prealloc`); `noprealloc` is a
   valid option. `prealloc` governs space preallocation when extending/overwriting files.
3. **ntfs3 driver repair messages.** `ntfs3(nvme0n1p3): Correct links count -> 2` — on-mount
   metadata corrections, suggesting the NTFS metadata may be dirty (no recent Windows `chkdsk`).
4. **D-state blocks shutdown/suspend.** Wedged threads can't be killed; session logout/suspend
   wait forever (`Freezing user space processes failed`). Only a reboot clears them.
5. **Current state is healthy.** After cleanup + reboot the system behaves normally; normal
   read/write, Trash, Documents/Downloads integration all work. The earlier functional problems
   are resolved.

## Recommendation (re-thought: stay on ntfs3)

### Phase 1 — conservative configuration

Keep `ntfs3` and `relatime`; change only `prealloc` → `noprealloc`:

```fstab
/dev/disk/by-uuid/76EF2667769FDDED /mnt/data ntfs3 nosuid,nodev,nofail,x-gvfs-show,noprealloc 0 0
```

Rationale: the native driver is currently working; this is a low-risk preventive experiment that
removes one suspicious variable without changing the driver.

> **Caveat (important):** the hang we observed was on the **delete path** (`unlinkat`/`rmdir`) and
> subsequent **`readdir`** — i.e. name/metadata operations. `prealloc` affects **write-time space
> allocation**, which is unrelated to that path. So `noprealloc` is **preventive, not a proven fix**
> for the exact failure we hit. It is still worth trying, but do not expect it to be a guaranteed
> cure.

### Phase 2 — observe

Do not deliberately stress the filesystem (no repeated `find`, mass deletes). Watch normal daily
use for: D-state (`ps -eo stat | grep '^D'`), hung-task in `journalctl -k`, ntfs3/I/O errors,
shutdown/suspend failures. Stability here is more useful evidence than a short synthetic test.

### Phase 3 — escalate only if the problem returns

1. NTFS filesystem health (`chkdsk /f` from Windows)
2. NVMe/storage errors (`smartctl`, `dmesg`)
3. ntfs3/kernel-specific issue (kernel is new — `7.0.0-29`; track kernel updates)
4. `ntfs-3g` (FUSE) A/B test — at this point a reasonable diagnostic experiment

### `chkdsk /f`

Still recommended **once** from Windows, because this shared NTFS volume showed unusual metadata
behavior — to establish a clean baseline, not because corruption is proven.

## Ranking

| Option | Recommendation | Reason |
|---|---|---|
| **NTFS3 + `noprealloc`** | ★★★★★ Best | Native, currently working, minimal change |
| NTFS3 + current `prealloc` | ★★★★☆ | Working, but leaves the suspicious variable enabled |
| NTFS-3G (FUSE) | ★★★☆☆ | Mature fallback; unnecessary while ntfs3 works |
| NTFS-3G permanently | ★★☆☆☆ | Sacrifices native driver without evidence it's needed |
| `noatime` additionally | ★★☆☆☆ | Not relevant enough to justify changing now |

## Why not acted on now

System-side changes were out of scope for this session (code-only fixes). Applying the
recommendation is an `/etc/fstab` edit + remount of a partition in use — requires sudo, careful
planning, and a backup first.