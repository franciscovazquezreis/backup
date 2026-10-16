"""Main window."""

from __future__ import annotations

from typing import List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME  # noqa: E402
from ..core import config as cfgmod  # noqa: E402
from ..core import controller, fmt, paths, report  # noqa: E402
from ..core.layout import ComputerBackup, sanitize_computer_name  # noqa: E402
from . import common  # noqa: E402
from .backup_page import BackupPage  # noqa: E402
from .dialogs import HistoryDialog, PreferencesDialog, SelectionDialog, about  # noqa: E402
from .disks import DiskMonitor  # noqa: E402
from .onboarding import Onboarding  # noqa: E402
from .restore_page import RestorePage  # noqa: E402


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.set_default_size(760, 720)
        self.set_size_request(420, 480)
        icon_name = common.app_icon_name()
        if icon_name:
            self.set_icon_name(icon_name)
        else:
            pixbuf = common.app_icon_image(64).get_pixbuf()
            if pixbuf is not None:
                self.set_icon(pixbuf)
        self.config = cfgmod.load()
        self.disks = DiskMonitor()
        self.disks.connect("changed", lambda *_: self.refresh())
        self._inhibit_cookie = 0
        self._busy = 0

        self.header = Gtk.HeaderBar()
        self.header.set_show_close_button(True)
        self.header.set_title(APP_NAME)
        self.set_titlebar(self.header)

        self.menu_button = Gtk.MenuButton()
        self.menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        self.menu_button.set_tooltip_text("Menu")
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("O que copiar…", "win.selection")
        section.append("Definições", "win.preferences")
        section.append("Histórico", "win.history")
        section.append("Abrir a pasta das cópias", "win.open-disk")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Sobre a Salvaguarda", "win.about")
        menu.append_section(None, section)
        self.menu_button.set_menu_model(menu)
        self.header.pack_end(self.menu_button)

        for name, callback in (("selection", self.edit_selection), ("preferences", self.edit_preferences),
                               ("history", self.show_history), ("open-disk", self.open_disk),
                               ("about", lambda: about(self))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            self.add_action(action)

        self.main = Gtk.Stack()
        self.main.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.add(self.main)

        self.pages = Gtk.Stack()
        self.pages.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.backup_page = BackupPage(self)
        self.restore_page = RestorePage(self)
        self.pages.add_titled(self.backup_page, "backup", "Cópia de segurança")
        self.pages.add_titled(self.restore_page, "restore", "Restaurar")
        self.switcher = Gtk.StackSwitcher()
        self.switcher.set_stack(self.pages)
        self.main.add_named(self.pages, "pages")

        self.onboarding: Optional[Onboarding] = None
        self.connect("delete-event", self._on_delete)
        self.show_all()
        if self.config.configured and self.config.destinations:
            self._show_pages()
        else:
            self._show_onboarding()

    # ------------------------------------------------------------ navigation
    def _show_pages(self) -> None:
        self.header.set_custom_title(self.switcher)
        self.switcher.show_all()
        self.menu_button.set_visible(True)
        self.main.set_visible_child_name("pages")

    def _show_onboarding(self) -> None:
        self.onboarding = Onboarding(self)
        self.main.add_named(self.onboarding, "onboarding")
        self.onboarding.show_all()
        self.header.set_custom_title(None)
        self.menu_button.set_visible(False)
        self.main.set_visible_child_name("onboarding")

    def finish_onboarding(self, restore_from: Optional[Tuple[str, Optional[str]]] = None) -> None:
        self._show_pages()
        if self.onboarding is not None:
            onboarding = self.onboarding
            self.onboarding = None
            GLib.timeout_add(600, lambda: (onboarding.destroy(), False)[1])
        self.refresh()
        if restore_from is not None:
            root, computer = restore_from
            self.pages.set_visible_child_name("restore")
            if computer:
                self.restore_page.open_named(root, computer)
            else:
                self.restore_page.show_sources()
        else:
            self.pages.set_visible_child_name("backup")

    # ----------------------------------------------------------------- state
    def connected_all(self) -> List[controller.Connected]:
        mounts = [d.path for d in self.disks.disks()]
        found = controller.connected_destinations(self.config, mounts)
        changed = False
        for item in found:
            if item.destination.path != item.root:
                item.destination.path = item.root
                changed = True
        if changed:
            self.save_config()
        return found

    def connected(self) -> Optional[controller.Connected]:
        found = self.connected_all()
        return found[0] if found else None

    def save_config(self) -> None:
        try:
            cfgmod.save(self.config)
        except OSError as exc:
            common.message(self, "Não foi possível guardar as definições", str(exc),
                           Gtk.MessageType.ERROR)

    def refresh(self) -> None:
        if self.main.get_visible_child_name() != "pages":
            return
        self.backup_page.refresh()
        self.restore_page.refresh()

    def last_success_from_history(self):
        for entry in reversed(report.load_history()):
            if entry.get("tipo", "copia") != "copia":
                continue
            if entry.get("computador") not in (None, self.config.computer_name):
                continue
            if entry.get("resultado") in ("ok", "warnings", "nothing"):
                return fmt.parse_iso(entry.get("fim"))
        return None

    def busy(self) -> bool:
        return self.backup_page.runner.running or self.restore_page.runner.running

    # --------------------------------------------------------------- actions
    def edit_selection(self) -> None:
        if self.busy():
            return
        dialog = SelectionDialog(self)
        if dialog.run() == Gtk.ResponseType.OK:
            self.config.sources = dialog.working.sources
            self.save_config()
        dialog.destroy()
        self.refresh()

    def edit_preferences(self) -> None:
        if self.busy():
            common.message(self, "Há uma tarefa a decorrer", "Espera que termine para mudar as definições.")
            return
        dialog = PreferencesDialog(self)
        if dialog.run() == Gtk.ResponseType.OK:
            working = dialog.apply()
            self.config.settings = working.settings
            self.config.destinations = working.destinations
            self.config.computer_name = working.computer_name
            self.save_config()
        dialog.destroy()
        self.refresh()

    def show_history(self) -> None:
        dialog = HistoryDialog(self)
        dialog.run()
        dialog.destroy()

    def open_disk(self) -> None:
        connected = self.connected()
        if connected is None:
            common.message(self, "O disco de cópias não está ligado")
            return
        layout = controller.layout_for(self.config, connected.root)
        common.open_path(layout.computer_dir if layout.has_backup() else connected.root)

    def ask_computer_name(self) -> bool:
        dialog = Gtk.Dialog(title="Nome deste computador", transient_for=self, modal=True,
                            use_header_bar=True)
        dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ok = dialog.add_button("Usar este nome", Gtk.ResponseType.OK)
        common.add_classes(ok, "suggested-action")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(18)
        box.pack_start(common.label("As cópias deste computador vão ficar numa pasta com este nome.",
                                    wrap=True, max_chars=50), False, False, 0)
        entry = Gtk.Entry()
        entry.set_text(self.config.computer_name + "-2")
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box.pack_start(entry, False, False, 0)
        dialog.get_content_area().add(box)
        dialog.show_all()
        response = dialog.run()
        name = sanitize_computer_name(entry.get_text())
        dialog.destroy()
        if response == Gtk.ResponseType.OK and name and name != self.config.computer_name:
            self.config.computer_name = name
            self.save_config()
            return True
        return False

    def adopt_backup(self, comp: ComputerBackup) -> None:
        """After restoring this computer's own copies, keep backing up into the same folder."""
        if comp.name != self.config.computer_name:
            return
        meta = comp.layout.read_meta()
        meta["machine_id"] = paths.machine_id()
        try:
            comp.layout.write_meta(meta)
        except OSError:
            pass

    # ------------------------------------------------------------- long jobs
    def job_started(self, reason: str) -> None:
        self._busy += 1
        flags = Gtk.ApplicationInhibitFlags.SUSPEND | Gtk.ApplicationInhibitFlags.LOGOUT
        try:
            self._inhibit_cookie = self.app.inhibit(self, flags, reason)
        except (GLib.Error, TypeError):
            self._inhibit_cookie = 0
        self.switcher.set_sensitive(False)
        self.menu_button.set_sensitive(False)

    def job_finished(self) -> None:
        self._busy = max(0, self._busy - 1)
        if self._inhibit_cookie:
            self.app.uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0
        self.switcher.set_sensitive(True)
        self.menu_button.set_sensitive(True)
        self.set_title(APP_NAME)

    def notify_done(self, title: str, body: str) -> None:
        if self.is_active():
            return
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        icon_name = common.app_icon_name()
        if icon_name:
            notification.set_icon(Gio.ThemedIcon.new(icon_name))
        try:
            self.app.send_notification("salvaguarda-done", notification)
        except GLib.Error:
            pass

    def _on_delete(self, *_args) -> bool:
        if not self.busy():
            return False
        if common.confirm(self, "Há uma tarefa a decorrer",
                          "Se fechares agora, a tarefa é cancelada. O que já foi copiado fica guardado.",
                          "Cancelar e fechar", "Continuar", destructive=True):
            for page in (self.backup_page, self.restore_page):
                if page.runner.running:
                    page.runner.cancel()
                    page.runner.wait(60)
            return False
        return True
