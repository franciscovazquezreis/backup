"""Persistent configuration (~/.config/salvaguarda/config.json)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

from . import paths

CONFIG_VERSION = 1

KIND_XDG = "xdg"          # XDG user folder, key = XDG key (DOCUMENTS, ...)
KIND_LOOSE = "loose"      # loose files directly in the home folder
KIND_FOLDER = "folder"    # any folder or file chosen by the user, key = portable path
KIND_PROJECT = "project"  # programming projects folder, key = portable path
KIND_PRESET = "preset"    # application settings from the catalogue, key = preset id

LOOSE_KEY = "home_files"


@dataclass
class SourceEntry:
    kind: str
    key: str
    enabled: bool = True


@dataclass
class Destination:
    id: str
    name: str
    path: str  # last known location of the "Salvaguarda" folder on the disk
    uuid: str = ""
    fs_type: str = ""


@dataclass
class Settings:
    exclude_caches: bool = True
    exclude_dev: bool = True
    exclude_tools: bool = True
    custom_excludes: List[str] = field(default_factory=list)
    keep_versions_days: int = 180  # 0 = keep until space is needed
    prune_when_full: bool = True
    preset_versions_max_mb: int = 10
    copy_app_to_disk: bool = True
    warn_open_apps: bool = True
    remind_after_days: int = 7  # 0 = never


@dataclass
class Config:
    computer_name: str = ""
    configured: bool = False
    sources: List[SourceEntry] = field(default_factory=list)
    destinations: List[Destination] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    version: int = CONFIG_VERSION

    # -- sources -----------------------------------------------------------
    def find(self, kind: str, key: str) -> Optional[SourceEntry]:
        for entry in self.sources:
            if entry.kind == kind and entry.key == key:
                return entry
        return None

    def set_enabled(self, kind: str, key: str, enabled: bool) -> None:
        entry = self.find(kind, key)
        if entry is None:
            self.sources.append(SourceEntry(kind, key, enabled))
        else:
            entry.enabled = enabled

    def remove(self, kind: str, key: str) -> None:
        self.sources = [e for e in self.sources if not (e.kind == kind and e.key == key)]

    def add_path(self, kind: str, path: str) -> SourceEntry:
        key = paths.to_portable(paths.normalize_source_path(path))
        for other in (KIND_FOLDER, KIND_PROJECT):
            if other != kind:
                self.remove(other, key)
        entry = self.find(kind, key)
        if entry is None:
            entry = SourceEntry(kind, key, True)
            self.sources.append(entry)
        entry.enabled = True
        return entry

    # -- destinations --------------------------------------------------------
    def destination(self, dest_id: str) -> Optional[Destination]:
        for dest in self.destinations:
            if dest.id == dest_id:
                return dest
        return None

    def add_destination(self, name: str, path: str, disk_id: Optional[str] = None,
                        uuid_: str = "", fs_type: str = "") -> Destination:
        dest = self.destination(disk_id) if disk_id else None
        if dest is None:
            dest = Destination(disk_id or uuid.uuid4().hex, name, path, uuid_, fs_type)
            self.destinations.append(dest)
        else:
            dest.name, dest.path = name, path
            dest.uuid = uuid_ or dest.uuid
            dest.fs_type = fs_type or dest.fs_type
        return dest

    def remove_destination(self, dest_id: str) -> None:
        self.destinations = [d for d in self.destinations if d.id != dest_id]

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def snapshot(self) -> Dict[str, Any]:
        """What is stored on the disk so a new installation can reuse the choices."""
        data = self.to_dict()
        data.pop("destinations", None)
        data.pop("configured", None)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        def pick(klass, raw):
            names = {f.name for f in fields(klass)}
            return {k: v for k, v in (raw or {}).items() if k in names}

        settings = Settings(**pick(Settings, data.get("settings")))
        settings.custom_excludes = [str(x) for x in settings.custom_excludes or []]
        sources = []
        for raw in data.get("sources") or []:
            try:
                sources.append(SourceEntry(**pick(SourceEntry, raw)))
            except TypeError:
                continue
        destinations = []
        for raw in data.get("destinations") or []:
            try:
                destinations.append(Destination(**pick(Destination, raw)))
            except TypeError:
                continue
        return cls(
            computer_name=str(data.get("computer_name") or ""),
            configured=bool(data.get("configured", False)),
            sources=sources,
            destinations=destinations,
            settings=settings,
            version=int(data.get("version") or CONFIG_VERSION),
        )

    def adopt_snapshot(self, data: Dict[str, Any]) -> None:
        """Import the choices saved on the disk by a previous installation."""
        other = Config.from_dict(data)
        self.sources = other.sources
        self.settings = other.settings
        if other.computer_name:
            self.computer_name = other.computer_name


def config_path() -> str:
    return os.path.join(paths.app_config_dir(), "config.json")


def load() -> Config:
    path = config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            config = Config.from_dict(json.load(fh))
    except FileNotFoundError:
        config = Config()
    except (OSError, ValueError, TypeError):
        # Keep the damaged file for inspection and start again.
        try:
            os.replace(path, path + ".danificado")
        except OSError:
            pass
        config = Config()
    if not config.computer_name:
        config.computer_name = paths.default_computer_name()
    return config


def save(config: Config) -> None:
    write_json_atomic(config_path(), config.to_dict())


def write_json_atomic(path: str, data: Any) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".{}.{}.tmp".format(os.path.basename(path), os.getpid()))
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
