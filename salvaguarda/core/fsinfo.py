"""Information about file systems and mount points (no GTK, reads /proc)."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from typing import List, Optional

POSIX_FS = {
    "ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "zfs", "jfs", "reiserfs", "nilfs2",
    "bcachefs", "tmpfs", "overlay",
}
NON_POSIX_FS = {"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk", "hfsplus", "iso9660", "udf"}

_FS_LABELS = {
    "vfat": "FAT32", "msdos": "FAT", "exfat": "exFAT", "ntfs": "NTFS", "ntfs3": "NTFS",
    "fuseblk": "NTFS/exFAT", "hfsplus": "HFS+", "btrfs": "Btrfs", "xfs": "XFS",
}


@dataclass
class MountInfo:
    mount_point: str
    fs_type: str
    device: str
    options: str

    @property
    def read_only(self) -> bool:
        return "ro" in self.options.split(",")


def _unescape(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def mounts() -> List[MountInfo]:
    result = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                parts = line.split()
                if "-" not in parts:
                    continue
                sep = parts.index("-")
                if len(parts) < sep + 3 or len(parts) < 6:
                    continue
                result.append(MountInfo(
                    _unescape(parts[4]), parts[sep + 1], _unescape(parts[sep + 2]), parts[5]
                ))
    except OSError:
        pass
    return result


def mount_for(path: str) -> Optional[MountInfo]:
    """The mount that contains ``path`` (longest matching mount point)."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    best: Optional[MountInfo] = None
    for info in mounts():
        mp = info.mount_point
        if real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) >= len(best.mount_point):
                best = info
    return best


def fs_type(path: str) -> str:
    info = mount_for(path)
    return info.fs_type if info else ""


def fs_label(fs: str) -> str:
    return _FS_LABELS.get(fs, fs or "desconhecido")


def is_posix(fs: str) -> bool:
    return fs in POSIX_FS or (fs not in NON_POSIX_FS and fs != "")


@dataclass
class Usage:
    total: int
    free: int

    @property
    def used(self) -> int:
        return max(0, self.total - self.free)

    @property
    def fraction_used(self) -> float:
        return self.used / self.total if self.total else 0.0


def usage(path: str) -> Optional[Usage]:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return Usage(st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize)


def time_granularity_ns(folder: str) -> int:
    """How precisely the file system at ``folder`` keeps modification times.

    Old ext4 file systems with 128-byte inodes keep whole seconds and FAT keeps two
    seconds; comparing nanoseconds there would copy every file on every run.
    """
    probe = 1_600_000_001_123_456_789
    try:
        fd, tmp = tempfile.mkstemp(prefix=".sgtmp-probe-", dir=folder)
    except OSError:
        return 2_000_000_000
    try:
        os.close(fd)
        os.utime(tmp, ns=(probe, probe))
        diff = abs(os.stat(tmp).st_mtime_ns - probe)
    except OSError:
        diff = 2_000_000_000
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    for step in (1, 1_000, 1_000_000, 1_000_000_000):
        if diff < step:
            return step
    return 2_000_000_000


def can_write(folder: str) -> bool:
    return os.access(folder, os.W_OK | os.X_OK)


def candidate_mount_points() -> List[MountInfo]:
    """Mounted removable-looking disks (used when GIO is not available)."""
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    prefixes = ("/media/{}/".format(user), "/run/media/{}/".format(user), "/media/", "/mnt/")
    result = []
    for info in mounts():
        if info.mount_point.startswith(prefixes) and info.fs_type not in ("autofs", "fuse.portal"):
            result.append(info)
    return result
