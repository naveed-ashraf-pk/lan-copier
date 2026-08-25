"""Profile storage (schema v2) for lan-copier.

Profiles are the saved SSH endpoints (host/port/user and optional plaintext
password). The flat v1 layout is deliberately NOT migrated: a v1 or non-v2
file loads as an empty v2 store (the testing run throws old profiles away, so
no backward compatibility is needed).

Schema
------
{
  "version": 3,
  "profiles": {
    "<name>": {"host": str, "port": int, "user": str,
               "hostname": str, "password": str, "remember": bool}
  },
  "last": {
    "source_profile": str|None,   # last SSH profile selected per side
    "dest_profile": str|None,
  }
}

"This computer" is a built-in endpoint, never stored here (callers treat it
as a dedicated value). Passwords are only persisted when "remember" is True
(the UI stores nothing otherwise).

`hostname` is the resolved remote machine name captured at connect time. It
identifies a machine independently of its (changeable) IP, so reconnecting to
the same box updates one profile instead of stacking "user@host 2" duplicates.
"""

import json
import os
import tempfile

VERSION = 3
THIS = "This computer"

_EMPTY = {
    "version": VERSION,
    "profiles": {},
    "last": {
        "source_profile": None,
        "dest_profile": None,
    },
}


def _path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "profiles.json")


def _fresh():
    """A deep, fresh copy of the empty store."""
    return {
        "version": VERSION,
        "profiles": {},
        "last": {"source_profile": None, "dest_profile": None},
    }


def _normalize(data):
    out = _fresh()
    if not isinstance(data, dict):
        return out
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        for name, p in profiles.items():
            if not isinstance(p, dict):
                continue
            pw = p.get("password") or ""
            remember = bool(p.get("remember", bool(pw)))
            out["profiles"][name] = {
                "host": str(p.get("host", "")),
                "port": int(p.get("port", 22) or 22),
                "user": str(p.get("user", "")),
                "hostname": str(p.get("hostname", "") or ""),
                "password": pw if remember else "",
                "remember": remember,
            }
    last = data.get("last")
    if isinstance(last, dict):
        sp = last.get("source_profile")
        dp = last.get("dest_profile")
        if sp is not None:
            out["last"]["source_profile"] = str(sp)
        if dp is not None:
            out["last"]["dest_profile"] = str(dp)
    return out


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _fresh()
    return _normalize(data)


def save(store):
    """Write atomically: dump to a temp file in the same directory and rename
    over the real one, so a crash mid-write can never leave a corrupt store."""
    d = os.path.dirname(_path())
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".profiles-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _path())
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def names(store):
    return list(store["profiles"].keys())


def get(store, name):
    return store["profiles"].get(name)


def remember_side(store, side, name):
    store["last"][f"{side}_profile"] = name


def _matches(p, host, port, user):
    return (str(p.get("host", "")), int(p.get("port", 22) or 22),
            str(p.get("user", ""))) == (str(host), int(port or 22), str(user))


def find_profile(store, host="", port=22, user="", hostname=""):
    """Return the stored profile name that names the machine defined by
    (host, port, user, hostname), or None.

    When a resolved `hostname` is given it takes priority — a profile with the
    same hostname is a strong identity match even if its IP changed. Otherwise
    (or if no profile has that hostname) it falls back to host+port+user so
    legacy no-hostname rows still match. This is the fix for the duplicate
    "user@host 2" stacking on every reconnect."""
    if hostname:
        for n, p in store["profiles"].items():
            if str(p.get("hostname") or "") == hostname:
                return n
    key = (str(host), int(port or 22), str(user))
    for n, p in store["profiles"].items():
        if not (p.get("hostname") or "") and _matches(p, *key):
            return n
    return None


def merge_duplicates(store):
    """Collapse profiles that share a non-empty resolved hostname into the
    first-encountered entry, dropping the redundant duplicates (which used to
    pile up as "name 2/3/..." before hostname-aware identity landed).

    Redirects `last.*_profile` references to the retained name and returns the
    number of profiles removed.
    """
    keep = {}
    for name, p in store["profiles"].items():
        hn = str(p.get("hostname") or "").strip()
        if hn and hn not in keep:
            keep[hn] = name
    removed = []
    redirect = {}
    for hn, retain in keep.items():
        for name in list(store["profiles"]):
            if name == retain:
                continue
            p = store["profiles"][name]
            if str(p.get("hostname") or "").strip() == hn:
                removed.append(name)
                redirect[name] = retain
    for name in removed:
        del store["profiles"][name]
    for side in ("source_profile", "dest_profile"):
        old = store["last"].get(side)
        if old in redirect:
            store["last"][side] = redirect[old]
    return len(removed)