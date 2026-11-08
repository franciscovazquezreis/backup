"""A new version installed over an old one (and back) must keep everything working."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from salvaguarda import VERSION
from salvaguarda.core import config as cfgmod
from salvaguarda.core import fmt, portable, report
from salvaguarda.core.layout import APP_DIR

from .helpers import SandboxTestCase


class VersionKeyTests(unittest.TestCase):
    def test_order(self):
        k = portable.version_key
        self.assertLess(k("1.0.9"), k("1.0.10"))
        self.assertLess(k("1.1.0~rc1"), k("1.1.0"))
        self.assertLess(k("lixo"), k("0.0.1"))
        self.assertEqual(k(" 1.2.3\n"), k("1.2.3"))


class CopyAppTests(SandboxTestCase):
    def app_version_on_disk(self):
        with open(os.path.join(self.root, APP_DIR, "VERSAO"), encoding="utf-8") as fh:
            return fh.read().strip()

    def fake_disk_copy(self, version: str) -> None:
        os.makedirs(os.path.join(self.root, APP_DIR), exist_ok=True)
        with open(os.path.join(self.root, APP_DIR, "VERSAO"), "w", encoding="utf-8") as fh:
            fh.write(version + "\n")

    def test_copies_when_missing_older_or_damaged(self):
        for on_disk in (None, "0.9.0", "lixo"):
            shutil.rmtree(os.path.join(self.root, APP_DIR), ignore_errors=True)
            if on_disk:
                self.fake_disk_copy(on_disk)
            self.assertTrue(portable.copy_app(self.root), on_disk)
            self.assertEqual(self.app_version_on_disk(), VERSION)

    def test_computer_folders_with_app_like_names_are_never_touched(self):
        for name in (APP_DIR + ".nova", APP_DIR + ".antiga"):
            folder = os.path.join(self.root, name, "Cópia atual", "Pasta pessoal")
            os.makedirs(folder)
            with open(os.path.join(folder, "tese.docx"), "w") as fh:
                fh.write("tese")
        self.fake_disk_copy("0.9.0")
        self.assertTrue(portable.copy_app(self.root))
        for name in (APP_DIR + ".nova", APP_DIR + ".antiga"):
            self.assertTrue(os.path.exists(os.path.join(
                self.root, name, "Cópia atual", "Pasta pessoal", "tese.docx")), name)
        self.assertFalse([n for n in os.listdir(self.root) if n.startswith(".sgtmp-")])

    def test_never_replaces_a_newer_copy(self):
        self.fake_disk_copy("99.0.0")
        self.assertFalse(portable.copy_app(self.root))
        self.assertEqual(self.app_version_on_disk(), "99.0.0")

    def test_skips_when_installed_files_are_another_version(self):
        # The .deb was updated while this (older) process kept running.
        with mock.patch.object(portable, "source_version", return_value="99.0.0"):
            self.assertFalse(portable.copy_app(self.root))
        self.assertFalse(os.path.exists(os.path.join(self.root, APP_DIR)))

    def test_ignores_files_left_by_dpkg_during_an_update(self):
        src = tempfile.mkdtemp(prefix="sg-pkg-")
        self.addCleanup(shutil.rmtree, src, True)
        pkg = os.path.join(src, "salvaguarda")
        shutil.copytree(portable.package_dir(), pkg,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("engine.py.dpkg-new", "engine.py.dpkg-tmp"):
            open(os.path.join(pkg, "core", name), "w").close()
        with mock.patch.object(portable, "package_dir", return_value=pkg):
            self.assertTrue(portable.copy_app(self.root))
        copied = os.listdir(os.path.join(self.root, APP_DIR, "salvaguarda", "core"))
        self.assertFalse([n for n in copied if ".dpkg-" in n])


class ConfigEvolutionTests(SandboxTestCase):
    BASE = {
        "computer_name": "portatil", "configured": True,
        "sources": [{"kind": "xdg", "key": "DOCUMENTS", "enabled": True}],
        "destinations": [{"id": "abc", "name": "Disco", "path": "/media/x/Salvaguarda"}],
        "settings": {"keep_versions_days": 90, "custom_excludes": ["*.tmp"]},
        "version": 1,
    }

    def write_config(self, data) -> None:
        path = cfgmod.config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def read_config(self):
        with open(cfgmod.config_path(), encoding="utf-8") as fh:
            return json.load(fh)

    def test_keys_from_a_newer_version_survive_a_save(self):
        data = json.loads(json.dumps(self.BASE))
        data["agendamento"] = {"diario": True}
        data["settings"]["nova_opcao"] = 5
        data["version"] = cfgmod.CONFIG_VERSION + 1
        self.write_config(data)
        config = cfgmod.load()
        self.assertTrue(config.configured)
        cfgmod.save(config)
        saved = self.read_config()
        self.assertEqual(saved["agendamento"], {"diario": True})
        self.assertEqual(saved["settings"]["nova_opcao"], 5)
        self.assertEqual(saved["settings"]["keep_versions_days"], 90)
        self.assertEqual(saved["version"], cfgmod.CONFIG_VERSION + 1)

    def test_unexpected_types_never_lose_the_whole_config(self):
        variants = {
            "version as text": {"version": "2.0"},
            "sources as dict": {"sources": {"xdg:DOCUMENTS": True}},
            "sources as lists": {"sources": [["xdg", "DOCUMENTS", True]]},
            "settings as list": {"settings": ["x"]},
            "number as text": {"settings": {"keep_versions_days": "90"}},
            "list as text": {"settings": {"custom_excludes": "*.tmp"}},
        }
        for name, change in variants.items():
            data = json.loads(json.dumps(self.BASE))
            data.update(change)
            self.write_config(data)
            config = cfgmod.load()
            self.assertTrue(config.configured, name)
            self.assertEqual(len(config.destinations), 1, name)
            self.assertIsInstance(config.settings.keep_versions_days, int, name)
            self.assertTrue(all(isinstance(x, str) and len(x) > 1 for x in config.settings.custom_excludes), name)
            self.assertFalse(os.path.exists(cfgmod.config_path() + ".danificado"), name)

    def test_adopting_a_newer_snapshot_keeps_its_unknown_keys(self):
        snap = json.loads(json.dumps(self.BASE))
        snap.pop("destinations")
        snap["agenda"] = {"hora": 9}
        snap["settings"]["agendamento_diario"] = True
        snap["settings"]["find_projects"] = False
        config = cfgmod.Config()
        config.adopt_snapshot(snap)
        data = config.to_dict()
        self.assertEqual(data["agenda"], {"hora": 9})
        self.assertIs(data["settings"]["agendamento_diario"], True)
        self.assertIs(data["settings"]["find_projects"], False)
        self.assertEqual(data["destinations"], [])

    def test_folder_names_that_are_not_utf8_can_be_saved(self):
        name = os.fsdecode(b"M\xfasica antiga")  # Latin-1, from an old archive
        config = cfgmod.Config()
        config.add_path(cfgmod.KIND_FOLDER, os.path.join(self.home, name))
        config.settings.ignored_projects.append("~/" + name)
        cfgmod.save(config)
        again = cfgmod.load()
        self.assertEqual(again.sources[-1].key, config.sources[-1].key)
        self.assertEqual(again.settings.ignored_projects, ["~/" + name])
        folder = os.path.dirname(cfgmod.config_path())
        self.assertEqual([f for f in os.listdir(folder) if f.endswith(".tmp")], [])
        # The usual case keeps readable accents.
        config = cfgmod.Config()
        config.computer_name = "computador-joão"
        cfgmod.save(config)
        with open(cfgmod.config_path(), encoding="utf-8") as fh:
            self.assertIn("joão", fh.read())

    def test_top_level_list_does_not_crash(self):
        self.write_config([self.BASE])
        self.assertFalse(cfgmod.load().configured)


class HistoryEvolutionTests(SandboxTestCase):
    ENTRY = {"resultado": "ok", "inicio": "2026-11-01T10:00:00", "fim": "2026-11-01T10:05:00",
             "tipo": "copia", "computador": "portatil"}

    def write_history(self, data) -> None:
        os.makedirs(os.path.dirname(report.history_path()), exist_ok=True)
        with open(report.history_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_entries_that_are_not_objects_are_skipped(self):
        self.write_history([self.ENTRY, "x", 3])
        self.assertEqual(report.load_history(), [self.ENTRY])

    def test_unknown_container_is_not_overwritten(self):
        newer = {"versao": 2, "entradas": [self.ENTRY, self.ENTRY]}
        self.write_history(newer)
        report.append_history(dict(self.ENTRY))
        with open(report.history_path(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), newer)

    def test_dates_of_another_type_are_ignored(self):
        self.assertIsNone(fmt.parse_iso(1730455200))
        self.assertIsNone(fmt.parse_stamp(None))


@unittest.skipUnless(subprocess.run([sys.executable, "-c", "import gi"],
                                    capture_output=True).returncode == 0, "sem python3-gi")
class RunningInstanceTests(unittest.TestCase):
    def test_gui_loads_every_module_at_start(self):
        """Nothing is imported from disk later, when a newer .deb may already be there."""
        code = (
            "import sys, os, pkgutil\n"
            "import gi; gi.require_version('Gtk', '3.0')\n"
            "import salvaguarda, salvaguarda.ui.app\n"
            "root = os.path.dirname(salvaguarda.__file__)\n"
            "names = {'salvaguarda'}\n"
            "for info in pkgutil.walk_packages([root], 'salvaguarda.'):\n"
            "    names.add(info.name)\n"
            "missing = sorted(n for n in names - set(sys.modules)\n"
            "                 if n not in ('salvaguarda.cli', 'salvaguarda.__main__'))\n"
            "print(','.join(missing))\n"
        )
        top = os.path.dirname(os.path.dirname(os.path.abspath(portable.__file__)))
        out = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(top),
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "", "imported lazily: " + out.stdout)


if __name__ == "__main__":
    unittest.main()
