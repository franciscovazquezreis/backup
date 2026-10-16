"""First run: welcome, choose the disk, choose what to copy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from ..core import controller, fmt  # noqa: E402
from ..core.layout import list_computers, root_for_mount  # noqa: E402
from . import common  # noqa: E402
from .dialogs import DiskChooser, prepare_disk  # noqa: E402
from .disks import Disk  # noqa: E402
from .selection_widget import SelectionWidget  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from .window import MainWindow


class Onboarding(Gtk.Stack):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.disk: Optional[Disk] = None
        self.add_named(self._welcome(), "welcome")
        self.add_named(self._disk_step(), "disk")
        self.selection_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add_named(self.selection_holder, "selection")

    # ---------------------------------------------------------------- welcome
    def _welcome(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        icon = common.app_icon_image(128)
        icon.set_margin_bottom(8)
        box.pack_start(icon, False, False, 0)
        box.pack_start(common.label("Bem-vindo à Salvaguarda", ("sg-hero-title",), xalign=0.5),
                       False, False, 0)
        text = ("Cópias de segurança para um disco externo, com um clique. Os ficheiros ficam "
                "guardados tal e qual: as mesmas pastas, as mesmas datas e as mesmas permissões, "
                "para voltarem ao sítio certo depois de formatares o computador.")
        box.pack_start(common.label(text, ("sg-dim",), wrap=True, xalign=0.5,
                                    justify=Gtk.Justification.CENTER, max_chars=58), False, False, 0)
        points = [
            ("emblem-synchronizing-symbolic", "Só copia o que mudou desde a última vez."),
            ("document-open-recent-symbolic", "O que apagares ou alterares fica guardado em «Versões anteriores»."),
            ("network-offline-symbolic", "Funciona sem internet e não precisa de conta."),
        ]
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        list_box.set_halign(Gtk.Align.CENTER)
        list_box.set_margin_top(10)
        for icon_name, line in points:
            list_box.pack_start(common.hbox(10, common.icon(icon_name, 18, ("sg-dim",)),
                                            common.label(line)), False, False, 0)
        box.pack_start(list_box, False, False, 0)
        start = Gtk.Button.new_with_mnemonic("_Começar")
        common.add_classes(start, "suggested-action", "sg-big-button")
        start.set_halign(Gtk.Align.CENTER)
        start.set_margin_top(22)
        start.connect("clicked", lambda *_: self.set_visible_child_name("disk"))
        box.pack_start(start, False, False, 0)
        return common.scrolled_page(box, 560)

    # ------------------------------------------------------------------- disk
    def _disk_step(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.pack_start(common.label("Onde vão ficar as cópias?", ("sg-hero-title",)), False, False, 0)
        box.pack_start(common.label(
            "Liga o disco externo e escolhe-o na lista. O ideal é um disco formatado em ext4, "
            "usado só para cópias. As cópias ficam numa pasta «Salvaguarda», por isso o disco "
            "pode ter outras coisas.", ("sg-dim",), wrap=True, max_chars=70), False, False, 0)
        self.chooser = DiskChooser(self.window.disks, lambda: self.window)
        box.pack_start(self.chooser, False, False, 0)
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        back = Gtk.Button.new_with_mnemonic("_Voltar")
        back.connect("clicked", lambda *_: self.set_visible_child_name("welcome"))
        nav.pack_start(back, False, False, 0)
        self.disk_next = Gtk.Button.new_with_mnemonic("_Continuar")
        common.add_classes(self.disk_next, "suggested-action", "sg-pill")
        self.disk_next.set_sensitive(False)
        self.disk_next.connect("clicked", lambda *_: self._use_disk())
        nav.pack_end(self.disk_next, False, False, 0)
        nav.set_margin_top(8)
        box.pack_start(nav, False, False, 0)
        self.chooser.connect("selected", lambda _c, d: self.disk_next.set_sensitive(d is not None))
        return common.scrolled_page(box, 600)

    def _use_disk(self) -> None:
        disk = self.chooser.selected
        if disk is None:
            return
        config = self.window.config
        dest = prepare_disk(self.window, config, disk)
        if dest is None:
            return
        self.window.save_config()
        computers = list_computers(root_for_mount(disk.path))
        if computers:
            names = []
            same = None
            for comp in computers:
                last = comp.last_success
                names.append("«{}», última cópia {}".format(
                    comp.name, fmt.relative(last) if last else "sem data"))
                if comp.name == config.computer_name:
                    same = comp
            text = ("Se acabaste de reinstalar o sistema, podes restaurar já os teus ficheiros e "
                    "definições, e depois continuar a fazer cópias na mesma pasta.")
            answer = common.choose(
                self.window, "Este disco já tem cópias de segurança", text,
                [("setup", "Configurar de novo"), ("restore", "Restaurar agora")],
                details=names, suggested="restore")
            if answer == "restore":
                chosen = same or (computers[0] if len(computers) == 1 else None)
                if chosen is not None:
                    snapshot = chosen.meta.get("configuracao")
                    if isinstance(snapshot, dict):
                        config.adopt_snapshot(snapshot)
                    config.computer_name = chosen.name
                config.configured = True
                self.window.save_config()
                self.window.finish_onboarding(restore_from=(dest.path, chosen.name if chosen else None))
                return
        self._show_selection()

    # -------------------------------------------------------------- selection
    def _show_selection(self) -> None:
        common.clear(self.selection_holder)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.pack_start(common.label("O que queres copiar?", ("sg-hero-title",)), False, False, 0)
        box.pack_start(common.label(
            "Já deixei marcado o que costuma interessar. Podes mudar isto quando quiseres em "
            "«Alterar…», na página principal.", ("sg-dim",), wrap=True, max_chars=70), False, False, 0)
        roots = [d.path for d in self.window.config.destinations]
        self.selection = SelectionWidget(self.window.config, lambda: self.window, roots)
        box.pack_start(self.selection, False, False, 0)
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        back = Gtk.Button.new_with_mnemonic("_Voltar")
        back.connect("clicked", lambda *_: self.set_visible_child_name("disk"))
        nav.pack_start(back, False, False, 0)
        done = Gtk.Button.new_with_mnemonic("_Concluir")
        common.add_classes(done, "suggested-action", "sg-pill")
        done.connect("clicked", lambda *_: self._finish())
        nav.pack_end(done, False, False, 0)
        nav.set_margin_top(8)
        box.pack_start(nav, False, False, 0)
        page = common.scrolled_page(box, 680)
        page.set_vexpand(True)
        self.selection_holder.pack_start(page, True, True, 0)
        self.selection_holder.show_all()
        self.set_visible_child_name("selection")

    def _finish(self) -> None:
        if self.selection.selected_count() == 0:
            common.message(self.window, "Não há nada selecionado",
                           "Ativa pelo menos uma pasta para copiar.", Gtk.MessageType.WARNING)
            return
        config = self.window.config
        controller.freeze_choices(config)
        config.configured = True
        self.window.save_config()
        self.window.finish_onboarding()
