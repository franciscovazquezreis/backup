"""Command line: ``salvaguarda --copiar`` makes a backup without opening the window."""

from __future__ import annotations

import sys
import threading

from .core import config as cfgmod
from .core import controller, fmt
from .core.engine import (PHASE_COPY, PHASE_SCAN, STATUS_CANCELLED, STATUS_FAILED,
                          STATUS_WARNINGS, Callbacks, Progress, Question)
from .core.report import STATUS_TITLES


class _TerminalCallbacks(Callbacks):
    def __init__(self, interactive: bool):
        self.interactive = interactive
        self._last_len = 0

    def _line(self, text: str) -> None:
        text = text[:110]
        pad = max(0, self._last_len - len(text))
        sys.stdout.write("\r" + text + " " * pad)
        sys.stdout.flush()
        self._last_len = len(text)

    def phase(self, phase: str, text: str) -> None:
        self._newline()
        print(text)

    def _newline(self) -> None:
        if self._last_len:
            sys.stdout.write("\n")
            self._last_len = 0

    def progress(self, p: Progress) -> None:
        if p.phase == PHASE_SCAN:
            self._line("  {} {}".format(fmt.files(p.scanned), fmt.agree(p.scanned, "analisado", "analisados")))
        elif p.phase == PHASE_COPY:
            parts = ["  {:3d} %".format(int(p.fraction * 100)),
                     "{} de {}".format(fmt.size(p.bytes_done), fmt.size(p.bytes_total))]
            if p.eta is not None:
                parts.append(fmt.remaining(p.eta))
            self._line(" · ".join(parts))

    def ask(self, question: Question) -> str:
        self._newline()
        print("\n{}\n{}".format(question.title, question.text))
        for item in question.details:
            print("  - " + item)
        if not self.interactive:
            print("(sem terminal interativo: a opção segura foi escolhida)")
            return question.options[0][0]
        for index, (_, label) in enumerate(question.options, 1):
            print("  {}) {}".format(index, label))
        while True:
            try:
                answer = input("Escolhe [1-{}]: ".format(len(question.options))).strip()
            except EOFError:
                return question.options[0][0]
            if answer.isdigit() and 1 <= int(answer) <= len(question.options):
                return question.options[int(answer) - 1][0]


def run_backup() -> int:
    config = cfgmod.load()
    if not config.configured or not config.destinations:
        print("Abre a Salvaguarda primeiro para escolher o disco e o que copiar.")
        return 2
    connected = controller.connected_destinations(config)
    if not connected:
        names = ", ".join(d.name for d in config.destinations)
        print("O disco de cópias não está ligado ({}).".format(names))
        return 2
    target = connected[0]
    controller.freeze_choices(config)
    cfgmod.save(config)
    print("Salvaguarda: cópia para «{}»".format(target.destination.name))
    callbacks = _TerminalCallbacks(sys.stdin.isatty())
    engine = controller.build_engine(config, target.root, callbacks, threading.Event())
    try:
        result = engine.run()
    except KeyboardInterrupt:
        engine.cancel_event.set()
        print("\nA cancelar…")
        return 3
    callbacks._newline()
    notes = controller.after_backup(config, target.destination, target.root, result)
    print("\n{}: {}".format(STATUS_TITLES.get(result.status, result.status), result.message))
    for note in result.notes + notes:
        print("  * " + note)
    if result.issues:
        print("  {} (ver o registo: {})".format(
            fmt.plural(len(result.issues), "aviso", "avisos"), result.log_path or "?"))
    if result.status in (STATUS_FAILED, STATUS_CANCELLED):
        return 2
    return 1 if result.status == STATUS_WARNINGS else 0
