# TODO(cleanup): LEGACY FILE (schema v1, flat). Superseded by app/profiles.py
# (schema v2 store). Keep only while tests/test_ui.py + legacy ui.py reference
# it; delete after the new window is confirmed.

import json
import os
import tempfile


def _path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "profiles.json")


def load():
    try:
        with open(_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(profiles):
    """Write atomically: the JSON is dumped to a temp file in the same
    directory and renamed over the real one, so a crash mid-write can never
    leave a truncated/corrupt profiles.json (which load() would read as {})"""
    d = os.path.dirname(_path())
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".profiles-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(profiles, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _path())
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise