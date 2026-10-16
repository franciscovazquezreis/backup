"""Backup engine: keeps an exact mirror and moves what changes to dated version folders.

Each run makes two passes over the same comparison logic. The first one only
counts (what is new, changed or gone, and how many bytes must be copied) so the
interface can show real progress, check free space and warn about unexpected mass
deletions before anything is touched. The second pass does the work.

Guarantees:

* Files are written to a temporary name and renamed into place, so the mirror
  never holds a half-written file, even if the disk is unplugged mid-copy.
* Nothing is deleted from the mirror without first being moved to
  "Versões anteriores" (except, by setting, large files of app settings).
* A source that is missing or unreadable never empties its copy.
* Interrupted runs are harmless: the next run simply carries on.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import fcntl
import os
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import fileops, fmt, fsinfo, paths
from .exclusions import CATEGORY_LABELS, Exclusions
from .fileops import Cancelled, DestinationLost, DiskFull, EntryError, Fatal
from .layout import CURRENT_DIR, HOME_DIR, OTHER_DIR, TMP_PREFIX, DiskLayout, list_version_runs
from .selection import Resolution, ResolvedSource

PHASE_PREPARE = "prepare"
PHASE_SCAN = "scan"
PHASE_COPY = "copy"
PHASE_FINISH = "finish"

STATUS_OK = "ok"
STATUS_WARNINGS = "warnings"
STATUS_NOTHING = "nothing"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

MAX_DEPTH = 400
PROGRESS_INTERVAL = 0.12


@dataclass
class Progress:
    phase: str = PHASE_PREPARE
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    scanned: int = 0
    current: str = ""
    speed: float = 0.0
    eta: Optional[float] = None

    @property
    def fraction(self) -> float:
        if self.bytes_total > 0:
            return min(1.0, self.bytes_done / self.bytes_total)
        if self.files_total > 0:
            return min(1.0, self.files_done / self.files_total)
        return 0.0


@dataclass
class Issue:
    path: str
    message: str
    level: str = "warning"  # "warning" or "error"


@dataclass
class Stats:
    new_files: int = 0
    changed_files: int = 0
    links: int = 0
    dirs_created: int = 0
    bytes_copied: int = 0
    archived: int = 0  # entries moved to "Versões anteriores"
    archived_files: int = 0  # files inside those entries
    dropped: int = 0  # replaced or removed without keeping a version
    removed_files: int = 0  # files that disappeared from the computer
    orphans: int = 0  # entries of items no longer selected
    orphan_files: int = 0  # files inside those entries
    newly_excluded: int = 0  # entries that an exclusion rule now leaves out
    meta_updates: int = 0
    unchanged_files: int = 0
    source_files: int = 0
    source_bytes: int = 0
    mirror_files: int = 0
    special_skipped: int = 0
    excluded: Dict[str, int] = field(default_factory=dict)
    excluded_categories: Dict[str, int] = field(default_factory=dict)

    @property
    def files_to_copy(self) -> int:
        return self.new_files + self.changed_files

    @property
    def anything_to_do(self) -> bool:
        return bool(self.new_files or self.changed_files or self.links or self.archived
                    or self.dropped or self.orphans or self.dirs_created or self.meta_updates)


@dataclass
class Question:
    kind: str
    title: str
    text: str
    options: List[Tuple[str, str]]  # (answer id, button label); first is the safe choice
    details: List[str] = field(default_factory=list)
    data: Dict[str, object] = field(default_factory=dict)


@dataclass
class RunResult:
    status: str
    message: str
    started: _dt.datetime
    finished: _dt.datetime
    stats: Stats
    issues: List[Issue]
    notes: List[str]
    version_dir: Optional[str] = None
    log_path: Optional[str] = None
    pruned: List[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return (self.finished - self.started).total_seconds()

    def summary_dict(self) -> Dict[str, object]:
        return {
            "resultado": self.status,
            "mensagem": self.message,
            "inicio": fmt.iso(self.started),
            "fim": fmt.iso(self.finished),
            "estatisticas": asdict(self.stats),
            "avisos": len([i for i in self.issues if i.level == "warning"]),
            "erros": len([i for i in self.issues if i.level == "error"]),
            "registo": self.log_path,
            "versoes": self.version_dir,
        }


class Callbacks:
    """Hooks used by the interface. All are called from the worker thread."""

    def phase(self, phase: str, text: str) -> None:
        pass

    def progress(self, progress: Progress) -> None:
        pass

    def ask(self, question: Question) -> str:
        """Return one of the option ids. The default answers with the safe option."""
        return question.options[0][0]


class _Pass:
    """Mutable state of one pass (counting or copying)."""

    def __init__(self, dry: bool):
        self.dry = dry
        self.stats = Stats()


class BackupEngine:
    def __init__(
        self,
        layout: DiskLayout,
        resolution: Resolution,
        exclusions: Exclusions,
        *,
        keep_versions_days: int = 180,
        prune_when_full: bool = True,
        warn_open_apps: bool = True,
        running_processes: Optional[Callable[[], Set[str]]] = None,
        dconf_dump: Optional[Callable[[str], Optional[str]]] = None,
        meta_extra: Optional[Dict[str, object]] = None,
        callbacks: Optional[Callbacks] = None,
        cancel_event: Optional[threading.Event] = None,
        now: Optional[Callable[[], _dt.datetime]] = None,
        mass_change_check: bool = True,
    ):
        self.layout = layout
        self.res = resolution
        self.excl = exclusions
        self.keep_versions_days = keep_versions_days
        self.prune_when_full = prune_when_full
        self.warn_open_apps = warn_open_apps
        self.running_processes = running_processes
        self.dconf_dump = dconf_dump
        self.meta_extra = meta_extra or {}
        self.cb = callbacks or Callbacks()
        self.cancel_event = cancel_event or threading.Event()
        self._now = now or _dt.datetime.now
        self.mass_change_check = mass_change_check

        self.started = self._now()
        self.issues: List[Issue] = []
        self.notes: List[str] = []
        self.version_dir: Optional[str] = None
        self.pruned: List[str] = []
        self.tolerance_ns = 1
        self.progress = Progress()
        self._pass = _Pass(dry=True)
        self._plan: Optional[Stats] = None
        self._last_emit = 0.0
        self._speed_t0 = 0.0
        self._speed_b0 = 0
        self._lock_fd = -1
        self._keep: Set[str] = set()
        self._keep_ancestors: Set[str] = set()
        self._root_mirrors: Set[str] = set()
        self._reported_mounts: Set[str] = set()
        self._home = paths.home()
        self._was_interrupted = False
        self._dest_ids: Set[Tuple[int, int]] = set()

    # ------------------------------------------------------------------ helpers
    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def _issue(self, path: str, message: str, level: str = "warning") -> None:
        if len(self.issues) < 5000:
            self.issues.append(Issue(paths.display_path(path), message, level))

    def _set_phase(self, phase: str, text: str) -> None:
        self.progress.phase = phase
        self.cb.phase(phase, text)
        self._emit(force=True)

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

    def _on_bytes(self, n: int) -> None:
        self.progress.bytes_done += n
        self._emit()

    def _scanned(self, path: str) -> None:
        self.progress.scanned += 1
        if self._pass.dry:
            self.progress.current = path
        self._emit()

    def _current(self, path: str) -> None:
        self.progress.current = path

    def _count_excluded(self, hit: Tuple[str, str]) -> None:
        category, label = hit
        stats = self._pass.stats
        stats.excluded[label] = stats.excluded.get(label, 0) + 1
        stats.excluded_categories[category] = stats.excluded_categories.get(category, 0) + 1

    def _rel_current(self, mirror_path: str) -> str:
        return os.path.relpath(mirror_path, self.layout.current_dir)

    def _source_for_mirror(self, mirror_path: str) -> Optional[str]:
        rel = self._rel_current(mirror_path)
        parts = rel.split(os.sep)
        if parts[0] == HOME_DIR:
            return os.path.join(self._home, *parts[1:]) if len(parts) > 1 else self._home
        if parts[0] == OTHER_DIR:
            return os.sep + os.path.join(*parts[1:]) if len(parts) > 1 else os.sep
        return None

    def _is_kept(self, mirror_path: str) -> bool:
        return mirror_path in self._keep or mirror_path in self._keep_ancestors

    def _protect(self, mirror_path: str) -> None:
        self._keep.add(mirror_path)
        parent = os.path.dirname(mirror_path)
        while parent.startswith(self.layout.current_dir) and parent not in self._keep_ancestors:
            self._keep_ancestors.add(parent)
            if parent == self.layout.current_dir:
                break
            parent = os.path.dirname(parent)

    # --------------------------------------------------------------- public API
    def run(self) -> RunResult:
        status, message = STATUS_FAILED, ""
        try:
            self._prepare()
            self._set_phase(PHASE_SCAN, "A analisar o que mudou…")
            plan = self._run_pass(dry=True)
            self._plan = plan
            if not plan.anything_to_do:
                status = STATUS_WARNINGS if self.issues else STATUS_NOTHING
                message = "Já estava tudo copiado. Nada mudou desde a última cópia."
                if self.issues:
                    message += " Há {} a rever.".format(
                        fmt.plural(len(self.issues), "aviso", "avisos"))
                self._set_phase(PHASE_FINISH, "A concluir…")
                self._finish(plan, status)
            else:
                self._confirm_plan(plan)
                self._ensure_space(plan)
                self.progress.files_total = plan.files_to_copy
                self.progress.bytes_total = plan.bytes_copied
                self.progress.files_done = 0
                self.progress.bytes_done = 0
                self._speed_t0, self._speed_b0 = time.monotonic(), 0
                self._set_phase(PHASE_COPY, "A copiar…")
                stats = self._run_pass(dry=False)
                self._set_phase(PHASE_FINISH, "A concluir…")
                status = STATUS_WARNINGS if self.issues else STATUS_OK
                self._finish(stats, status)
                message = self._final_message(stats, status)
        except Cancelled:
            status, message = STATUS_CANCELLED, "A cópia foi cancelada. O que já foi copiado fica guardado."
        except Fatal as exc:
            status, message = STATUS_FAILED, str(exc)
        except OSError as exc:
            status = STATUS_FAILED
            message = "Erro inesperado: {}".format(fileops.describe_error(exc))
        finally:
            stats = self._pass.stats
            finished = self._now()
            result = RunResult(status, message, self.started, finished, stats, self.issues,
                               self.notes, self.version_dir, None, self.pruned)
            if self._lock_fd >= 0:
                result.log_path = self._write_log(result)
            self._unlock()
        return result

    # ------------------------------------------------------------------ prepare
    def _prepare(self) -> None:
        self._set_phase(PHASE_PREPARE, "A preparar…")
        root = self.layout.root
        if not os.path.isdir(root):
            raise DestinationLost("O disco de cópias não está ligado ou a pasta «{}» não existe."
                                  .format(paths.display_path(root)))
        try:
            root_st = os.stat(root)
            self._dest_ids.add((root_st.st_dev, root_st.st_ino))
        except OSError:
            pass
        mount = fsinfo.mount_for(root)
        if mount is not None and mount.read_only:
            raise Fatal("O disco de cópias está montado só para leitura. Desliga-o, volta a "
                        "ligá-lo e, se o problema continuar, verifica o disco com a app Discos.")
        try:
            os.makedirs(self.layout.current_dir, exist_ok=True)
        except OSError as exc:
            if isinstance(exc, PermissionError):
                raise Fatal("Não há permissão para escrever no disco de cópias.") from exc
            fileops.raise_for_dest(exc, "Não foi possível preparar o disco")
        self._lock()
        self.tolerance_ns = fsinfo.time_granularity_ns(self.layout.computer_dir)

        meta = self.layout.read_meta()
        current_mid = paths.machine_id()
        disk_mid = str(meta.get("machine_id") or "")
        if disk_mid and current_mid and disk_mid != current_mid:
            when = fmt.parse_iso(str(meta.get("atualizado") or ""))
            system = str(meta.get("sistema") or "outra instalação")
            question = Question(
                "adopt",
                "Estas cópias foram feitas noutra instalação",
                "A pasta «{}» no disco tem cópias de {}{}. É este computador depois de "
                "reinstalar o sistema? Se sim, restaura primeiro o que precisas e depois "
                "continua a usar esta pasta. Se for outro computador, dá-lhe outro nome "
                "para não misturar as cópias.".format(
                    self.layout.computer, system,
                    ", a última de {}".format(fmt.datetime_short(when)) if when else ""),
                [("cancel", "Cancelar"), ("rename", "É outro computador"),
                 ("adopt", "É este computador, continuar")],
            )
            if self.cb.ask(question) != "adopt":
                raise Cancelled()

        status = self.layout.read_status()
        self._was_interrupted = bool(status.get("em_curso"))
        if self._was_interrupted:
            self.notes.append("A cópia anterior foi interrompida antes do fim; esta completa-a.")
        status["em_curso"] = True
        status["ultima_execucao"] = fmt.iso(self.started)
        try:
            self.layout.write_status(status)
        except OSError as exc:
            fileops.raise_for_dest(exc, "Não foi possível escrever no disco")

        if self.warn_open_apps and self.running_processes is not None:
            running = self.running_processes()
            open_apps = []
            for src in self.res.sources:
                if src.processes and running.intersection(src.processes):
                    if src.label not in open_apps:
                        open_apps.append(src.label)
            if open_apps:
                question = Question(
                    "open_apps",
                    "Há apps abertas",
                    "Para que os dados destas apps fiquem coerentes na cópia, fecha-as antes "
                    "de continuar. Se continuares com elas abertas, a cópia faz-se na mesma, "
                    "mas o histórico ou as mensagens mais recentes podem ficar de fora.",
                    [("cancel", "Cancelar"), ("continue", "Continuar mesmo assim")],
                    details=open_apps,
                )
                if self.cb.ask(question) != "continue":
                    raise Cancelled()

        if self.res.wants_dconf and self.dconf_dump is not None:
            target = next((s.path for s in self.res.sources if s.extra == "dconf"), None)
            if target:
                error = self.dconf_dump(target)
                if error:
                    self._issue(target, "Definições do ambiente de trabalho não exportadas: " + error)

        self._root_mirrors = {self.layout.mirror_abs(s.mirror_rel) for s in self.res.sources}
        for rel in self.res.keep:
            self._protect(self.layout.mirror_abs(rel))
        for src in self.res.missing:
            if src.kind != "preset":
                self._issue(src.path, "Não encontrado no computador. A cópia que existe no "
                                      "disco foi mantida.")

    def _lock(self) -> None:
        try:
            fd = os.open(self.layout.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError as exc:
            fileops.raise_for_dest(exc, "Não foi possível escrever no disco")
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise Fatal("Já está a decorrer outra cópia para este disco.")
        self._lock_fd = fd

    def _unlock(self) -> None:
        if self._lock_fd >= 0:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = -1

    # -------------------------------------------------------------- decisions
    def _confirm_plan(self, plan: Stats) -> None:
        if not self.mass_change_check:
            return
        if plan.orphan_files >= 200:
            question = Question(
                "orphans", "Retiraste itens da lista do que copiar",
                "A cópia desses itens ({}) sai da «Cópia atual» e passa para «Versões "
                "anteriores», onde fica até ao fim do prazo definido. Se não querias "
                "retirá-los, cancela e volta a ativá-los em «Alterar…».".format(
                    fmt.files(plan.orphan_files)),
                [("cancel", "Cancelar"), ("continue", "Continuar")],
            )
            if self.cb.ask(question) != "continue":
                raise Cancelled()
        if plan.mirror_files < 100:
            return
        removed_ratio = plan.removed_files / float(plan.mirror_files)
        changed_ratio = plan.changed_files / float(plan.mirror_files)
        if removed_ratio < 0.30 and (changed_ratio < 0.50 or plan.changed_files < 500):
            return
        if removed_ratio >= 0.30:
            text = ("{} ({} % do que já estava copiado) desapareceram do computador desde a "
                    "última cópia. Se continuares, saem da cópia atual e passam para "
                    "«Versões anteriores», de onde podem ser recuperados.").format(
                        fmt.files(plan.removed_files), int(round(removed_ratio * 100)))
        else:
            text = ("{} ({} % do que já estava copiado) foram alterados desde a última cópia. "
                    "Se não esperavas tantas alterações, cancela e verifica os teus "
                    "ficheiros antes de continuar.").format(
                        fmt.files(plan.changed_files), int(round(changed_ratio * 100)))
        question = Question(
            "mass_changes", "Muitas alterações desde a última cópia", text,
            [("cancel", "Cancelar"), ("continue", "Continuar")],
        )
        if self.cb.ask(question) != "continue":
            raise Cancelled()

    def _ensure_space(self, plan: Stats) -> None:
        needed = plan.bytes_copied + max(50 * 1000 * 1000, plan.bytes_copied // 100)
        usage = fsinfo.usage(self.layout.computer_dir)
        if usage is None or usage.free >= needed:
            return
        if self.prune_when_full:
            for run in list_version_runs(self.layout):
                if usage.free >= needed:
                    break
                self._check_cancel()
                self.cb.phase(PHASE_PREPARE, "A libertar espaço (versões antigas)…")
                try:
                    fileops.force_remove(run.path)
                    self.pruned.append(run.name)
                except OSError as exc:
                    self._issue(run.path, "Não foi possível apagar: " + fileops.describe_error(exc))
                    break
                usage = fsinfo.usage(self.layout.computer_dir) or usage
            if self.pruned:
                self.notes.append("Para libertar espaço foram apagadas as versões anteriores de: "
                                  + ", ".join(self.pruned) + ".")
        if usage.free < needed:
            raise DiskFull(
                "Não há espaço suficiente no disco de cópias: são precisos {} e há {} livres. "
                "Liberta espaço no disco, usa um disco maior ou retira itens da lista."
                .format(fmt.size(needed), fmt.size(usage.free)))

    # -------------------------------------------------------------------- pass
    def _run_pass(self, dry: bool) -> Stats:
        self._pass = _Pass(dry)
        for src in self.res.sources:
            self._check_cancel()
            self._process_root(src)
        self._process_orphans()
        return self._pass.stats

    def _process_root(self, src: ResolvedSource) -> None:
        dst = self.layout.mirror_abs(src.mirror_rel)
        if src.shallow_files:
            self._sync_loose(src, dst)
            return
        try:
            st = os.stat(src.path)  # a root that is a symlink is followed on purpose
        except FileNotFoundError:
            self._protect(dst)
            # A generated file that could not be produced was already reported.
            if not self._pass.dry and not src.extra:
                self._issue(src.path, "Deixou de existir durante a cópia; a cópia anterior foi mantida.")
            return
        except OSError as exc:
            self._protect(dst)
            if self._pass.dry:
                self._issue(src.path, "Não foi possível ler: " + fileops.describe_error(exc), "error")
            return
        d_st = _lstat(dst)
        if stat.S_ISDIR(st.st_mode):
            try:
                entries = _list_dir(src.path)
            except OSError as exc:
                self._protect(dst)
                if self._pass.dry:
                    self._issue(src.path, "Não foi possível ler a pasta: "
                                + fileops.describe_error(exc), "error")
                return
            if d_st is not None and not stat.S_ISDIR(d_st.st_mode):
                self._archive(dst, d_st, src)
                d_st = None
            self._ensure_parents(dst)
            self._sync_dir(src.path, dst, st, entries, src, st.st_dev,
                           paths.home_relative(src.path), d_st is not None, 0)
        elif stat.S_ISREG(st.st_mode):
            if d_st is not None and stat.S_ISDIR(d_st.st_mode):
                self._archive(dst, d_st, src)
                d_st = None
            self._ensure_parents(dst)
            self._scanned(src.path)
            self._sync_file(src.path, dst, st, d_st, src)
        else:
            self._protect(dst)
            if self._pass.dry:
                self._issue(src.path, "Tipo de ficheiro não suportado (só ficheiros e pastas).")

    def _ensure_parents(self, dst: str) -> None:
        if self._pass.dry:
            return
        missing = []
        parent = os.path.dirname(dst)
        while parent != self.layout.current_dir and not os.path.isdir(parent):
            missing.append(parent)
            parent = os.path.dirname(parent)
        for folder in reversed(missing):
            mode = 0o755
            source = self._source_for_mirror(folder)
            if source:
                try:
                    mode = stat.S_IMODE(os.stat(source).st_mode) | 0o700
                except OSError:
                    pass
            try:
                os.mkdir(folder, mode)
                os.chmod(folder, mode)
            except FileExistsError:
                pass
            except OSError as exc:
                fileops.raise_for_dest(exc, "Não foi possível criar a pasta")

    def _sync_dir(self, spath: str, dpath: str, sst: os.stat_result, entries: List[os.DirEntry],
                  src: ResolvedSource, root_dev: int, home_rel: Optional[str],
                  d_exists: bool, depth: int) -> None:
        p = self._pass
        stats = p.stats
        if depth > MAX_DEPTH:
            self._protect(dpath)
            if p.dry:
                self._issue(spath, "Pasta com demasiados níveis; foi ignorada.")
            return
        if not d_exists:
            stats.dirs_created += 1
            if not p.dry:
                try:
                    os.mkdir(dpath, 0o700)
                except FileExistsError:
                    d_exists = True
                except OSError as exc:
                    try:
                        fileops.raise_for_dest(exc, "Não foi possível criar a pasta")
                    except EntryError as err:
                        self._issue(spath, str(err), "error")
                        self._protect(dpath)
                        return
        if d_exists and not p.dry:
            fileops.ensure_writable_dir(dpath)
        dst_entries: Dict[str, os.DirEntry] = {}
        if d_exists:
            try:
                for entry in _list_dir(dpath):
                    dst_entries[entry.name] = entry
            except OSError as exc:
                if not p.dry:
                    fileops.raise_for_dest(exc, "Não foi possível ler a cópia")
                self._issue(dpath, "Não foi possível ler a cópia: " + fileops.describe_error(exc), "error")
                self._protect(dpath)
                return
            if p.dry:
                for d in dst_entries.values():
                    try:
                        if d.is_file(follow_symlinks=False):
                            stats.mirror_files += 1
                    except OSError:
                        pass

        excluded_names: Set[str] = set()
        for entry in entries:
            self._check_cancel()
            name = entry.name
            child_src = entry.path
            child_dst = os.path.join(dpath, name)
            child_rel = None if home_rel is None else (name if home_rel == "" else home_rel + "/" + name)
            self._scanned(child_src)
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError as exc:
                dst_entries.pop(name, None)
                self._protect(child_dst)
                if p.dry:
                    self._issue(child_src, "Não foi possível ler: " + fileops.describe_error(exc), "error")
                continue
            mode = est.st_mode
            is_dir = stat.S_ISDIR(mode)
            hit = self.excl.match(name, child_src, child_rel, is_dir)
            if hit is not None:
                self._count_excluded(hit)
                excluded_names.add(name)
                continue
            if is_dir:
                if (est.st_dev, est.st_ino) in self._dest_ids:
                    dst_entries.pop(name, None)
                    continue
                if est.st_dev != root_dev:
                    dst_entries.pop(name, None)
                    self._protect(child_dst)
                    if p.dry and child_src not in self._reported_mounts:
                        self._reported_mounts.add(child_src)
                        self.notes.append(
                            "«{}» é outro disco ou partição e não foi incluído. Se quiseres "
                            "copiá-lo, adiciona-o como pasta própria.".format(paths.display_path(child_src)))
                    continue
                try:
                    sub = _list_dir(child_src)
                except OSError as exc:
                    dst_entries.pop(name, None)
                    self._protect(child_dst)
                    if p.dry:
                        self._issue(child_src, "Não foi possível ler a pasta: "
                                    + fileops.describe_error(exc), "error")
                    continue
                marker = self.excl.match_listing(child_src, {e.name for e in sub})
                if marker is not None:
                    self._count_excluded(marker)
                    excluded_names.add(name)
                    continue
                d = dst_entries.pop(name, None)
                d_st = _entry_lstat(d)
                if d_st is not None and not stat.S_ISDIR(d_st.st_mode):
                    self._archive(child_dst, d_st, src)
                    d_st = None
                self._sync_dir(child_src, child_dst, est, sub, src, root_dev, child_rel,
                               d_st is not None, depth + 1)
            elif stat.S_ISREG(mode):
                d = dst_entries.pop(name, None)
                d_st = _entry_lstat(d)
                if d_st is not None and stat.S_ISDIR(d_st.st_mode):
                    self._archive(child_dst, d_st, src)
                    d_st = None
                self._sync_file(child_src, child_dst, est, d_st, src)
            elif stat.S_ISLNK(mode):
                d = dst_entries.pop(name, None)
                d_st = _entry_lstat(d)
                if d_st is not None and stat.S_ISDIR(d_st.st_mode):
                    self._archive(child_dst, d_st, src)
                    d_st = None
                self._sync_link(child_src, child_dst, est, d_st, src)
            else:
                # Sockets, pipes and devices cannot be copied and do not need to be.
                dst_entries.pop(name, None)
                stats.special_skipped += 1

        for name in sorted(dst_entries):
            self._check_cancel()
            child_dst = os.path.join(dpath, name)
            if name.startswith(TMP_PREFIX):
                if not p.dry:
                    try:
                        fileops.force_remove(child_dst)
                    except OSError:
                        pass
                continue
            if self._is_kept(child_dst):
                continue
            d_st = _entry_lstat(dst_entries[name])
            if d_st is None:
                continue
            if name in excluded_names:
                stats.newly_excluded += 1
                self._archive(child_dst, d_st, src)
            else:
                self._archive(child_dst, d_st, src, removed=True)

        if not p.dry:
            self._apply_dir_meta(dpath, sst)
        elif d_exists:
            d_now = _lstat(dpath)
            if d_now is not None and (
                    stat.S_IMODE(d_now.st_mode) != stat.S_IMODE(sst.st_mode)
                    or abs(d_now.st_mtime_ns - sst.st_mtime_ns) >= self.tolerance_ns):
                stats.meta_updates += 1

    def _sync_loose(self, src: ResolvedSource, dst: str) -> None:
        """Loose files directly in the home folder (folders and dotfiles are handled elsewhere)."""
        p = self._pass
        try:
            entries = [e for e in _list_dir(src.path) if not e.name.startswith(".")]
        except OSError as exc:
            self._protect(dst)
            if p.dry:
                self._issue(src.path, "Não foi possível ler a pasta pessoal: "
                            + fileops.describe_error(exc), "error")
            return
        d_st = _lstat(dst)
        if d_st is None:
            self._ensure_parents(dst)
            if not p.dry:
                try:
                    mode = stat.S_IMODE(os.stat(src.path).st_mode) | 0o700
                    os.mkdir(dst, mode)
                    os.chmod(dst, mode)
                except FileExistsError:
                    pass
                except OSError as exc:
                    fileops.raise_for_dest(exc, "Não foi possível criar a pasta")
        dst_entries: Dict[str, os.DirEntry] = {}
        if d_st is not None:
            try:
                for entry in _list_dir(dst):
                    if entry.name.startswith(".") and not entry.name.startswith(TMP_PREFIX):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.path in self._root_mirrors:
                        continue
                    dst_entries[entry.name] = entry
            except OSError as exc:
                fileops.raise_for_dest(exc, "Não foi possível ler a cópia")
        for entry in entries:
            self._check_cancel()
            child_dst = os.path.join(dst, entry.name)
            if child_dst in self._root_mirrors:
                continue
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError:
                dst_entries.pop(entry.name, None)
                continue
            if stat.S_ISDIR(est.st_mode):
                continue
            self._scanned(entry.path)
            hit = self.excl.match(entry.name, entry.path, entry.name, False)
            if hit is not None:
                self._count_excluded(hit)
                continue
            d = dst_entries.pop(entry.name, None)
            d_st_child = _entry_lstat(d)
            if p.dry and d_st_child is not None and stat.S_ISREG(d_st_child.st_mode):
                p.stats.mirror_files += 1
            if stat.S_ISREG(est.st_mode):
                self._sync_file(entry.path, child_dst, est, d_st_child, src)
            elif stat.S_ISLNK(est.st_mode):
                self._sync_link(entry.path, child_dst, est, d_st_child, src)
        for name in sorted(dst_entries):
            child_dst = os.path.join(dst, name)
            if name.startswith(TMP_PREFIX):
                if not p.dry:
                    try:
                        os.unlink(child_dst)
                    except OSError:
                        pass
                continue
            if self._is_kept(child_dst):
                continue
            d_st_child = _entry_lstat(dst_entries[name])
            if d_st_child is not None:
                if p.dry and stat.S_ISREG(d_st_child.st_mode):
                    p.stats.mirror_files += 1
                self._archive(child_dst, d_st_child, src, removed=True)

    def _sync_file(self, spath: str, dpath: str, est: os.stat_result,
                   d_st: Optional[os.stat_result], src: ResolvedSource) -> None:
        p = self._pass
        stats = p.stats
        stats.source_files += 1
        stats.source_bytes += est.st_size
        if d_st is not None and stat.S_ISREG(d_st.st_mode):
            if (d_st.st_size == est.st_size
                    and abs(d_st.st_mtime_ns - est.st_mtime_ns) < self.tolerance_ns):
                stats.unchanged_files += 1
                if stat.S_IMODE(d_st.st_mode) != stat.S_IMODE(est.st_mode):
                    stats.meta_updates += 1
                    if not p.dry:
                        try:
                            os.chmod(dpath, stat.S_IMODE(est.st_mode))
                        except OSError:
                            pass
                return
        if p.dry:
            if d_st is None:
                stats.new_files += 1
            else:
                stats.changed_files += 1
                self._count_retire(d_st, src)
            stats.bytes_copied += est.st_size
            return

        self._current(spath)
        tmp = fileops.tmp_name(os.path.dirname(dpath))
        warning = None
        try:
            used = fileops.copy_file(spath, tmp, self._on_bytes, self._check_cancel)
        except fileops.ChangedWhileCopying as changed:
            used = changed.st
            warning = "Estava a ser alterado durante a cópia; volta a ser copiado na próxima vez."
        except EntryError as exc:
            self._issue(spath, str(exc), "error")
            return
        except (Cancelled, Fatal):
            raise
        except OSError as exc:
            self._issue(spath, fileops.describe_error(exc), "error")
            _silent_unlink(tmp)
            return
        try:
            self._install(tmp, dpath, d_st, src)
        except EntryError as exc:
            _silent_unlink(tmp)
            self._issue(spath, str(exc), "error")
            return
        except BaseException:
            _silent_unlink(tmp)
            raise
        if warning:
            self._issue(spath, warning)
            # Force a fresh copy next time: the stored date no longer matches.
            try:
                os.utime(dpath, ns=(used.st_atime_ns, used.st_mtime_ns - 2_000_000_000))
            except OSError:
                pass
        if d_st is None:
            stats.new_files += 1
        else:
            stats.changed_files += 1
        stats.bytes_copied += used.st_size
        self.progress.files_done += 1
        self._emit()

    def _sync_link(self, spath: str, dpath: str, est: os.stat_result,
                   d_st: Optional[os.stat_result], src: ResolvedSource) -> None:
        p = self._pass
        try:
            target = os.readlink(spath)
        except OSError as exc:
            if p.dry:
                self._issue(spath, "Não foi possível ler a ligação: " + fileops.describe_error(exc), "error")
            self._protect(dpath)
            return
        if d_st is not None and stat.S_ISLNK(d_st.st_mode):
            try:
                if os.readlink(dpath) == target:
                    return
            except OSError:
                pass
        if p.dry:
            p.stats.links += 1
            if d_st is not None:
                self._count_retire(d_st, src)
            return
        tmp = fileops.tmp_name(os.path.dirname(dpath))
        try:
            fileops.make_symlink(target, tmp, est)
            self._install(tmp, dpath, d_st, src)
        except EntryError as exc:
            _silent_unlink(tmp)
            self._issue(spath, str(exc), "error")
            return
        except BaseException:
            _silent_unlink(tmp)
            raise
        p.stats.links += 1

    # ------------------------------------------------------------- versions
    def _keeps_version(self, d_st: os.stat_result, src: Optional[ResolvedSource]) -> bool:
        if src is None or src.version_max_size is None:
            return True
        if stat.S_ISREG(d_st.st_mode):
            return d_st.st_size <= src.version_max_size
        return True

    def _count_retire(self, d_st: os.stat_result, src: Optional[ResolvedSource]) -> None:
        stats = self._pass.stats
        if self._keeps_version(d_st, src):
            stats.archived += 1
            stats.archived_files += 1 if not stat.S_ISDIR(d_st.st_mode) else 0
        else:
            stats.dropped += 1

    def _version_path(self, mirror_path: str) -> str:
        if self.version_dir is None:
            self.version_dir = self.layout.new_version_dir(self.started)
        return os.path.join(self.version_dir, self._rel_current(mirror_path))

    def _install(self, tmp: str, dpath: str, d_st: Optional[os.stat_result],
                 src: Optional[ResolvedSource]) -> None:
        """Put ``tmp`` in place of ``dpath``, saving the previous version when there is one."""
        if d_st is None:
            try:
                os.rename(tmp, dpath)
            except OSError as exc:
                fileops.raise_for_dest(exc, "Não foi possível guardar o ficheiro")
            return
        if self._keeps_version(d_st, src):
            target = self._version_path(dpath)
            if stat.S_ISDIR(d_st.st_mode):
                fileops.move_aside(dpath, target)
            else:
                try:
                    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                    os.link(dpath, target, follow_symlinks=False)
                except OSError:
                    fileops.move_aside(dpath, target)
            self._pass.stats.archived += 1
            if not stat.S_ISDIR(d_st.st_mode):
                self._pass.stats.archived_files += 1
        else:
            if stat.S_ISDIR(d_st.st_mode):
                fileops.force_remove(dpath)
            self._pass.stats.dropped += 1
        try:
            os.replace(tmp, dpath)
        except OSError as exc:
            fileops.raise_for_dest(exc, "Não foi possível guardar o ficheiro")

    def _archive(self, mirror_path: str, d_st: os.stat_result, src: Optional[ResolvedSource],
                 removed: bool = False, orphan: bool = False) -> None:
        """Take an entry out of the mirror (moving it to the version folder when kept)."""
        p = self._pass
        stats = p.stats
        is_dir = stat.S_ISDIR(d_st.st_mode)
        files = fileops.tree_stats(mirror_path)[0] if is_dir else 1
        keep = self._keeps_version(d_st, src)
        if orphan:
            stats.orphans += 1
            stats.orphan_files += files
        if removed:
            stats.removed_files += files
            if p.dry and is_dir:
                stats.mirror_files += files
        if keep:
            stats.archived += 1
            stats.archived_files += files
        else:
            stats.dropped += 1
        if p.dry:
            return
        self._check_cancel()
        if keep:
            fileops.move_aside(mirror_path, self._version_path(mirror_path))
        else:
            try:
                fileops.force_remove(mirror_path)
            except OSError as exc:
                fileops.raise_for_dest(exc, "Não foi possível apagar da cópia")

    def _process_orphans(self) -> None:
        """Entries of items that are no longer selected leave the mirror (to the versions)."""
        anchors = {self.layout.mirror_abs(s.mirror_rel) for s in self.res.sources
                   if not s.shallow_files} | self._keep
        ancestors: Set[str] = set()
        for anchor in anchors:
            parent = os.path.dirname(anchor)
            while parent.startswith(self.layout.current_dir) and parent not in ancestors:
                ancestors.add(parent)
                if parent == self.layout.current_dir:
                    break
                parent = os.path.dirname(parent)
        home_mirror = self.layout.mirror_abs(HOME_DIR)
        loose_active = any(s.shallow_files for s in self.res.sources)
        if loose_active:
            ancestors.add(home_mirror)
            ancestors.add(self.layout.current_dir)
        stack = [self.layout.current_dir]
        while stack:
            folder = stack.pop()
            try:
                entries = _list_dir(folder)
            except OSError:
                continue
            for entry in entries:
                self._check_cancel()
                path = entry.path
                if path in anchors:
                    continue
                if entry.name.startswith(TMP_PREFIX):
                    if not self._pass.dry:
                        try:
                            fileops.force_remove(path)
                        except OSError:
                            pass
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if folder == home_mirror and loose_active and not is_dir \
                        and not entry.name.startswith("."):
                    continue
                if path in ancestors and is_dir:
                    stack.append(path)
                    continue
                d_st = _entry_lstat(entry)
                if d_st is not None:
                    self._archive(path, d_st, None, orphan=True)

    # -------------------------------------------------------------- metadata
    def _apply_dir_meta(self, dpath: str, sst: os.stat_result) -> None:
        try:
            dst = os.lstat(dpath)
            mode = stat.S_IMODE(sst.st_mode)
            if stat.S_IMODE(dst.st_mode) != mode:
                os.chmod(dpath, mode)
            if abs(dst.st_mtime_ns - sst.st_mtime_ns) >= self.tolerance_ns:
                os.utime(dpath, ns=(sst.st_atime_ns, sst.st_mtime_ns))
        except OSError:
            pass

    # ---------------------------------------------------------------- finish
    def _finish(self, stats: Stats, status: str) -> None:
        try:
            os.sync()
        except (AttributeError, OSError):
            pass
        if self.keep_versions_days > 0:
            limit = self._now() - _dt.timedelta(days=self.keep_versions_days)
            for run in list_version_runs(self.layout):
                if run.moment is None or run.moment >= limit:
                    continue
                if self.version_dir and os.path.normpath(run.path) == os.path.normpath(self.version_dir):
                    continue
                try:
                    fileops.force_remove(run.path)
                    self.pruned.append(run.name)
                except OSError as exc:
                    self._issue(run.path, "Não foi possível apagar versões antigas: "
                                + fileops.describe_error(exc))
        meta = self.layout.read_meta()
        meta.update(self.meta_extra)
        meta["computador"] = self.layout.computer
        meta["machine_id"] = paths.machine_id()
        meta["atualizado"] = fmt.iso(self._now())
        meta["fontes"] = [
            {"tipo": s.kind, "chave": s.key, "nome": s.label, "caminho": paths.to_portable(s.path),
             "copia": s.mirror_rel, "xdg": s.xdg_key, "icone": s.icon, "extra": s.extra}
            for s in list(self.res.sources) + list(self.res.missing)
            if os.path.lexists(self.layout.mirror_abs(s.mirror_rel))
        ]
        try:
            self.layout.write_meta(meta)
        except OSError as exc:
            self._issue(self.layout.meta_path, "Não foi possível guardar: " + fileops.describe_error(exc))

    def _final_message(self, stats: Stats, status: str) -> str:
        parts = []
        copied = stats.new_files + stats.changed_files
        if copied:
            parts.append("{} {} ({})".format(fmt.files(copied), fmt.agree(copied, "copiado", "copiados"),
                                             fmt.size(stats.bytes_copied)))
        kept = max(stats.archived_files, stats.archived)
        if kept:
            parts.append("{} {} em «Versões anteriores»".format(
                fmt.plural(kept, "ficheiro antigo", "ficheiros antigos"),
                fmt.agree(kept, "guardado", "guardados")))
        text = "; ".join(parts) + "." if parts else "Cópia atualizada (só datas e permissões)."
        if status == STATUS_WARNINGS:
            text += " Há {} a rever.".format(fmt.plural(len(self.issues), "aviso", "avisos"))
        return text[:1].upper() + text[1:]

    def _write_log(self, result: RunResult) -> Optional[str]:
        from .report import render_text  # local import keeps the module graph simple

        if not os.path.isdir(self.layout.computer_dir):
            return None
        try:
            os.makedirs(self.layout.logs_dir, exist_ok=True)
            path = os.path.join(self.layout.logs_dir, fmt.stamp(self.started) + ".txt")
            with open(path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(render_text(result, self.layout.computer))
            status = self.layout.read_status()
            status["em_curso"] = False
            status["ultima_execucao"] = fmt.iso(result.started)
            status["resultado"] = result.status
            status["mensagem"] = result.message
            status["registo"] = os.path.relpath(path, self.layout.computer_dir)
            status["estatisticas"] = asdict(result.stats)
            if result.status in (STATUS_OK, STATUS_WARNINGS, STATUS_NOTHING):
                status["ultima_copia_concluida"] = fmt.iso(result.finished)
                status["ficheiros"] = result.stats.source_files
                status["tamanho"] = result.stats.source_bytes
            self.layout.write_status(status)
            return path
        except OSError:
            return None


def _list_dir(path: str) -> List[os.DirEntry]:
    with os.scandir(path) as it:
        entries = list(it)
    entries.sort(key=lambda e: e.name)
    return entries


def _lstat(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _entry_lstat(entry: Optional[os.DirEntry]) -> Optional[os.stat_result]:
    if entry is None:
        return None
    try:
        return entry.stat(follow_symlinks=False)
    except OSError:
        return None


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)
