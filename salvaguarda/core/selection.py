"""Turn the configuration into concrete candidates (for the interface) and sources (for the engine)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import catalog, paths
from .config import (KIND_FOLDER, KIND_LOOSE, KIND_PRESET, KIND_PROJECT, KIND_XDG, LOOSE_KEY,
                     Config)
from .layout import EXTRAS_DIR, HOME_DIR, OTHER_DIR

SECTION_PERSONAL = "personal"
SECTION_PROJECTS = "projects"
SECTION_PRESETS = "presets"
SECTION_OTHER = "other"

DCONF_EXTRA_NAME = "definicoes-do-ambiente-de-trabalho.ini"


@dataclass
class Candidate:
    """Something the user can switch on or off in the "O que copiar" page."""

    kind: str
    key: str
    section: str
    label: str
    description: str
    icon: str
    paths: List[str]  # absolute paths (for presets: only those that exist)
    enabled: bool
    exists: bool = True
    group: str = ""  # preset group
    removable: bool = False  # user-added entries can be removed from the list


@dataclass
class ResolvedSource:
    """One root folder or file that the engine copies."""

    kind: str
    key: str
    label: str
    path: str
    mirror_rel: str
    exists: bool = True
    version_max_size: Optional[int] = None  # None: always keep versions
    shallow_files: bool = False
    icon: str = "folder-symbolic"
    xdg_key: str = ""
    processes: Tuple[str, ...] = ()
    extra: str = ""


def mirror_rel_for(path: str) -> str:
    rel = paths.home_relative(path)
    if rel is not None:
        return HOME_DIR if rel == "" else os.path.join(HOME_DIR, rel)
    return os.path.join(OTHER_DIR, path.lstrip(os.sep))


def _folder_label(path: str) -> str:
    rel = paths.home_relative(path)
    if rel == "":
        return "Toda a pasta pessoal"
    return os.path.basename(path.rstrip(os.sep)) or path


def build_candidates(config: Config, scan: Optional[catalog.HomeScan] = None) -> List[Candidate]:
    scan = scan or catalog.scan_home()
    result: List[Candidate] = []
    seen: Set[Tuple[str, str]] = set()
    user_home = paths.home()

    xdg = paths.xdg_user_dirs()
    for key in paths.XDG_KEYS:
        if key not in xdg:
            continue
        entry = config.find(KIND_XDG, key)
        folder = xdg[key]
        result.append(Candidate(
            KIND_XDG, key, SECTION_PERSONAL, os.path.basename(folder) or catalog.XDG_LABELS[key],
            paths.display_path(folder), catalog.XDG_ICONS[key], [folder],
            entry.enabled if entry else True,
        ))
        seen.add((KIND_XDG, key))

    # Always offered (even with no loose files), so that deleting the last loose file
    # is treated as a normal deletion and not as an item leaving the selection.
    entry = config.find(KIND_LOOSE, LOOSE_KEY)
    result.append(Candidate(
        KIND_LOOSE, LOOSE_KEY, SECTION_PERSONAL, "Ficheiros soltos na pasta pessoal",
        "Ficheiros guardados diretamente em ~ (fora de qualquer pasta)",
        "text-x-generic-symbolic", [user_home], entry.enabled if entry else True,
    ))
    seen.add((KIND_LOOSE, LOOSE_KEY))

    def add_path_candidate(path: str, default_kind: str, default_on: bool) -> None:
        key = paths.to_portable(path)
        entry_project = config.find(KIND_PROJECT, key)
        entry_folder = config.find(KIND_FOLDER, key)
        entry = entry_project or entry_folder
        kind = entry.kind if entry else default_kind
        if (kind, key) in seen:
            return
        seen.add((kind, key))
        exists = os.path.lexists(path)
        if kind == KIND_PROJECT:
            section, icon = SECTION_PROJECTS, "folder-symbolic"
            description = paths.display_path(path)
        else:
            parent_is_home = os.path.dirname(path) == user_home
            section = SECTION_PERSONAL if parent_is_home else SECTION_OTHER
            icon = "folder-symbolic" if os.path.isdir(path) or not exists else "text-x-generic-symbolic"
            description = paths.display_path(path)
        result.append(Candidate(
            kind, key, section, _folder_label(path), description, icon, [path],
            entry.enabled if entry else default_on, exists=exists,
            removable=entry is not None and not (
                os.path.dirname(path) == user_home and kind == KIND_FOLDER and exists),
        ))

    for path in scan.project_dirs:
        add_path_candidate(path, KIND_PROJECT, True)
    for path in scan.other_dirs:
        add_path_candidate(path, KIND_FOLDER, False)
    for entry in config.sources:
        if entry.kind in (KIND_PROJECT, KIND_FOLDER) and (entry.kind, entry.key) not in seen:
            add_path_candidate(paths.from_portable(entry.key), entry.kind, entry.enabled)

    for preset in catalog.PRESETS:
        entry = config.find(KIND_PRESET, preset.id)
        existing = catalog.preset_existing_paths(preset)
        has_extra = preset.extra == "dconf" and _dconf_available()
        if not existing and not has_extra and entry is None:
            continue
        result.append(Candidate(
            KIND_PRESET, preset.id, SECTION_PRESETS, preset.label, preset.description,
            preset.icon, existing, entry.enabled if entry else preset.default_on,
            exists=bool(existing) or has_extra, group=preset.group,
        ))
    return result


def _dconf_available() -> bool:
    from shutil import which

    return which("dconf") is not None


def dconf_extra_path() -> str:
    return os.path.join(paths.app_state_dir(), "extras", DCONF_EXTRA_NAME)


@dataclass
class Resolution:
    sources: List[ResolvedSource] = field(default_factory=list)  # roots to copy, in order
    keep: List[str] = field(default_factory=list)  # mirror paths that must never be archived
    missing: List[ResolvedSource] = field(default_factory=list)  # enabled but not found
    nested: List[ResolvedSource] = field(default_factory=list)  # already inside another root
    wants_dconf: bool = False


_ORDER = {KIND_PRESET: 0, KIND_PROJECT: 1, KIND_XDG: 2, KIND_LOOSE: 3, KIND_FOLDER: 4}


def resolve(config: Config, candidates: Optional[List[Candidate]] = None) -> Resolution:
    """Compute the list of roots to back up.

    Order matters when a run is interrupted or the disk fills up: small and precious
    settings (keys, passwords) go first, then projects, then personal folders.
    """
    candidates = candidates if candidates is not None else build_candidates(config)
    max_preset = max(0, int(config.settings.preset_versions_max_mb)) * 1000 * 1000
    raw: List[ResolvedSource] = []
    res = Resolution()
    for cand in candidates:
        if not cand.enabled:
            continue
        if cand.kind == KIND_PRESET:
            preset = catalog.PRESETS_BY_ID[cand.key]
            for rel in preset.paths:
                path = os.path.join(paths.home(), rel)
                raw.append(ResolvedSource(
                    KIND_PRESET, preset.id, preset.label, path, mirror_rel_for(path),
                    exists=os.path.lexists(path), version_max_size=max_preset,
                    icon=preset.icon, processes=preset.processes,
                ))
            if preset.extra == "dconf" and _dconf_available():
                res.wants_dconf = True
                raw.append(ResolvedSource(
                    KIND_PRESET, preset.id, preset.label, dconf_extra_path(),
                    os.path.join(EXTRAS_DIR, DCONF_EXTRA_NAME), exists=True,
                    version_max_size=max_preset, icon=preset.icon, extra="dconf",
                ))
        elif cand.kind == KIND_LOOSE:
            raw.append(ResolvedSource(
                KIND_LOOSE, cand.key, cand.label, paths.home(), HOME_DIR, exists=True,
                shallow_files=True, icon=cand.icon,
            ))
        else:
            path = cand.paths[0]
            raw.append(ResolvedSource(
                cand.kind, cand.key, cand.label, path, mirror_rel_for(path),
                exists=os.path.lexists(path), icon=cand.icon,
                xdg_key=cand.key if cand.kind == KIND_XDG else "",
            ))

    raw.sort(key=lambda s: _ORDER.get(s.kind, 9))
    present = [s for s in raw if s.exists]
    # A root inside another full root is redundant; the loose-files source is
    # redundant only when the whole home folder is selected.
    full_roots = [s for s in present if not s.shallow_files and not s.extra]
    chosen: List[ResolvedSource] = []
    chosen_mirrors: Dict[str, ResolvedSource] = {}
    for src in raw:
        if not src.exists:
            res.missing.append(src)
            res.keep.append(src.mirror_rel)
            continue
        if src.extra:
            chosen.append(src)
            continue
        container = None
        for other in full_roots:
            if other is src:
                continue
            if src.shallow_files:
                if other.path == src.path:
                    container = other
                    break
                continue
            if paths.is_inside(src.path, other.path) and (
                    src.path != other.path or full_roots.index(other) < full_roots.index(src)):
                container = other
                break
        if container is not None:
            res.nested.append(src)
            res.keep.append(src.mirror_rel)
            continue
        if src.mirror_rel in chosen_mirrors:
            continue
        chosen_mirrors[src.mirror_rel] = src
        chosen.append(src)
    res.sources = chosen
    return res


def summary_by_section(candidates: List[Candidate]) -> Dict[str, List[Candidate]]:
    result: Dict[str, List[Candidate]] = {}
    for cand in candidates:
        if cand.enabled and cand.exists:
            result.setdefault(cand.section, []).append(cand)
    return result
