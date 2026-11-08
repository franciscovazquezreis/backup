import os
import shutil
import stat
import unittest

from salvaguarda.core import restore
from salvaguarda.core.config import KIND_FOLDER, KIND_PRESET, KIND_PROJECT
from salvaguarda.core.engine import STATUS_FAILED, STATUS_NOTHING, STATUS_OK
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

    def test_space_is_checked_where_the_files_go(self):
        """A folder on a data partition is restored there even when the home folder is
        nearly full, and is refused when the data partition itself has no room."""
        from unittest import mock

        from salvaguarda.core import fsinfo

        data = os.path.join(self.tmp, "dados", "Fotos")
        os.makedirs(data)
        with open(os.path.join(data, "ferias.jpg"), "w") as fh:
            fh.write("foto")
        self.config.add_path(KIND_FOLDER, data)
        self.backup()
        shutil.rmtree(data)
        items = [i for i in self.items() if i.kind == KIND_FOLDER]
        self.assertEqual(len(items), 1)

        def space(home_free, data_free):
            def usage(path):
                inside_home = os.path.realpath(path).startswith(os.path.realpath(self.home))
                return fsinfo.Usage(10 ** 12, home_free if inside_home else data_free)
            return mock.patch("salvaguarda.core.restore.fsinfo.usage", side_effect=usage)

        with space(home_free=10 ** 6, data_free=10 ** 11):
            result = restore.RestoreEngine(self.layout.current_dir, items).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(read(os.path.join(data, "ferias.jpg")), "foto")

        shutil.rmtree(data)
        with space(home_free=10 ** 11, data_free=10 ** 6):
            result = restore.RestoreEngine(self.layout.current_dir, items).run()
        self.assertEqual(result.status, "failed")
        self.assertIn("espaço", result.message)
        self.assertFalse(os.path.exists(data))

    def test_restore_to_a_folder_inside_the_copy_is_refused(self):
        self.write("Documentos/Atas.txt", "atas", mtime=T0)
        self.write("Documentos/2026/fatura.pdf", "pdf", mtime=T0)
        self.backup()
        self.write("Documentos/Atas.txt", "atas novas", mtime=T0 + 10)
        self.backup()
        before = snapshot(self.layout.root)
        items = [i for i in self.items() if i.label == "Documentos"]
        link = os.path.join(self.tmp, "atalho")
        os.symlink(self.mirror("Documentos"), link)
        run = list_version_runs(self.layout)[0].path
        old_items = [i for i in self.items(run) if i.label == "Documentos"]
        for tree, chosen, folder in (
                (self.layout.current_dir, items, self.mirror("Documentos")),
                (self.layout.current_dir, items, os.path.join(link, "2026")),
                # An old version into «Cópia atual» would change the current copy.
                (run, old_items, self.mirror("Documentos"))):
            result = restore.RestoreEngine(tree, chosen, target=restore.TARGET_FOLDER,
                                           folder=folder).run()
            self.assertEqual(result.status, STATUS_FAILED, folder)
            self.assertIn("dentro da cópia", result.message)
        self.assertEqual(snapshot(self.layout.root), before)

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

    def test_version_saved_a_second_earlier_is_restored(self):
        nota = self.write("Documentos/nota.txt", "versão A", mtime=T0)
        self.backup()
        self.write("Documentos/nota.txt", "versão B", mtime=T0 + 1)  # same size
        self.backup()
        runs = list_version_runs(self.layout)
        self.assertEqual(len(runs), 1)
        docs = [i for i in self.items(runs[0].path) if i.label == "Documentos"]
        result = restore.RestoreEngine(runs[0].path, docs).run()
        self.assertEqual(read(nota), "versão A")
        self.assertEqual(result.stats.replaced_files, 1)
        self.assertEqual(read(os.path.join(result.replaced_dir, "Documentos", "nota.txt")),
                         "versão B")

    def test_close_dates_with_the_same_contents_are_left_alone(self):
        # A restore folder on a disk that keeps coarser dates than the copy disk.
        self.write("Documentos/nota.txt", "versão A", mtime=T0)
        self.backup()
        target = os.path.join(self.tmp, "recuperado")
        docs = [i for i in self.items() if i.label == "Documentos"]
        restore.RestoreEngine(self.layout.current_dir, docs,
                              target=restore.TARGET_FOLDER, folder=target).run()
        restored = os.path.join(target, "Documentos", "nota.txt")
        os.utime(restored, ns=(T0 * 10 ** 9 + 600_000_000,) * 2)
        result = restore.RestoreEngine(self.layout.current_dir, docs,
                                       target=restore.TARGET_FOLDER, folder=target).run()
        self.assertEqual(result.status, STATUS_NOTHING, result.message)
        self.assertEqual(result.stats.identical_files, 1)

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

    def test_version_lists_what_a_narrower_selection_left_behind(self):
        # All of .config was copied, then only .config/Code/User: the version holds the
        # old settings and .config/Code/outro.txt, two levels below the shared folder.
        self.write(".config/Code/User/settings.json", "antigo", mtime=T0)
        self.write(".config/Code/outro.txt", "outro", mtime=T0)
        config_dir = self.config.add_path(KIND_FOLDER, os.path.join(self.home, ".config"))
        self.backup()
        self.config.remove(KIND_FOLDER, config_dir.key)
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, ".config", "Code", "User"))
        self.write(".config/Code/User/settings.json", "novo", mtime=T0 + 100)
        self.backup()
        runs = list_version_runs(self.layout)
        self.assertEqual(len(runs), 1)
        os.remove(os.path.join(self.home, ".config", "Code", "outro.txt"))
        items = self.items(runs[0].path)
        result = restore.RestoreEngine(runs[0].path, items, conflict=restore.CONFLICT_SKIP).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(read(os.path.join(self.home, ".config", "Code", "outro.txt")), "outro")
        self.assertEqual(read(os.path.join(self.home, ".config", "Code", "User", "settings.json")),
                         "novo")

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
        result = restore.RestoreEngine(self.layout.current_dir, items, here_desktop="cinnamon",
                                       dconf_load=lambda p: loaded.append(read(p))).run()
        self.assertEqual(len(loaded), 1)
        self.assertIn("favorite-apps", loaded[0])
        self.assertTrue(any("sessão" in n for n in result.notes))



class CrossDistroRestoreTests(SandboxTestCase):
    """Restoring a copy made on Linux Mint on another system or another user name."""

    def items(self, tree=None):
        return restore.items_for_tree(tree or self.layout.current_dir, self.layout.read_meta())

    def reinstall(self, user="bruno", english=False):
        home = os.path.join(self.tmp, "home", user)
        os.makedirs(os.path.join(home, ".config"))
        names = ("Documents", "Pictures") if english else ("Documentos", "Imagens")
        with open(os.path.join(home, ".config", "user-dirs.dirs"), "w") as fh:
            fh.write('XDG_DOCUMENTS_DIR="$HOME/{}"\nXDG_PICTURES_DIR="$HOME/{}"\n'.format(*names))
        os.environ["HOME"] = home
        return home

    def backup_with_meta(self, **kwargs):
        meta = {"pasta_pessoal": self.home, "utilizador": "ana", "ambiente": "cinnamon",
                "pastas_xdg": {"DOCUMENTS": "~/Documentos", "PICTURES": "~/Imagens"}}
        result = self.backup(meta_extra=meta, **kwargs)
        self.assertIn(result.status, ("ok", "warnings"), result.message)
        return result

    def test_desktop_preset_restores_its_files_and_applies_only_the_export(self):
        from unittest import mock

        self.write(".config/gtk-3.0/bookmarks", "file:///home/ana/Documentos", mtime=T0)
        self.write(".config/mimeapps.list", "[Default Applications]\ntext/html=firefox.desktop\n",
                   mtime=T0)
        self.write(".local/share/applications/app.desktop", "[Desktop Entry]\n", mtime=T0)
        self.config.set_enabled(KIND_PRESET, "desktop", True)

        def fake_dump(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as fh:
                fh.write("[org/cinnamon]\nfavorite-apps=['firefox.desktop']\n\n"
                         "[org/x/editor/preferences/editor]\ntabs-size=uint32 4\n")

        with mock.patch("salvaguarda.core.selection._dconf_available", return_value=True):
            self.backup_with_meta(dconf_dump=fake_dump)
        new_home = self.reinstall()
        loaded, saved = [], []

        def fake_save(target):
            with open(target, "w") as fh:
                fh.write("[org/cinnamon]\nold=true\n")
            saved.append(target)

        items = [i for i in self.items() if i.key == "desktop"]
        self.assertEqual(len(items), 1)
        result = restore.RestoreEngine(self.layout.current_dir, items, here_desktop="gnome",
                                       meta=self.layout.read_meta(), dconf_dump=fake_save,
                                       dconf_load=lambda p: loaded.append(read(p))).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertTrue(os.path.exists(os.path.join(new_home, ".config/gtk-3.0/bookmarks")))
        self.assertTrue(os.path.exists(os.path.join(new_home, ".local/share/applications/app.desktop")))
        self.assertEqual(len(loaded), 1, "only the dconf export is loaded, never mimeapps.list")
        self.assertIn("tabs-size", loaded[0])
        self.assertNotIn("favorite-apps", loaded[0], "Cinnamon settings are not applied on GNOME")
        self.assertEqual(len(saved), 1, "current settings are kept before loading")
        self.assertTrue(saved[0].startswith(result.replaced_dir))
        self.assertTrue(any("GNOME" in n for n in result.notes))

    def test_settings_are_not_loaded_when_current_ones_cannot_be_saved(self):
        from unittest import mock

        self.config.set_enabled(KIND_PRESET, "desktop", True)

        def fake_dump(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as fh:
                fh.write("[org/x/editor/preferences/editor]\ntabs-size=uint32 4\n")

        with mock.patch("salvaguarda.core.selection._dconf_available", return_value=True):
            self.backup_with_meta(dconf_dump=fake_dump)
        self.reinstall()
        loaded = []
        items = [i for i in self.items() if i.is_settings_export]
        result = restore.RestoreEngine(self.layout.current_dir, items, here_desktop="cinnamon",
                                       dconf_dump=lambda t: "sem dconf",
                                       dconf_load=lambda p: loaded.append(p)).run()
        self.assertEqual(loaded, [])
        self.assertTrue(result.issues)

    def test_absolute_links_follow_the_new_home(self):
        self.write("Documentos/script.sh", "#!/bin/sh", mtime=T0)
        os.symlink(os.path.join(self.home, "Documentos", "script.sh"),
                   os.path.join(self.home, "Documentos", "atalho"))
        os.symlink("/usr/bin/env", os.path.join(self.home, "Documentos", "sistema"))
        self.backup_with_meta()
        new_home = self.reinstall()
        items = [i for i in self.items() if i.label == "Documentos"]
        engine = restore.RestoreEngine(self.layout.current_dir, items, meta=self.layout.read_meta())
        self.assertEqual(engine.run().status, STATUS_OK)
        self.assertEqual(os.readlink(os.path.join(new_home, "Documentos", "atalho")),
                         os.path.join(new_home, "Documentos", "script.sh"))
        self.assertEqual(os.readlink(os.path.join(new_home, "Documentos", "sistema")), "/usr/bin/env")
        again = restore.RestoreEngine(self.layout.current_dir, items, meta=self.layout.read_meta())
        self.assertEqual(again.run().status, STATUS_NOTHING)

    def test_links_into_a_renamed_personal_folder_follow_it(self):
        self.write("Documentos/script.sh", "#!/bin/sh", mtime=T0)
        os.symlink(os.path.join(self.home, "Documentos", "script.sh"),
                   os.path.join(self.home, "Documentos", "atalho"))
        self.backup_with_meta()
        new_home = self.reinstall(english=True)
        items = [i for i in self.items() if i.xdg_key == "DOCUMENTS"]
        restore.RestoreEngine(self.layout.current_dir, items, meta=self.layout.read_meta()).run()
        link = os.path.join(new_home, "Documents", "atalho")
        self.assertEqual(os.readlink(link), os.path.join(new_home, "Documents", "script.sh"))
        self.assertTrue(os.path.exists(link))

    def test_old_personal_folder_never_merges_into_another_copied_folder(self):
        self.write("Documentos/notas.txt", "pt", mtime=T0)
        self.write("Documents/notas.txt", "en", mtime=T0)
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, "Documents"))
        self.backup_with_meta()
        new_home = self.reinstall(english=True)
        items = [i for i in self.items() if i.xdg_key == "DOCUMENTS" or i.kind == KIND_FOLDER]
        self.assertEqual(len(items), 2)
        meta = self.layout.read_meta()
        result = restore.RestoreEngine(self.layout.current_dir, items, meta=meta).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(read(os.path.join(new_home, "Documents", "notas.txt")), "en")
        self.assertEqual(read(os.path.join(new_home, "Documentos", "notas.txt")), "pt")
        again = restore.RestoreEngine(self.layout.current_dir, items, meta=meta).run()
        self.assertEqual(again.status, STATUS_NOTHING)

    def test_snap_that_never_ran_gets_no_real_current_folder(self):
        self.write("snap/libreoffice/x5/.config/libreoffice/registo.xcu", "r", mtime=T0)
        os.symlink("x5", os.path.join(self.home, "snap", "libreoffice", "current"))
        self.config.set_enabled(KIND_PRESET, "libreoffice", True)
        self.backup_with_meta()
        new_home = self.reinstall()
        current = os.path.join(new_home, "snap", "libreoffice", "current")
        items = [i for i in self.items() if i.key == "libreoffice"]
        warnings = restore.snap_warnings(items, {})
        self.assertTrue(any("LibreOffice" in w for w in warnings.values()), warnings)
        result = restore.RestoreEngine(self.layout.current_dir, items,
                                       meta=self.layout.read_meta()).run()
        self.assertFalse(os.path.lexists(current))
        self.assertTrue(any("LibreOffice" in i.message for i in result.issues))
        # Once the snap ran, the profile goes through the link snapd made.
        os.makedirs(os.path.join(new_home, "snap", "libreoffice", "x7"))
        os.symlink("x7", current)
        self.assertEqual(restore.snap_warnings(items, {}), {})
        result = restore.RestoreEngine(self.layout.current_dir, items,
                                       meta=self.layout.read_meta()).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertTrue(os.path.islink(current))
        self.assertEqual(read(os.path.join(new_home, "snap/libreoffice/x7/.config/libreoffice/"
                                                     "registo.xcu")), "r")

    def test_whole_snap_item_warns_about_a_snap_that_never_ran(self):
        self.write("snap/libreoffice/x5/.config/libreoffice/registo.xcu", "r", mtime=T0)
        os.symlink("x5", os.path.join(self.home, "snap", "libreoffice", "current"))
        self.config.set_enabled(KIND_PRESET, "libreoffice", True)
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, "snap"))
        self.backup_with_meta()
        new_home = self.reinstall()
        items = [i for i in self.items() if i.kind == KIND_FOLDER]
        self.assertEqual([i.rels for i in items], [["Pasta pessoal/snap"]])
        warnings = restore.snap_warnings(items, {}, tree_root=self.layout.current_dir)
        self.assertTrue(any("LibreOffice" in w for w in warnings.values()), warnings)
        result = restore.RestoreEngine(self.layout.current_dir, items,
                                       meta=self.layout.read_meta()).run()
        self.assertFalse(os.path.lexists(os.path.join(new_home, "snap/libreoffice/current")))
        self.assertTrue(any("LibreOffice" in i.message for i in result.issues), result.issues)

    def test_version_folder_keeps_xdg_folders_apart_like_the_current_copy(self):
        self.write("Documentos/notas.txt", "pt-v1", mtime=T0)
        self.write("Documents/outro.txt", "en", mtime=T0)
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, "Documents"))
        self.backup_with_meta()
        self.write("Documentos/notas.txt", "pt-v2", mtime=T0 + 10)
        self.backup_with_meta()
        new_home = self.reinstall(english=True)
        run = list_version_runs(self.layout)[0].path
        items = [i for i in self.items(run) if i.xdg_key == "DOCUMENTS"]
        restore.RestoreEngine(run, items, meta=self.layout.read_meta()).run()
        self.assertEqual(read(os.path.join(new_home, "Documentos", "notas.txt")), "pt-v1")
        self.assertFalse(os.path.exists(os.path.join(new_home, "Documents", "notas.txt")))

    def test_settings_paths_follow_the_folders_the_files_went_to(self):
        from unittest import mock

        self.write("Imagens/fundo.jpg", "jpg", mtime=T0)
        self.write("Pictures/outra.jpg", "jpg", mtime=T0)
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, "Pictures"))
        self.config.set_enabled(KIND_PRESET, "desktop", True)
        uri = "file://" + os.path.join(self.home, "Imagens", "fundo.jpg")

        def fake_dump(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as fh:
                fh.write("[org/x/background]\npicture-uri='{}'\n".format(uri))

        with mock.patch("salvaguarda.core.selection._dconf_available", return_value=True):
            self.backup_with_meta(dconf_dump=fake_dump)
        new_home = self.reinstall(english=True)
        loaded = []
        items = [i for i in self.items() if i.key == "desktop" or i.xdg_key == "PICTURES"
                 or i.kind == KIND_FOLDER]
        restore.RestoreEngine(self.layout.current_dir, items, here_desktop="cinnamon",
                              meta=self.layout.read_meta(), dconf_dump=lambda t: None,
                              dconf_load=lambda p: loaded.append(read(p))).run()
        self.assertTrue(os.path.exists(os.path.join(new_home, "Imagens", "fundo.jpg")))
        self.assertIn(os.path.join(new_home, "Imagens", "fundo.jpg"), loaded[0])

    def test_old_export_rewrites_the_home_it_records(self):
        from salvaguarda.core import desktop

        self.config.set_enabled(KIND_PRESET, "desktop", True)
        self.backup_with_meta()
        export = os.path.join(self.layout.current_dir, "Extras", "definicoes-do-ambiente-de-trabalho.ini")
        os.makedirs(os.path.dirname(export), exist_ok=True)
        with open(export, "w") as fh:
            fh.write(desktop.dump_header("cinnamon", "/home/ana", {"PICTURES": "~/Imagens"}))
            fh.write("[org/x/editor]\nlast-folder='file:///home/ana/Imagens'\n")
        new_home = self.reinstall(english=True)
        meta = dict(self.layout.read_meta(), pasta_pessoal=new_home,
                    pastas_xdg={"PICTURES": "~/Pictures"}, ambiente="gnome")
        loaded = []
        items = [i for i in restore.items_for_tree(self.layout.current_dir, meta)
                 if i.is_settings_export]
        restore.RestoreEngine(self.layout.current_dir, items, here_desktop="cinnamon", meta=meta,
                              dconf_dump=lambda t: None,
                              dconf_load=lambda p: loaded.append(read(p))).run()
        self.assertIn("file://{}/Pictures".format(new_home), loaded[0])
        self.assertNotIn("/home/ana", loaded[0])

    def test_restore_goes_through_a_link_to_a_folder(self):
        """Dotfiles kept with stow: ~/.config/app is a link to ~/dotfiles/app."""
        self.write(".config/minhaapp/config.toml", "[tools]", mtime=T0)
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, ".config", "minhaapp"))
        self.backup_with_meta()
        new_home = self.reinstall()
        os.makedirs(os.path.join(new_home, "dotfiles", "minhaapp"))
        os.symlink(os.path.join(new_home, "dotfiles", "minhaapp"),
                   os.path.join(new_home, ".config", "minhaapp"))
        items = [i for i in self.items() if i.kind == KIND_FOLDER]
        result = restore.RestoreEngine(self.layout.current_dir, items,
                                       meta=self.layout.read_meta()).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertTrue(os.path.islink(os.path.join(new_home, ".config", "minhaapp")))
        self.assertEqual(read(os.path.join(new_home, "dotfiles", "minhaapp", "config.toml")), "[tools]")

    def test_saved_copies_never_overwrite_each_other(self):
        new_home = self.reinstall()
        engine = restore.RestoreEngine(self.layout.current_dir, [])
        path = os.path.join(new_home, "nota.txt")
        for text in ("1", "2", "3"):
            with open(path, "w") as fh:
                fh.write(text)
            engine._move_aside(path)
        folder = os.path.join(engine.replaced_dir)
        saved = sorted(read(os.path.join(dp, f)) for dp, _, fs in os.walk(folder) for f in fs)
        self.assertEqual(saved, ["1", "2", "3"])

    def test_project_inside_a_personal_folder_follows_the_new_language(self):
        project = os.path.join(self.home, "Documentos", "app")
        self.write("Documentos/app/main.py", "print(1)", mtime=T0)
        self.config.add_path(KIND_PROJECT, project)
        self.config.set_enabled("xdg", "DOCUMENTS", False)
        self.backup_with_meta()
        new_home = self.reinstall(english=True)
        items = [i for i in self.items() if i.kind == KIND_PROJECT]
        restore.RestoreEngine(self.layout.current_dir, items, meta=self.layout.read_meta()).run()
        self.assertTrue(os.path.exists(os.path.join(new_home, "Documents", "app", "main.py")))
        self.assertFalse(os.path.exists(os.path.join(new_home, "Documentos")))

    def test_firefox_profile_goes_to_the_snap_installed_here(self):
        from salvaguarda.core import variants

        self.write(".mozilla/firefox/profiles.ini", "[Profile0]\nPath=a.default\nDefault=1\n", mtime=T0)
        self.write(".mozilla/firefox/a.default/places.sqlite", "marcadores", mtime=T0)
        self.config.set_enabled(KIND_PRESET, "firefox", True)
        self.backup_with_meta()
        new_home = self.reinstall()
        fake_root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(fake_root, "snap", "firefox", "current"))
        items = [i for i in self.items() if i.key == "firefox"]
        plan = restore.plan_variants(items, variants.Probe(new_home, fake_root))
        self.assertEqual([r.destination for r in plan.values()], ["snap/firefox/common/.mozilla"])
        result = restore.RestoreEngine(self.layout.current_dir, items, remaps=plan,
                                       meta=self.layout.read_meta()).run()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(read(os.path.join(new_home, "snap/firefox/common/.mozilla/firefox/"
                                           "a.default/places.sqlite")), "marcadores")
        self.assertFalse(os.path.exists(os.path.join(new_home, ".mozilla")))
        self.assertTrue(any("snap" in n for n in result.notes))

    def test_folder_target_ignores_remaps_and_settings(self):
        from salvaguarda.core import variants

        self.write(".mozilla/firefox/a.default/prefs.js", "x", mtime=T0)
        self.config.set_enabled(KIND_PRESET, "firefox", True)
        self.backup_with_meta()
        self.reinstall()
        target = os.path.join(self.tmp, "ver-primeiro")
        items = [i for i in self.items() if i.key == "firefox"]
        remap = variants.Remap("firefox", "Pasta pessoal/.mozilla", "deb", "snap",
                               "snap/firefox/common/.mozilla")
        restore.RestoreEngine(self.layout.current_dir, items, target=restore.TARGET_FOLDER,
                              folder=target, remaps={remap.rel: remap}).run()
        self.assertTrue(os.path.exists(os.path.join(target, ".mozilla/firefox/a.default/prefs.js")))


if __name__ == "__main__":
    unittest.main()
