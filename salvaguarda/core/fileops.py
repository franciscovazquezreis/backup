"""Low-level file operations shared by the backup and the restore engines."""

from __future__ import annotations

import errno
import os
import stat
import uuid
from typing import Callable, Optional

from .layout import TMP_PREFIX

CHUNK = 1024 * 1024

# Errors on the destination that mean it cannot be trusted any more.
_DEST_LOST = {errno.EIO, errno.EROFS, errno.ENODEV, errno.ENXIO, errno.ESTALE,
              errno.ENOTCONN, errno.ESHUTDOWN}
_DEST_FULL = {errno.ENOSPC, errno.EDQUOT}


class Cancelled(Exception):
    """The user asked to stop."""


class Fatal(Exception):
    """A problem that stops the whole run. ``str(exc)`` is shown to the user."""


class DiskFull(Fatal):
    pass


class DestinationLost(Fatal):
    pass


class EntryError(Exception):
    """A problem with one file or folder; the run continues with the others."""


def tmp_name(folder: str) -> str:
    return os.path.join(folder, TMP_PREFIX + uuid.uuid4().hex[:12])


def raise_for_dest(exc: OSError, what: str) -> None:
    """Translate an OSError raised while writing to the destination."""
    if exc.errno in _DEST_FULL:
        raise DiskFull("O disco de cópias ficou sem espaço livre.") from exc
    if exc.errno in _DEST_LOST:
        raise DestinationLost(
            "O disco de cópias deixou de responder ({}). Verifica se continua ligado."
            .format(os.strerror(exc.errno or 0))
        ) from exc
    raise EntryError("{}: {}".format(what, describe_error(exc))) from exc


def describe_error(exc: OSError) -> str:
    code = exc.errno
    if code in (errno.EACCES, errno.EPERM):
        return "sem permissão"
    if code == errno.ENOENT:
        return "deixou de existir"
    if code == errno.EIO:
        return "erro de leitura (o disco pode estar danificado)"
    if code == errno.ENAMETOOLONG:
        return "nome ou caminho demasiado comprido"
    if code == errno.EFBIG:
        return "ficheiro demasiado grande para o formato do disco"
    if code == errno.EINVAL:
        return "nome ou operação não suportados pelo disco"
    if code == errno.ELOOP:
        return "mudou de tipo durante a cópia"
    return exc.strerror or str(exc)


def open_source(path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        if noatime:
            try:
                return os.open(path, flags | noatime)
            except PermissionError:
                pass
        return os.open(path, flags)
    except OSError as exc:
        raise EntryError(describe_error(exc)) from exc


def copy_xattrs(fd_in: int, fd_out: int) -> None:
    if not hasattr(os, "listxattr"):
        return
    try:
        names = os.listxattr(fd_in)
    except OSError:
        return
    for name in names:
        if not name.startswith("user."):
            continue
        try:
            os.setxattr(fd_out, name, os.getxattr(fd_in, name))
        except OSError:
            continue


def copy_file(
    src: str,
    dst_tmp: str,
    on_bytes: Callable[[int], None],
    check_cancel: Callable[[], None],
) -> os.stat_result:
    """Copy data and metadata of ``src`` into the new file ``dst_tmp``.

    Returns the source metadata the copy corresponds to. Retries when the source
    changes while it is being read. ``dst_tmp`` is removed on any failure.
    """
    fd_in = open_source(src)
    fd_out = -1
    buf = bytearray(CHUNK)
    view = memoryview(buf)
    try:
        try:
            fd_out = os.open(dst_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError as exc:
            raise_for_dest(exc, "Não foi possível criar o ficheiro no disco")
        attempt = 0
        while True:
            st = os.fstat(fd_in)
            if not stat.S_ISREG(st.st_mode):
                raise EntryError("deixou de ser um ficheiro normal")
            offset = 0
            while True:
                check_cancel()
                try:
                    n = os.preadv(fd_in, [view], offset) if hasattr(os, "preadv") else \
                        _pread_into(fd_in, view, offset)
                except OSError as exc:
                    raise EntryError(describe_error(exc)) from exc
                if n == 0:
                    break
                _write_all(fd_out, view[:n])
                offset += n
                on_bytes(n)
            st_after = os.fstat(fd_in)
            unchanged = (st_after.st_size == st.st_size and st_after.st_mtime_ns == st.st_mtime_ns
                         and offset == st.st_size)
            if unchanged or attempt >= 2:
                break
            attempt += 1
            try:
                os.ftruncate(fd_out, 0)
                os.lseek(fd_out, 0, os.SEEK_SET)
            except OSError as exc:
                raise_for_dest(exc, "Não foi possível reescrever o ficheiro")
        try:
            os.fchmod(fd_out, stat.S_IMODE(st.st_mode))
        except OSError:
            pass
        copy_xattrs(fd_in, fd_out)
        try:
            os.utime(fd_out, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError as exc:
            raise_for_dest(exc, "Não foi possível guardar a data do ficheiro")
        try:
            os.close(fd_out)
        except OSError as exc:
            fd_out = -1
            raise_for_dest(exc, "Não foi possível concluir a escrita")
        fd_out = -1
        if not unchanged:
            raise _ChangedWhileCopying(st)
        return st
    except _ChangedWhileCopying:
        raise
    except BaseException:
        if fd_out >= 0:
            try:
                os.close(fd_out)
            except OSError:
                pass
        try:
            os.unlink(dst_tmp)
        except OSError:
            pass
        raise
    finally:
        try:
            os.close(fd_in)
        except OSError:
            pass


class _ChangedWhileCopying(Exception):
    """The copy finished but the source kept changing; carries the metadata used."""

    def __init__(self, st: os.stat_result):
        super().__init__("alterado durante a cópia")
        self.st = st


ChangedWhileCopying = _ChangedWhileCopying


def _pread_into(fd: int, view: memoryview, offset: int) -> int:
    data = os.pread(fd, len(view), offset)
    view[: len(data)] = data
    return len(data)


def _write_all(fd: int, data: memoryview) -> None:
    while len(data):
        try:
            written = os.write(fd, data)
        except OSError as exc:
            raise_for_dest(exc, "Erro ao escrever no disco")
            raise
        data = data[written:]


def make_symlink(target: str, dst_tmp: str, st: Optional[os.stat_result]) -> None:
    try:
        os.symlink(target, dst_tmp)
    except OSError as exc:
        raise_for_dest(exc, "Não foi possível criar a ligação simbólica")
    if st is not None:
        try:
            os.utime(dst_tmp, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass


def ensure_writable_dir(path: str, st: Optional[os.stat_result] = None) -> None:
    """Folders copied from read-only sources keep their mode; open them up to change them."""
    try:
        st = st or os.lstat(path)
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o700 != 0o700:
            os.chmod(path, mode | 0o700)
    except OSError:
        pass


def force_remove(path: str) -> None:
    """Remove a file or a whole folder tree, even with read-only folders inside."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return
    stack = [path]
    order = []
    while stack:
        folder = stack.pop()
        ensure_writable_dir(folder)
        order.append(folder)
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    os.unlink(entry.path)
    for folder in reversed(order):
        os.rmdir(folder)


def move_aside(path: str, target: str) -> None:
    """Rename ``path`` to ``target`` (same file system), creating parent folders."""
    parent = os.path.dirname(target)
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise_for_dest(exc, "Não foi possível criar a pasta de versões")
    try:
        os.rename(path, target)
    except PermissionError:
        # Moving a folder needs write permission on the folder itself.
        ensure_writable_dir(path)
        ensure_writable_dir(os.path.dirname(path))
        try:
            os.rename(path, target)
        except OSError as exc:
            raise_for_dest(exc, "Não foi possível mover para as versões anteriores")
    except OSError as exc:
        raise_for_dest(exc, "Não foi possível mover para as versões anteriores")


def tree_stats(path: str) -> "tuple[int, int]":
    """(files, bytes) below ``path`` without following symlinks."""
    files = size = 0
    stack = [path]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            files += 1
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return files, size
