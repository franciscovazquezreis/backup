"""Gtk.Application: single instance, styles and keyboard shortcuts."""

from __future__ import annotations

import sys
from typing import List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME  # noqa: E402
from . import common  # noqa: E402


class SalvaguardaApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname(APP_ID)
        self.window = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        common.load_css()
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self._quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("win.preferences", ["<Primary>comma"])

    def do_activate(self) -> None:
        if self.window is None:
            from .window import MainWindow

            self.window = MainWindow(self)
        self.window.present()

    def _quit(self) -> None:
        if self.window is not None and not self.window._on_delete():
            self.window.destroy()
            self.quit()


def run(argv: Optional[List[str]] = None) -> int:
    app = SalvaguardaApp()
    return app.run(argv if argv is not None else sys.argv)
