"""User directories, XDG locations and portable path notation."""

from __future__ import annotations

import os
import re
import socket
from typing import Dict, Optional

# XDG user directory keys, in the order they are shown to the user.
XDG_KEYS = (
    "DOCUMENTS",
    "PICTURES",
    "MUSIC",
    "VIDEOS",
    "DESKTOP",
    "DOWNLOAD",
    "TEMPLATES",
    "PUBLICSHARE",
)

# Fallback folder names when ~/.config/user-dirs.dirs does not exist.
_XDG_FALLBACK_NAMES = {
    "DESKTOP": ("Ambiente de trabalho", "Área de Trabalho", "Desktop"),
    "DOCUMENTS": ("Documentos", "Documents"),
    "DOWNLOAD": ("Transferências", "Downloads"),
    "MUSIC": ("Música", "Music"),
    "PICTURES": ("Imagens", "Pictures"),
    "VIDEOS": ("Vídeos", "Videos"),
    "TEMPLATES": ("Modelos", "Templates"),
    "PUBLICSHARE": ("Público", "Public"),
}

_USER_DIRS_LINE = re.compile(r'^\s*XDG_([A-Z]+)_DIR\s*=\s*"(.*)"\s*$')


def home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def _xdg_base(env: str, default: str) -> str:
    value = os.environ.get(env, "")
    if value and os.path.isabs(value):
        return value
    return os.path.join(home(), default)


def config_home() -> str:
    return _xdg_base("XDG_CONFIG_HOME", ".config")


def state_home() -> str:
    return _xdg_base("XDG_STATE_HOME", os.path.join(".local", "state"))


def data_home() -> str:
    return _xdg_base("XDG_DATA_HOME", os.path.join(".local", "share"))


def app_config_dir() -> str:
    return os.path.join(config_home(), "salvaguarda")


def app_state_dir() -> str:
    return os.path.join(state_home(), "salvaguarda")


def app_data_dir() -> str:
    return os.path.join(data_home(), "salvaguarda")


def xdg_user_dirs() -> Dict[str, str]:
    """Return the XDG user directories that exist and are not the home itself."""
    result: Dict[str, str] = {}
    user_home = home()
    path = os.path.join(config_home(), "user-dirs.dirs")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                match = _USER_DIRS_LINE.match(line)
                if not match:
                    continue
                key, value = match.group(1), match.group(2)
                if key not in XDG_KEYS:
                    continue
                value = value.replace("$HOME", user_home)
                if not os.path.isabs(value):
                    continue
                result[key] = os.path.normpath(value)
    except OSError:
        pass
    if not result:
        for key, names in _XDG_FALLBACK_NAMES.items():
            for name in names:
                candidate = os.path.join(user_home, name)
                if os.path.isdir(candidate):
                    result[key] = candidate
                    break
    return {
        key: value
        for key, value in result.items()
        if os.path.normpath(value) != user_home and os.path.isdir(value)
    }


def normalize_source_path(path: str) -> str:
    """Resolve symlinks in the parent folders but keep the last component as chosen.

    A folder like ``~/Projetos`` that is a symlink to another disk must still be
    stored as ``~/Projetos``, while a ``/home`` that is itself a symlink must not
    prevent recognising paths inside the home folder.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if path == os.sep:
        return path
    parent, name = os.path.split(path)
    return os.path.join(os.path.realpath(parent), name)


def to_portable(path: str) -> str:
    """Write paths inside the home folder as ``~/...`` so they survive a new username."""
    path = os.path.normpath(path)
    user_home = home()
    if path == user_home:
        return "~"
    if path.startswith(user_home + os.sep):
        return "~/" + path[len(user_home) + 1:]
    return path


def from_portable(path: str) -> str:
    if path == "~":
        return home()
    if path.startswith("~/"):
        return os.path.join(home(), path[2:])
    return os.path.normpath(path)


def is_inside(path: str, parent: str) -> bool:
    """True when ``path`` is ``parent`` or lies below it (no symlink resolution)."""
    path = os.path.normpath(path)
    parent = os.path.normpath(parent)
    if parent == os.sep:
        return True
    return path == parent or path.startswith(parent + os.sep)


def home_relative(path: str) -> Optional[str]:
    """Path relative to the home folder, or None when outside it."""
    user_home = home()
    path = os.path.normpath(path)
    if path == user_home:
        return ""
    if path.startswith(user_home + os.sep):
        return path[len(user_home) + 1:]
    return None


def default_computer_name() -> str:
    name = socket.gethostname().split(".")[0].strip()
    name = re.sub(r"[/\x00]", "-", name)
    return name or "computador"


def machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, encoding="ascii", errors="replace") as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            continue
    return ""


def display_path(path: str) -> str:
    """Make any path safe to show in the interface (handles undecodable names)."""
    try:
        raw = os.fsencode(path)
    except (UnicodeError, TypeError):
        return str(path)
    text = raw.decode("utf-8", "replace")
    user_home = home()
    if text == user_home:
        return "~"
    if text.startswith(user_home + "/"):
        return "~" + text[len(user_home):]
    return text


def safe_text(text: str) -> str:
    """Replace lone surrogates (from undecodable file names) so GTK can show the text."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


# System locations that cannot be selected as sources.
BLOCKED_SOURCES = (
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32",
    "/proc", "/run", "/sbin", "/sys", "/tmp", "/usr", "/var",
)


def is_blocked_source(path: str) -> bool:
    path = os.path.normpath(path)
    if path in BLOCKED_SOURCES:
        return True
    for blocked in BLOCKED_SOURCES:
        if blocked != "/" and path.startswith(blocked + os.sep):
            # /run/media/<user>/... is where some distributions mount disks.
            if path.startswith("/run/media/"):
                return False
            return True
    return False
