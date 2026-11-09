"""Gtk.Application: single instance, styles and keyboard shortcuts."""

from __future__ import annotations

import os
import sys
from typing import List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME, VERSION  # noqa: E402
from ..core import portable  # noqa: E402
from . import common  # noqa: E402
from .window import MainWindow  # noqa: E402  (eager: no module is read from disk later)

INSTALLED_LAUNCHER = "/usr/bin/salvaguarda"


class SalvaguardaApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname(APP_ID)
        self.window = None
        self.restart_requested = False
        self._update_seen = ""

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
            self.window = MainWindow(self)
            self.window.present()
            return
        # A second launch reaches the instance that is already open. If a new .deb was
        # installed meanwhile, this instance is still the old code: offer to restart.
        self.window.present()
        GLib.idle_add(self._check_update)

    def installed_version(self) -> str:
        return portable.source_version(portable.package_dir())

    def _open_dialog(self) -> Optional[Gtk.Window]:
        """A dialog of this app on screen (its own run() loop would outlive the window)."""
        for win in Gtk.Window.list_toplevels():
            if (win is not self.window and win.get_visible()
                    and win.get_window_type() == Gtk.WindowType.TOPLEVEL
                    and (win.get_transient_for() is not None or win.get_modal())):
                return win
        return None

    def _check_update(self) -> bool:
        installed = self.installed_version()
        if not installed or installed == VERSION or installed == self._update_seen:
            return False
        self._update_seen = installed
        newer = portable.version_key(installed) > portable.version_key(VERSION)
        title = "Foi instalada a versão {}".format(installed)
        target = "a versão nova" if newer else "a versão instalada"
        dialog = self._open_dialog()
        if self.window.busy() or dialog is not None:
            common.message(dialog or self.window, title,
                           "Quando terminares, fecha a Salvaguarda e volta a abri-la para usar "
                           "{}.".format(target))
            return False
        answer = common.choose(
            self.window, title,
            "Esta janela ainda é a versão {}. Reinicia a Salvaguarda para usar {}; as tuas "
            "escolhas e cópias ficam como estão.".format(VERSION, target),
            [("later", "Mais tarde"), ("restart", "Reiniciar agora")], suggested="restart")
        if answer == "restart":
            self.restart_requested = True
            self._quit()
        return False

    def _quit(self) -> None:
        if self.window is not None and not self.window._on_delete():
            self.window.destroy()
            self.quit()


def run(argv: Optional[List[str]] = None) -> int:
    app = SalvaguardaApp()
    status = app.run(argv if argv is not None else sys.argv)
    if app.window is not None:
        # exec skips the normal exit: let the project search stop its git commands first.
        app.window.projects.stop()
        app.window.projects.wait(2.0)
    if app.restart_requested and os.access(INSTALLED_LAUNCHER, os.X_OK):
        # exec keeps nothing of the old code; the D-Bus name is released on exec.
        os.execv(INSTALLED_LAUNCHER, [INSTALLED_LAUNCHER])
    return status
