"""Small integrations with the running system: processes, dconf, privileged fixes."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Optional, Set


def running_process_names() -> Set[str]:
    """Names (as in /proc/<pid>/comm) of the processes of the current user."""
    names: Set[str] = set()
    uid = os.getuid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return names
    for pid in pids:
        try:
            if os.stat("/proc/" + pid).st_uid != uid:
                continue
            with open("/proc/{}/comm".format(pid), encoding="utf-8", errors="replace") as fh:
                names.add(fh.read().strip())
        except OSError:
            continue
    return names


def dconf_available() -> bool:
    return shutil.which("dconf") is not None


def dconf_dump(target: str) -> Optional[str]:
    """Export all desktop settings to ``target``. Returns an error message or None."""
    if not dconf_available():
        return "o programa dconf não está instalado"
    try:
        out = subprocess.run(["dconf", "dump", "/"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if out.returncode != 0:
        return out.stderr.decode("utf-8", "replace").strip() or "erro desconhecido"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(out.stdout)
    # Keep the modification date stable when nothing changed, so it is not copied again.
    try:
        with open(target, "rb") as fh:
            if fh.read() == out.stdout:
                os.unlink(tmp)
                return None
    except OSError:
        pass
    os.replace(tmp, target)
    return None


def dconf_load(source: str) -> Optional[str]:
    """Apply settings exported with :func:`dconf_dump`. Returns an error message or None."""
    if not dconf_available():
        return "o programa dconf não está instalado"
    try:
        with open(source, "rb") as fh:
            data = fh.read()
        out = subprocess.run(["dconf", "load", "/"], input=data, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if out.returncode != 0:
        return out.stderr.decode("utf-8", "replace").strip() or "erro desconhecido"
    return None


def pkexec_available() -> bool:
    return shutil.which("pkexec") is not None


def take_ownership(folder: str, create: bool = False) -> Optional[str]:
    """Ask for the administrator password (pkexec) to give ``folder`` to the current user.

    Used when a freshly formatted disk only lets root write to it, or when the copies
    were made by a user with another numeric id. Returns an error message or None.
    """
    if not pkexec_available():
        return "o pkexec não está disponível; usa: sudo chown -R {}:{} '{}'".format(
            os.getuid(), os.getgid(), folder)
    if create:
        cmd = ["pkexec", "install", "-d", "-m", "0755", "-o", str(os.getuid()),
               "-g", str(os.getgid()), folder]
    else:
        cmd = ["pkexec", "chown", "-R", "{}:{}".format(os.getuid(), os.getgid()), folder]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        return str(exc)
    if out.returncode in (126, 127):
        return "pedido de autorização cancelado"
    if out.returncode != 0:
        return out.stderr.decode("utf-8", "replace").strip() or "erro desconhecido"
    return None


def os_pretty_name() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "Linux"


def open_in_file_manager(path: str) -> None:
    for cmd in (["xdg-open", path], ["gio", "open", path]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue


def disk_tool() -> Optional[List[str]]:
    for cmd in (["gnome-disks"], ["mintstick", "-m", "format"], ["gparted"]):
        if shutil.which(cmd[0]):
            return cmd
    return None
