"""Restore page: choose a backup, choose what to bring back, and where."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from ..core import catalog, fileops, fmt, paths, report, restore, system  # noqa: E402
from ..core.engine import STATUS_FAILED, STATUS_NOTHING, STATUS_OK, STATUS_WARNINGS  # noqa: E402
from ..core.layout import (ComputerBackup, DiskLayout, VersionRun, list_computers,  # noqa: E402
                           list_version_runs, read_disk_marker, ROOT_DIR_NAME)
from . import common  # noqa: E402
from .runner import Runner  # noqa: E402
from .views import ProgressView, ResultView  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from .window import MainWindow

RESTORE_TITLES = {
    STATUS_OK: "Restauro concluído",
    STATUS_WARNINGS: "Restauro concluído com avisos",
    STATUS_NOTHING: "Nada a restaurar",
    "cancelled": "Restauro cancelado",
    STATUS_FAILED: "O restauro não foi concluído",
}


class RestorePage(Gtk.Stack):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.runner = Runner(window, self._on_phase, self._on_progress, self._on_done)
        self.extra_roots: List[str] = []
        self.computer: Optional[ComputerBackup] = None
        self.root: Optional[str] = None
        self.runs: List[VersionRun] = []
        self.items: List[restore.RestoreItem] = []
        self.checks: Dict[int, Gtk.CheckButton] = {}
        self._size_cancel = threading.Event()

        self.sources_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add_named(common.scrolled_page(self.sources_box), "sources")
        self.items_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.add_named(common.scrolled_page(self.items_box), "items")
        self.progress = ProgressView(
            "A restaurar", "Não desligues o disco até ao fim. Se possível, não uses as apps "
            "que estão a ser restauradas.", on_cancel=self._confirm_cancel)
        self.add_named(common.scrolled_page(self.progress, 560), "progress")
        self.result = ResultView()
        self.add_named(common.scrolled_page(self.result, 560), "result")
        self.refresh()

    # ---------------------------------------------------------------- sources
    def refresh(self) -> None:
        if self.runner.running or self.get_visible_child_name() in ("items", "progress", "result"):
            return
        self.show_sources()

    def _roots(self) -> List[str]:
        roots = [c.root for c in self.window.connected_all()]
        for disk in self.window.disks.disks():
            if read_disk_marker(disk.root) and disk.root not in roots:
                roots.append(disk.root)
        for extra in self.extra_roots:
            if extra not in roots and os.path.isdir(extra):
                roots.append(extra)
        return roots

    def show_sources(self) -> None:
        box = self.sources_box
        common.clear(box)
        head = common.vbox(4, common.label("Restaurar", ("sg-hero-title",)),
                           common.label("Traz de volta ficheiros e definições guardados no disco "
                                        "de cópias: depois de formatar, ou só para recuperar algo "
                                        "que apagaste.", ("sg-dim",), wrap=True, max_chars=70))
        box.pack_start(head, False, False, 0)
        found = []
        for root in self._roots():
            for comp in list_computers(root):
                found.append((root, comp))
        if found:
            listbox = common.boxed_list()
            listbox.connect("row-activated", lambda lb, r: self.open_computer(*r.payload))
            for root, comp in found:
                marker = read_disk_marker(root) or {}
                disk_name = marker.get("nome") or os.path.basename(os.path.dirname(root))
                last = comp.last_success
                subtitle = "Disco «{}» · última cópia {}".format(
                    disk_name, fmt.relative(last) if last else "sem data")
                extra = common.badge("este computador") \
                    if comp.name == self.window.config.computer_name else None
                arrow = common.icon("go-next-symbolic", 16, ("sg-dim",))
                lrow = common.row(comp.name, subtitle, "computer-symbolic", arrow,
                                  activatable=True, title_classes=("sg-heading",), extra=extra)
                lrow.payload = (root, comp)  # type: ignore[attr-defined]
                listbox.add(lrow)
            box.pack_start(listbox, False, False, 0)
        else:
            empty = common.vbox(
                8,
                common.icon("drive-harddisk-symbolic", 64, ("sg-dim",)),
                common.label("Liga o disco de cópias", ("sg-title",), xalign=0.5),
                common.label("As cópias aparecem aqui assim que o disco for ligado.",
                             ("sg-dim",), xalign=0.5, wrap=True),
            )
            empty.set_margin_top(30)
            for child in empty.get_children():
                child.set_halign(Gtk.Align.CENTER)
            box.pack_start(empty, False, False, 0)
        other = Gtk.Button.new_with_mnemonic("Procurar cópias noutra _pasta…")
        other.set_relief(Gtk.ReliefStyle.NONE)
        other.set_halign(Gtk.Align.CENTER)
        other.connect("clicked", lambda *_: self._pick_other())
        box.pack_start(other, False, False, 0)
        box.show_all()
        self.set_visible_child_name("sources")

    def _pick_other(self) -> None:
        folder = common.choose_folder(self.window, "Pasta com cópias da Salvaguarda", "Abrir")
        if not folder:
            return
        candidates = [folder, os.path.join(folder, ROOT_DIR_NAME), os.path.dirname(folder)]
        for root in candidates:
            if list_computers(root):
                if root not in self.extra_roots:
                    self.extra_roots.append(root)
                self.show_sources()
                return
        common.message(self.window, "Não há cópias nessa pasta",
                       "Escolhe a pasta «Salvaguarda» do disco de cópias (ou a pasta que a contém).",
                       Gtk.MessageType.WARNING)

    def open_named(self, root: str, computer_name: str) -> None:
        for comp in list_computers(root):
            if comp.name == computer_name:
                self.open_computer(root, comp)
                return
        self.show_sources()

    # ------------------------------------------------------------------ items
    def open_computer(self, root: str, comp: ComputerBackup) -> None:
        self.root = root
        self.computer = comp
        self.runs = list(reversed(list_version_runs(comp.layout)))
        self._build_items_page()
        self.set_visible_child_name("items")

    def _tree(self) -> str:
        index = self.when.get_active() if hasattr(self, "when") else 0
        if index <= 0 or index > len(self.runs):
            return self.computer.layout.current_dir
        return self.runs[index - 1].path

    def _build_items_page(self) -> None:
        comp = self.computer
        box = self.items_box
        common.clear(box)
        self._size_cancel.set()
        self._size_cancel = threading.Event()

        back = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        back.set_tooltip_text("Voltar")
        back.set_valign(Gtk.Align.CENTER)
        back.connect("clicked", lambda *_: self.show_sources())
        title = common.vbox(2, common.label("Restaurar «{}»".format(comp.name), ("sg-title",)),
                            common.label("Última cópia {}".format(fmt.relative(comp.last_success))
                                         if comp.last_success else "", ("sg-dim",)))
        box.pack_start(common.hbox(12, back, title), False, False, 0)

        when_box = common.section("De quando", None)
        self.when = Gtk.ComboBoxText()
        latest = comp.last_success
        self.when.append_text("Cópia mais recente{}".format(
            " ({})".format(fmt.datetime_short(latest)) if latest else ""))
        for run in self.runs:
            stamp = fmt.datetime_short(run.moment) if run.moment else run.name
            self.when.append_text("Ficheiros apagados ou substituídos em {}".format(stamp))
        self.when.set_active(0)
        self.when.connect("changed", lambda *_: self._on_when_changed())
        when_box.pack_start(self.when, False, False, 4)
        box.pack_start(when_box, False, False, 0)

        self.items_section = common.section("O que restaurar", None)
        box.pack_start(self.items_section, False, False, 0)

        where = common.section("Para onde", None)
        self.to_original = Gtk.RadioButton.new_with_label(
            None, "Para os sítios de onde vieram (pasta pessoal: {})".format(paths.display_path(paths.home())))
        self.to_folder = Gtk.RadioButton.new_with_label_from_widget(
            self.to_original, "Para outra pasta, para ver primeiro o que lá está:")
        self.folder_button = Gtk.FileChooserButton.new("Pasta de destino", Gtk.FileChooserAction.SELECT_FOLDER)
        self.folder_button.set_current_folder(paths.home())
        self.folder_button.set_sensitive(False)
        self.to_folder.connect("toggled", lambda b: self.folder_button.set_sensitive(b.get_active()))
        where.pack_start(self.to_original, False, False, 2)
        where.pack_start(common.hbox(10, self.to_folder, self.folder_button), False, False, 2)
        box.pack_start(where, False, False, 0)

        conflicts = common.section("Se um ficheiro já existir no computador", None)
        self.conflict_replace = Gtk.RadioButton.new_with_label(
            None, "Substituir pelo da cópia (o que lá estava fica guardado à parte)")
        self.conflict_skip = Gtk.RadioButton.new_with_label_from_widget(
            self.conflict_replace, "Manter o que está no computador")
        self.conflict_newer = Gtk.RadioButton.new_with_label_from_widget(
            self.conflict_replace, "Substituir só se o da cópia for mais recente")
        for radio in (self.conflict_replace, self.conflict_skip, self.conflict_newer):
            conflicts.pack_start(radio, False, False, 2)
        box.pack_start(conflicts, False, False, 0)

        self.settings_check = Gtk.CheckButton.new_with_label(
            "Aplicar também as definições do ambiente de trabalho (painel, atalhos, aparência)")
        self.settings_check.set_active(True)
        box.pack_start(self.settings_check, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        browse = Gtk.Button.new_with_mnemonic("Abrir no _gestor de ficheiros")
        browse.connect("clicked", lambda *_: common.open_path(self._tree()))
        actions.pack_start(browse, False, False, 0)
        self.restore_button = Gtk.Button.new_with_mnemonic("_Restaurar")
        common.add_classes(self.restore_button, "suggested-action", "sg-big-button")
        self.restore_button.connect("clicked", lambda *_: self.start_restore())
        actions.pack_end(self.restore_button, False, False, 0)
        actions.set_margin_top(6)
        box.pack_start(actions, False, False, 0)
        self._fill_items()
        box.show_all()

    def _on_when_changed(self) -> None:
        latest = self.when.get_active() <= 0
        (self.conflict_replace if latest else self.conflict_skip).set_active(True)
        self._fill_items()

    def _fill_items(self) -> None:
        for child in self.items_section.get_children()[1:]:
            self.items_section.remove(child)
            child.destroy()
        self._size_cancel.set()
        self._size_cancel = threading.Event()
        tree = self._tree()
        self.items = restore.items_for_tree(tree, self.computer.layout.read_meta())
        self.checks = {}
        if not self.items:
            self.items_section.pack_start(common.label("Esta cópia está vazia.", ("sg-dim",)), False, False, 4)
            self.items_section.show_all()
            return
        toggles = common.hbox(4)
        for text, value in (("Tudo", True), ("Nada", False)):
            button = Gtk.Button.new_with_label(text)
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.connect("clicked", lambda _b, v=value: self._set_all(v))
            toggles.pack_start(button, False, False, 0)
        self.items_section.pack_start(toggles, False, False, 0)
        listbox = common.boxed_list()
        size_labels = {}
        for index, item in enumerate(self.items):
            check = Gtk.CheckButton()
            check.set_active(True)
            check.connect("toggled", lambda *_: self._update_button())
            self.checks[index] = check
            size = common.label("…", ("sg-dim", "sg-small"), xalign=1.0)
            size.set_width_chars(9)
            suffix = common.hbox(10, size, check)
            subtitle = paths.safe_text(item.where) if item.where else None
            if item.is_settings_export:
                subtitle = "Painel, atalhos de teclado, aparência e applets"
            lrow = common.row(item.label, subtitle, item.icon, suffix, activatable=True)
            lrow.check = check  # type: ignore[attr-defined]
            listbox.add(lrow)
            size_labels[index] = size
        listbox.connect("row-activated", lambda lb, r: r.check.set_active(not r.check.get_active()))
        self.items_section.pack_start(listbox, False, False, 6)
        self.items_section.show_all()
        self.settings_check.set_visible(any(i.is_settings_export for i in self.items))
        self._measure(tree, size_labels)
        self._update_button()

    def _measure(self, tree: str, labels: Dict[int, Gtk.Label]) -> None:
        cancel = self._size_cancel
        items = list(self.items)

        def work():
            for index, item in enumerate(items):
                if cancel.is_set():
                    return
                files = size = 0
                for rel in item.rels:
                    path = os.path.join(tree, rel)
                    if item.kind == "loose":
                        try:
                            for entry in os.scandir(path):
                                if not entry.name.startswith(".") and entry.is_file(follow_symlinks=False):
                                    files += 1
                                    size += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
                    elif os.path.isdir(path) and not os.path.islink(path):
                        f, s = fileops.tree_stats(path)
                        files += f
                        size += s
                    elif os.path.lexists(path):
                        files += 1
                        size += os.lstat(path).st_size
                GLib.idle_add(self._show_size, labels, index, files, size, cancel)

        threading.Thread(target=work, daemon=True, name="salvaguarda-restore-sizes").start()

    def _show_size(self, labels, index, files, size, cancel) -> bool:
        if not cancel.is_set() and index in labels:
            labels[index].set_text(fmt.size(size) if files else "vazio")
            labels[index].set_tooltip_text(fmt.files(files))
        return False

    def _set_all(self, value: bool) -> None:
        for check in self.checks.values():
            check.set_active(value)

    def _update_button(self) -> None:
        if hasattr(self, "restore_button"):
            self.restore_button.set_sensitive(any(c.get_active() for c in self.checks.values()))

    def selected_items(self) -> List[restore.RestoreItem]:
        return [item for i, item in enumerate(self.items) if self.checks.get(i) and self.checks[i].get_active()]

    # ---------------------------------------------------------------- restore
    def start_restore(self) -> None:
        items = self.selected_items()
        if not items:
            return
        comp = self.computer
        target = restore.TARGET_FOLDER if self.to_folder.get_active() else restore.TARGET_ORIGINAL
        folder = self.folder_button.get_filename() if target == restore.TARGET_FOLDER else ""
        if target == restore.TARGET_FOLDER and not folder:
            common.message(self.window, "Escolhe a pasta de destino")
            return
        if self.conflict_skip.get_active():
            conflict = restore.CONFLICT_SKIP
        elif self.conflict_newer.get_active():
            conflict = restore.CONFLICT_NEWER
        else:
            conflict = restore.CONFLICT_REPLACE

        owner = restore.ownership_problem(comp.layout.computer_dir)
        if owner is not None:
            answer = common.choose(
                self.window, "Estas cópias pertencem a outro utilizador",
                "Foram feitas por um utilizador com outro número interno ({}), o que acontece "
                "depois de reinstalar o sistema. Alguns ficheiros privados (como as chaves SSH) "
                "podem não ser legíveis. Corrigir dá a posse das cópias ao teu utilizador e "
                "pede a palavra-passe de administrador.".format(owner),
                [("cancel", "Cancelar"), ("continue", "Continuar assim"), ("fix", "Corrigir")],
                suggested="fix", kind=Gtk.MessageType.WARNING)
            if answer == "cancel":
                return
            if answer == "fix":
                error = system.take_ownership(comp.layout.computer_dir)
                if error:
                    common.message(self.window, "Não foi possível corrigir", error, Gtk.MessageType.ERROR)
                    return

        if target == restore.TARGET_ORIGINAL:
            running = system.running_process_names()
            open_apps = []
            for item in items:
                preset = catalog.PRESETS_BY_ID.get(item.key) if item.kind == "preset" else None
                if preset and running.intersection(preset.processes) and preset.label not in open_apps:
                    open_apps.append(preset.label)
            if open_apps:
                answer = common.choose(
                    self.window, "Fecha estas apps antes de restaurar",
                    "Se estiverem abertas enquanto os dados delas são restaurados, podem "
                    "voltar a gravar por cima ou ficar com dados misturados.",
                    [("cancel", "Cancelar"), ("continue", "Continuar mesmo assim")],
                    details=open_apps, kind=Gtk.MessageType.WARNING)
                if answer != "continue":
                    return

        tree = self._tree()
        apply_settings = self.settings_check.get_active() and self.settings_check.get_visible()
        self._job = (tree, target, comp)
        self.progress.reset()
        self.set_visible_child_name("progress")
        self.window.job_started("A restaurar ficheiros")
        self.runner.start(lambda cb, cancel: restore.RestoreEngine(
            tree, items, target=target, folder=folder, conflict=conflict,
            apply_settings=apply_settings, dconf_load=system.dconf_load,
            callbacks=cb, cancel_event=cancel))

    def _confirm_cancel(self) -> None:
        if common.confirm(self.window, "Cancelar o restauro?",
                          "O que já foi restaurado fica no sítio.",
                          "Cancelar o restauro", "Continuar", destructive=True):
            self.progress.cancel_button.set_sensitive(False)
            self.runner.cancel()

    def _on_phase(self, phase: str, text: str) -> None:
        self.progress.set_phase(phase, text)

    def _on_progress(self, progress) -> None:
        self.progress.update(progress)

    def _on_done(self, result: Optional[restore.RestoreResult]) -> None:
        self.progress.stop()
        self.window.job_finished()
        if result is None:
            common.message(self.window, "Erro inesperado", "O restauro parou devido a um erro interno.",
                           Gtk.MessageType.ERROR)
            self.show_sources()
            return
        tree, target, comp = self._job
        if (result.status in (STATUS_OK, STATUS_WARNINGS, STATUS_NOTHING)
                and tree == comp.layout.current_dir and target == restore.TARGET_ORIGINAL):
            self.window.adopt_backup(comp)
        report.append_history({
            "tipo": "restauro", "resultado": result.status, "mensagem": result.message,
            "inicio": fmt.iso(result.started), "fim": fmt.iso(result.finished),
            "computador": comp.name, "disco": "",
        })
        self.window.notify_done(RESTORE_TITLES.get(result.status, ""), result.message)
        buttons = []
        if result.replaced_dir:
            replaced = Gtk.Button.new_with_mnemonic("Ver ficheiros _substituídos")
            common.add_classes(replaced, "sg-pill")
            replaced.connect("clicked", lambda *_: common.open_path(result.replaced_dir))
            buttons.append(replaced)
        done = Gtk.Button.new_with_mnemonic("_Concluído")
        common.add_classes(done, "sg-pill", "suggested-action")
        done.connect("clicked", lambda *_: self.show_sources())
        buttons.append(done)
        notes = list(result.notes)
        if result.replaced_dir:
            notes.append("Os ficheiros que foram substituídos estão em {}.".format(
                paths.display_path(result.replaced_dir)))
        self.result.show_result(result.status, RESTORE_TITLES.get(result.status, ""),
                                result.message, notes, result.issues, buttons)
        self.set_visible_child_name("result")
