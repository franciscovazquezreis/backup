"""External disks: detection through GIO (the same source the file manager uses)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

from ..core import fsinfo  # noqa: E402
from ..core.layout import read_disk_marker, root_for_mount  # noqa: E402

_SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/home", "/var", "/usr", "/opt", "/srv", "/tmp")


@dataclass
class Disk:
    name: str
    path: str  # mount point
    icon_name: str
    uuid: str
    fs_type: str
    usage: Optional[fsinfo.Usage]
    removable: bool
    read_only: bool
    marker_id: Optional[str]
    mount: Optional[Gio.Mount] = None

    @property
    def root(self) -> str:
        return root_for_mount(self.path)

    @property
    def posix(self) -> bool:
        return fsinfo.is_posix(self.fs_type)


def _icon_name(gicon: Optional[Gio.Icon], removable: bool) -> str:
    if isinstance(gicon, Gio.ThemedIcon):
        names = gicon.get_names()
        if names:
            return names[0]
    return "drive-harddisk-usb" if removable else "drive-harddisk"


def describe_path(path: str, name: Optional[str] = None, mount: Optional[Gio.Mount] = None,
                  removable: bool = True, icon_name: Optional[str] = None,
                  uuid: str = "") -> Disk:
    info = fsinfo.mount_for(path)
    marker = read_disk_marker(root_for_mount(path))
    return Disk(
        name=name or os.path.basename(path.rstrip("/")) or path,
        path=path,
        icon_name=icon_name or ("drive-harddisk-usb" if removable else "folder"),
        uuid=uuid,
        fs_type=info.fs_type if info else "",
        usage=fsinfo.usage(path),
        removable=removable,
        read_only=bool(info and info.read_only),
        marker_id=marker.get("id") if marker else None,
        mount=mount,
    )


class DiskMonitor(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self) -> None:
        super().__init__()
        self._monitor = Gio.VolumeMonitor.get()
        self._pending = 0
        for signal in ("mount-added", "mount-removed", "mount-changed",
                       "volume-added", "volume-removed", "drive-disconnected"):
            self._monitor.connect(signal, self._on_change)

    def _on_change(self, *args) -> None:
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(400, self._emit)

    def _emit(self) -> bool:
        self._pending = 0
        self.emit("changed")
        return False

    def disks(self) -> List[Disk]:
        result: List[Disk] = []
        seen = set()
        for mount in self._monitor.get_mounts():
            try:
                if mount.is_shadowed():
                    continue
                root = mount.get_root()
                path = root.get_path() if root is not None else None
            except GLib.Error:
                continue
            if not path or path in _SYSTEM_MOUNTS or "/gvfs/" in path or path.startswith("/snap/"):
                continue
            drive = mount.get_drive()
            volume = mount.get_volume()
            removable = bool(drive and (drive.is_removable() or drive.can_eject())) or \
                mount.can_eject()
            uuid = ""
            if volume is not None:
                uuid = volume.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UUID) or ""
            disk = describe_path(path, mount.get_name(), mount, removable,
                                 _icon_name(mount.get_symbolic_icon(), removable), uuid)
            if disk.read_only:
                continue
            seen.add(path)
            result.append(disk)
        for info in fsinfo.candidate_mount_points():
            if info.mount_point in seen or info.read_only:
                continue
            seen.add(info.mount_point)
            result.append(describe_path(info.mount_point, icon_name="drive-harddisk-symbolic"))
        result.sort(key=lambda d: (not d.removable, d.name.lower()))
        return result

    def unmounted_volumes(self) -> List[Gio.Volume]:
        result = []
        for volume in self._monitor.get_volumes():
            if volume.get_mount() is None and volume.can_mount():
                result.append(volume)
        return result

    def volume_for_uuid(self, uuid: str) -> Optional[Gio.Volume]:
        if not uuid:
            return None
        for volume in self.unmounted_volumes():
            if volume.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UUID) == uuid:
                return volume
        return None

    def mount_for_path(self, path: str) -> Optional[Gio.Mount]:
        best = None
        for mount in self._monitor.get_mounts():
            root = mount.get_root()
            mp = root.get_path() if root is not None else None
            if mp and (path == mp or path.startswith(mp.rstrip("/") + "/")):
                if best is None or len(mp) > len(best[0]):
                    best = (mp, mount)
        return best[1] if best else None


def eject(mount: Gio.Mount, parent: Optional[Gtk.Window],
          done: Callable[[Optional[str]], None]) -> None:
    """Safely remove the disk (power off when possible), like the file manager does."""
    operation = Gtk.MountOperation.new(parent)
    drive = mount.get_drive()
    flags = Gio.MountUnmountFlags.NONE

    def finish(source, result, method):
        try:
            getattr(source, method)(result)
            done(None)
        except GLib.Error as exc:
            done(exc.message)

    if drive is not None and drive.can_stop():
        drive.stop(flags, operation, None, finish, "stop_finish")
    elif drive is not None and drive.can_eject():
        drive.eject_with_operation(flags, operation, None, finish, "eject_with_operation_finish")
    elif mount.can_eject():
        mount.eject_with_operation(flags, operation, None, finish, "eject_with_operation_finish")
    elif mount.can_unmount():
        mount.unmount_with_operation(flags, operation, None, finish, "unmount_with_operation_finish")
    else:
        done("Este disco não pode ser ejetado a partir da app.")


def mount_volume(volume: Gio.Volume, parent: Optional[Gtk.Window],
                 done: Callable[[Optional[str]], None]) -> None:
    operation = Gtk.MountOperation.new(parent)

    def finish(source, result):
        try:
            source.mount_finish(result)
            done(None)
        except GLib.Error as exc:
            done(exc.message)

    volume.mount(Gio.MountMountFlags.NONE, operation, None, finish)
