"""Dialogs: disk choice, "O que copiar", settings, history and about."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk, Pango  # noqa: E402

from .. import APP_NAME, VERSION, WEBSITE  # noqa: E402
from ..core import controller, fmt, fsinfo, paths, report, system  # noqa: E402
from ..core.config import Config, Destination  # noqa: E402
from ..core.layout import list_computers, root_for_mount  # noqa: E402
from . import common  # noqa: E402
from .disks import Disk, DiskMonitor, describe_path  # noqa: E402
from .selection_widget import SelectionWidget  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from .window import MainWindow


# --------------------------------------------------------------- disk chooser
class DiskChooser(Gtk.Box):
    """List of connected disks; emits ``selected`` with a Disk (or None)."""

    __gsignals__ = {"selected": (GObject.SignalFlags.RUN_LAST, None, (object,))}

    def __init__(self, monitor: DiskMonitor, window_getter: Callable[[], Gtk.Window]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.monitor = monitor
        self._window = window_getter
        self.extra: List[Disk] = []
        self.selected: Optional[Disk] = None
        self.listbox = common.boxed_list()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self._on_selected)
        self.empty = common.vbox(
            6,
            common.icon("drive-harddisk-symbolic", 48, ("sg-dim",)),
            common.label("Liga o disco externo", ("sg-heading",), xalign=0.5),
            common.label("Aparece aqui assim que for reconhecido.", ("sg-dim",), xalign=0.5),
        )
        for child in self.empty.get_children():
            child.set_halign(Gtk.Align.CENTER)
        self.empty.set_margin_top(12)
        self.empty.set_margin_bottom(12)
        self.empty.set_no_show_all(True)
        self.listbox.set_no_show_all(True)
        self.pack_start(self.listbox, False, False, 0)
        self.pack_start(self.empty, False, False, 0)
        other = Gtk.Button.new_with_mnemonic("Usar uma _pasta (outro disco interno, por exemplo)…")
        other.set_relief(Gtk.ReliefStyle.NONE)
        other.set_halign(Gtk.Align.START)
        other.connect("clicked", lambda *_: self._pick_folder())
        self.pack_start(other, False, False, 0)
        self._handler = monitor.connect("changed", lambda *_: self.reload())
        self.connect("destroy", lambda *_: monitor.disconnect(self._handler))
        self.reload()

    def reload(self) -> None:
        keep = self.selected.path if self.selected else None
        common.clear(self.listbox)
        disks = self.monitor.disks() + [d for d in self.extra]
        seen = set()
        select_row = None
        for disk in disks:
            if disk.path in seen:
                continue
            seen.add(disk.path)
            lrow = self._disk_row(disk)
            self.listbox.add(lrow)
            if disk.path == keep:
                select_row = lrow
        for child in self.listbox.get_children():
            child.show_all()
        self.listbox.set_visible(bool(seen))
        if seen:
            self.empty.hide()
        else:
            for child in self.empty.get_children():
                child.show()
            self.empty.show()
        if select_row is not None:
            self.listbox.select_row(select_row)
        else:
            self.selected = None
            self.emit("selected", None)

    def _disk_row(self, disk: Disk) -> Gtk.ListBoxRow:
        parts = []
        if disk.usage is not None:
            parts.append("{} livres de {}".format(fmt.size(disk.usage.free), fmt.size(disk.usage.total)))
        parts.append(fsinfo.fs_label(disk.fs_type))
        extra = None
        if disk.marker_id:
            extra = common.badge("já tem cópias")
        subtitle = " · ".join(parts)
        if not disk.posix:
            subtitle += "\nFormato sem permissões Linux: recomenda-se formatar em ext4."
        lrow = common.row(disk.name, subtitle, disk.icon_name, None, activatable=True,
                          title_classes=("sg-heading",), extra=extra)
        lrow.disk = disk  # type: ignore[attr-defined]
        return lrow

    def _on_selected(self, _listbox, lrow) -> None:
        self.selected = getattr(lrow, "disk", None) if lrow is not None else None
        self.emit("selected", self.selected)

    def _pick_folder(self) -> None:
        folder = common.choose_folder(self._window(), "Pasta para guardar as cópias", "Usar esta pasta")
        if not folder:
            return
        disk = describe_path(folder, removable=False, icon_name="folder-symbolic")
        self.extra.append(disk)
        self.selected = disk
        self.reload()


def prepare_disk(window: Gtk.Window, config: Config, disk: Disk) -> Optional[Destination]:
    """Checks before using a disk; registers it. Returns None when the user gives up."""
    if os.path.normpath(disk.path) == paths.home():
        common.message(window, "Escolhe outra pasta",
                       "A pasta pessoal não pode guardar as próprias cópias.", Gtk.MessageType.WARNING)
        return None
    if not disk.posix:
        options = [("back", "Escolher outro disco"), ("anyway", "Usar mesmo assim")]
        tool = system.disk_tool()
        if tool:
            options.insert(1, ("format", "Abrir a app Discos"))
        answer = common.choose(
            window, "Este disco usa o formato {}".format(fsinfo.fs_label(disk.fs_type)),
            "Esse formato não guarda as permissões nem as ligações simbólicas do Linux, por isso "
            "a cópia não fica fiel (por exemplo, as chaves SSH perdem a proteção). Para cópias "
            "fiéis, formata o disco em ext4 com a app Discos. Atenção: formatar apaga tudo o "
            "que está no disco.", options, kind=Gtk.MessageType.WARNING, suggested="back")
        if answer == "format" and tool:
            import subprocess
            try:
                subprocess.Popen(tool)
            except OSError:
                pass
            return None
        if answer != "anyway":
            return None
    root = root_for_mount(disk.path)
    if not os.path.isdir(root) and not fsinfo.can_write(disk.path) or \
            os.path.isdir(root) and not fsinfo.can_write(root):
        answer = common.choose(
            window, "O disco não deixa o teu utilizador escrever",
            "Acontece com discos acabados de formatar em ext4: pertencem ao administrador. "
            "A Salvaguarda pode criar a pasta das cópias e dá-la ao teu utilizador. Vai ser "
            "pedida a palavra-passe de administrador.",
            [("cancel", "Cancelar"), ("fix", "Corrigir")], suggested="fix",
            kind=Gtk.MessageType.WARNING)
        if answer != "fix":
            return None
        error = system.take_ownership(root, create=not os.path.isdir(root))
        if error:
            common.message(window, "Não foi possível corrigir", error, Gtk.MessageType.ERROR)
            return None
    try:
        return controller.register_destination(config, disk.path, disk.name, disk.uuid)
    except OSError as exc:
        common.message(window, "Não foi possível preparar o disco",
                       "{}: {}".format(paths.display_path(root), exc.strerror or exc),
                       Gtk.MessageType.ERROR)
        return None


class AddDiskDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, monitor: DiskMonitor):
        super().__init__(title="Adicionar disco de cópias", transient_for=parent, modal=True,
                         use_header_bar=True)
        self.set_default_size(520, 420)
        self.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        self.ok = self.add_button("Usar este disco", Gtk.ResponseType.OK)
        common.add_classes(self.ok, "suggested-action")
        self.ok.set_sensitive(False)
        self.chooser = DiskChooser(monitor, lambda: self)
        self.chooser.connect("selected", lambda _c, d: self.ok.set_sensitive(d is not None))
        area = self.get_content_area()
        area.add(common.scrolled_page(self.chooser, 560))
        area.set_vexpand(True)
        self.show_all()


# -------------------------------------------------------------- what to copy
class SelectionDialog(Gtk.Dialog):
    def __init__(self, window: "MainWindow"):
        super().__init__(title="O que copiar", transient_for=window, modal=True, use_header_bar=True)
        self.set_default_size(680, 720)
        self.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        save = self.add_button("Guardar", Gtk.ResponseType.OK)
        common.add_classes(save, "suggested-action")
        self.working = Config.from_dict(window.config.to_dict())
        roots = [c.root for c in window.connected_all()] + [d.path for d in window.config.destinations]
        self.selector = SelectionWidget(self.working, lambda: self, roots)
        area = self.get_content_area()
        scroller = common.scrolled_page(self.selector, 640)
        scroller.set_vexpand(True)
        area.add(scroller)
        self.show_all()


# ---------------------------------------------------------------- preferences
KEEP_OPTIONS = [(30, "1 mês"), (90, "3 meses"), (180, "6 meses"), (365, "1 ano"),
                (0, "Até faltar espaço no disco")]
REMIND_OPTIONS = [(0, "Nunca"), (3, "3 dias"), (7, "1 semana"), (14, "2 semanas"), (30, "1 mês")]


class PreferencesDialog(Gtk.Dialog):
    def __init__(self, window: "MainWindow"):
        super().__init__(title="Definições", transient_for=window, modal=True, use_header_bar=True)
        self.main_window = window
        self.set_default_size(640, 720)
        self.working = Config.from_dict(window.config.to_dict())
        self.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        save = self.add_button("Guardar", Gtk.ResponseType.OK)
        common.add_classes(save, "suggested-action")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        s = self.working.settings

        # Disks
        disks = common.section("Discos de cópias",
                               "Podes usar mais do que um disco (por exemplo, um em casa e outro "
                               "noutro sítio). A cópia é feita para o que estiver ligado.")
        self.disk_list = common.boxed_list()
        disks.pack_start(self.disk_list, False, False, 6)
        add = Gtk.Button.new_with_mnemonic("_Adicionar disco…")
        add.set_halign(Gtk.Align.START)
        add.connect("clicked", lambda *_: self._add_disk())
        disks.pack_start(add, False, False, 0)
        content.pack_start(disks, False, False, 0)
        self._fill_disks()

        # Computer name
        name_box = common.section("Nome deste computador nas cópias",
                                  "É o nome da pasta deste computador no disco. Se mudares o nome, "
                                  "as próximas cópias começam numa pasta nova.")
        self.name_entry = Gtk.Entry()
        self.name_entry.set_text(self.working.computer_name)
        name_box.pack_start(self.name_entry, False, False, 6)
        content.pack_start(name_box, False, False, 0)

        # Versions
        versions = common.section("Versões anteriores",
                                  "Quando apagas ou alteras um ficheiro, a versão antiga fica no "
                                  "disco, na pasta «Versões anteriores».")
        listbox = common.boxed_list()
        self.keep_combo = Gtk.ComboBoxText()
        for value, text in KEEP_OPTIONS:
            self.keep_combo.append(str(value), text)
        self.keep_combo.set_active_id(str(s.keep_versions_days) if str(s.keep_versions_days) in
                                      [str(v) for v, _ in KEEP_OPTIONS] else "180")
        listbox.add(common.row("Guardar versões anteriores durante", None, None, self.keep_combo))
        self.prune_switch = Gtk.Switch()
        self.prune_switch.set_active(s.prune_when_full)
        listbox.add(common.row("Apagar as mais antigas quando faltar espaço",
                               "Só quando o disco não tem espaço para a cópia seguinte.", None,
                               self.prune_switch))
        self.max_spin = Gtk.SpinButton.new_with_range(0, 1000, 1)
        self.max_spin.set_value(s.preset_versions_max_mb)
        listbox.add(common.row("Versões de configurações de apps até (MB)",
                               "Bases de dados de navegadores e caixas de correio mudam sempre "
                               "que as usas; acima deste tamanho, só se guarda a versão atual.",
                               None, self.max_spin))
        versions.pack_start(listbox, False, False, 6)
        content.pack_start(versions, False, False, 0)

        # Exclusions
        excl = common.section("Não copiar",
                              "Coisas que ocupam espaço e se recriam sozinhas. Recomenda-se manter tudo ativo.")
        listbox = common.boxed_list()
        self.caches_switch = Gtk.Switch()
        self.caches_switch.set_active(s.exclude_caches)
        listbox.add(common.row("Caches, lixo e ficheiros temporários",
                               "~/.cache, Lixo, miniaturas, caches dos navegadores, "
                               "transferências incompletas", None, self.caches_switch))
        self.dev_switch = Gtk.Switch()
        self.dev_switch.set_active(s.exclude_dev)
        listbox.add(common.row("Dependências e ficheiros gerados de projetos",
                               "node_modules, ambientes virtuais Python, __pycache__, .next, "
                               ".turbo e afins", None, self.dev_switch))
        self.tools_switch = Gtk.Switch()
        self.tools_switch.set_active(s.exclude_tools)
        listbox.add(common.row("Ferramentas e pacotes reinstaláveis",
                               "~/.npm, ~/.cargo, ~/.rustup, versões do Node e do Python, "
                               "extensões de editores, Flatpak, modelos de IA locais",
                               None, self.tools_switch))
        excl.pack_start(listbox, False, False, 6)
        excl.pack_start(common.label(
            "Exclusões personalizadas, uma por linha. Exemplos: *.iso (em qualquer pasta), "
            "Transferências/Filmes/ (a partir da pasta pessoal), /media/dados/tmp (caminho completo).",
            ("sg-dim", "sg-small"), wrap=True), False, False, 2)
        self.custom_view = Gtk.TextView()
        self.custom_view.set_monospace(True)
        self.custom_view.set_top_margin(6)
        self.custom_view.set_bottom_margin(6)
        self.custom_view.set_left_margin(8)
        self.custom_view.get_buffer().set_text("\n".join(s.custom_excludes))
        frame = Gtk.Frame()
        frame.set_size_request(-1, 90)
        frame.add(self.custom_view)
        excl.pack_start(frame, False, False, 4)
        content.pack_start(excl, False, False, 0)

        # Other options
        other = common.section("Outras opções", None)
        listbox = common.boxed_list()
        self.app_switch = Gtk.Switch()
        self.app_switch.set_active(s.copy_app_to_disk)
        listbox.add(common.row("Guardar uma cópia da app no disco",
                               "Para restaurar depois de formatar, mesmo sem internet e sem instalar nada.",
                               None, self.app_switch))
        self.apps_switch = Gtk.Switch()
        self.apps_switch.set_active(s.warn_open_apps)
        listbox.add(common.row("Avisar se o navegador ou o e-mail estiverem abertos",
                               "Os dados destas apps ficam mais fiáveis na cópia com elas fechadas.",
                               None, self.apps_switch))
        self.remind_combo = Gtk.ComboBoxText()
        for value, text in REMIND_OPTIONS:
            self.remind_combo.append(str(value), text)
        if not self.remind_combo.set_active_id(str(s.remind_after_days)):
            self.remind_combo.set_active_id("7")
        listbox.add(common.row("Lembrar quando a última cópia tiver mais de", None, None,
                               self.remind_combo))
        other.pack_start(listbox, False, False, 6)
        content.pack_start(other, False, False, 0)

        area = self.get_content_area()
        scroller = common.scrolled_page(content, 620)
        scroller.set_vexpand(True)
        area.add(scroller)
        self.show_all()

    def _fill_disks(self) -> None:
        common.clear(self.disk_list)
        connected = {c.destination.id for c in self.main_window.connected_all()}
        if not self.working.destinations:
            self.disk_list.add(common.row("Nenhum disco escolhido", None, "drive-harddisk-symbolic"))
        for dest in self.working.destinations:
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON)
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.set_tooltip_text("Deixar de usar este disco (as cópias no disco não são apagadas)")
            remove.connect("clicked", lambda _b, d=dest: self._remove_disk(d))
            state = "ligado" if dest.id in connected else "não ligado"
            subtitle = "{} · {}".format(state, paths.display_path(dest.path))
            if dest.fs_type:
                subtitle += " · " + fsinfo.fs_label(dest.fs_type)
            self.disk_list.add(common.row(dest.name, subtitle, "drive-harddisk-symbolic", remove))
        self.disk_list.show_all()

    def _add_disk(self) -> None:
        dialog = AddDiskDialog(self, self.main_window.disks)
        response = dialog.run()
        disk = dialog.chooser.selected
        dialog.destroy()
        if response == Gtk.ResponseType.OK and disk is not None:
            if prepare_disk(self, self.working, disk) is not None:
                self._fill_disks()

    def _remove_disk(self, dest: Destination) -> None:
        if common.confirm(self, "Deixar de usar «{}»?".format(dest.name),
                          "As cópias que já estão nesse disco não são apagadas.", "Deixar de usar"):
            self.working.remove_destination(dest.id)
            self._fill_disks()

    def apply(self) -> Config:
        s = self.working.settings
        s.keep_versions_days = int(self.keep_combo.get_active_id() or 180)
        s.prune_when_full = self.prune_switch.get_active()
        s.preset_versions_max_mb = int(self.max_spin.get_value())
        s.exclude_caches = self.caches_switch.get_active()
        s.exclude_dev = self.dev_switch.get_active()
        s.exclude_tools = self.tools_switch.get_active()
        buf = self.custom_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        from ..core.exclusions import validate_custom
        s.custom_excludes = validate_custom(text.splitlines())
        s.copy_app_to_disk = self.app_switch.get_active()
        s.warn_open_apps = self.apps_switch.get_active()
        s.remind_after_days = int(self.remind_combo.get_active_id() or 7)
        name = self.name_entry.get_text().strip()
        if name:
            from ..core.layout import sanitize_computer_name
            self.working.computer_name = sanitize_computer_name(name)
        return self.working


# ------------------------------------------------------------------- history
STATUS_ICONS = {
    "ok": ("emblem-ok-symbolic", "sg-success"),
    "nothing": ("emblem-ok-symbolic", "sg-success"),
    "warnings": ("dialog-warning-symbolic", "sg-warning"),
    "cancelled": ("process-stop-symbolic", "sg-dim"),
    "failed": ("dialog-error-symbolic", "sg-error"),
}


class HistoryDialog(Gtk.Dialog):
    def __init__(self, window: "MainWindow"):
        super().__init__(title="Histórico", transient_for=window, modal=True, use_header_bar=True)
        self.set_default_size(600, 560)
        history = list(reversed(report.load_history()))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        if not history:
            content.pack_start(common.label("Ainda não há cópias registadas neste computador.",
                                            ("sg-dim",), xalign=0.5), False, False, 30)
        else:
            listbox = common.boxed_list()
            for entry in history:
                status = entry.get("resultado", "")
                icon_name, css = STATUS_ICONS.get(status, STATUS_ICONS["ok"])
                when = fmt.parse_iso(entry.get("inicio"))
                kind = "Restauro" if entry.get("tipo") == "restauro" else "Cópia"
                title = "{} · {}".format(kind, fmt.datetime_short(when) if when else "?")
                if entry.get("disco"):
                    title += " · " + entry["disco"]
                suffix = None
                log = entry.get("registo")
                if log and os.path.exists(log):
                    suffix = Gtk.Button.new_with_label("Registo")
                    suffix.connect("clicked", lambda _b, p=log: common.open_path(p))
                lrow = common.row(title, entry.get("mensagem", ""), None, suffix)
                box = lrow.get_child()
                image = common.icon(icon_name, 20, (css,))
                image.set_valign(Gtk.Align.CENTER)
                box.pack_start(image, False, False, 0)
                box.reorder_child(image, 0)
                listbox.add(lrow)
            content.pack_start(listbox, False, False, 0)
        area = self.get_content_area()
        scroller = common.scrolled_page(content, 580)
        scroller.set_vexpand(True)
        area.add(scroller)
        self.show_all()


def about(window: Gtk.Window) -> None:
    dialog = Gtk.AboutDialog(transient_for=window, modal=True)
    dialog.set_program_name(APP_NAME)
    dialog.set_version(VERSION)
    dialog.set_comments("Cópias de segurança simples e fiéis para um disco externo.\n"
                        "Funciona sem internet.")
    dialog.set_website(WEBSITE)
    dialog.set_website_label("Código-fonte")
    dialog.set_license_type(Gtk.License.GPL_3_0)
    icon_name = common.app_icon_name()
    if icon_name:
        dialog.set_logo_icon_name(icon_name)
    else:
        image = common.app_icon_image(96)
        pixbuf = image.get_pixbuf()
        if pixbuf is not None:
            dialog.set_logo(pixbuf)
    dialog.run()
    dialog.destroy()
