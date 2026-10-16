"""Estimate how much a selection takes, applying the same rules as the engine."""

from __future__ import annotations

import os
import stat
import threading
from typing import Iterable, Optional, Tuple

from . import paths
from .exclusions import Exclusions


def measure(path: str, exclusions: Exclusions, cancel: Optional[threading.Event] = None,
            loose_only: bool = False) -> Tuple[int, int]:
    """(files, bytes) that a backup of ``path`` would hold."""
    try:
        st = os.stat(path)
    except OSError:
        return 0, 0
    if stat.S_ISREG(st.st_mode):
        return 1, st.st_size
    if not stat.S_ISDIR(st.st_mode):
        return 0, 0
    files = size = 0
    root_dev = st.st_dev
    stack = [(path, paths.home_relative(path))]
    while stack:
        if cancel is not None and cancel.is_set():
            break
        folder, home_rel = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if loose_only and entry.name.startswith("."):
                continue
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = stat.S_ISDIR(est.st_mode)
            if loose_only and is_dir:
                continue
            child_rel = None if home_rel is None else (
                entry.name if home_rel == "" else home_rel + "/" + entry.name)
            if exclusions.match(entry.name, entry.path, child_rel, is_dir) is not None:
                continue
            if is_dir:
                if est.st_dev != root_dev:
                    continue
                try:
                    names = set(os.listdir(entry.path))
                except OSError:
                    continue
                if exclusions.match_listing(entry.path, names) is not None:
                    continue
                stack.append((entry.path, child_rel))
            elif stat.S_ISREG(est.st_mode):
                files += 1
                size += est.st_size
            elif stat.S_ISLNK(est.st_mode):
                files += 1
    return files, size


def measure_many(items: Iterable[str], exclusions: Exclusions,
                 cancel: Optional[threading.Event] = None) -> Tuple[int, int]:
    files = size = 0
    for path in items:
        f, s = measure(path, exclusions, cancel)
        files += f
        size += s
    return files, size
