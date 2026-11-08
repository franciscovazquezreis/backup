"""Background search for programming projects, shared by the main page and "O que copiar"."""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, GObject  # noqa: E402

from ..core import controller, projects  # noqa: E402
from ..core.config import Config  # noqa: E402

REUSE_SECONDS = 600  # a result younger than this is reused instead of searching again


class ProjectScanner(GObject.Object):
    """Runs :func:`controller.scan_projects` in a thread; emits ``updated`` when done."""

    __gsignals__ = {
        "updated": (GObject.SignalFlags.RUN_LAST, None, ()),
        "progress": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(self, config_getter: Callable[[], Config]):
        super().__init__()
        self._config = config_getter
        self.result: Optional[projects.ScanResult] = None
        self.finished_at = 0.0
        self.running = False
        self._cancel: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._last_progress = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._config().settings.find_projects)

    def request(self, force: bool = False) -> bool:
        """Start a search unless one is running or the last one is recent. Returns False."""
        if not self.enabled or self.running:
            return False
        if not force and self.result is not None and \
                time.monotonic() - self.finished_at < REUSE_SECONDS:
            return False
        # The thread works on a copy: the interface may change the configuration meanwhile.
        config = Config.from_dict(self._config().to_dict())
        cancel = threading.Event()
        self._cancel = cancel
        self.running = True

        def progress(dirs: int, _entries: int, _path: str) -> None:
            now = time.monotonic()
            if now - self._last_progress > 0.25:
                self._last_progress = now
                GLib.idle_add(self._emit_progress, dirs)

        def work() -> None:
            try:
                result = controller.scan_projects(config, cancel, progress)
            except Exception:  # never leave the interface waiting forever
                result = None
            GLib.idle_add(self._done, result, cancel)

        self._thread = threading.Thread(target=work, name="salvaguarda-projects", daemon=True)
        self._thread.start()
        self.emit("updated")
        return False

    def _emit_progress(self, dirs: int) -> bool:
        if self.running:
            self.emit("progress", dirs)
        return False

    def _done(self, result: Optional[projects.ScanResult], cancel: threading.Event) -> bool:
        if cancel is not self._cancel:
            return False  # a newer search replaced this one
        self.running = False
        if result is not None and not result.cancelled:
            self.result = result
            self.finished_at = time.monotonic()
        self.emit("updated")
        return False

    def recompute(self, config: Config) -> None:
        """Coverage for another selection, without searching again."""
        if self.result is not None:
            controller.refresh_coverage(config, self.result)

    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def wait(self, seconds: float) -> None:
        """After stop(): give the search a moment to end its git commands."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(seconds)
