import os
import shutil
import stat
import unittest

from salvaguarda.core import restore
from salvaguarda.core.config import KIND_PRESET, KIND_PROJECT
from salvaguarda.core.engine import STATUS_NOTHING, STATUS_OK
from salvaguarda.core.layout import list_version_runs

from .helpers import SandboxTestCase, read

T0 = 1_700_000_000


def snapshot(root):
    """{relative path: (type, mode, mtime_ns, content or link target)} of a tree."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            rel = os.path.relpath(path, root)
            if stat.S_ISLNK(st.st_mode):
                result[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                result[rel] = ("dir", stat.S_IMODE(st.st_mode))
            else:
                with open(path, "rb") as fh:
                    result[rel] = ("file", stat.S_IMODE(st.st_mode), st.st_mtime_ns, fh.read())
    return result


class RestoreTests(SandboxTestCase):
    def populate(self):
        self.write("Documentos/relatório.odt", "conteúdo", mtime=T0)
        self.write("Documentos/2026/março/fatura.pdf", "pdf", mtime=T0 + 1)
        os.chmod(self.write(".ssh/id_ed25519", "privada", mtime=T0), 0o600)
        os.chmod(os.path.join(self.home, ".ssh"), 0o700)
        self.write(".ssh/config", "Host github.com", mtime=T0)
        self.write("Projetos/site/index.html", "<h1>ola</h1>", mtime=T0)
        self.write("Projetos/site/.env", "TOKEN=1", mtime=T0)
        os.symlink("index.html", os.path.join(self.home, "Projetos", "site", "inicio.html"))
        self.write("lista-de-compras.txt", "pão", mtime=T0)
        self.config.set_enabled(KIND_PRESET, "ssh", True)
        self.config.add_path(KIND_PROJECT, os.path.join(self.home, "Projetos"))

    def items(self, tree=None):
        tree = tree or self.layout.current_dir
        return restore.items_for_tree(tree, self.layout.read_meta())

    def reinstall(self, new_user="bruno", english=False):
        """Simulate formatting: a brand new, empty home folder for another user name."""
        new_home = os.path.join(self.tmp, "home", new_user)
        os.makedirs(os.path.join(new_home, ".config"))
        names = ("Documents", "Pictures") if english else ("Documentos", "Imagens")
        with open(os.path.join(new_home, ".config", "user-dirs.dirs"), "w") as fh:
            fh.write('XDG_DOCUMENTS_DIR="$HOME/{}"\n'.format(names[0]))
            fh.write('XDG_PICTURES_DIR="$HOME/{}"\n'.format(names[1]))
        for name in names:
            os.makedirs(os.path.join(new_home, name))
        os.environ["HOME"] = new_home
        return new_home

    def test_full_restore_on_a_new_installation(self):
        self.populate()
        before = snapshot(self.home)
        self.assertEqual(self.backup().status, STATUS_OK)
        new_home = self.reinstall()
        items = self.items()
        labels = {i.label for i in items}
        self.assertTrue({"Documentos", "Chaves SSH", "Projetos"} <= labels, labels)
        result = restore.RestoreEngine(self.layout.current_dir, items).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        after = snapshot(new_home)
        for rel, value in before.items():
            if rel.startswith(".config") or rel in ("Imagens",):
                continue
            self.assertIn(rel, after, rel)
            if value[0] == "file":
                self.assertEqual(after[rel][0], "file", rel)
                self.assertEqual(after[rel][1:], value[1:], rel)
            elif value[0] == "link":
                self.assertEqual(after[rel], value, rel)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(new_home, ".ssh")).st_mode), 0o700)

    def test_personal_folders_follow_the_new_language(self):
        self.populate()
        self.backup()
        new_home = self.reinstall(english=True)
        items = [i for i in self.items() if i.label == "Documentos"]
        result = restore.RestoreEngine(self.layout.current_dir, items).run()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(read(os.path.join(new_home, "Documents", "relatório.odt")), "conteúdo")
        self.assertFalse(os.path.exists(os.path.join(new_home, "Documentos")))

    def test_loose_files_restore_only_loose_files(self):
        self.populate()
        self.backup()
        new_home = self.reinstall()
        items = [i for i in self.items() if i.kind == "loose"]
        self.assertEqual(len(items), 1)
        restore.RestoreEngine(self.layout.current_dir, items).run()
        self.assertTrue(os.path.exists(os.path.join(new_home, "lista-de-compras.txt")))
        self.assertFalse(os.path.exists(os.path.join(new_home, "Projetos")))
        self.assertEqual(os.listdir(os.path.join(new_home, "Documentos")), [])

    def test_restore_to_another_folder(self):
        self.populate()
        self.backup()
        target = os.path.join(self.tmp, "recuperado")
        items = [i for i in self.items() if i.label == "Projetos"]
        result = restore.RestoreEngine(self.layout.current_dir, items,
                                       target=restore.TARGET_FOLDER, folder=target).run()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(read(os.path.join(target, "Projetos", "site", ".env")), "TOKEN=1")

    def test_conflict_policies(self):
        self.populate()
        self.backup()
        mine = os.path.join(self.home, "Documentos", "relatório.odt")
        with open(mine, "w") as fh:
            fh.write("editado depois da cópia")
        os.utime(mine, (T0 + 1000, T0 + 1000))
        docs = [i for i in self.items() if i.label == "Documentos"]

        result = restore.RestoreEngine(self.layout.current_dir, docs,
                                       conflict=restore.CONFLICT_SKIP).run()
        self.assertEqual(read(mine), "editado depois da cópia")
        self.assertEqual(result.stats.kept_existing, 1)

        result = restore.RestoreEngine(self.layout.current_dir, docs,
                                       conflict=restore.CONFLICT_NEWER).run()
        self.assertEqual(read(mine), "editado depois da cópia")

        result = restore.RestoreEngine(self.layout.current_dir, docs,
                                       conflict=restore.CONFLICT_REPLACE).run()
        self.assertEqual(read(mine), "conteúdo")
        self.assertEqual(result.stats.replaced_files, 1)
        saved = os.path.join(result.replaced_dir, "Documentos", "relatório.odt")
        self.assertEqual(read(saved), "editado depois da cópia")

    def test_second_restore_changes_nothing(self):
        self.populate()
        self.backup()
        self.reinstall()
        items = self.items()
        restore.RestoreEngine(self.layout.current_dir, items).run()
        result = restore.RestoreEngine(self.layout.current_dir, items).run()
        self.assertEqual(result.status, STATUS_NOTHING, result.message)

    def test_recover_deleted_file_from_versions(self):
        self.populate()
        self.backup()
        os.remove(os.path.join(self.home, "Documentos", "relatório.odt"))
        self.write("Documentos/novo.txt", "n", mtime=T0)
        self.backup()
        runs = list_version_runs(self.layout)
        self.assertEqual(len(runs), 1)
        items = self.items(runs[0].path)
        self.assertTrue(items)
        result = restore.RestoreEngine(runs[0].path, items, conflict=restore.CONFLICT_SKIP).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(read(os.path.join(self.home, "Documentos", "relatório.odt")), "conteúdo")

    def test_desktop_settings_are_applied_with_dconf(self):
        from unittest import mock

        from salvaguarda.core import selection

        self.config.set_enabled(KIND_PRESET, "desktop", True)

        def fake_dump(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as fh:
                fh.write("[org/cinnamon]\nfavorite-apps=['firefox.desktop']\n")

        with mock.patch("salvaguarda.core.selection._dconf_available", return_value=True):
            self.backup(dconf_dump=fake_dump)
        loaded = []
        items = [i for i in self.items() if i.is_settings_export]
        self.assertEqual(len(items), 1)
        result = restore.RestoreEngine(self.layout.current_dir, items,
                                       dconf_load=lambda p: loaded.append(read(p))).run()
        self.assertEqual(len(loaded), 1)
        self.assertIn("favorite-apps", loaded[0])
        self.assertTrue(any("sessão" in n for n in result.notes))


if __name__ == "__main__":
    unittest.main()
