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

    def _check_update(self) -> bool:
        installed = self.installed_version()
        if not installed or installed == VERSION or installed == self._update_seen:
            return False
        self._update_seen = installed
        if self.window.busy():
            common.message(self.window, "Foi instalada a versão {}".format(installed),
                           "Quando a tarefa terminar, fecha a Salvaguarda e volta a abri-la para "
                           "usar a nova versão.")
            return False
        answer = common.choose(
            self.window, "Foi instalada a versão {}".format(installed),
            "Esta janela ainda é a versão {}. Reinicia a Salvaguarda para usar a nova versão; "
            "as tuas escolhas e cópias ficam como estão.".format(VERSION),
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
    if app.restart_requested and os.access(INSTALLED_LAUNCHER, os.X_OK):
        # exec keeps nothing of the old code; the D-Bus name is released on exec.
        os.execv(INSTALLED_LAUNCHER, [INSTALLED_LAUNCHER])
    return status
