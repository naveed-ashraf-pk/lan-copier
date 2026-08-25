"""OS-family-aware path helpers.

The control machine's `os.path` reflects *its* OS, not a remote endpoint's.
A Linux control-run process must not call `os.path.dirname` on a Windows
remote path string (`C:\\Users\\Bob\\Docs`) — it would silently split on the
wrong separator.  Every remote path manipulation goes through these helpers,
selected by the endpoint family (`windows` for a Windows host, `posix`
otherwise).  Local endpoints keep using the real local `os.path` (it *is*
the local OS) via `commands.local`.
"""

import ntpath
import posixpath


def family_for(os_type):
    return "windows" if os_type == "Windows" else "posix"


def _module(family):
    return ntpath if family == "windows" else posixpath


def normalize(path, family):
    """Canonical form for comparisons: forward slashes, trailing slash trimmed,
    uppercase for windows (case-insensitive), unchanged case for posix."""
    p = str(path or "").replace("\\", "/") if family == "windows" else str(path or "")
    p = p.rstrip("/")
    return p.upper() if family == "windows" else p


def join(family, *parts):
    return _module(family).join(*parts)


def basename(path, family):
    return _module(family).basename(str(path))


def dirname(path, family):
    return _module(family).dirname(str(path))


def splitext(path, family):
    return _module(family).splitext(str(path))


def is_subpath(parent, child, family):
    """True when `child` equals or lies under `parent` (normalized)."""
    p = normalize(parent, family)
    c = normalize(child, family)
    if not p or not c:
        return False
    if p == "/" and family != "windows":
        return True
    return c == p or c.startswith(p + "/")


def is_same_path(a, b, family):
    return normalize(a, family) == normalize(b, family)