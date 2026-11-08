import contextlib
import datetime as _dt
import os
import stat
import threading
import unittest
from unittest import mock

from salvaguarda.core import controller, fileops, fsinfo, selection
from salvaguarda.core.config import KIND_FOLDER, KIND_PRESET, KIND_PROJECT, KIND_XDG
from salvaguarda.core.engine import (STATUS_CANCELLED, STATUS_FAILED, STATUS_NOTHING, STATUS_OK,
                                     STATUS_WARNINGS, Callbacks)
from salvaguarda.core.layout import DiskLayout, list_version_runs

from .helpers import RecordingCallbacks, SandboxTestCase, read

T0 = 1_700_000_000  # fixed modification time used in tests
IS_ROOT = os.geteuid() == 0


class FirstBackupTests(SandboxTestCase):
    def test_copies_files_with_dates_modes_and_links(self):
        doc = self.write("Documentos/relatório final.txt", "versão 1", mtime=T0)
        script = self.write("Documentos/scripts/correr.sh", "#!/bin/sh\necho ok\n", mtime=T0 + 5)
        os.chmod(script, 0o750)
        secret = self.write("Documentos/segredo.txt", "s", mtime=T0)
        os.chmod(secret, 0o600)
        os.symlink("relatório final.txt", os.path.join(self.home, "Documentos", "atalho.txt"))
        os.symlink("/nao/existe", os.path.join(self.home, "Documentos", "partida"))
        folder = os.path.join(self.home, "Documentos", "scripts")
        os.utime(folder, (T0 - 100, T0 - 100))

        result = self.backup()
        self.assertEqual(result.status, STATUS_OK, result.message)

        copy = self.mirror("Documentos/relatório final.txt")
        self.assertEqual(read(copy), "versão 1")
        self.assertEqual(os.stat(copy).st_mtime_ns, os.stat(doc).st_mtime_ns)
        self.assertEqual(stat.S_IMODE(os.stat(self.mirror("Documentos/scripts/correr.sh")).st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(os.stat(self.mirror("Documentos/segredo.txt")).st_mode), 0o600)
        self.assertEqual(os.readlink(self.mirror("Documentos/atalho.txt")), "relatório final.txt")
        self.assertEqual(os.readlink(self.mirror("Documentos/partida")), "/nao/existe")
        self.assertEqual(os.stat(self.mirror("Documentos/scripts")).st_mtime_ns,
                         os.stat(folder).st_mtime_ns)
        self.assertEqual(result.stats.new_files, 3)
        self.assertEqual(result.stats.links, 2)
        self.assertIsNone(result.version_dir)
        self.assertTrue(os.path.isfile(result.log_path))
        status = self.layout.read_status()
        self.assertFalse(status["em_curso"])
        self.assertIn("ultima_copia_concluida", status)

    def test_second_run_has_nothing_to_do(self):
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.write("Imagens/b.jpg", "b", mtime=T0)
        self.assertEqual(self.backup().status, STATUS_OK)
        result = self.backup()
        self.assertEqual(result.status, STATUS_NOTHING)
        self.assertEqual(self.version_runs(), [])

    def test_no_temporary_files_left_behind(self):
        for i in range(20):
            self.write("Documentos/f{}.txt".format(i), "x" * i, mtime=T0)
        self.backup()
        leftovers = []
        for dirpath, dirnames, filenames in os.walk(self.layout.computer_dir):
            leftovers += [n for n in filenames + dirnames if n.startswith(".sgtmp-")]
        self.assertEqual(leftovers, [])

    def test_special_files_are_skipped(self):
        self.write("Documentos/normal.txt", "n", mtime=T0)
        os.mkfifo(os.path.join(self.home, "Documentos", "pipe"))
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertFalse(os.path.lexists(self.mirror("Documentos/pipe")))
        self.assertEqual(result.stats.special_skipped, 1)

    def test_undecodable_file_names(self):
        raw = os.path.join(os.fsencode(self.home), b"Documentos", b"caf\xe9.txt")
        with open(raw, "wb") as fh:
            fh.write(b"latin-1")
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        copy = os.path.join(os.fsencode(self.layout.current_dir), "Pasta pessoal".encode(),
                            b"Documentos", b"caf\xe9.txt")
        with open(copy, "rb") as fh:
            self.assertEqual(fh.read(), b"latin-1")
        with open(result.log_path, encoding="utf-8") as fh:
            fh.read()

    def test_read_only_folders_can_be_updated_later(self):
        self.write("Documentos/fechada/a.txt", "1", mtime=T0)
        folder = os.path.join(self.home, "Documentos", "fechada")
        os.chmod(folder, 0o555)
        self.assertEqual(self.backup().status, STATUS_OK)
        self.assertEqual(stat.S_IMODE(os.stat(self.mirror("Documentos/fechada")).st_mode), 0o555)
        os.chmod(folder, 0o755)
        self.write("Documentos/fechada/b.txt", "2", mtime=T0)
        os.remove(os.path.join(folder, "a.txt"))
        os.chmod(folder, 0o555)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK, [i.message for i in result.issues])
        self.assertTrue(os.path.exists(self.mirror("Documentos/fechada/b.txt")))
        self.assertFalse(os.path.exists(self.mirror("Documentos/fechada/a.txt")))
        self.assertEqual(len(self.find_in_versions("Documentos/fechada/a.txt")), 1)


class VersionTests(SandboxTestCase):
    def test_changed_file_keeps_previous_version(self):
        path = self.write("Documentos/notas.txt", "antes", mtime=T0)
        self.backup()
        with open(path, "w") as fh:
            fh.write("depois, mais comprido")
        os.utime(path, (T0 + 60, T0 + 60))
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(read(self.mirror("Documentos/notas.txt")), "depois, mais comprido")
        old = self.find_in_versions("Documentos/notas.txt")
        self.assertEqual(len(old), 1)
        self.assertEqual(read(old[0]), "antes")
        self.assertEqual(result.stats.changed_files, 1)
        self.assertEqual(result.stats.archived_files, 1)

    def test_same_size_but_new_date_is_copied(self):
        path = self.write("Documentos/n.txt", "aaaa", mtime=T0)
        self.backup()
        with open(path, "w") as fh:
            fh.write("bbbb")
        os.utime(path, (T0 + 1, T0 + 1))
        self.backup()
        self.assertEqual(read(self.mirror("Documentos/n.txt")), "bbbb")

    def test_deleted_file_and_folder_go_to_versions(self):
        self.write("Documentos/fica.txt", "1", mtime=T0)
        self.write("Documentos/sai.txt", "2", mtime=T0)
        self.write("Documentos/pasta/x.txt", "3", mtime=T0)
        self.write("Documentos/pasta/sub/y.txt", "4", mtime=T0)
        self.backup()
        os.remove(os.path.join(self.home, "Documentos", "sai.txt"))
        import shutil
        shutil.rmtree(os.path.join(self.home, "Documentos", "pasta"))
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertFalse(os.path.exists(self.mirror("Documentos/sai.txt")))
        self.assertFalse(os.path.exists(self.mirror("Documentos/pasta")))
        self.assertTrue(os.path.exists(self.mirror("Documentos/fica.txt")))
        self.assertEqual(len(self.find_in_versions("Documentos/sai.txt")), 1)
        self.assertEqual(len(self.find_in_versions("Documentos/pasta/sub/y.txt")), 1)
        self.assertEqual(result.stats.removed_files, 3)

    def test_type_change_file_to_folder(self):
        path = self.write("Documentos/coisa", "ficheiro", mtime=T0)
        self.backup()
        os.remove(path)
        self.write("Documentos/coisa/dentro.txt", "pasta", mtime=T0)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(os.path.isdir(self.mirror("Documentos/coisa")))
        self.assertEqual(read(self.find_in_versions("Documentos/coisa")[0]), "ficheiro")

    def test_large_settings_files_are_not_versioned(self):
        self.config.settings.preset_versions_max_mb = 0
        key = self.write(".ssh/id_ed25519", "chave-1", mtime=T0)
        self.config.set_enabled(KIND_PRESET, "ssh", True)
        self.backup()
        with open(key, "w") as fh:
            fh.write("chave-2")
        os.utime(key, (T0 + 9, T0 + 9))
        result = self.backup()
        self.assertEqual(read(self.mirror(".ssh/id_ed25519")), "chave-2")
        self.assertEqual(self.find_in_versions(".ssh/id_ed25519"), [])
        self.assertEqual(result.stats.dropped, 1)

    def test_old_version_folders_are_pruned(self):
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.backup()
        old = os.path.join(self.layout.versions_dir, "2020-01-01_10-00-00")
        recent = os.path.join(self.layout.versions_dir,
                              (_dt.datetime.now() - _dt.timedelta(days=2)).strftime("%Y-%m-%d_%H-%M-%S"))
        for folder in (old, recent):
            os.makedirs(os.path.join(folder, "Pasta pessoal"))
            with open(os.path.join(folder, "Pasta pessoal", "x"), "w") as fh:
                fh.write("x")
        self.write("Documentos/b.txt", "b", mtime=T0)
        result = self.backup(keep_versions_days=30)
        self.assertEqual(result.status, STATUS_OK)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))
        self.assertIn("2020-01-01_10-00-00", result.pruned)


class SafetyTests(SandboxTestCase):
    def test_missing_source_keeps_its_copy(self):
        self.config.add_path(KIND_PROJECT, os.path.join(self.home, "Projetos"))
        self.write("Projetos/app/main.py", "print(1)", mtime=T0)
        self.backup()
        os.rename(os.path.join(self.home, "Projetos"), os.path.join(self.home, "Projetos-antigo"))
        self.config.set_enabled(KIND_FOLDER, "~/Projetos-antigo", False)
        result = self.backup()
        self.assertTrue(os.path.exists(self.mirror("Projetos/app/main.py")))
        self.assertEqual(self.find_in_versions("Projetos/app/main.py"), [])
        self.assertTrue(any("Não encontrado" in i.message for i in result.issues))

    def test_missing_xdg_folder_keeps_its_copy(self):
        self.write("Documentos/a.txt", "a", mtime=T0)
        controller.freeze_choices(self.config)  # as the app does before each copy
        self.backup()
        os.rename(os.path.join(self.home, "Documentos"), os.path.join(self.home, "Documentos-antigo"))
        controller.freeze_choices(self.config)
        result = self.backup()
        self.assertTrue(os.path.exists(self.mirror("Documentos/a.txt")))
        self.assertEqual(self.find_in_versions("Documentos/a.txt"), [])
        self.assertTrue(any("Não encontrado" in i.message for i in result.issues))

    @unittest.skipIf(IS_ROOT, "root reads everything")
    def test_unreadable_folder_keeps_its_copy(self):
        self.write("Documentos/privada/a.txt", "a", mtime=T0)
        self.backup()
        folder = os.path.join(self.home, "Documentos", "privada")
        os.chmod(folder, 0)
        try:
            result = self.backup()
        finally:
            os.chmod(folder, 0o755)
        self.assertEqual(result.status, STATUS_WARNINGS)
        self.assertTrue(os.path.exists(self.mirror("Documentos/privada/a.txt")))
        self.assertEqual(self.find_in_versions("Documentos/privada/a.txt"), [])

    @unittest.skipIf(IS_ROOT, "root reads everything")
    def test_unreadable_file_is_reported_and_others_copied(self):
        self.write("Documentos/ok.txt", "ok", mtime=T0)
        bad = self.write("Documentos/bloqueado.txt", "x", mtime=T0)
        os.chmod(bad, 0)
        result = self.backup()
        self.assertEqual(result.status, STATUS_WARNINGS)
        self.assertTrue(os.path.exists(self.mirror("Documentos/ok.txt")))
        self.assertTrue(any("bloqueado" in i.path for i in result.issues))

    def test_mass_deletion_asks_before_touching_anything(self):
        for i in range(150):
            self.write("Documentos/f{:03}.txt".format(i), "x", mtime=T0)
        self.backup()
        for i in range(100):
            os.remove(os.path.join(self.home, "Documentos", "f{:03}.txt".format(i)))
        cb = RecordingCallbacks()
        result = self.backup(cb)
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual([q.kind for q in cb.questions], ["mass_changes"])
        self.assertTrue(os.path.exists(self.mirror("Documentos/f000.txt")))
        cb = RecordingCallbacks({"mass_changes": "continue"})
        result = self.backup(cb)
        self.assertEqual(result.status, STATUS_OK)
        self.assertFalse(os.path.exists(self.mirror("Documentos/f000.txt")))
        self.assertEqual(len(self.find_in_versions("Documentos/f000.txt")), 1)

    def test_other_installation_must_be_confirmed(self):
        from salvaguarda.core import paths
        if not paths.machine_id():
            self.skipTest("no machine-id on this system")
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.backup()
        meta = self.layout.read_meta()
        meta["machine_id"] = "outra-instalacao"
        self.layout.write_meta(meta)
        cb = RecordingCallbacks()
        self.write("Documentos/b.txt", "b", mtime=T0)
        result = self.backup(cb)
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual([q.kind for q in cb.questions], ["adopt"])
        self.assertFalse(os.path.exists(self.mirror("Documentos/b.txt")))
        result = self.backup(RecordingCallbacks({"adopt": "adopt"}))
        self.assertEqual(result.status, STATUS_OK)
        self.assertNotEqual(self.layout.read_meta()["machine_id"], "outra-instalacao")

    def test_cancel_during_copy_is_clean_and_resumable(self):
        for i in range(30):
            self.write("Documentos/f{:02}.bin".format(i), "y" * 50_000, mtime=T0)
        cancel = threading.Event()

        class Stopper(Callbacks):
            def __init__(self):
                self.seen = 0

            def progress(self, progress):
                if progress.phase == "copy" and progress.files_done >= 5:
                    cancel.set()

        engine = self.engine(Stopper())
        engine.cancel_event = cancel
        with mock.patch("salvaguarda.core.engine.PROGRESS_INTERVAL", 0):
            result = engine.run()
        self.assertEqual(result.status, STATUS_CANCELLED)
        for dirpath, dirnames, filenames in os.walk(self.layout.current_dir):
            self.assertFalse([n for n in filenames if n.startswith(".sgtmp-")])
        self.assertFalse(self.layout.read_status()["em_curso"])
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(len(os.listdir(self.mirror("Documentos"))), 30)

    def test_interrupted_run_is_detected(self):
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.backup()
        status = self.layout.read_status()
        status["em_curso"] = True
        self.layout.write_status(status)
        self.write("Documentos/b.txt", "b", mtime=T0)
        result = self.backup()
        self.assertTrue(any("interrompida" in n for n in result.notes))

    def test_not_enough_space_stops_before_copying(self):
        self.write("Documentos/grande.bin", "z" * 1000, mtime=T0)
        old = os.path.join(self.layout.versions_dir, "2020-01-01_10-00-00")
        os.makedirs(old)
        fake = fsinfo.Usage(total=10**12, free=1000)
        with mock.patch("salvaguarda.core.engine.fsinfo.usage", return_value=fake):
            result = self.backup()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("espaço", result.message)
        self.assertFalse(os.path.exists(old), "old versions are pruned to make room")
        self.assertFalse(os.path.exists(self.mirror("Documentos/grande.bin")))

    def test_leftover_temporary_files_are_cleaned(self):
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.backup()
        junk = self.mirror("Documentos/.sgtmp-abcdef")
        with open(junk, "w") as fh:
            fh.write("meio")
        self.write("Documentos/b.txt", "b", mtime=T0)
        self.backup()
        self.assertFalse(os.path.exists(junk))
        self.assertEqual(self.find_in_versions("Documentos/.sgtmp-abcdef"), [])

    def test_destination_inside_a_source_is_not_copied(self):
        inner_disk = os.path.join(self.home, "Cópias")
        os.makedirs(inner_disk)
        from salvaguarda.core import controller
        self.config.destinations = []
        self.dest = controller.register_destination(self.config, inner_disk, "Interno")
        self.root = os.path.join(inner_disk, "Salvaguarda")
        self.config.add_path(KIND_FOLDER, self.home)
        self.write("Documentos/a.txt", "a", mtime=T0)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertFalse(os.path.exists(self.mirror("Cópias/Salvaguarda")))
        self.assertTrue(os.path.exists(self.mirror("Documentos/a.txt")))


class SelectionBehaviourTests(SandboxTestCase):
    def test_disabled_item_leaves_the_mirror_into_versions(self):
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.write("Imagens/foto.jpg", "f", mtime=T0)
        self.backup()
        self.config.set_enabled(KIND_XDG, "PICTURES", False)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertFalse(os.path.exists(self.mirror("Imagens")))
        self.assertEqual(len(self.find_in_versions("Imagens/foto.jpg")), 1)
        self.assertTrue(os.path.exists(self.mirror("Documentos/a.txt")))
        self.assertEqual(result.stats.orphans, 1)

    def test_removing_a_big_item_asks_first(self):
        for i in range(250):
            self.write("Imagens/f{:03}.jpg".format(i), "x", mtime=T0)
        self.backup()
        self.config.set_enabled(KIND_XDG, "PICTURES", False)
        cb = RecordingCallbacks()
        result = self.backup(cb)
        self.assertEqual([q.kind for q in cb.questions], ["orphans"])
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertTrue(os.path.exists(self.mirror("Imagens/f000.jpg")))
        result = self.backup(RecordingCallbacks({"orphans": "continue"}))
        self.assertEqual(result.status, STATUS_OK)
        self.assertFalse(os.path.exists(self.mirror("Imagens")))
        self.assertEqual(result.stats.orphan_files, 250)

    def test_loose_files_in_home(self):
        self.write("lista.txt", "l", mtime=T0)
        self.write(".escondido", "h", mtime=T0)
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.backup()
        self.assertTrue(os.path.exists(self.mirror("lista.txt")))
        self.assertFalse(os.path.exists(self.mirror(".escondido")))
        os.remove(os.path.join(self.home, "lista.txt"))
        result = self.backup()
        self.assertFalse(os.path.exists(self.mirror("lista.txt")))
        self.assertEqual(len(self.find_in_versions("lista.txt")), 1)
        self.assertTrue(os.path.exists(self.mirror("Documentos/a.txt")))
        self.assertEqual(result.stats.orphans, 0)

    def test_nested_sources_are_copied_once(self):
        self.config.add_path(KIND_FOLDER, self.home)
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.write(".config/app/c.ini", "c", mtime=T0)
        res = selection.resolve(self.config)
        self.assertEqual([s.path for s in res.sources if not s.extra], [self.home])
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.stats.new_files, 3)  # a.txt, c.ini and user-dirs.dirs

    def test_selected_disk_mounted_inside_a_selected_folder_is_copied(self):
        # «Documentos» and a disk mounted at «Documentos/Disco»: «Documentos» stops at the
        # other disk, so the disk is copied as its own item and not dropped as nested.
        docs = os.path.join(self.home, "Documentos")
        disk = os.path.join(docs, "Disco")
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.write("Documentos/Disco/fotos.txt", "f", mtime=T0)
        self.config.add_path(KIND_FOLDER, disk)
        real_stat = os.stat
        extra = ("st_atime", "st_mtime", "st_ctime", "st_atime_ns", "st_mtime_ns",
                 "st_ctime_ns", "st_blksize", "st_blocks", "st_rdev")

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if isinstance(path, str) and os.path.normpath(path) == docs:
                fields = list(st[:10])
                fields[2] = st.st_dev + 1  # «Documentos» is on another disk than «Disco»
                return os.stat_result(fields, {k: getattr(st, k) for k in extra})
            return st

        with mock.patch("os.stat", side_effect=fake_stat):
            res = selection.resolve(self.config)
            self.assertIn(disk, [s.path for s in res.sources])
            self.assertNotIn(disk, [s.path for s in res.nested])
            result = self.backup()
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(read(self.mirror("Documentos/Disco/fotos.txt")), "f")
        self.assertEqual(read(self.mirror("Documentos/a.txt")), "a")
        self.assertFalse([n for n in result.notes if "outro disco" in n])

    def test_single_file_source(self):
        path = self.write("notas/importante.md", "# nota", mtime=T0)
        self.config.add_path(KIND_FOLDER, path)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(read(self.mirror("notas/importante.md")), "# nota")


class CaseInsensitiveDiskTests(SandboxTestCase):
    """exFAT and FAT32 disks (allowed after a warning) do not tell RELATORIO from relatorio."""

    def _case_insensitive_disk(self):
        real_rename, real_replace, real_lexists = os.rename, os.replace, os.path.lexists
        disk = self.disk + os.sep

        def resolve(path):
            if not isinstance(path, str) or not path.startswith(disk) or real_lexists(path):
                return path
            parent, name = os.path.split(path)
            try:
                names = os.listdir(parent)
            except OSError:
                return path
            for other in names:
                if other.casefold() == name.casefold():
                    return os.path.join(parent, other)
            return path

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch(
            "os.rename", lambda s, d, *a, **k: real_rename(s, resolve(d), *a, **k)))
        stack.enter_context(mock.patch(
            "os.replace", lambda s, d, *a, **k: real_replace(s, resolve(d), *a, **k)))
        stack.enter_context(mock.patch("os.path.lexists", lambda p: real_lexists(resolve(p))))
        return stack

    def test_names_that_differ_only_in_case_do_not_overwrite_each_other(self):
        contents = {"RELATORIO.txt": "maiúsculas", "relatorio.txt": "minúsculas"}
        for name, text in contents.items():
            self.write("Documentos/" + name, text, mtime=T0)
        folder = self.mirror("Documentos")
        for _run in range(2):
            with self._case_insensitive_disk():
                result = self.backup()
            self.assertEqual(result.status, STATUS_WARNINGS, result.message)
            names = [n for n in os.listdir(folder) if not n.startswith(".sgtmp-")]
            self.assertEqual(len(names), 1, names)
            self.assertEqual(read(os.path.join(folder, names[0])), contents[names[0]])
            other = next(n for n in contents if n != names[0])
            self.assertTrue(any(i.path.endswith("/" + other) and i.level == "error"
                                for i in result.issues), result.issues)


class SymlinkedSourceTests(SandboxTestCase):
    """Selected folders reached through a symbolic link (stow dotfiles, another disk,
    snap "current") keep their content in «Cópia atual», whatever else is selected."""

    def snapshot(self, folder: str):
        out = {}
        for dirpath, dirnames, filenames in os.walk(folder):
            for name in dirnames + filenames:
                st = os.lstat(os.path.join(dirpath, name))
                out[os.path.join(dirpath, name)] = (st.st_mtime_ns, st.st_size)
        return out

    def test_stow_link_inside_a_selected_folder(self):
        self.write("dotfiles/mise/config.toml", "[tools]", mtime=T0)
        os.symlink(os.path.join(self.home, "dotfiles", "mise"),
                   os.path.join(self.home, ".config", "mise"))
        mise = os.path.join(self.home, ".config", "mise")
        self.config.add_path(KIND_FOLDER, mise)
        self.assertEqual(self.backup().status, STATUS_OK)
        self.assertEqual(read(self.mirror(".config/mise/config.toml")), "[tools]")
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, ".config"))
        res = selection.resolve(self.config)
        self.assertIn(mise, [s.path for s in res.sources])
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.stats.archived, 0)
        self.assertFalse(os.path.islink(self.mirror(".config/mise")))
        self.assertEqual(read(self.mirror(".config/mise/config.toml")), "[tools]")
        self.assertEqual(self.backup().status, STATUS_NOTHING)

    def test_stow_link_to_a_selected_file(self):
        self.write("dotfiles/git/.gitconfig", "[user]\n\tname = Ana\n", mtime=T0)
        os.symlink(os.path.join(self.home, "dotfiles", "git", ".gitconfig"),
                   os.path.join(self.home, ".gitconfig"))
        self.config.set_enabled(KIND_PRESET, "git", True)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK, [i.message for i in result.issues])
        copy = self.mirror(".gitconfig")
        self.assertFalse(os.path.islink(copy))
        self.assertEqual(read(copy), "[user]\n\tname = Ana\n")
        self.assertEqual(self.backup().status, STATUS_NOTHING)
        self.config.add_path(KIND_FOLDER, self.home)
        self.assertEqual(self.backup().status, STATUS_OK)
        self.assertFalse(os.path.islink(copy))
        self.assertEqual(read(copy), "[user]\n\tname = Ana\n")

    def test_documents_on_another_partition_with_the_whole_home(self):
        data = os.path.join(self.tmp, "dados", "Documentos")
        os.makedirs(data)
        with open(os.path.join(data, "tese.odt"), "w") as fh:
            fh.write("tese")
        os.rmdir(os.path.join(self.home, "Documentos"))
        os.symlink(data, os.path.join(self.home, "Documentos"))
        self.backup()
        self.assertEqual(read(self.mirror("Documentos/tese.odt")), "tese")
        self.config.add_path(KIND_FOLDER, self.home)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.stats.archived, 0)
        self.assertEqual(read(self.mirror("Documentos/tese.odt")), "tese")

    def snap(self) -> None:
        x5 = os.path.join(self.home, "snap", "libreoffice", "x5")
        self.write("snap/libreoffice/x5/.config/libreoffice/registo.xcu", "r", mtime=T0)
        self.write("snap/libreoffice/x5/outro.txt", "o", mtime=T0)
        os.symlink("x5", os.path.join(os.path.dirname(x5), "current"))

    def test_snap_profile_while_the_snap_folder_is_turned_on_and_off(self):
        self.snap()
        self.config.set_enabled(KIND_PRESET, "libreoffice", True)
        kept = self.mirror("snap/libreoffice/current/.config/libreoffice/registo.xcu")
        self.backup()
        self.assertEqual(read(kept), "r")
        snap = os.path.join(self.home, "snap")
        self.config.add_path(KIND_FOLDER, snap)
        self.assertEqual(self.backup().status, STATUS_OK)
        self.assertEqual(read(kept), "r")
        self.assertEqual(read(self.mirror("snap/libreoffice/x5/outro.txt")), "o")
        self.config.remove(KIND_FOLDER, "~/snap")
        self.assertEqual(self.backup().status, STATUS_OK)
        self.assertEqual(read(kept), "r")
        self.assertEqual(self.backup().status, STATUS_NOTHING)

    def test_link_left_in_the_mirror_is_never_written_through(self):
        self.snap()
        self.config.set_enabled(KIND_PRESET, "libreoffice", True)
        self.backup()
        current = self.mirror("snap/libreoffice/current")
        fileops.force_remove(current)
        # As left by an older version: a link that points back to the computer.
        os.symlink(os.path.join(self.home, "snap", "libreoffice", "x5"), current)
        self.write("snap/libreoffice/x5/.config/libreoffice/novo.xcu", "n", mtime=T0)
        before = self.snapshot(os.path.join(self.home, "snap"))
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(self.snapshot(os.path.join(self.home, "snap")), before)
        self.assertFalse(os.path.islink(current))
        self.assertEqual(read(os.path.join(current, ".config/libreoffice/novo.xcu")), "n")
        self.assertEqual(len(self.find_in_versions("snap/libreoffice/current")), 1)

    def test_missing_item_below_a_link_in_the_mirror_stays(self):
        self.snap()
        self.config.set_enabled(KIND_PRESET, "libreoffice", True)
        self.backup()
        current = self.mirror("snap/libreoffice/current")
        fileops.force_remove(current)
        os.symlink("x5", current)
        os.makedirs(self.mirror("snap/libreoffice/x5/.config/libreoffice"))
        with open(self.mirror("snap/libreoffice/x5/.config/libreoffice/registo.xcu"), "w") as fh:
            fh.write("r")
        fileops.force_remove(os.path.join(self.home, "snap"))  # snap removed
        result = self.backup()
        self.assertIn(result.status, (STATUS_OK, STATUS_WARNINGS, STATUS_NOTHING))
        self.assertTrue(os.path.islink(current))
        # What the link points to stays too: the kept profile is still in «Cópia atual».
        self.assertEqual(read(os.path.join(current, ".config/libreoffice/registo.xcu")), "r")

    def var_link(self, target: str) -> None:
        var = os.path.join(self.home, ".var")
        if os.path.lexists(var):
            fileops.force_remove(var)
        os.symlink(target, var)

    def test_real_folder_replaces_a_link_kept_for_a_missing_item(self):
        """«Toda a pasta pessoal» + LibreOffice (its Flatpak path is missing, so kept)."""
        self.config.add_path(KIND_FOLDER, self.home)
        self.config.set_enabled(KIND_PRESET, "libreoffice", True)
        self.write("outro/app/org.gimp.GIMP/gimprc", "v1", mtime=T0)
        self.var_link(os.path.join(self.home, "outro"))
        self.backup()
        self.assertTrue(os.path.islink(self.mirror(".var")))
        self.var_link("/nao/existe")
        fileops.force_remove(os.path.join(self.home, ".var"))
        self.write(".var/app/org.gimp.GIMP/gimprc", "v2", mtime=T0)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK, [i.message for i in result.issues])
        self.assertEqual(read(self.mirror(".var/app/org.gimp.GIMP/gimprc")), "v2")
        self.assertEqual(len(self.find_in_versions(".var")), 1)

    def test_link_replaces_a_folder_when_nothing_needed_is_in_it(self):
        self.config.add_path(KIND_FOLDER, self.home)
        self.config.set_enabled(KIND_PRESET, "libreoffice", True)
        self.write(".var/app/org.gimp.GIMP/gimprc", "v1", mtime=T0)
        self.backup()
        os.rename(os.path.join(self.home, ".var"), os.path.join(self.home, "outro"))
        self.var_link(os.path.join(self.home, "outro"))
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK, [i.message for i in result.issues])
        self.assertTrue(os.path.islink(self.mirror(".var")))
        self.assertEqual(self.backup().status, STATUS_NOTHING)

    def test_what_nobody_copies_below_a_new_link_leaves_the_copy(self):
        """~/.var moved to another disk and linked; Firefox (Flatpak) is still selected."""
        self.config.add_path(KIND_FOLDER, self.home)
        self.config.set_enabled(KIND_PRESET, "firefox", True)
        self.write(".var/app/org.gimp.GIMP/gimprc", "g", mtime=T0)
        self.write(".var/app/org.mozilla.firefox/.mozilla/profiles.ini", "p1", mtime=T0)
        self.backup()
        moved = os.path.join(self.tmp, "dados", "var")
        os.makedirs(os.path.dirname(moved))
        os.rename(os.path.join(self.home, ".var"), moved)
        os.remove(os.path.join(moved, "app/org.gimp.GIMP/gimprc"))  # deleted afterwards
        self.var_link(moved)
        self.write(".var/app/org.mozilla.firefox/.mozilla/profiles.ini", "p2", mtime=T0 + 5)
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK, [i.message for i in result.issues])
        self.assertFalse(os.path.exists(self.mirror(".var/app/org.gimp.GIMP/gimprc")))
        self.assertEqual(len(self.find_in_versions(".var/app/org.gimp.GIMP/gimprc")), 1)
        self.assertEqual(read(self.mirror(".var/app/org.mozilla.firefox/.mozilla/profiles.ini")),
                         "p2")
        self.assertEqual(self.backup().status, STATUS_NOTHING)


class ExclusionBehaviourTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.home, "Projetos", "app")
        self.config.add_path(KIND_PROJECT, os.path.join(self.home, "Projetos"))
        self.write("Projetos/app/package.json", "{}", mtime=T0)
        self.write("Projetos/app/.env", "API_KEY=1", mtime=T0)
        self.write("Projetos/app/.git/HEAD", "ref: refs/heads/main", mtime=T0)
        self.write("Projetos/app/node_modules/react/index.js", "x", mtime=T0)
        self.write("Projetos/app/src/__pycache__/m.cpython-312.pyc", "x", mtime=T0)
        self.write("Projetos/app/src/m.py", "x", mtime=T0)
        self.write("Projetos/app/.venv/pyvenv.cfg", "home = /usr", mtime=T0)
        self.write("Projetos/app/.venv/lib/site.py", "x", mtime=T0)
        self.write("Projetos/app/target/CACHEDIR.TAG",
                   "Signature: 8a477f597d28d172789f06886806bc55\n", mtime=T0)
        self.write("Projetos/app/target/debug/app", "bin", mtime=T0)
        self.write("Projetos/app/.cache/thing", "c", mtime=T0)
        self.write("Projetos/app/build/notes.txt", "kept", mtime=T0)

    def test_dependencies_and_caches_are_skipped(self):
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        m = lambda rel: self.mirror("Projetos/app/" + rel)
        for kept in ("package.json", ".env", ".git/HEAD", "src/m.py", "build/notes.txt"):
            self.assertTrue(os.path.exists(m(kept)), kept)
        for skipped in ("node_modules", "src/__pycache__", ".venv", "target", ".cache"):
            self.assertFalse(os.path.exists(m(skipped)), skipped)
        self.assertIn("node_modules", result.stats.excluded)

    def test_turning_exclusions_on_later_moves_old_copies_to_versions(self):
        self.config.settings.exclude_dev = False
        self.backup()
        self.assertTrue(os.path.exists(self.mirror("Projetos/app/node_modules/react/index.js")))
        self.config.settings.exclude_dev = True
        cb = RecordingCallbacks()
        result = self.backup(cb)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(cb.questions, [])
        self.assertFalse(os.path.exists(self.mirror("Projetos/app/node_modules")))
        self.assertGreaterEqual(result.stats.newly_excluded, 1)
        self.assertEqual(result.stats.removed_files, 0)

    def test_custom_patterns(self):
        self.config.settings.custom_excludes = ["*.iso", "Projetos/app/build/"]
        self.write("Documentos/ubuntu.iso", "iso", mtime=T0)
        self.backup()
        self.assertFalse(os.path.exists(self.mirror("Documentos/ubuntu.iso")))
        self.assertFalse(os.path.exists(self.mirror("Projetos/app/build")))


class UpgradeSafetyTests(SandboxTestCase):
    def test_disk_prepared_by_a_newer_version_is_refused(self):
        import json
        self.write("Documentos/a.txt", "a", mtime=T0)
        marker = os.path.join(self.root, "disco.json")
        with open(marker) as fh:
            data = json.load(fh)
        data["formato"] = 99
        with open(marker, "w") as fh:
            json.dump(data, fh)
        result = self.backup()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("mais recente", result.message)
        self.assertFalse(os.path.exists(self.mirror("Documentos/a.txt")))

    def test_new_version_that_drops_items_asks_first(self):
        self.write(".codex/config.toml", "model = 'x'", mtime=T0)
        self.write(".codex/log/codex-tui.log", "log", mtime=T0)
        self.config.set_enabled(KIND_PRESET, "ai", True)
        self.config.settings.exclude_caches = False
        self.backup(meta_extra={"versao_app": "1.0.0"})
        self.assertTrue(os.path.exists(self.mirror(".codex/log/codex-tui.log")))
        self.config.settings.exclude_caches = True
        cb = RecordingCallbacks()
        result = self.backup(cb, meta_extra={"versao_app": "1.1.0"})
        self.assertEqual([q.kind for q in cb.questions], ["upgrade_shrink"])
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertIn("~/.codex/log", " ".join(cb.questions[0].details))
        self.assertTrue(os.path.exists(self.mirror(".codex/log/codex-tui.log")))
        result = self.backup(RecordingCallbacks({"upgrade_shrink": "continue"}),
                             meta_extra={"versao_app": "1.1.0"})
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(len(self.find_in_versions(".codex/log/codex-tui.log")), 1)
        self.assertTrue(os.path.exists(self.mirror(".codex/config.toml")))

    def test_unattended_run_after_an_upgrade_keeps_copying(self):
        """A keyboard shortcut (salvaguarda --copiar, no terminal) is never blocked."""
        from salvaguarda.cli import _TerminalCallbacks

        self.write(".codex/config.toml", "model = 'x'", mtime=T0)
        self.write(".codex/log/codex-tui.log", "log", mtime=T0)
        self.config.set_enabled(KIND_PRESET, "ai", True)
        self.config.settings.exclude_caches = False
        self.backup(meta_extra={"versao_app": "1.0.0"})
        self.config.settings.exclude_caches = True
        self.write("Documentos/novo.txt", "n", mtime=T0)
        with mock.patch("sys.stdout"):
            result = self.backup(_TerminalCallbacks(False), meta_extra={"versao_app": "1.1.0"})
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(read(self.mirror("Documentos/novo.txt")), "n")
        self.assertTrue(os.path.exists(self.mirror(".codex/log/codex-tui.log")))
        self.assertEqual(self.find_in_versions(".codex/log/codex-tui.log"), [])
        self.assertTrue(any("a partir da janela" in n for n in result.notes))
        self.assertEqual(self.layout.read_meta()["versao_app"], "1.0.0")
        with mock.patch("sys.stdout"):
            again = self.backup(_TerminalCallbacks(False), meta_extra={"versao_app": "1.1.0"})
        self.assertEqual(again.message, "Já estava tudo copiado.")
        self.assertEqual(again.stats.newly_excluded, 0)
        cb = RecordingCallbacks({"upgrade_shrink": "continue"})
        self.assertEqual(self.backup(cb, meta_extra={"versao_app": "1.1.0"}).status, STATUS_OK)
        self.assertEqual([q.kind for q in cb.questions], ["upgrade_shrink"])
        self.assertIn("1 item deixa de ser copiado", cb.questions[0].text)
        self.assertFalse(os.path.exists(self.mirror(".codex/log/codex-tui.log")))
        self.assertEqual(self.layout.read_meta()["versao_app"], "1.1.0")

    def test_unattended_run_with_an_open_app_copies_everything_else(self):
        from salvaguarda.cli import _TerminalCallbacks

        self.config.set_enabled(KIND_PRESET, "firefox", True)
        self.write(".mozilla/firefox/profiles.ini", "p1", mtime=T0)
        self.write("Documentos/a.txt", "a", mtime=T0)
        self.backup()
        self.write(".mozilla/firefox/profiles.ini", "p2", mtime=T0 + 5)
        self.write("Documentos/novo.txt", "n", mtime=T0)
        with mock.patch("sys.stdout"):
            result = self.backup(_TerminalCallbacks(False), warn_open_apps=True,
                                 running_processes=lambda: {"firefox"})
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(read(self.mirror("Documentos/novo.txt")), "n")
        self.assertEqual(read(self.mirror(".mozilla/firefox/profiles.ini")), "p1")
        self.assertEqual(self.find_in_versions(".mozilla/firefox/profiles.ini"), [])
        self.assertTrue(any("Firefox" in n and "abertas" in n for n in result.notes))
        # In the window, the question is still asked and «Cancelar» still cancels.
        cb = RecordingCallbacks()
        result = self.backup(cb, warn_open_apps=True, running_processes=lambda: {"firefox"})
        self.assertEqual([q.kind for q in cb.questions], ["open_apps"])
        self.assertEqual(result.status, STATUS_CANCELLED)

    def test_same_version_with_new_exclusion_does_not_ask(self):
        self.write("Projetos/app/node_modules/x.js", "x", mtime=T0)
        self.config.add_path(KIND_PROJECT, os.path.join(self.home, "Projetos"))
        self.config.settings.exclude_dev = False
        self.backup(meta_extra={"versao_app": "1.1.0"})
        self.config.settings.exclude_dev = True
        cb = RecordingCallbacks()
        self.assertEqual(self.backup(cb, meta_extra={"versao_app": "1.1.0"}).status, STATUS_OK)
        self.assertEqual(cb.questions, [])

    def test_removed_large_settings_file_keeps_its_version(self):
        self.config.settings.preset_versions_max_mb = 0
        self.write(".claude/projects/app/sessao.jsonl", "conversa longa", mtime=T0)
        self.config.set_enabled(KIND_PRESET, "ai", True)
        self.backup()
        os.remove(os.path.join(self.home, ".claude/projects/app/sessao.jsonl"))
        result = self.backup()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(len(self.find_in_versions(".claude/projects/app/sessao.jsonl")), 1)

    def test_inventory_is_copied_into_extras(self):
        def fake_inventory(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as fh:
                fh.write("# Programas instalados\n")

        self.write("Documentos/a.txt", "a", mtime=T0)
        result = self.backup(inventory_dump=fake_inventory)
        self.assertEqual(result.status, STATUS_OK, result.message)
        extra = os.path.join(self.layout.current_dir, "Extras", selection.INVENTORY_EXTRA_NAME)
        self.assertIn("Programas instalados", read(extra))

    def test_metadata_records_where_the_copy_comes_from(self):
        from salvaguarda.core import controller
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "X-Cinnamon"}):
            meta = controller.environment_meta(self.config)
        self.assertEqual(meta["ambiente"], "cinnamon")
        self.assertEqual(meta["pasta_pessoal"], self.home)
        self.assertEqual(meta["pastas_xdg"]["DOCUMENTS"], "~/Documentos")
        self.assertTrue(meta["utilizador"])
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "", "XDG_SESSION_DESKTOP": "",
                                          "DESKTOP_SESSION": ""}), \
                mock.patch("salvaguarda.core.system.running_process_names", return_value=set()):
            self.assertNotIn("ambiente", controller.environment_meta(self.config))


class DconfExtraTests(SandboxTestCase):
    def test_desktop_settings_are_exported_into_extras(self):
        self.config.set_enabled(KIND_PRESET, "desktop", True)

        def fake_dump(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as fh:
                fh.write("[org/cinnamon]\npanels-enabled=['1:0:bottom']\n")
            return None

        with mock.patch("salvaguarda.core.selection._dconf_available", return_value=True):
            result = self.backup(dconf_dump=fake_dump)
        self.assertEqual(result.status, STATUS_OK, result.message)
        extra = os.path.join(self.layout.current_dir, "Extras", selection.DCONF_EXTRA_NAME)
        self.assertIn("panels-enabled", read(extra))
        # A new system without the dconf program: the export on the disk stays.
        result = self.backup(dconf_dump=fake_dump)
        self.assertIn(result.status, (STATUS_OK, STATUS_NOTHING), result.message)
        self.assertIn("panels-enabled", read(extra))
        self.assertEqual(result.stats.orphans, 0)

    def test_export_records_the_desktop_it_comes_from(self):
        from salvaguarda.core import controller, desktop

        self.config.set_enabled(KIND_PRESET, "desktop", True)
        written = []
        engine = None
        with mock.patch("salvaguarda.core.selection._dconf_available", return_value=True), \
                mock.patch("salvaguarda.core.system.dconf_dump",
                           side_effect=lambda t, header="": written.append(header) or "x"), \
                mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "X-Cinnamon"}):
            engine = controller.build_engine(self.config, self.root, RecordingCallbacks(),
                                             threading.Event())
            engine.dconf_dump("/nao/existe")
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].startswith(desktop.dump_header(desktop.CINNAMON)))
        self.assertEqual(desktop.home_in_header(written[0])[0], self.home)
        self.assertEqual(desktop.home_in_header(written[0])[1]["DOCUMENTS"], "~/Documentos")


if __name__ == "__main__":
    unittest.main()
