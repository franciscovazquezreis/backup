"""High-level operations shared by the graphical interface and the command line."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from .. import VERSION
from . import (desktop, fmt, fsinfo, inventory, paths, portable, projects, report, selection,
               system)
from .config import Config, Destination
from .engine import BackupEngine, Callbacks, RunResult, STATUS_FAILED, STATUS_CANCELLED
from .exclusions import Exclusions, validate_custom
from .layout import DiskLayout, init_root, read_disk_marker, root_for_mount


def make_exclusions(config: Config) -> Exclusions:
    s = config.settings
    return Exclusions(caches=s.exclude_caches, dev=s.exclude_dev, tools=s.exclude_tools,
                      custom=validate_custom(s.custom_excludes))


def locate(dest: Destination, extra_mounts: Optional[List[str]] = None) -> Optional[str]:
    """Current path of the Salvaguarda folder of ``dest``, or None when it is not connected."""
    candidates = []
    if dest.path:
        candidates.append(dest.path)
    for mount in extra_mounts or []:
        candidates.append(root_for_mount(mount))
    for info in fsinfo.candidate_mount_points():
        candidates.append(root_for_mount(info.mount_point))
    seen = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        marker = read_disk_marker(root)
        if marker and marker.get("id") == dest.id:
            return root
    return None


@dataclass
class Connected:
    destination: Destination
    root: str


def connected_destinations(config: Config, extra_mounts: Optional[List[str]] = None) -> List[Connected]:
    result = []
    for dest in config.destinations:
        root = locate(dest, extra_mounts)
        if root:
            result.append(Connected(dest, root))
    return result


def register_destination(config: Config, mount_or_folder: str, name: str, uuid_: str = "") -> Destination:
    """Use ``mount_or_folder`` as a backup disk (creates <it>/Salvaguarda/disco.json)."""
    root = root_for_mount(mount_or_folder)
    marker = read_disk_marker(root)
    disk_id = marker.get("id") if marker else None
    dest = config.add_destination(name, root, disk_id=disk_id, uuid_=uuid_,
                                  fs_type=fsinfo.fs_type(mount_or_folder))
    init_root(root, dest.id, name)
    portable.write_readme(root)
    return dest


def layout_for(config: Config, root: str) -> DiskLayout:
    return DiskLayout(root, config.computer_name)


def environment_meta(config: Config) -> Dict[str, object]:
    """What the computer metadata records about the system the copy comes from."""
    meta: Dict[str, object] = {
        "sistema": system.os_pretty_name(),
        "distribuicao": system.os_release(),
        "utilizador": system.user_name(),
        "uid": os.getuid(),
        "pasta_pessoal": paths.home(),
        "pastas_xdg": {k: paths.to_portable(v) for k, v in paths.xdg_user_dirs().items()},
        "configuracao": config.snapshot(),
        "versao_app": VERSION,
    }
    here = desktop.current_desktop(processes=system.running_process_names())
    if here:  # never replace a known desktop with "" (command line without a session)
        meta["ambiente"] = here
    return meta


def build_engine(config: Config, root: str, callbacks: Callbacks,
                 cancel_event: threading.Event) -> BackupEngine:
    resolution = selection.resolve(config)
    meta_extra = environment_meta(config)
    project_roots = [s.path for s in resolution.sources if s.kind == "project"]

    def inventory_dump(target: str) -> Optional[str]:
        return inventory.generate(
            target, computer=config.computer_name, system=str(meta_extra["sistema"]),
            desktop=desktop.label(str(meta_extra.get("ambiente", ""))) if meta_extra.get("ambiente") else "",
            user=str(meta_extra["utilizador"]), home=paths.home(), version=VERSION,
            xdg=paths.xdg_user_dirs(), project_roots=project_roots)

    return BackupEngine(
        layout_for(config, root), resolution, make_exclusions(config),
        keep_versions_days=config.settings.keep_versions_days,
        prune_when_full=config.settings.prune_when_full,
        warn_open_apps=config.settings.warn_open_apps,
        running_processes=system.running_process_names,
        dconf_dump=lambda target: system.dconf_dump(
            target, desktop.dump_header(str(meta_extra.get("ambiente") or ""))),
        inventory_dump=inventory_dump,
        meta_extra=meta_extra,
        callbacks=callbacks,
        cancel_event=cancel_event,
    )


def scan_projects(config: Config, cancel: Optional[threading.Event] = None,
                  progress=None) -> projects.ScanResult:
    """Look for programming projects and tell which ones the copy does not hold.

    Takes a few seconds on a big home folder: call it from a worker thread.
    """
    resolution = selection.resolve(config)
    return projects.find_projects(projects.coverage_roots(resolution),
                                  projects.search_roots(resolution), make_exclusions(config),
                                  cancel=cancel, progress=progress)


def refresh_coverage(config: Config, result: projects.ScanResult) -> None:
    """Recompute what is covered after the selection changed (no new search, no git)."""
    resolution = selection.resolve(config)
    projects.apply_coverage(result.projects, projects.coverage_roots(resolution),
                            make_exclusions(config))


def projects_at_risk(config: Config, result: projects.ScanResult) -> List[projects.Project]:
    """Projects with work that exists only on this computer and is not in the copy."""
    ignored = set(config.settings.ignored_projects)
    return [p for p in result.projects
            if projects.level(p) in (projects.LEVEL_LOCAL, projects.LEVEL_IGNORED)
            and not projects.in_backup(p) and paths.to_portable(p.path) not in ignored]


def projects_only_here(config: Config, result: projects.ScanResult) -> List[projects.Project]:
    """Projects in the copy whose work is on no remote: the copy is their only other place."""
    return [p for p in result.projects
            if projects.level(p) == projects.LEVEL_LOCAL and projects.in_backup(p)]


def after_backup(config: Config, dest: Destination, root: str, result: RunResult) -> List[str]:
    """Bookkeeping after a run. Returns extra notes for the interface."""
    notes = []
    report.append_history(report.history_entry(result, dest.name, config.computer_name))
    if result.status in (STATUS_FAILED, STATUS_CANCELLED):
        return notes
    try:
        portable.write_readme(root)
        if config.settings.copy_app_to_disk and portable.copy_app(root):
            notes.append("A app foi copiada para o disco, para poderes restaurar sem a instalar.")
    except OSError:
        pass
    return notes


def last_success(config: Config, root: str):
    layout = layout_for(config, root)
    status = layout.read_status()
    return fmt.parse_iso(status.get("ultima_copia_concluida")), status


def freeze_choices(config: Config) -> None:
    """Store the current state of folder-based candidates explicitly.

    Project folders are detected by heuristics; if a folder stopped looking like a
    project, its default would change and its copy would leave the mirror. Saving the
    decision avoids that. Presets are left alone so newly installed apps are included.
    """
    for cand in selection.build_candidates(config):
        if cand.kind in ("project", "folder", "xdg", "loose"):
            config.set_enabled(cand.kind, cand.key, cand.enabled)
