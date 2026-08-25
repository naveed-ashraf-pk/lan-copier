import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import sys
import threading
import traceback

from app.window import AppWindow


def _log_unhandled(exc_type, exc, tb):
    """Surface otherwise-silent failures (GTK swallows errors raised inside
    idle callbacks) so a crash is never invisible on the console."""
    sys.stderr.write("Unhandled exception:\n")
    traceback.print_exception(exc_type, exc, tb)


def _thread_excepthook(args):
    _log_unhandled(args.exc_type, args.exc_value, args.exc_traceback)


def main():
    sys.excepthook = _log_unhandled
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook
    win = AppWindow()
    win.show_all()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        if Gtk.main_level() > 0:
            Gtk.main_quit()
        sys.exit(130)


if __name__ == "__main__":
    main()