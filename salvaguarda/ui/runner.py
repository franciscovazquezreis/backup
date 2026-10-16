"""Run a backup or restore engine in a thread and relay its callbacks to GTK."""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..core.engine import Callbacks, Progress, Question  # noqa: E402
from . import common  # noqa: E402


class _Relay(Callbacks):
    def __init__(self, runner: "Runner"):
        self.runner = runner
        self._latest: Optional[Progress] = None
        self._scheduled = False
        self._lock = threading.Lock()

    def phase(self, phase: str, text: str) -> None:
        GLib.idle_add(self.runner._deliver_phase, phase, text)

    def progress(self, progress: Progress) -> None:
        with self._lock:
            self._latest = progress
            if self._scheduled:
                return
            self._scheduled = True
        GLib.idle_add(self._flush)

    def _flush(self) -> bool:
        with self._lock:
            progress, self._latest = self._latest, None
            self._scheduled = False
        if progress is not None:
            self.runner._deliver_progress(progress)
        return False

    def ask(self, question: Question) -> str:
        event = threading.Event()
        holder = {"answer": question.options[0][0]}
        self.runner._pending_question = (holder, event)
        GLib.idle_add(self.runner._show_question, question, holder, event)
        event.wait()
        self.runner._pending_question = None
        return holder["answer"]


class Runner:
    """One job at a time. ``factory(callbacks, cancel_event)`` must return an object with run()."""

    def __init__(self, window: Gtk.Window,
                 on_phase: Callable[[str, str], None],
                 on_progress: Callable[[Progress], None],
                 on_done: Callable[[Any], None]):
        self.window = window
        self.on_phase = on_phase
        self.on_progress = on_progress
        self.on_done = on_done
        self.cancel_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_question = None
        self.ask_handler: Optional[Callable[[Question], str]] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, factory: Callable[[Callbacks, threading.Event], Any]) -> None:
        if self.running:
            return
        self.cancel_event = threading.Event()
        relay = _Relay(self)
        engine = factory(relay, self.cancel_event)

        def work():
            try:
                result = engine.run()
            except Exception:  # never leave the interface stuck in "running"
                traceback.print_exc()
                result = None
            GLib.idle_add(self._finish, result)

        self._thread = threading.Thread(target=work, name="salvaguarda-job", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        pending = self._pending_question
        if pending is not None:
            holder, event = pending
            event.set()

    def wait(self, timeout: float = 30.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _finish(self, result: Any) -> bool:
        self._thread = None
        self.on_done(result)
        return False

    def _deliver_phase(self, phase: str, text: str) -> bool:
        self.on_phase(phase, text)
        return False

    def _deliver_progress(self, progress: Progress) -> None:
        self.on_progress(progress)

    def _show_question(self, question: Question, holder: dict, event: threading.Event) -> bool:
        if event.is_set():
            return False
        if self.ask_handler is not None:
            holder["answer"] = self.ask_handler(question)
        else:
            holder["answer"] = ask_question(self.window, question)
        event.set()
        return False


def ask_question(window: Gtk.Window, question: Question) -> str:
    destructive = None
    suggested = None
    if question.kind == "mass_changes":
        suggested = None
        destructive = None
    elif question.kind == "adopt":
        suggested = "adopt"
    elif question.kind == "open_apps":
        suggested = None
    kind = Gtk.MessageType.WARNING if question.kind in ("mass_changes", "open_apps", "adopt") \
        else Gtk.MessageType.QUESTION
    return common.choose(window, question.title, question.text, question.options,
                         details=question.details, destructive=destructive,
                         suggested=suggested, kind=kind)
