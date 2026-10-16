"""The "O que copiar" editor, used in the first-run assistant and in its own dialog."""

from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, GObject, Gtk, Pango  # noqa: E402

from ..core import catalog, controller, fmt, paths, selection  # noqa: E402
from ..core.config import KIND_FOLDER, KIND_LOOSE, KIND_PRESET, KIND_PROJECT, Config  # noqa: E402
from ..core.measure import measure  # noqa: E402
from . import common  # noqa: E402

_SIZE_CACHE: Dict[Tuple, Tuple[int, int]] = {}

PROJECTS_HELP = (
    "São copiados o código, os repositórios Git (.git) e os ficheiros .env. Ficam de fora "
    "node_modules, ambientes virtuais Python, __pycache__ e outras pastas que um "
    "«npm install» ou «pip install» volta a criar."
)


class _SizeWorker:
    """Measures candidates one at a time in a background thread."""

    def __init__(self, config: Config, deliver):
        self.config = config
        self.deliver = deliver
        self.cancel = threading.Event()
        self._jobs: List[Tuple[Tuple, List[str], bool]] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def signature(self) -> Tuple:
        s = self.config.settings
        return (s.exclude_caches, s.exclude_dev, s.exclude_tools, tuple(s.custom_excludes))

    def request(self, key: Tuple, paths_: List[str], loose: bool) -> Optional[Tuple[int, int]]:
        cache_key = (tuple(paths_), loose, self.signature())
        if cache_key in _SIZE_CACHE:
            return _SIZE_CACHE[cache_key]
        with self._lock:
            self._jobs.append((key, list(paths_), loose))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="salvaguarda-sizes")
                self._thread.start()
        return None

    def _run(self) -> None:
        exclusions = controller.make_exclusions(self.config)
        signature = self.signature()
        while not self.cancel.is_set():
            with self._lock:
                if not self._jobs:
                    self._thread = None
                    return
                key, paths_, loose = self._jobs.pop(0)
            cache_key = (tuple(paths_), loose, signature)
            if cache_key not in _SIZE_CACHE:
                files = size = 0
                for path in paths_:
                    f, s = measure(path, exclusions, self.cancel, loose_only=loose)
                    files += f
                    size += s
                if self.cancel.is_set():
                    return
                _SIZE_CACHE[cache_key] = (files, size)
            GLib.idle_add(self.deliver, key, _SIZE_CACHE[cache_key])

    def stop(self) -> None:
        self.cancel.set()


class SelectionWidget(Gtk.Box):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, config: Config, window_getter, destination_roots: Optional[List[str]] = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.config = config
        self._window = window_getter
        self.destination_roots = destination_roots or []
        self._sizes: Dict[Tuple[str, str], Optional[Tuple[int, int]]] = {}
        self._size_labels: Dict[Tuple[str, str], Gtk.Label] = {}
        self._candidates: List[selection.Candidate] = []
        self.worker = _SizeWorker(config, self._on_size)
        self.total_label = common.label("", ("sg-dim",), wrap=True)
        self.connect("destroy", lambda *_: self.worker.stop())
        self.rebuild()

    # ---------------------------------------------------------------- building
    def rebuild(self) -> None:
        common.clear(self)
        self._size_labels.clear()
        self._candidates = selection.build_candidates(self.config)
        by_section: Dict[str, List[selection.Candidate]] = {}
        for cand in self._candidates:
            by_section.setdefault(cand.section, []).append(cand)

        self.pack_start(self.total_label, False, False, 0)

        self._add_section(
            "Pastas pessoais",
            "Ficam tal e qual: as mesmas pastas, com as mesmas datas e permissões.",
            by_section.get(selection.SECTION_PERSONAL, []),
        )
        projects = self._add_section(
            "Projetos de programação", PROJECTS_HELP,
            by_section.get(selection.SECTION_PROJECTS, []),
            empty_text="Ainda não há pastas de projetos. Adiciona a pasta onde guardas os teus projetos.",
        )
        projects.pack_start(self._add_button("Adicionar pasta de projetos…", self._add_project),
                            False, False, 0)
        self._add_presets(by_section.get(selection.SECTION_PRESETS, []))
        others = self._add_section(
            "Outras pastas e ficheiros",
            "Qualquer pasta ou ficheiro, dentro ou fora da pasta pessoal (por exemplo, "
            "noutro disco interno).",
            by_section.get(selection.SECTION_OTHER, []), empty_text=None,
        )
        buttons = common.hbox(8, self._add_button("Adicionar pasta…", self._add_folder),
                              self._add_button("Adicionar ficheiro…", self._add_file))
        others.pack_start(buttons, False, False, 0)
        self.show_all()
        self._update_total()

    def _add_button(self, text: str, callback) -> Gtk.Button:
        button = Gtk.Button()
        box = common.hbox(6, common.icon("list-add-symbolic", 16), common.label(text))
        button.add(box)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_halign(Gtk.Align.START)
        button.connect("clicked", lambda *_: callback())
        return button

    def _add_section(self, title: str, description: Optional[str],
                     candidates: List[selection.Candidate], empty_text: Optional[str] = "") -> Gtk.Box:
        box = common.section(title, description)
        box.set_spacing(6)
        if candidates:
            listbox = common.boxed_list()
            listbox.set_margin_top(6)
            for cand in candidates:
                listbox.add(self._candidate_row(cand))
            box.pack_start(listbox, False, False, 0)
        elif empty_text:
            box.pack_start(common.label(empty_text, ("sg-dim", "sg-small"), wrap=True), False, False, 4)
        self.pack_start(box, False, False, 0)
        return box

    def _add_presets(self, candidates: List[selection.Candidate]) -> None:
        box = common.section(
            "Configurações de apps",
            "Para voltar a ter tudo como estava depois de formatar: marcadores e "
            "palavras-passe do navegador, e-mail, chaves, editores e o próprio ambiente de "
            "trabalho. Só aparecem as apps encontradas neste computador.")
        box.set_spacing(6)
        if not candidates:
            box.pack_start(common.label("Não foram encontradas configurações conhecidas.",
                                        ("sg-dim", "sg-small")), False, False, 4)
            self.pack_start(box, False, False, 0)
            return
        group_names = dict(catalog.GROUPS)
        order = {g: i for i, (g, _) in enumerate(catalog.GROUPS)}
        candidates = sorted(candidates, key=lambda c: (order.get(c.group, 99), c.label.lower()))
        listbox = common.boxed_list()
        listbox.set_margin_top(6)
        for cand in candidates:
            lrow = self._candidate_row(cand)
            lrow.group = cand.group  # type: ignore[attr-defined]
            listbox.add(lrow)

        def header(lrow, before):
            group = getattr(lrow, "group", "")
            if before is not None and getattr(before, "group", "") == group:
                lrow.set_header(None)
                return
            head = common.label(group_names.get(group, ""), ("sg-group-header",))
            lrow.set_header(head)

        listbox.set_header_func(header)
        box.pack_start(listbox, False, False, 0)
        self.pack_start(box, False, False, 0)

    def _candidate_row(self, cand: selection.Candidate) -> Gtk.ListBoxRow:
        key = (cand.kind, cand.key)
        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        size_label = common.label("", ("sg-dim", "sg-small"), xalign=1.0)
        size_label.set_width_chars(9)
        suffix.pack_start(size_label, False, False, 0)
        self._size_labels[key] = size_label
        if cand.removable:
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON)
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.set_tooltip_text("Retirar da lista")
            remove.connect("clicked", lambda *_: self._remove(cand))
            suffix.pack_start(remove, False, False, 0)
        switch = Gtk.Switch()
        switch.set_active(cand.enabled and cand.exists)
        switch.set_sensitive(cand.exists)
        switch.connect("notify::active", self._on_toggled, cand)
        suffix.pack_start(switch, False, False, 0)
        subtitle = cand.description
        extra = None
        if not cand.exists:
            subtitle = "Não encontrado neste computador. A cópia que já existe no disco é mantida."
            extra = common.badge("em falta")
        lrow = common.row(cand.label, subtitle, cand.icon, suffix, extra=extra)
        lrow.set_tooltip_text("\n".join(paths.display_path(p) for p in cand.paths) or None)
        if cand.exists:
            self._request_size(cand)
        return lrow

    # ------------------------------------------------------------------ sizes
    def _request_size(self, cand: selection.Candidate) -> None:
        key = (cand.kind, cand.key)
        loose = cand.kind == KIND_LOOSE
        found = self.worker.request(key, cand.paths, loose)
        if found is not None:
            self._sizes[key] = found
            self._show_size(key, found)
        else:
            self._sizes[key] = None
            label = self._size_labels.get(key)
            if label is not None:
                label.set_text("…")
                label.set_tooltip_text("A calcular o tamanho")

    def _on_size(self, key, result) -> bool:
        self._sizes[key] = result
        self._show_size(key, result)
        self._update_total()
        return False

    def _show_size(self, key, result) -> None:
        label = self._size_labels.get(key)
        if label is None:
            return
        files, size = result
        label.set_text(fmt.size(size) if files else "vazio")
        label.set_tooltip_text(fmt.files(files))

    def _update_total(self) -> None:
        total = 0
        pending = False
        count = 0
        for cand in self._candidates:
            if not (cand.enabled and cand.exists):
                continue
            count += 1
            value = self._sizes.get((cand.kind, cand.key))
            if value is None:
                pending = True
            else:
                total += value[1]
        if count == 0:
            text = "Nada selecionado. Ativa pelo menos uma pasta."
        else:
            text = "Selecionados {}: cerca de {}".format(
                fmt.plural(count, "item", "itens"), fmt.size(total))
            if pending:
                text += " (a calcular…)"
        self.total_label.set_text(text)

    # ---------------------------------------------------------------- changes
    def _on_toggled(self, switch: Gtk.Switch, _pspec, cand: selection.Candidate) -> None:
        cand.enabled = switch.get_active()
        self.config.set_enabled(cand.kind, cand.key, cand.enabled)
        self._update_total()
        self.emit("changed")

    def _remove(self, cand: selection.Candidate) -> None:
        self.config.remove(cand.kind, cand.key)
        self.rebuild()
        self.emit("changed")

    def _validate(self, path: str) -> Optional[str]:
        path = paths.normalize_source_path(path)
        if paths.is_blocked_source(path):
            return ("Pastas do sistema não podem ser copiadas pela Salvaguarda. "
                    "Escolhe pastas com os teus ficheiros.")
        for root in self.destination_roots:
            if paths.is_inside(path, root) or paths.is_inside(os.path.realpath(path), root):
                return "Essa pasta está no próprio disco de cópias."
        return None

    def _pick(self, title: str, files: bool = False) -> Optional[str]:
        return common.choose_folder(self._window(), title, "Adicionar", paths.home(), files=files)

    def _add(self, kind: str, path: Optional[str]) -> None:
        if not path:
            return
        error = self._validate(path)
        if error:
            common.message(self._window(), "Não é possível adicionar", error, Gtk.MessageType.WARNING)
            return
        if kind == KIND_PROJECT and not os.path.isdir(path):
            common.message(self._window(), "Escolhe uma pasta",
                           "Os projetos têm de ser pastas.", Gtk.MessageType.WARNING)
            return
        self.config.add_path(kind, path)
        self.rebuild()
        self.emit("changed")

    def _add_project(self) -> None:
        self._add(KIND_PROJECT, self._pick("Pasta de projetos"))

    def _add_folder(self) -> None:
        self._add(KIND_FOLDER, self._pick("Pasta a copiar"))

    def _add_file(self) -> None:
        self._add(KIND_FOLDER, self._pick("Ficheiro a copiar", files=True))

    def selected_count(self) -> int:
        return sum(1 for c in self._candidates if c.enabled and c.exists)


def summary_lines(config: Config) -> List[Tuple[str, str, str]]:
    """(icon, title, names) per section, for the overview on the main page."""
    cands = selection.build_candidates(config)
    sections = selection.summary_by_section(cands)
    result = []
    titles = [
        (selection.SECTION_PERSONAL, "folder-documents-symbolic", "Pastas pessoais"),
        (selection.SECTION_PROJECTS, "folder-symbolic", "Projetos de programação"),
        (selection.SECTION_PRESETS, "preferences-system-symbolic", "Configurações de apps"),
        (selection.SECTION_OTHER, "folder-open-symbolic", "Outras pastas e ficheiros"),
    ]
    for section, icon_name, title in titles:
        items = sections.get(section, [])
        if items:
            result.append((icon_name, title, common.join_names([c.label for c in items], 4)))
    return result
