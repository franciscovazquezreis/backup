"""How backups are organised on the external disk.

    <disco>/Salvaguarda/
        LEIA-ME.txt               how to restore by hand, without the app
        disco.json                identifies this disk as a Salvaguarda disk
        Aplicação/                portable copy of the app (runs without installing)
        <computador>/
            salvaguarda.json      choices and details of the computer
            estado.json           summary of the last run
            Cópia atual/          exact mirror of the selected folders
                Pasta pessoal/    everything that lives inside the home folder
                Outras localizações/  folders outside the home folder (absolute paths)
                Extras/           generated files (desktop settings)
            Versões anteriores/
                2026-10-16_15-30-12/  files deleted or replaced in that run
            Registos/
                2026-10-16_15-30-12.txt
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import fmt
from .config import read_json, write_json_atomic

ROOT_DIR_NAME = "Salvaguarda"
DISK_MARKER = "disco.json"
README_NAME = "LEIA-ME.txt"
APP_DIR = "Aplicação"
CURRENT_DIR = "Cópia atual"
VERSIONS_DIR = "Versões anteriores"
LOGS_DIR = "Registos"
HOME_DIR = "Pasta pessoal"
OTHER_DIR = "Outras localizações"
EXTRAS_DIR = "Extras"
COMPUTER_META = "salvaguarda.json"
STATUS_FILE = "estado.json"
LOCK_FILE = ".salvaguarda.lock"
TMP_PREFIX = ".sgtmp-"

_RESERVED = {APP_DIR.lower(), README_NAME.lower(), DISK_MARKER.lower()}


def sanitize_computer_name(name: str) -> str:
    name = re.sub(r"[/\x00-\x1f]", "-", name or "").strip().strip(".")
    if not name or name.lower() in _RESERVED:
        name = "computador"
    return name[:80]


class DiskLayout:
    def __init__(self, root: str, computer: str):
        self.root = root
        self.computer = sanitize_computer_name(computer)
        self.computer_dir = os.path.join(root, self.computer)
        self.current_dir = os.path.join(self.computer_dir, CURRENT_DIR)
        self.versions_dir = os.path.join(self.computer_dir, VERSIONS_DIR)
        self.logs_dir = os.path.join(self.computer_dir, LOGS_DIR)
        self.meta_path = os.path.join(self.computer_dir, COMPUTER_META)
        self.status_path = os.path.join(self.computer_dir, STATUS_FILE)
        self.lock_path = os.path.join(self.computer_dir, LOCK_FILE)

    def mirror_abs(self, rel: str) -> str:
        return os.path.join(self.current_dir, rel) if rel else self.current_dir

    def new_version_dir(self, moment: _dt.datetime) -> str:
        base = os.path.join(self.versions_dir, fmt.stamp(moment))
        candidate, n = base, 2
        while os.path.lexists(candidate):
            candidate = "{}-{}".format(base, n)
            n += 1
        return candidate

    def read_meta(self) -> Dict[str, Any]:
        data = read_json(self.meta_path)
        return data if isinstance(data, dict) else {}

    def write_meta(self, data: Dict[str, Any]) -> None:
        write_json_atomic(self.meta_path, data)

    def read_status(self) -> Dict[str, Any]:
        data = read_json(self.status_path)
        return data if isinstance(data, dict) else {}

    def write_status(self, data: Dict[str, Any]) -> None:
        write_json_atomic(self.status_path, data)

    def has_backup(self) -> bool:
        return os.path.isdir(self.current_dir)


def root_for_mount(mount_path: str) -> str:
    return os.path.join(mount_path, ROOT_DIR_NAME)


def read_disk_marker(root: str) -> Optional[Dict[str, Any]]:
    data = read_json(os.path.join(root, DISK_MARKER))
    if isinstance(data, dict) and data.get("id"):
        return data
    return None


def init_root(root: str, disk_id: str, name: str) -> None:
    """Create the Salvaguarda folder and its marker (raises OSError when not allowed)."""
    os.makedirs(root, exist_ok=True)
    marker = read_disk_marker(root)
    if marker is None or marker.get("id") != disk_id:
        data = {
            "id": disk_id,
            "nome": name,
            "criado": fmt.iso(_dt.datetime.now()),
            "aviso": "Ficheiro da Salvaguarda. Não apagues: identifica este disco de cópias.",
        }
        if marker:
            data["id_anterior"] = marker.get("id")
        write_json_atomic(os.path.join(root, DISK_MARKER), data)


@dataclass
class ComputerBackup:
    name: str
    layout: DiskLayout
    meta: Dict[str, Any] = field(default_factory=dict)
    status: Dict[str, Any] = field(default_factory=dict)

    @property
    def last_success(self) -> Optional[_dt.datetime]:
        return fmt.parse_iso(self.status.get("ultima_copia_concluida"))

    @property
    def last_run(self) -> Optional[_dt.datetime]:
        return fmt.parse_iso(self.status.get("ultima_execucao"))


def list_computers(root: str) -> List[ComputerBackup]:
    result = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError:
        return result
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        layout = DiskLayout(root, entry.name)
        if not os.path.isdir(layout.current_dir):
            continue
        result.append(ComputerBackup(entry.name, layout, layout.read_meta(), layout.read_status()))
    return result


@dataclass
class VersionRun:
    name: str
    path: str
    moment: Optional[_dt.datetime]


def list_version_runs(layout: DiskLayout) -> List[VersionRun]:
    """Version folders, oldest first."""
    result = []
    try:
        entries = list(os.scandir(layout.versions_dir))
    except OSError:
        return result
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        result.append(VersionRun(entry.name, entry.path, fmt.parse_stamp(entry.name)))
    result.sort(key=lambda r: (r.moment or _dt.datetime.min, r.name))
    return result
