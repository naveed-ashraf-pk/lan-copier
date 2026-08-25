"""Root runner for the lan-copier test suite.

`python3 tests.py` executes every module's ALL_TESTS (then the GTK UI smoke
test). Individual modules live in tests/ and each exposes its test functions
via ALL_TESTS; the friendly per-test "OK <name>" output comes from here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tests.test_commands as test_commands
import tests.test_ssh as test_ssh
import tests.test_local as test_local
import tests.test_engine as test_engine
import tests.test_export as test_export
import tests.test_app as test_app

GROUPS = [
    ("commands", test_commands),
    ("ssh", test_ssh),
    ("local", test_local),
    ("engine", test_engine),
    ("export", test_export),
    ("app", test_app),
]


def main():
    count = 0
    for group, mod in GROUPS:
        for fn in mod.ALL_TESTS:
            fn()
            count += 1
            print(f"OK [{group}] {fn.__name__}")
    test_app.SMOKE()
    test_app.SMOKE_REMOTE_DEST()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()