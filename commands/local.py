"""Thin wrappers for local (this-computer) filesystem operations.

The local endpoint expresses every operation through these functions so the
higher layers never call `os`/`shutil` directly. `local_transport` re-exports
`dir_list` / `dir_tree` / `delete_local_item` (and `LocalConnection` methods)
from here, so the historical import sites keep working while the actual logic
lives in one place under the commands layer.
"""

import os
import shutil
import stat
import time


def exists(path):
    return os.path.lexists(path)


def is_link(path):
    return os.path.islink(path)


def is_dir(path):
    return os.path.isdir(path) and not os.path.islink(path)


def list_dir(path):
    """Direct children of a local folder as {"name", "is_dir", "is_link",
    "size", "mtime", "mtime_epoch", "path"} dicts, mirroring the remote
    list shape. Returns None when the folder is not accessible."""
    path = os.path.expanduser(path)
    try:
        with os.scandir(path) as it:
            items = []
            for e in it:
                try:
                    st = e.stat()
                except OSError:
                    continue
                is_link = e.is_symlink()
                items.append({
                    "name": e.name,
                    "is_dir": not is_link and e.is_dir(),
                    "is_link": is_link,
                    "size": 0 if e.is_dir() and not is_link else st.st_size,
                    "mtime": time.strftime("%b %d %H:%M", time.localtime(st.st_mtime)),
                    "mtime_epoch": int(st.st_mtime),
                    "path": os.path.join(path, e.name),
                })
        return items
    except OSError:
        return None


def tree(root):
    """{relative_path: size} for every file under root (sizes follow symlinks,
    keeping local comparisons self-consistent with the destination walk)."""
    tree = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            try:
                tree[os.path.relpath(full, root)] = os.path.getsize(full)
            except OSError:
                pass
    return tree


def delete(path):
    """Permanently delete a local file, folder, or symlink. Symlinks are
    always unlinked (never traversed), so deleting a symlinked folder never
    touches its target's contents. Read-only files/dirs (e.g. NTFS mounts)
    are retried after chmod'ing them writable. Returns (ok, error_message)."""
    path = os.path.expanduser(path)
    clean = path.rstrip("/")
    home = os.path.expanduser("~").rstrip("/")
    if clean in ("", "/", ".", "..", home):
        return False, f"Refusing to delete protected path: {path}"
    if not os.path.lexists(path):
        return True, ""  # already gone

    def _handle_readonly(func, fpath, exc_info):
        try:
            os.chmod(fpath, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(fpath)
        except Exception as e:
            raise e

    try:
        if os.path.islink(path) or not os.path.isdir(path):
            try:
                os.unlink(path)
            except PermissionError:
                os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
                os.unlink(path)
        else:
            shutil.rmtree(path, onerror=_handle_readonly)
        return True, ""
    except Exception as e:
        return False, str(e)


def mkdir_p(path):
    os.makedirs(path, exist_ok=True)


def rename(src, dst):
    os.rename(src, dst)


def stat_bytes_files(path):
    """{'bytes': sum of regular-file sizes, 'files': N} for a local path."""
    if is_dir(path):
        total = 0
        n = 0
        for dirpath, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    continue
                n += 1
        return {"bytes": total, "files": n}
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"bytes": st.st_size, "files": 1}


def size_of(path):
    """Recursive byte total for a local path, or None when inaccessible."""
    st = stat_bytes_files(path)
    return st["bytes"] if st else None


def unique_path(p):
    """Return `p`, or `name (n).ext` with the first n that does not exist."""
    if not os.path.exists(p):
        return p
    base, ext = os.path.splitext(p)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def expand_home(path):
    return os.path.expanduser(path)


def home_dir():
    return os.path.expanduser("~")


def delete_local_item(path):
    """Back-compat alias for delete()."""
    return delete(path)


def dir_list(path):
    return list_dir(path)


def dir_tree(root):
    return tree(root)