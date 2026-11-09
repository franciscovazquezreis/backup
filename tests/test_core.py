import datetime as _dt
import json
import os
import unittest

from salvaguarda.core import catalog, config as cfgmod, fmt, paths, selection
from salvaguarda.core.config import KIND_FOLDER, KIND_PRESET, KIND_PROJECT, KIND_XDG
from salvaguarda.core.exclusions import Exclusions, glob_to_regex, validate_custom

from .helpers import SandboxTestCase


class FormatTests(unittest.TestCase):
    def test_sizes_use_portuguese_conventions(self):
        self.assertEqual(fmt.size(0), "0 bytes")
        self.assertEqual(fmt.size(1), "1 byte")
        self.assertEqual(fmt.size(1234), "1,2 kB")
        self.assertEqual(fmt.size(1_500_000_000), "1,5 GB")
        self.assertEqual(fmt.size(250_000_000_000), "250 GB")

    def test_numbers_and_plurals(self):
        self.assertEqual(fmt.number(1234567), "1 234 567")
        self.assertEqual(fmt.files(1), "1 ficheiro")
        self.assertEqual(fmt.files(3), "3 ficheiros")

    def test_relative_dates(self):
        now = _dt.datetime(2026, 10, 16, 15, 30)
        self.assertEqual(fmt.relative(now - _dt.timedelta(minutes=5), now), "há 5 minutos")
        self.assertEqual(fmt.relative(_dt.datetime(2026, 10, 16, 9, 5), now), "hoje às 09:05")
        self.assertEqual(fmt.relative(_dt.datetime(2026, 10, 15, 22, 0), now), "ontem às 22:00")
        self.assertEqual(fmt.relative(_dt.datetime(2026, 10, 10, 9, 0), now), "há 6 dias")
        self.assertEqual(fmt.relative(_dt.datetime(2026, 3, 2, 9, 0), now), "2 de março de 2026")
        self.assertEqual(fmt.relative(None, now), "nunca")

    def test_durations(self):
        self.assertEqual(fmt.duration(42), "42 s")
        self.assertEqual(fmt.duration(125), "2 min 5 s")
        self.assertEqual(fmt.duration(3720), "1 h 2 min")
        self.assertEqual(fmt.remaining(5), "quase a terminar")
        self.assertEqual(fmt.remaining(600), "cerca de 10 minutos")


class GlobTests(unittest.TestCase):
    def match(self, pattern, text):
        import re
        return re.match(glob_to_regex(pattern) + r"\Z", text) is not None

    def test_star_does_not_cross_folders(self):
        self.assertTrue(self.match("*.iso", "ubuntu.iso"))
        self.assertFalse(self.match("a/*.iso", "a/b/ubuntu.iso"))
        self.assertTrue(self.match("a/**/x", "a/b/c/x"))
        self.assertTrue(self.match("a/**/x", "a/x"))
        self.assertTrue(self.match("[!a]bc", "xbc"))
        self.assertFalse(self.match("[!a]bc", "abc"))
        self.assertTrue(self.match("~$*", "~$documento.docx"))


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.ex = Exclusions()

    def test_names_anywhere(self):
        self.assertIsNotNone(self.ex.match("node_modules", "/h/p/node_modules", "p/node_modules", True))
        self.assertIsNone(self.ex.match("node_modules", "/h/p/node_modules", "p/node_modules", False))
        self.assertIsNotNone(self.ex.match("x.pyc", "/h/x.pyc", "x.pyc", False))
        self.assertIsNone(self.ex.match(".git", "/h/p/.git", "p/.git", True))
        self.assertIsNone(self.ex.match(".env", "/h/p/.env", "p/.env", False))

    def test_home_anchored_rules(self):
        self.assertIsNotNone(self.ex.match(".npm", "/h/.npm", ".npm", True))
        self.assertIsNone(self.ex.match(".npm", "/h/Projetos/.npm", "Projetos/.npm", True))
        self.assertIsNotNone(self.ex.match("Cache", "/h/.config/google-chrome/Default/Cache",
                                           ".config/google-chrome/Default/Cache", True))
        self.assertIsNone(self.ex.match("Cache", "/h/Documentos/Cache", "Documentos/Cache", True))
        self.assertIsNotNone(self.ex.match(
            "cache", "x", ".mozilla/firefox/abc.default/storage/default/site/cache", True))
        self.assertIsNotNone(self.ex.match("ms-python", "x", ".vscode/extensions/ms-python", True))
        self.assertIsNone(self.ex.match("extensions.json", "x", ".vscode/extensions/extensions.json", False))

    def test_categories_can_be_disabled(self):
        ex = Exclusions(caches=False, dev=False, tools=False)
        self.assertIsNone(ex.match("node_modules", "/h/node_modules", "node_modules", True))
        self.assertIsNone(ex.match(".cache", "/h/.cache", ".cache", True))
        self.assertIsNotNone(ex.match("salvaguarda", "x", ".local/state/salvaguarda", True))

    def test_markers(self):
        self.assertEqual(self.ex.match_listing("/p/venv", {"pyvenv.cfg", "lib"})[0], "dev")
        self.assertIsNone(self.ex.match_listing("/p/src", {"main.py"}))

    def test_custom_patterns(self):
        ex = Exclusions(custom=["*.iso", "Transferências/Filmes/", "/media/dados/tmp"])
        self.assertIsNotNone(ex.match("a.iso", "/h/a.iso", "a.iso", False))
        self.assertIsNotNone(ex.match("Filmes", "/h/Transferências/Filmes", "Transferências/Filmes", True))
        self.assertIsNone(ex.match("Filmes", "/h/Transferências/Filmes", "Transferências/Filmes", False))
        self.assertIsNotNone(ex.match("tmp", "/media/dados/tmp", None, True))
        self.assertEqual(validate_custom(["", "# nota", "*", "/", "*.tmp"]), ["*.tmp"])


class ConfigTests(SandboxTestCase):
    def test_round_trip_and_damaged_file(self):
        self.config.set_enabled(KIND_PRESET, "firefox", False)
        self.config.settings.custom_excludes = ["*.iso"]
        cfgmod.save(self.config)
        loaded = cfgmod.load()
        self.assertEqual(loaded.find(KIND_PRESET, "firefox").enabled, False)
        self.assertEqual(loaded.settings.custom_excludes, ["*.iso"])
        self.assertEqual(loaded.destinations[0].name, "Disco de teste")
        with open(cfgmod.config_path(), "w") as fh:
            fh.write("{ isto não é json")
        fresh = cfgmod.load()
        self.assertEqual(fresh.destinations, [])
        self.assertTrue(os.path.exists(cfgmod.config_path() + ".danificado"))

    def test_unknown_keys_are_ignored(self):
        data = self.config.to_dict()
        data["futuro"] = 1
        data["settings"]["opcao_nova"] = True
        cfg = cfgmod.Config.from_dict(json.loads(json.dumps(data)))
        self.assertEqual(cfg.computer_name, "portatil")

    def test_portable_paths(self):
        self.assertEqual(paths.to_portable(os.path.join(self.home, "Projetos")), "~/Projetos")
        self.assertEqual(paths.from_portable("~/Projetos"), os.path.join(self.home, "Projetos"))
        self.assertEqual(paths.to_portable("/media/dados"), "/media/dados")

    def test_blocked_sources(self):
        self.assertTrue(paths.is_blocked_source("/"))
        self.assertTrue(paths.is_blocked_source("/etc/fstab"))
        self.assertFalse(paths.is_blocked_source("/run/media/ana/disco"))
        self.assertFalse(paths.is_blocked_source("/media/dados"))


class CandidateTests(SandboxTestCase):
    def test_detects_folders_projects_and_presets(self):
        os.makedirs(os.path.join(self.home, "Projetos", "app", ".git"))
        os.makedirs(os.path.join(self.home, "Calibre Library"))
        os.makedirs(os.path.join(self.home, ".mozilla", "firefox"))
        os.makedirs(os.path.join(self.home, ".ssh"))
        with open(os.path.join(self.home, "notas.txt"), "w") as fh:
            fh.write("n")
        cands = selection.build_candidates(self.config)
        by_key = {(c.kind, c.key): c for c in cands}
        self.assertTrue(by_key[(KIND_XDG, "DOCUMENTS")].enabled)
        self.assertTrue(by_key[(KIND_PROJECT, "~/Projetos")].enabled)
        self.assertFalse(by_key[(KIND_FOLDER, "~/Calibre Library")].enabled)
        self.assertTrue(by_key[(KIND_PRESET, "firefox")].enabled)
        self.assertTrue(by_key[(KIND_PRESET, "ssh")].enabled)
        self.assertNotIn((KIND_PRESET, "thunderbird"), by_key)
        self.assertFalse(by_key[(KIND_PRESET, "config_all")].enabled)

    def test_resolution_order_and_missing_sources(self):
        os.makedirs(os.path.join(self.home, ".ssh"))
        os.makedirs(os.path.join(self.home, "Projetos", "x", ".git"))
        self.config.add_path(KIND_FOLDER, "/caminho/que/nao/existe")
        res = selection.resolve(self.config)
        kinds = [s.kind for s in res.sources]
        self.assertEqual(kinds[0], "preset")
        self.assertLess(kinds.index("project"), kinds.index("xdg"))
        self.assertEqual([s.path for s in res.missing if s.kind == "folder"],
                         ["/caminho/que/nao/existe"])
        self.assertIn("Outras localizações/caminho/que/nao/existe", res.keep)

    def test_freeze_keeps_heuristic_choices_stable(self):
        from salvaguarda.core import controller
        os.makedirs(os.path.join(self.home, "Projetos", "x", ".git"))
        controller.freeze_choices(self.config)
        os.rmdir(os.path.join(self.home, "Projetos", "x", ".git"))
        cands = {(c.kind, c.key): c for c in selection.build_candidates(self.config)}
        self.assertTrue(cands[(KIND_PROJECT, "~/Projetos")].enabled)


class CatalogTests(unittest.TestCase):
    def test_presets_are_consistent(self):
        ids = [p.id for p in catalog.PRESETS]
        self.assertEqual(len(ids), len(set(ids)))
        groups = {g for g, _ in catalog.GROUPS}
        for preset in catalog.PRESETS:
            self.assertIn(preset.group, groups)
            for path in preset.paths:
                self.assertFalse(path.startswith(("/", "~")), path)
            self.assertNotIn("—", preset.description)


if __name__ == "__main__":
    unittest.main()
