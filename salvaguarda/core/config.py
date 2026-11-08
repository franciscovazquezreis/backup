"""Persistent configuration (~/.config/salvaguarda/config.json)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Dict, List, Optional, get_type_hints

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
    save_inventory: bool = True  # list of installed programs in Extras (added in 1.1.0)
    find_projects: bool = True  # look for projects outside the copy (added in 1.1.0)
    ignored_projects: List[str] = field(default_factory=list)  # portable paths not to warn about


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
        """Known fields plus any keys a newer version wrote (kept untouched)."""
        data: Dict[str, Any] = dict(getattr(self, "_unknown", {}))
        data.update(asdict(self))
        settings = dict(getattr(self, "_unknown_settings", {}))
        settings.update(data["settings"])
        data["settings"] = settings
        data["version"] = max(self.version, CONFIG_VERSION)
        return data

    def snapshot(self) -> Dict[str, Any]:
        """What is stored on the disk so a new installation can reuse the choices."""
        data = self.to_dict()
        data.pop("destinations", None)
        data.pop("configured", None)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "Config":
        data = migrate(data if isinstance(data, dict) else {})
        settings_raw = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        settings = Settings(**_clean(Settings, settings_raw))
        sources = [SourceEntry(**v) for v in (_clean(SourceEntry, r) for r in _as_list(data.get("sources")))
                   if "kind" in v and "key" in v]
        destinations = [Destination(**v) for v in (_clean(Destination, r) for r in _as_list(data.get("destinations")))
                        if "id" in v and "name" in v and "path" in v]
        top = _clean(cls, data)
        config = cls(
            computer_name=top.get("computer_name", ""),
            configured=top.get("configured", False),
            sources=sources,
            destinations=destinations,
            settings=settings,
            version=max(top.get("version", CONFIG_VERSION), CONFIG_VERSION),
        )
        known = {f.name for f in fields(cls)}
        config._unknown = {k: v for k, v in data.items() if k not in known}
        known_settings = {f.name for f in fields(Settings)}
        config._unknown_settings = {k: v for k, v in settings_raw.items() if k not in known_settings}
        return config

    def adopt_snapshot(self, data: Dict[str, Any]) -> None:
        """Import the choices saved on the disk by a previous installation."""
        other = Config.from_dict(data)
        self.sources = other.sources
        self.settings = other.settings
        if other.computer_name:
            self.computer_name = other.computer_name
        # Keys written by a newer version stay, so that version finds them again.
        self._unknown_settings = dict(getattr(other, "_unknown_settings", {}))
        unknown = dict(getattr(self, "_unknown", {}))
        unknown.update(getattr(other, "_unknown", {}))
        self._unknown = unknown
        self.version = max(self.version, other.version)


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
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        # A folder name that is not valid UTF-8 (lone surrogates): written as \udcXX
        # escapes, which json.load turns back into the same name.
        raw = (json.dumps(data, ensure_ascii=True, indent=2, sort_keys=False) + "\n").encode(
            "ascii")
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# -- schema evolution ---------------------------------------------------------
# Rules: a new version only ADDS keys (never renames, removes or changes the type
# of an existing one); each migration takes the dict of version N and returns the
# dict of version N+1, and must be idempotent because an older app keeps writing
# the keys it knows.
MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def migrate(data: Dict[str, Any]) -> Dict[str, Any]:
    raw = data.get("version")
    version = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 1
    while version < CONFIG_VERSION:
        step = MIGRATIONS.get(version)
        if step is not None:
            data = step(dict(data))
        version += 1
    return data


def _as_list(raw: Any) -> List[Any]:
    return raw if isinstance(raw, list) else []


def _clean(klass, raw: Any) -> Dict[str, Any]:
    """Known fields of ``klass`` whose value has the expected type; the rest is dropped."""
    if not isinstance(raw, dict):
        return {}
    hints = get_type_hints(klass)
    out: Dict[str, Any] = {}
    for f in fields(klass):
        if f.name not in raw:
            continue
        value, kind = raw[f.name], hints.get(f.name)
        if kind is bool:
            if isinstance(value, bool):
                out[f.name] = value
        elif kind is int:
            if isinstance(value, int) and not isinstance(value, bool):
                out[f.name] = value
            elif isinstance(value, str) and value.strip().isdigit():
                out[f.name] = int(value)
        elif kind is str:
            if isinstance(value, str):
                out[f.name] = value
        elif getattr(kind, "__origin__", None) is list:
            if isinstance(value, list):
                out[f.name] = [x for x in value if isinstance(x, str)]
    return out
