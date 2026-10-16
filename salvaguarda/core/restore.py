"""Restore: put the files of a backup back in their place (or in another folder).

Nothing on the computer is ever deleted. When a file already exists, the chosen
policy decides; with "replace", the file that was there is moved to
``~/.local/share/salvaguarda/substituidos/<data>/`` instead of being lost.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import fileops, fmt, fsinfo, paths
from .engine import (PHASE_COPY, PHASE_FINISH, PHASE_PREPARE, PHASE_SCAN, PROGRESS_INTERVAL,
                     STATUS_CANCELLED, STATUS_FAILED, STATUS_NOTHING, STATUS_OK, STATUS_WARNINGS,
                     Callbacks, Issue, Progress)
from .fileops import Cancelled, EntryError, Fatal
from .layout import EXTRAS_DIR, HOME_DIR, OTHER_DIR, TMP_PREFIX

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

    @property
    def is_settings_export(self) -> bool:
        return self.extra == "dconf"

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
            item.extra = str(raw.get("extra"))
        if rel not in item.rels:
            item.rels.append(rel)
            if raw.get("caminho") and not raw.get("extra"):
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
    if os.path.isdir(extras_dir) and not any(i.extra for i in result):
        for entry in sorted(os.scandir(extras_dir), key=lambda e: e.name):
            if entry.name.endswith(".ini"):
                result.append(RestoreItem(
                    "Definições do ambiente de trabalho", "preset", "desktop",
                    [os.path.join(EXTRAS_DIR, entry.name)], "preferences-desktop-appearance-symbolic",
                    extra="dconf",
                ))
                break
    return result + extra_items


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


def destination_for(rel: str, item: RestoreItem, target: str, folder: str,
                    xdg_now: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Where ``rel`` (one of ``item.rels``) goes on this computer."""
    parts = rel.split(os.sep)
    top, rest = parts[0], parts[1:]
    if top == HOME_DIR:
        if target == TARGET_FOLDER:
            return os.path.join(folder, *rest) if rest else folder
        if item.xdg_key and xdg_now and item.xdg_key in xdg_now and len(rest) == 1:
            return xdg_now[item.xdg_key]
        return os.path.join(paths.home(), *rest) if rest else paths.home()
    if top == OTHER_DIR:
        if target == TARGET_FOLDER:
            return os.path.join(folder, *rest) if rest else folder
        return os.sep + os.path.join(*rest) if rest else None
    return None


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
        self._last_emit = 0.0
        self._speed_t0 = 0.0
        self._speed_b0 = 0
        self._xdg = paths.xdg_user_dirs()
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
            changed = self.stats.files_to_copy or self.stats.links
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
            if item.is_settings_export:
                continue
            for rel in item.rels:
                self._check_cancel()
                src = os.path.join(self.tree_root, rel)
                dst = destination_for(rel, item, self.target, self.folder, self._xdg)
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

    def _move_aside(self, dpath: str) -> None:
        rel = paths.home_relative(dpath)
        if rel is None:
            rel = os.path.join(OTHER_DIR, dpath.lstrip(os.sep))
        target = os.path.join(self.replaced_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):
            target += ".{}".format(int(time.time()))
        shutil.move(dpath, target)
        self._replaced_used = True

    def _restore_dir(self, spath: str, dpath: str, sst: os.stat_result) -> None:
        self._check_cancel()
        d_st = _lstat(dpath)
        created = False
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
        self._emit()

    def _restore_link(self, spath: str, dpath: str, est: os.stat_result) -> None:
        try:
            target = os.readlink(spath)
        except OSError as exc:
            self._issue(spath, "Não foi possível ler a ligação: " + fileops.describe_error(exc), "error")
            return
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
        if not self.apply_settings or self.dconf_load is None:
            return
        for item in self.items:
            if not item.is_settings_export:
                continue
            for rel in item.rels:
                source = os.path.join(self.tree_root, rel)
                if not os.path.isfile(source):
                    continue
                error = self.dconf_load(source)
                if error:
                    self._issue(source, "Não foi possível aplicar as definições: " + error, "error")
                else:
                    self.notes.append("As definições do ambiente de trabalho foram aplicadas. "
                                      "Termina a sessão e volta a entrar para ver tudo como estava.")


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
