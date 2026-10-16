"""Main page: disk status, last backup, what is included and the big button."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk  # noqa: E402

from ..core import controller, fmt, fsinfo, paths  # noqa: E402
from ..core.engine import (STATUS_CANCELLED, STATUS_FAILED, STATUS_NOTHING,  # noqa: E402
                           STATUS_OK, STATUS_WARNINGS, Question, RunResult)
from ..core.report import STATUS_TITLES  # noqa: E402
from . import common, disks  # noqa: E402
from .runner import Runner, ask_question  # noqa: E402
from .selection_widget import summary_lines  # noqa: E402
from .views import ProgressView, ResultView  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from .window import MainWindow


class BackupPage(Gtk.Stack):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.runner = Runner(window, self._on_phase, self._on_progress, self._on_done)
        self.runner.ask_handler = self._ask
        self._rename_requested = False
        self._current_root: Optional[str] = None
        self._current_dest = None

        self.overview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add_named(common.scrolled_page(self.overview_box), "overview")

        self.progress = ProgressView("A copiar", on_cancel=self._confirm_cancel)
        self.add_named(common.scrolled_page(self.progress, 560), "progress")

        self.result = ResultView()
        self.add_named(common.scrolled_page(self.result, 560), "result")
        self.refresh()

    # --------------------------------------------------------------- overview
    def refresh(self) -> None:
        if self.runner.running or self.get_visible_child_name() == "result":
            return
        box = self.overview_box
        common.clear(box)
        config = self.window.config
        connected = self.window.connected()

        box.pack_start(self._disk_card(connected), False, False, 0)
        box.pack_start(self._last_backup_card(connected), False, False, 0)
        box.pack_start(self._selection_card(), False, False, 0)

        button = Gtk.Button.new_with_mnemonic("_Fazer cópia agora")
        common.add_classes(button, "suggested-action", "sg-big-button")
        button.set_halign(Gtk.Align.CENTER)
        button.set_margin_top(8)
        button.set_sensitive(connected is not None)
        button.connect("clicked", lambda *_: self.start_backup())
        box.pack_start(button, False, False, 0)
        if connected is None:
            hint = "Liga o disco de cópias para poderes fazer a cópia."
            if not config.destinations:
                hint = "Escolhe primeiro um disco de cópias nas Definições."
            box.pack_start(common.label(hint, ("sg-dim", "sg-small"), xalign=0.5), False, False, 0)
        box.show_all()
        self.set_visible_child_name("overview")
        self.window.set_title("Salvaguarda")

    def _disk_card(self, connected) -> Gtk.Widget:
        config = self.window.config
        grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        if connected is not None:
            dest = connected.destination
            usage = fsinfo.usage(connected.root)
            fs = fsinfo.fs_label(fsinfo.fs_type(connected.root))
            grid.pack_start(common.icon("drive-harddisk-symbolic", 40), False, False, 0)
            texts.pack_start(common.label(dest.name, ("sg-title",)), False, False, 0)
            if usage is not None:
                detail = "Ligado · {} livres de {} · {}".format(
                    fmt.size(usage.free), fmt.size(usage.total), fs)
            else:
                detail = "Ligado"
            texts.pack_start(common.label(detail, ("sg-dim",), wrap=True), False, False, 0)
            if usage is not None and usage.total:
                level = Gtk.LevelBar()
                common.add_classes(level, "sg-level")
                for name in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_HIGH, "full"):
                    level.remove_offset_value(name)
                # Disk usage: the fuller, the worse (the opposite of a battery).
                level.add_offset_value("sg-normal", 0.85)
                level.add_offset_value("sg-warn", 0.95)
                level.add_offset_value("sg-crit", 1.0)
                level.set_value(usage.fraction_used)
                level.set_margin_top(4)
                texts.pack_start(level, False, False, 0)
            grid.pack_start(texts, True, True, 0)
            mount = self.window.disks.mount_for_path(connected.root)
            if mount is not None:
                eject = Gtk.Button.new_from_icon_name("media-eject-symbolic", Gtk.IconSize.BUTTON)
                eject.set_tooltip_text("Ejetar o disco em segurança")
                eject.set_valign(Gtk.Align.CENTER)
                eject.connect("clicked", lambda *_: self.eject(connected.root))
                grid.pack_end(eject, False, False, 0)
        else:
            grid.pack_start(common.icon("drive-harddisk-symbolic", 40, ("sg-dim",)), False, False, 0)
            if config.destinations:
                names = common.join_names(["«{}»".format(d.name) for d in config.destinations])
                texts.pack_start(common.label("Disco não ligado", ("sg-title",)), False, False, 0)
                texts.pack_start(common.label(
                    "Liga o disco {} e ele é reconhecido automaticamente.".format(names),
                    ("sg-dim",), wrap=True), False, False, 0)
                volume = None
                for dest in config.destinations:
                    volume = self.window.disks.volume_for_uuid(dest.uuid)
                    if volume is not None:
                        break
                grid.pack_start(texts, True, True, 0)
                if volume is not None:
                    mount_btn = Gtk.Button.new_with_label("Montar")
                    mount_btn.set_valign(Gtk.Align.CENTER)
                    mount_btn.connect("clicked", lambda *_: disks.mount_volume(
                        volume, self.window, lambda err: self._after_mount(err)))
                    grid.pack_end(mount_btn, False, False, 0)
            else:
                texts.pack_start(common.label("Sem disco de cópias", ("sg-title",)), False, False, 0)
                texts.pack_start(common.label("Escolhe o disco nas Definições.", ("sg-dim",)),
                                 False, False, 0)
                grid.pack_start(texts, True, True, 0)
        return common.card(grid)

    def _after_mount(self, error: Optional[str]) -> None:
        if error:
            common.message(self.window, "Não foi possível montar o disco", error,
                           Gtk.MessageType.ERROR)
        self.window.refresh()

    def _last_backup_card(self, connected) -> Gtk.Widget:
        config = self.window.config
        last = None
        status = {}
        if connected is not None:
            last, status = controller.last_success(config, connected.root)
        else:
            last = self.window.last_success_from_history()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        days = fmt.days_since(last)
        remind = config.settings.remind_after_days
        if last is None:
            icon_name, css = "dialog-information-symbolic", "sg-dim"
            title = "Ainda não há cópias deste computador"
            detail = "A primeira cópia copia tudo e pode demorar; as seguintes só copiam o que mudou."
        elif remind and days is not None and days >= remind:
            icon_name, css = "dialog-warning-symbolic", "sg-warning"
            title = "Última cópia: {}".format(fmt.relative(last))
            detail = "Já passaram {} dias. Liga o disco e faz uma cópia.".format(days)
        else:
            icon_name, css = "emblem-ok-symbolic", "sg-success"
            title = "Última cópia: {}".format(fmt.relative(last))
            parts = []
            if status.get("ficheiros"):
                parts.append(fmt.files(int(status["ficheiros"])))
            if status.get("tamanho"):
                parts.append(fmt.size(int(status["tamanho"])))
            if status.get("resultado") == STATUS_WARNINGS:
                parts.append("com avisos")
            detail = " · ".join(parts) if parts else ""
        box.pack_start(common.icon(icon_name, 32, (css,)), False, False, 0)
        texts.pack_start(common.label(title, ("sg-heading",), wrap=True), False, False, 0)
        if detail:
            texts.pack_start(common.label(detail, ("sg-dim",), wrap=True), False, False, 0)
        box.pack_start(texts, True, True, 0)
        log = status.get("registo") if status else None
        if connected is not None and log:
            path = os.path.join(controller.layout_for(config, connected.root).computer_dir, log)
            if os.path.exists(path):
                button = Gtk.Button.new_with_label("Ver registo")
                button.set_valign(Gtk.Align.CENTER)
                button.connect("clicked", lambda *_: common.open_path(path))
                box.pack_end(button, False, False, 0)
        return common.card(box)

    def _selection_card(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.pack_start(common.label("O que vai ser copiado", ("sg-heading",)), True, True, 0)
        change = Gtk.Button.new_with_mnemonic("_Alterar…")
        change.connect("clicked", lambda *_: self.window.edit_selection())
        head.pack_end(change, False, False, 0)
        box.pack_start(head, False, False, 0)
        lines = summary_lines(self.window.config)
        if not lines:
            box.pack_start(common.label("Nada selecionado.", ("sg-dim",)), False, False, 0)
        for icon_name, title, names in lines:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            image = common.icon(icon_name, 18, ("sg-dim",))
            image.set_valign(Gtk.Align.START)
            line.pack_start(image, False, False, 0)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            texts.pack_start(common.label(title), False, False, 0)
            texts.pack_start(common.label(names, ("sg-dim", "sg-small"), wrap=True), False, False, 0)
            line.pack_start(texts, True, True, 0)
            box.pack_start(line, False, False, 0)
        return common.card(box)

    # ----------------------------------------------------------------- backup
    def start_backup(self) -> None:
        if self.runner.running:
            return
        connected = self.window.connected()
        if connected is None:
            common.message(self.window, "O disco de cópias não está ligado",
                           "Liga o disco e tenta de novo.", Gtk.MessageType.WARNING)
            return
        config = self.window.config
        controller.freeze_choices(config)
        self.window.save_config()
        if not summary_lines(config):
            common.message(self.window, "Não há nada selecionado",
                           "Escolhe o que queres copiar em «Alterar…».", Gtk.MessageType.WARNING)
            return
        self._current_root = connected.root
        self._current_dest = connected.destination
        self.progress.reset()
        self.set_visible_child_name("progress")
        self.window.job_started("A fazer uma cópia de segurança")
        self.runner.start(lambda cb, cancel: controller.build_engine(config, connected.root, cb, cancel))

    def _ask(self, question: Question) -> str:
        answer = ask_question(self.window, question)
        if question.kind == "adopt" and answer == "rename":
            self._rename_requested = True
        return answer

    def _confirm_cancel(self) -> None:
        if common.confirm(self.window, "Cancelar a cópia?",
                          "O que já foi copiado fica guardado e a próxima cópia continua a partir daí.",
                          "Cancelar a cópia", "Continuar a copiar", destructive=True):
            self.progress.cancel_button.set_sensitive(False)
            self.runner.cancel()

    def _on_phase(self, phase: str, text: str) -> None:
        self.progress.set_phase(phase, text)

    def _on_progress(self, progress) -> None:
        self.progress.update(progress)
        if progress.phase == "copy":
            self.window.set_title("{} % · Salvaguarda".format(int(progress.fraction * 100)))

    def _on_done(self, result: Optional[RunResult]) -> None:
        self.progress.stop()
        self.window.job_finished()
        self.window.set_title("Salvaguarda")
        root, dest = self._current_root, self._current_dest
        if result is None:
            common.message(self.window, "Erro inesperado",
                           "A cópia parou devido a um erro interno. Tenta de novo.",
                           Gtk.MessageType.ERROR)
            self.refresh()
            return
        notes = list(result.notes)
        if dest is not None and root is not None:
            notes += controller.after_backup(self.window.config, dest, root, result)
        if self._rename_requested:
            self._rename_requested = False
            if self.window.ask_computer_name():
                self.refresh()
                self.start_backup()
                return
        self.window.notify_done(STATUS_TITLES.get(result.status, ""), result.message)
        buttons = []
        if result.status in (STATUS_OK, STATUS_WARNINGS, STATUS_NOTHING) and root is not None:
            mount = self.window.disks.mount_for_path(root)
            if mount is not None:
                eject = Gtk.Button.new_with_mnemonic("_Ejetar disco")
                common.add_classes(eject, "sg-pill")
                eject.connect("clicked", lambda *_: self.eject(root))
                buttons.append(eject)
        if result.log_path and os.path.exists(result.log_path):
            log = Gtk.Button.new_with_mnemonic("Ver _registo")
            common.add_classes(log, "sg-pill")
            log.connect("clicked", lambda *_: common.open_path(result.log_path))
            buttons.append(log)
        done = Gtk.Button.new_with_mnemonic("_Concluído")
        common.add_classes(done, "sg-pill", "suggested-action")
        done.connect("clicked", lambda *_: self.back_to_overview())
        buttons.append(done)
        if result.status == STATUS_OK:
            notes.insert(0, "Duração: {}.".format(fmt.duration(result.duration)))
        self.result.show_result(result.status, STATUS_TITLES.get(result.status, ""),
                                result.message, notes, result.issues, buttons)
        self.set_visible_child_name("result")
        done.grab_focus()

    def back_to_overview(self) -> None:
        self.set_visible_child_name("overview")
        self.refresh()

    def eject(self, root: str) -> None:
        mount = self.window.disks.mount_for_path(root)
        if mount is None:
            common.message(self.window, "Não foi possível ejetar",
                           "Este disco não é gerido pelo sistema como disco removível.")
            return
        if self.window.busy():
            common.message(self.window, "Ainda há uma tarefa a decorrer",
                           "Espera que termine antes de ejetar o disco.")
            return

        def done(error: Optional[str]) -> None:
            if error:
                common.message(self.window, "Não foi possível ejetar o disco", error,
                               Gtk.MessageType.WARNING)
            else:
                self.window.notify_done("Disco ejetado", "Já podes desligar o disco em segurança.")
                common.message(self.window, "Já podes desligar o disco",
                               "O disco foi ejetado em segurança.")
            self.back_to_overview()

        disks.eject(mount, self.window, done)
