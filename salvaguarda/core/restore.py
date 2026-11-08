"""Restore: put the files of a backup back in their place (or in another folder).

Nothing on the computer is ever deleted. When a file already exists, the chosen
policy decides; with "replace", the file that was there is moved to
``~/.local/share/salvaguarda/substituidos/<data>/`` instead of being lost.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import fnmatch
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import desktop, fileops, fmt, fsinfo, paths, postrestore, system, variants
from .engine import (PHASE_COPY, PHASE_FINISH, PHASE_PREPARE, PHASE_SCAN, PROGRESS_INTERVAL,
                     STATUS_CANCELLED, STATUS_FAILED, STATUS_NOTHING, STATUS_OK, STATUS_WARNINGS,
                     Callbacks, Issue, Progress)
from .fileops import Cancelled, EntryError, Fatal
from .layout import CURRENT_DIR, EXTRAS_DIR, HOME_DIR, OTHER_DIR, TMP_PREFIX

CONFLICT_REPLACE = "replace"
CONFLICT_SKIP = "skip"
CONFLICT_NEWER = "newer"

TARGET_ORIGINAL = "original"
TARGET_FOLDER = "folder"


@dataclass
class RestoreItem:
    label: str
    kind: str
    key: str
    rels: List[str]  # paths inside the tree ("Pasta pessoal/Documentos", ...)
    icon: str = "folder-symbolic"
    xdg_key: str = ""
    extra: str = ""
    original: str = ""  # where it lived, for display
    originals: List[str] = field(default_factory=list)
    exports: List[str] = field(default_factory=list)  # generated files (Extras/...), never copied

    @property
    def settings_exports(self) -> List[str]:
        return [rel for rel in self.exports if rel.endswith(".ini")]

    @property
    def is_settings_export(self) -> bool:
        """The item carries desktop settings exported with dconf."""
        return bool(self.settings_exports)

    @property
    def is_inventory(self) -> bool:
        return self.extra == "inventory"

    @property
    def where(self) -> str:
        """'~/.gitconfig e mais 2' style summary of the original locations."""
        places = self.originals or ([self.original] if self.original else [])
        if not places:
            return ""
        if len(places) == 1:
            return places[0]
        return "{} e mais {}".format(places[0], len(places) - 1)


@dataclass
class RestoreStats:
    restored_files: int = 0
    replaced_files: int = 0
    identical_files: int = 0
    kept_existing: int = 0
    links: int = 0
    dirs_created: int = 0
    bytes_copied: int = 0
    files_total: int = 0

    @property
    def files_to_copy(self) -> int:
        return self.restored_files + self.replaced_files


@dataclass
class RestoreResult:
    status: str
    message: str
    started: _dt.datetime
    finished: _dt.datetime
    stats: RestoreStats
    issues: List[Issue]
    notes: List[str] = field(default_factory=list)
    replaced_dir: Optional[str] = None


def items_for_tree(tree_root: str, meta: Dict[str, object]) -> List[RestoreItem]:
    """List what can be restored from a tree ("Cópia atual" or a version folder)."""
    items: Dict[Tuple[str, str], RestoreItem] = {}
    order: List[Tuple[str, str]] = []
    covered: List[str] = []
    for raw in meta.get("fontes") or []:  # type: ignore[union-attr]
        if not isinstance(raw, dict):
            continue
        rel = str(raw.get("copia") or "")
        if not rel or not os.path.lexists(os.path.join(tree_root, rel)):
            continue
        key = (str(raw.get("tipo") or ""), str(raw.get("chave") or rel))
        if key not in items:
            items[key] = RestoreItem(
                label=str(raw.get("nome") or os.path.basename(rel)),
                kind=key[0], key=key[1], rels=[], icon=str(raw.get("icone") or "folder-symbolic"),
                xdg_key=str(raw.get("xdg") or ""), extra=str(raw.get("extra") or ""),
                original=str(raw.get("caminho") or ""),
            )
            order.append(key)
        item = items[key]
        if raw.get("extra"):
            # A generated file: applied or shown, never copied back as a file.
            item.extra = str(raw.get("extra"))
            if rel not in item.exports:
                item.exports.append(rel)
            continue
        if rel not in item.rels:
            item.rels.append(rel)
            if raw.get("caminho"):
                item.originals.append(str(raw.get("caminho")))
        if key[0] != "loose":
            covered.append(rel)

    def is_covered(rel: str) -> bool:
        return any(paths.is_inside(rel, c) or paths.is_inside(c, rel) for c in covered)

    loose_present = any(k[0] == "loose" for k in order)

    # Anything in the tree that the metadata does not describe (old disks, version folders).
    extra_items: List[RestoreItem] = []
    for top, label_prefix in ((HOME_DIR, "~/"), (OTHER_DIR, "/")):
        base = os.path.join(tree_root, top)
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(TMP_PREFIX):
                continue
            rel = os.path.join(top, entry.name)
            if rel in covered:
                continue
            if top == HOME_DIR and loose_present and not entry.name.startswith("."):
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
            if is_covered(rel):
                # Partially described (e.g. ".config" when only ".config/Code/User" was
                # selected): describe the uncovered children one level down.
                try:
                    children = sorted(os.scandir(entry.path), key=lambda e: e.name.lower())
                except OSError:
                    continue
                for child in children:
                    child_rel = os.path.join(rel, child.name)
                    if is_covered(child_rel) or child.name.startswith(TMP_PREFIX):
                        continue
                    extra_items.append(_generic_item(child_rel, label_prefix, child))
                continue
            extra_items.append(_generic_item(rel, label_prefix, entry))
    extras_dir = os.path.join(tree_root, EXTRAS_DIR)
    result = [items[k] for k in order]
    if os.path.isdir(extras_dir) and not any(i.is_settings_export for i in result):
        for entry in sorted(os.scandir(extras_dir), key=lambda e: e.name):
            if entry.name.endswith(".ini"):
                result.append(RestoreItem(
                    "Definições do ambiente de trabalho", "preset", "desktop", [],
                    "preferences-desktop-appearance-symbolic", extra="dconf",
                    exports=[os.path.join(EXTRAS_DIR, entry.name)],
                ))
                break
    return result + extra_items


def old_xdg_dirs(meta: Dict[str, Any]) -> Dict[str, str]:
    """XDG folders of the computer the copy came from (key -> "~/Imagens")."""
    recorded = meta.get("pastas_xdg")
    if isinstance(recorded, dict):
        return {str(k): str(v) for k, v in recorded.items() if isinstance(v, str)}
    result = {}
    for raw in meta.get("fontes") or []:  # disks written by version 1.0.0
        if isinstance(raw, dict) and raw.get("xdg") and isinstance(raw.get("caminho"), str):
            result[str(raw["xdg"])] = raw["caminho"]
    return result


def plan_variants(items: List[RestoreItem],
                  probe: Optional[variants.Probe] = None) -> Dict[str, variants.Remap]:
    """Profiles that should go to another packaging of the same app (deb, snap, Flatpak)."""
    probe = probe or variants.Probe(paths.home())
    all_rels = [rel for item in items for rel in item.rels]
    found: Dict[str, variants.Remap] = {}
    for item in items:
        if item.kind == "preset" and item.key in variants.APP_VARIANTS:
            for remap in variants.plan_remaps(item.key, item.rels, probe):
                dest = HOME_DIR + "/" + remap.destination
                if any(rel != remap.rel and _overlaps(rel, dest) for rel in all_rels):
                    # Another item of the copy restores into that place (the old snap
                    # profile, with «Apps em snap e Flatpak»): both go back as they were.
                    continue
                found[remap.rel] = remap
    return found


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def snap_warnings(items: List[RestoreItem], remaps: Dict[str, variants.Remap],
                  probe: Optional[variants.Probe] = None) -> Dict[str, str]:
    """Rels restored to their own place inside ~/snap/<x>/current while that snap never
    ran here: snapd needs a link there, so the restore leaves them out (warning)."""
    probe = probe or variants.Probe(paths.home())
    out: Dict[str, str] = {}
    for item in items:
        for rel in item.rels:
            parts = rel.split("/", 1)
            if rel in remaps or parts[0] != HOME_DIR or len(parts) < 2:
                continue
            snap = variants.snap_user_dir_problem(parts[1], probe)
            if snap:
                out[rel] = variants.snap_warning(snap)
    return out


class Destinations:
    """Where each path of the copy goes on this computer."""

    def __init__(self, target: str, folder: str, meta: Optional[Dict[str, Any]] = None,
                 remaps: Optional[Dict[str, str]] = None,
                 items: Optional[List[RestoreItem]] = None):
        meta = meta or {}
        self.target = target
        self.folder = folder
        self.home = paths.home()
        self.remaps = remaps or {}
        self.old_user = str(meta.get("utilizador") or "")
        self.new_user = system.user_name()
        new_xdg = paths.xdg_user_dirs(existing_only=False)
        # Old XDG folder (home-relative, e.g. "Documentos") -> folder on this computer
        # (e.g. /home/bruno/Documents when the new system is in English).
        self.xdg_map: Dict[str, str] = {}
        old_xdg = old_xdg_dirs(meta)
        for item in items or []:  # without metadata, the items still say where they lived
            if item.xdg_key and item.xdg_key not in old_xdg and item.original.startswith("~/"):
                old_xdg[item.xdg_key] = item.original
        for key, old in old_xdg.items():
            if key in new_xdg and old.startswith("~/") and len(old) > 2:
                self.xdg_map[old[2:]] = new_xdg[key]

    def keep_apart(self, tree_root: str) -> None:
        """An old XDG folder ("Documentos") is not moved onto a folder that the same copy
        also holds ("Documents", chosen on its own): the two would mix on every restore."""
        for old in list(self.xdg_map):
            new_rel = os.path.relpath(self.xdg_map[old], self.home)
            if new_rel != old and not new_rel.startswith("..") and os.path.lexists(
                    os.path.join(tree_root, HOME_DIR, new_rel)):
                del self.xdg_map[old]

    def moved_folders(self, old_home: str) -> List[Tuple[str, str]]:
        """(old absolute folder, new folder) for the XDG folders that change place."""
        if not old_home:
            return []
        return [(os.path.join(old_home, old), new) for old, new in self.xdg_map.items()
                if os.path.join(self.home, old) != new]

    def for_rel(self, rel: str) -> Optional[str]:
        parts = rel.split(os.sep)
        top, rest = parts[0], parts[1:]
        if top not in (HOME_DIR, OTHER_DIR):
            return None
        if self.target == TARGET_FOLDER:
            return os.path.join(self.folder, *rest) if rest else self.folder
        if top == OTHER_DIR:
            if not rest:
                return None
            if (self.old_user and self.new_user and self.old_user != self.new_user
                    and len(rest) >= 2 and (rest[:2] == ["media", self.old_user]
                                            or rest[:3] == ["run", "media", self.old_user])):
                # Disks mounted under /media/<user>: follow the new user name when it exists.
                index = 1 if rest[0] == "media" else 2
                swapped = rest[:index] + [self.new_user] + rest[index + 1:]
                if os.path.isdir(os.sep + os.path.join(*swapped[:index + 1])):
                    rest = swapped
            return os.sep + os.path.join(*rest)
        if rel in self.remaps:
            return os.path.join(self.home, self.remaps[rel])
        home_rel = "/".join(rest)
        for old in sorted(self.xdg_map, key=len, reverse=True):
            if home_rel == old or home_rel.startswith(old + "/"):
                return self.xdg_map[old] + home_rel[len(old):]
        return os.path.join(self.home, *rest) if rest else self.home


def destination_for(rel: str, item: RestoreItem, target: str, folder: str,
                    meta: Optional[Dict[str, Any]] = None,
                    remaps: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Where ``rel`` (one of ``item.rels``) goes on this computer."""
    return Destinations(target, folder, meta, remaps).for_rel(rel)


def _generic_item(rel: str, prefix: str, entry: os.DirEntry) -> RestoreItem:
    parts = rel.split(os.sep)[1:]
    shown = prefix + "/".join(parts)
    try:
        is_dir = entry.is_dir(follow_symlinks=False)
    except OSError:
        is_dir = False
    return RestoreItem(
        label=paths.safe_text(shown), kind="generic", key=rel, rels=[rel],
        icon="folder-symbolic" if is_dir else "text-x-generic-symbolic", original=shown,
    )


class RestoreEngine:
    def __init__(
        self,
        tree_root: str,
        items: List[RestoreItem],
        *,
        target: str = TARGET_ORIGINAL,
        folder: str = "",
        conflict: str = CONFLICT_REPLACE,
        apply_settings: bool = True,
        dconf_load: Optional[Callable[[str], Optional[str]]] = None,
        dconf_dump: Optional[Callable[[str], Optional[str]]] = None,
        meta: Optional[Dict[str, Any]] = None,
        remaps: Optional[Dict[str, variants.Remap]] = None,
        here_desktop: Optional[str] = None,
        callbacks: Optional[Callbacks] = None,
        cancel_event: Optional[threading.Event] = None,
        now: Optional[Callable[[], _dt.datetime]] = None,
    ):
        self.tree_root = tree_root
        self.items = items
        self.target = target
        self.folder = folder
        self.conflict = conflict
        self.apply_settings = apply_settings
        self.dconf_load = dconf_load
        self.dconf_dump = dconf_dump
        self.meta = meta or {}
        # Only remaps without warnings are applied (see variants.plan_remaps).
        self.remaps = {rel: r for rel, r in (remaps or {}).items() if not r.warnings}
        self.here_desktop = here_desktop
        self.old_home = str(self.meta.get("pasta_pessoal") or "")
        self.settings_applied = False
        self._restored_settings: List[str] = []
        self.cb = callbacks or Callbacks()
        self.cancel_event = cancel_event or threading.Event()
        self._now = now or _dt.datetime.now
        self.started = self._now()
        self.issues: List[Issue] = []
        self.notes: List[str] = []
        self.progress = Progress()
        self.stats = RestoreStats()
        self.dry = True
        self.replaced_dir = os.path.join(paths.app_data_dir(), "substituidos",
                                         fmt.stamp(self.started))
        self._replaced_used = False
        self._snap_reported: Set[str] = set()
        self._settings_partial = False
        self._last_emit = 0.0
        self._speed_t0 = 0.0
        self._speed_b0 = 0
        self._dest = Destinations(target, folder, self.meta,
                                  {rel: r.destination for rel, r in self.remaps.items()}, items)
        self._dest.keep_apart(tree_root)
        self._link_pairs = self._dest.moved_folders(self.old_home)
        self.tolerance_ns = 2_000_000_000

    # ----------------------------------------------------------------- helpers
    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def _issue(self, path: str, message: str, level: str = "warning") -> None:
        if len(self.issues) < 5000:
            self.issues.append(Issue(paths.display_path(path), message, level))

    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < PROGRESS_INTERVAL:
            return
        p = self.progress
        if p.phase == PHASE_COPY and self._speed_t0:
            elapsed = now - self._speed_t0
            if elapsed > 1.5:
                instant = (p.bytes_done - self._speed_b0) / elapsed
                p.speed = instant if not p.speed else 0.7 * p.speed + 0.3 * instant
                self._speed_t0, self._speed_b0 = now, p.bytes_done
            if p.speed > 0:
                p.eta = max(0.0, (p.bytes_total - p.bytes_done) / p.speed)
        self._last_emit = now
        self.cb.progress(dataclasses.replace(p))

    def _set_phase(self, phase: str, text: str) -> None:
        self.progress.phase = phase
        self.cb.phase(phase, text)
        self._emit(force=True)

    def _on_bytes(self, n: int) -> None:
        self.progress.bytes_done += n
        self._emit()

    # --------------------------------------------------------------------- run
    def run(self) -> RestoreResult:
        status, message = STATUS_FAILED, ""
        try:
            self._set_phase(PHASE_PREPARE, "A preparar…")
            if not os.path.isdir(self.tree_root):
                raise Fatal("A cópia já não está acessível. Verifica se o disco está ligado.")
            if self.target == TARGET_FOLDER:
                if not self.folder:
                    raise Fatal("Escolhe a pasta onde queres restaurar.")
                os.makedirs(self.folder, exist_ok=True)
            self._set_phase(PHASE_SCAN, "A comparar com o que já existe…")
            self.dry = True
            self._pass()
            plan = self.stats
            self._check_space(plan)
            self.stats = RestoreStats(files_total=plan.files_to_copy)
            self.progress.files_total = plan.files_to_copy
            self.progress.bytes_total = plan.bytes_copied
            self.progress.bytes_done = self.progress.files_done = 0
            self._speed_t0, self._speed_b0 = time.monotonic(), 0
            if plan.files_to_copy or plan.links or plan.dirs_created:
                self._set_phase(PHASE_COPY, "A restaurar…")
                self.dry = False
                self._pass()
            else:
                self.stats = plan
            self._set_phase(PHASE_FINISH, "A concluir…")
            self._apply_desktop_settings()
            if self.target == TARGET_ORIGINAL:
                self._after_restore_notes()
            changed = self.stats.files_to_copy or self.stats.links or self.settings_applied
            if not changed and not self.issues:
                status = STATUS_NOTHING
                message = "Os ficheiros já estavam todos no sítio. Nada foi alterado."
            else:
                status = STATUS_WARNINGS if self.issues else STATUS_OK
                message = self._message()
        except Cancelled:
            status = STATUS_CANCELLED
            message = "O restauro foi cancelado. O que já foi restaurado fica no sítio."
        except Fatal as exc:
            status, message = STATUS_FAILED, str(exc)
        except OSError as exc:
            status = STATUS_FAILED
            message = "Erro inesperado: {}".format(fileops.describe_error(exc))
        return RestoreResult(status, message, self.started, self._now(), self.stats, self.issues,
                             self.notes, self.replaced_dir if self._replaced_used else None)

    def _message(self) -> str:
        s = self.stats
        n = s.files_to_copy
        if not n and not s.links and self.settings_applied:
            if self._settings_partial:
                return "As definições das apps foram aplicadas."
            return "As definições do ambiente de trabalho foram aplicadas."
        parts = ["{} {} ({})".format(fmt.files(n), fmt.agree(n, "restaurado", "restaurados"),
                                     fmt.size(s.bytes_copied))]
        if s.kept_existing:
            parts.append("{} {}".format(fmt.files(s.kept_existing), fmt.agree(
                s.kept_existing, "mantido como estava", "mantidos como estavam")))
        if s.replaced_files:
            parts.append(fmt.agree(s.replaced_files, "o ficheiro substituído foi guardado à parte",
                                   "os {} substituídos foram guardados à parte".format(
                                       fmt.number(s.replaced_files))))
        text = "; ".join(parts) + "."
        return text[:1].upper() + text[1:]

    def _check_space(self, plan: RestoreStats) -> None:
        base = self.folder if self.target == TARGET_FOLDER else paths.home()
        usage = fsinfo.usage(base)
        needed = plan.bytes_copied + 50 * 1000 * 1000
        if usage is not None and usage.free < needed:
            raise fileops.DiskFull(
                "Não há espaço suficiente neste computador: são precisos {} e há {} livres."
                .format(fmt.size(needed), fmt.size(usage.free)))

    def _pass(self) -> None:
        for item in self.items:
            for rel in item.rels:
                if rel.split(os.sep)[0] not in (HOME_DIR, OTHER_DIR):
                    continue  # generated files (Extras) are applied or shown, never copied
                self._check_cancel()
                src = os.path.join(self.tree_root, rel)
                dst = self._dest.for_rel(rel)
                if dst is None:
                    continue
                if item.kind == "loose":
                    self._restore_loose(src, dst)
                else:
                    self._restore_root(src, dst)

    def _restore_loose(self, src: str, dst: str) -> None:
        """Only the visible files directly inside the folder (not its subfolders)."""
        try:
            entries = sorted(os.scandir(src), key=lambda e: e.name)
        except OSError as exc:
            self._issue(src, "Não foi possível ler a cópia: " + fileops.describe_error(exc), "error")
            return
        if not self.dry:
            os.makedirs(dst, exist_ok=True)
        for entry in entries:
            if entry.name.startswith("."):
                continue
            self._check_cancel()
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            child_dst = os.path.join(dst, entry.name)
            if stat.S_ISREG(est.st_mode):
                self._restore_file(entry.path, child_dst, est)
            elif stat.S_ISLNK(est.st_mode):
                self._restore_link(entry.path, child_dst, est)

    def _restore_root(self, src: str, dst: str) -> None:
        try:
            st = os.lstat(src)
        except OSError as exc:
            if self.dry:
                self._issue(src, "Não foi possível ler a cópia: " + fileops.describe_error(exc), "error")
            return
        missing = os.path.dirname(dst)
        while missing and missing != os.sep and not os.path.lexists(missing):
            if self._snap_current(missing):
                return
            missing = os.path.dirname(missing)
        if not self.dry:
            parent = os.path.dirname(dst)
            if not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as exc:
                    self._issue(parent, "Não foi possível criar a pasta: " + fileops.describe_error(exc), "error")
                    return
        if stat.S_ISDIR(st.st_mode):
            self._restore_dir(src, dst, st)
        elif stat.S_ISREG(st.st_mode):
            self._restore_file(src, dst, st)
        elif stat.S_ISLNK(st.st_mode):
            self._restore_link(src, dst, st)

    def _snap_current(self, dpath: str) -> bool:
        """``dpath`` is ~/snap/<x>/current, which does not exist: snapd creates it as a
        link the first time the snap runs, and a real folder there stops the snap."""
        if self.target != TARGET_ORIGINAL:
            return False
        rel = paths.home_relative(dpath)
        match = re.match(r"^snap/([^/]+)/current$", rel or "")
        if not match or os.path.lexists(dpath):
            return False
        if dpath not in self._snap_reported:
            self._snap_reported.add(dpath)
            if self.dry:
                self._issue(dpath, variants.snap_warning(match.group(1)))
        return True

    def _move_aside(self, dpath: str) -> None:
        rel = paths.home_relative(dpath)
        if rel is None:
            rel = os.path.join(OTHER_DIR, dpath.lstrip(os.sep))
        base = os.path.join(self.replaced_dir, rel)
        os.makedirs(os.path.dirname(base), exist_ok=True)
        target, number = base, 1
        while os.path.lexists(target):  # never over an earlier saved copy
            number += 1
            target = "{}.{}".format(base, number)
        shutil.move(dpath, target)
        self._replaced_used = True

    def _restore_dir(self, spath: str, dpath: str, sst: os.stat_result) -> None:
        self._check_cancel()
        d_st = _lstat(dpath)
        created = False
        if d_st is not None and stat.S_ISLNK(d_st.st_mode):
            # A link to a folder on this computer (dotfiles, snap "current", a data
            # partition): the files go through it, the link stays.
            try:
                followed = os.stat(dpath)
            except OSError:
                followed = None
            if followed is not None and stat.S_ISDIR(followed.st_mode):
                d_st = followed
        if d_st is None and self._snap_current(dpath):
            return
        if d_st is not None and not stat.S_ISDIR(d_st.st_mode):
            if self.conflict == CONFLICT_SKIP:
                self.stats.kept_existing += 1
                if self.dry:
                    self._issue(dpath, "Já existe um ficheiro com o nome desta pasta; não foi alterado.")
                return
            if not self.dry:
                try:
                    self._move_aside(dpath)
                except OSError as exc:
                    self._issue(dpath, "Não foi possível substituir: " + fileops.describe_error(exc), "error")
                    return
            d_st = None
        if d_st is None:
            self.stats.dirs_created += 1
            if not self.dry:
                try:
                    os.mkdir(dpath, 0o700)
                    created = True
                except FileExistsError:
                    pass
                except OSError as exc:
                    self._issue(dpath, "Não foi possível criar a pasta: " + fileops.describe_error(exc), "error")
                    return
        try:
            entries = sorted(os.scandir(spath), key=lambda e: e.name)
        except OSError as exc:
            self._issue(spath, "Não foi possível ler a cópia: " + fileops.describe_error(exc), "error")
            return
        for entry in entries:
            if entry.name.startswith(TMP_PREFIX):
                continue
            self._check_cancel()
            child_dst = os.path.join(dpath, entry.name)
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError as exc:
                self._issue(entry.path, "Não foi possível ler: " + fileops.describe_error(exc), "error")
                continue
            if self.dry:
                self.progress.scanned += 1
                self.progress.current = entry.path
                self._emit()
            if stat.S_ISDIR(est.st_mode):
                if self.dry and d_st is None:
                    # Nothing exists below a folder that is about to be created: count only.
                    self._count_new_tree(entry.path)
                    continue
                self._restore_dir(entry.path, child_dst, est)
            elif stat.S_ISREG(est.st_mode):
                self._restore_file(entry.path, child_dst, est)
            elif stat.S_ISLNK(est.st_mode):
                self._restore_link(entry.path, child_dst, est)
        if created:
            try:
                os.chmod(dpath, stat.S_IMODE(sst.st_mode))
                os.utime(dpath, ns=(sst.st_atime_ns, sst.st_mtime_ns))
            except OSError:
                pass

    def _count_new_tree(self, path: str) -> None:
        stack = [path]
        while stack:
            folder = stack.pop()
            self.stats.dirs_created += 1
            try:
                with os.scandir(folder) as it:
                    for entry in it:
                        if entry.name.startswith(TMP_PREFIX):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_symlink():
                                self.stats.links += 1
                            else:
                                self.stats.restored_files += 1
                                self.stats.bytes_copied += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
                        self.progress.scanned += 1
                        self._emit()
            except OSError as exc:
                self._issue(folder, "Não foi possível ler a cópia: " + fileops.describe_error(exc), "error")

    def _decide(self, est: os.stat_result, d_st: Optional[os.stat_result]) -> str:
        """'new', 'identical', 'keep' or 'replace'."""
        if d_st is None:
            return "new"
        if (stat.S_ISREG(d_st.st_mode) and stat.S_ISREG(est.st_mode)
                and d_st.st_size == est.st_size
                and abs(d_st.st_mtime_ns - est.st_mtime_ns) < self.tolerance_ns):
            return "identical"
        if stat.S_ISDIR(d_st.st_mode):
            return "keep" if self.conflict == CONFLICT_SKIP else "replace"
        if self.conflict == CONFLICT_SKIP:
            return "keep"
        if self.conflict == CONFLICT_NEWER and est.st_mtime_ns <= d_st.st_mtime_ns + self.tolerance_ns:
            return "keep"
        return "replace"

    def _restore_file(self, spath: str, dpath: str, est: os.stat_result) -> None:
        d_st = _lstat(dpath)
        decision = self._decide(est, d_st)
        if decision == "identical":
            self.stats.identical_files += 1
            return
        if decision == "keep":
            self.stats.kept_existing += 1
            return
        if self.dry:
            if decision == "new":
                self.stats.restored_files += 1
            else:
                self.stats.replaced_files += 1
            self.stats.bytes_copied += est.st_size
            return
        self.progress.current = spath
        tmp = fileops.tmp_name(os.path.dirname(dpath))
        try:
            fileops.copy_file(spath, tmp, self._on_bytes, self._check_cancel)
        except fileops.ChangedWhileCopying:
            pass
        except (EntryError, OSError) as exc:
            _silent_unlink(tmp)
            self._issue(spath, _entry_message(exc), "error")
            return
        try:
            if d_st is not None:
                self._move_aside(dpath)
            os.replace(tmp, dpath)
        except OSError as exc:
            _silent_unlink(tmp)
            self._issue(dpath, "Não foi possível restaurar: " + fileops.describe_error(exc), "error")
            return
        if decision == "new":
            self.stats.restored_files += 1
        else:
            self.stats.replaced_files += 1
        self.stats.bytes_copied += est.st_size
        self.progress.files_done += 1
        home_rel = paths.home_relative(dpath)
        if home_rel and len(self._restored_settings) < 500 and any(
                fnmatch.fnmatchcase(home_rel, pattern) for pattern in postrestore.SETTINGS_FILES):
            self._restored_settings.append(home_rel)
        self._emit()

    def _restore_link(self, spath: str, dpath: str, est: os.stat_result) -> None:
        try:
            target = os.readlink(spath)
        except OSError as exc:
            self._issue(spath, "Não foi possível ler a ligação: " + fileops.describe_error(exc), "error")
            return
        if self.target == TARGET_ORIGINAL:
            # A link into the old home folder (/home/ana/...) follows the new one.
            target = postrestore.relink(target, self.old_home, paths.home(), self._link_pairs)
        d_st = _lstat(dpath)
        if d_st is not None and stat.S_ISLNK(d_st.st_mode):
            try:
                if os.readlink(dpath) == target:
                    return
            except OSError:
                pass
        if d_st is not None and self.conflict != CONFLICT_REPLACE:
            self.stats.kept_existing += 1
            return
        if self.dry:
            self.stats.links += 1
            return
        tmp = fileops.tmp_name(os.path.dirname(dpath))
        try:
            fileops.make_symlink(target, tmp, est)
            if d_st is not None:
                self._move_aside(dpath)
            os.replace(tmp, dpath)
        except (EntryError, OSError) as exc:
            _silent_unlink(tmp)
            self._issue(dpath, _entry_message(exc), "error")
            return
        self.stats.links += 1

    def _apply_desktop_settings(self) -> None:
        if not self.apply_settings or self.dconf_load is None or self.target != TARGET_ORIGINAL:
            return
        here = self.here_desktop
        if here is None:
            here = desktop.current_desktop(processes=system.running_process_names())
        pairs = desktop.path_pairs(self.old_home, paths.home(), old_xdg_dirs(self.meta),
                                   paths.xdg_user_dirs(existing_only=False))
        # The metadata describes the latest backup: an older export (from «Versões
        # anteriores») may come from another desktop, so there only the file itself counts.
        latest = os.path.basename(os.path.normpath(self.tree_root)) == CURRENT_DIR
        source_desktop = str(self.meta.get("ambiente") or "") if latest else ""
        for item in self.items:
            for rel in item.settings_exports:
                self._check_cancel()
                source = os.path.join(self.tree_root, rel)
                try:
                    with open(source, encoding="utf-8", errors="surrogateescape") as fh:
                        text = fh.read()
                except OSError as exc:
                    self._issue(source, "Não foi possível ler: " + fileops.describe_error(exc), "error")
                    continue
                plan = desktop.DesktopPlan(text, source_desktop, here, paths.home(), pairs)
                if not plan.text.strip():
                    self.notes.extend(plan.notes())
                    continue
                try:
                    os.makedirs(self.replaced_dir, exist_ok=True)
                except OSError as exc:
                    self._issue(self.replaced_dir, fileops.describe_error(exc), "error")
                    continue
                if self.dconf_dump is not None:
                    # Rule: what is replaced is kept. The current settings go aside first.
                    before = os.path.join(self.replaced_dir, "definicoes-antes-do-restauro.ini")
                    error = self.dconf_dump(before)
                    if error:
                        self._issue(before, "As definições não foram aplicadas porque não foi "
                                    "possível guardar as que estão em uso: " + error, "error")
                        continue
                    self._replaced_used = True
                applied = os.path.join(self.replaced_dir, "definicoes-aplicadas.ini")
                try:
                    with open(applied, "w", encoding="utf-8", errors="surrogateescape") as fh:
                        fh.write(plan.text)
                except OSError as exc:
                    self._issue(applied, fileops.describe_error(exc), "error")
                    continue
                self._replaced_used = True
                error = self.dconf_load(applied)
                if error:
                    self._issue(source, "Não foi possível aplicar as definições: " + error, "error")
                    continue
                self.settings_applied = True
                self.notes.extend(plan.notes())
                if plan.skipped:
                    self._settings_partial = True
                    self.notes.append("Termina a sessão e volta a entrar para veres as "
                                      "definições das apps.")
                else:
                    self.notes.append("Termina a sessão e volta a entrar para ver tudo como "
                                      "estava.")

    def _after_restore_notes(self) -> None:
        """Things the user should know or do after restoring on this computer."""
        home = paths.home()
        keys = {i.key for i in self.items if i.kind == "preset"}
        by_app: Dict[Tuple[str, str], List[str]] = {}
        for remap in self.remaps_detail():
            by_app.setdefault((remap.app, remap.target), []).append("~/" + remap.destination)
        for (app, kind), places in by_app.items():
            self.notes.append("O {} foi restaurado para a versão instalada aqui ({}): {}.".format(
                variants.app_label(app), variants.KIND_LABELS.get(kind, kind),
                ", ".join(places)))
        if "shell" in keys:
            for file, number, _line, problem in postrestore.shell_findings(home, self.old_home)[:8]:
                self.notes.append("{}, linha {}: {}.".format(file, number, problem))
        mentions = postrestore.old_home_mentions(home, self.old_home, self._restored_settings)
        if mentions:
            shown = ", ".join("~/" + m for m in mentions[:8])
            self.notes.append(fmt.agree(
                len(mentions),
                "Este ficheiro ainda menciona a pasta pessoal antiga ({}): {}. Corrige esse "
                "caminho se alguma coisa não funcionar.",
                "Estes ficheiros ainda mencionam a pasta pessoal antiga ({}): {}. Corrige esses "
                "caminhos se alguma coisa não funcionar.").format(self.old_home, shown))
        probe = variants.Probe(home)
        for app, label in (("firefox", "Firefox"), ("thunderbird", "Thunderbird")):
            if app not in keys:
                continue
            kinds = variants.installed_kinds(app, probe)
            if not kinds:
                continue
            installed = variants.installed_mozilla_version(app, kinds[0], probe)
            for base in self._mozilla_bases(app):
                for profile in variants.default_profiles(base):
                    warning = variants.downgrade_warning(label, installed,
                                                         variants.profile_last_version(profile))
                    if warning and warning not in self.notes:
                        self.notes.append(warning)
        here = self.here_desktop
        if here is None:
            here = desktop.current_desktop(processes=system.running_process_names())
        if "keyrings" in keys:
            self.notes.append("Se a palavra-passe com que entras no computador for diferente da "
                              "antiga, o sistema vai pedir a antiga para abrir o porta-chaves. "
                              "Depois podes mudá-la na app «Palavras-passe e chaves».")
        if here == desktop.KDE and keys & {"chrome", "chromium", "brave", "edge", "vivaldi", "opera",
                                           "vscode", "cursor", "windsurf"}:
            self.notes.append("No KDE, os navegadores e editores procuram as palavras-passe na "
                              "carteira do KDE. Para verem as que guardaste antes, instala o "
                              "gnome-keyring e abre-os com a opção --password-store=gnome-libsecret.")
        if any(r.app == "chromium" and r.target == variants.SNAP for r in self.remaps_detail()):
            self.notes.append("Para o Chromium (snap) ler as palavras-passe guardadas, corre no "
                              "terminal: sudo snap connect chromium:password-manager-service")

    def remaps_detail(self) -> List[variants.Remap]:
        return list(self.remaps.values())

    def _mozilla_bases(self, app: str) -> List[str]:
        bases = []
        for item in self.items:
            if item.key != app:
                continue
            for rel in item.rels:
                dest = self._dest.for_rel(rel)
                if not dest:
                    continue
                bases.append(os.path.join(dest, "firefox") if app == "firefox" else dest)
        return [b for b in bases if os.path.isdir(b)]


def _entry_message(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return fileops.describe_error(exc)
    return str(exc)


def _lstat(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def ownership_problem(computer_dir: str) -> Optional[int]:
    """The numeric owner of the copies when it is not the current user (and they are unreadable)."""
    try:
        st = os.stat(computer_dir)
    except OSError:
        return None
    if st.st_uid == os.getuid():
        return None
    return st.st_uid
