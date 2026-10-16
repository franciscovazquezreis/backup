"""Test helpers: a fake home folder and a fake external disk in a temporary directory."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from typing import Dict, List, Optional
from unittest import mock

from salvaguarda.core import config as cfgmod
from salvaguarda.core import controller, selection
from salvaguarda.core.engine import BackupEngine, Callbacks, Question
from salvaguarda.core.layout import DiskLayout, init_root, root_for_mount


class RecordingCallbacks(Callbacks):
    def __init__(self, answers: Optional[Dict[str, str]] = None):
        self.answers = answers or {}
        self.questions: List[Question] = []
        self.phases: List[str] = []
        self.progress_events = 0

    def phase(self, phase: str, text: str) -> None:
        self.phases.append(phase)

    def progress(self, progress) -> None:
        self.progress_events += 1

    def ask(self, question: Question) -> str:
        self.questions.append(question)
        return self.answers.get(question.kind, question.options[0][0])


class SandboxTestCase(unittest.TestCase):
    """Each test gets HOME, XDG folders and a 'disk' inside a temporary folder."""

    def setUp(self) -> None:
        # Results must not depend on whether this machine has the dconf tool.
        patcher = mock.patch("salvaguarda.core.selection._dconf_available", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="sg-test-")
        self.home = os.path.join(self.tmp, "home", "ana")
        self.disk = os.path.join(self.tmp, "disk")
        os.makedirs(self.home)
        os.makedirs(self.disk)
        self._env = {k: os.environ.get(k) for k in
                     ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}
        os.environ["HOME"] = self.home
        for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
            os.environ.pop(k, None)
        os.makedirs(os.path.join(self.home, ".config"))
        with open(os.path.join(self.home, ".config", "user-dirs.dirs"), "w") as fh:
            fh.write('XDG_DOCUMENTS_DIR="$HOME/Documentos"\n')
            fh.write('XDG_PICTURES_DIR="$HOME/Imagens"\n')
            fh.write('XDG_DESKTOP_DIR="$HOME"\n')
        os.makedirs(os.path.join(self.home, "Documentos"))
        os.makedirs(os.path.join(self.home, "Imagens"))
        self.config = cfgmod.Config(computer_name="portatil")
        self.dest = controller.register_destination(self.config, self.disk, "Disco de teste")
        self.root = root_for_mount(self.disk)

    def tearDown(self) -> None:
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers
    def write(self, rel: str, content: str = "x", mtime: Optional[int] = None) -> str:
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    @property
    def layout(self) -> DiskLayout:
        return DiskLayout(self.root, self.config.computer_name)

    def mirror(self, rel: str) -> str:
        return os.path.join(self.layout.current_dir, "Pasta pessoal", rel)

    def engine(self, callbacks: Optional[Callbacks] = None, **kwargs) -> BackupEngine:
        resolution = selection.resolve(self.config)
        options = dict(warn_open_apps=False, running_processes=None, dconf_dump=None)
        options.update(kwargs)
        return BackupEngine(
            self.layout, resolution, controller.make_exclusions(self.config),
            callbacks=callbacks or RecordingCallbacks(),
            cancel_event=threading.Event(), **options
        )

    def backup(self, callbacks: Optional[Callbacks] = None, **kwargs):
        return self.engine(callbacks, **kwargs).run()

    def version_runs(self) -> List[str]:
        try:
            return sorted(os.listdir(self.layout.versions_dir))
        except OSError:
            return []

    def find_in_versions(self, rel: str) -> List[str]:
        found = []
        for run in self.version_runs():
            path = os.path.join(self.layout.versions_dir, run, "Pasta pessoal", rel)
            if os.path.lexists(path):
                found.append(path)
        return found


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def ensure_root(root: str) -> None:
    init_root(root, "id", "disk")
