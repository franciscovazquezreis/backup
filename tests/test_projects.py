"""Project finder: projects anywhere in the home folder and what exists only here."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import unittest

from salvaguarda.core import controller, paths, projects, selection
from salvaguarda.core.config import KIND_FOLDER, KIND_PROJECT

from .helpers import SandboxTestCase

GIT = shutil.which("git")
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Ana", GIT_AUTHOR_EMAIL="ana@example.com",
               GIT_COMMITTER_NAME="Ana", GIT_COMMITTER_EMAIL="ana@example.com")


class FinderTestCase(SandboxTestCase):
    def git(self, cwd: str, *args: str) -> None:
        subprocess.run([GIT, "-c", "init.defaultBranch=main"] + list(args), cwd=cwd,
                       env=GIT_ENV, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def repo(self, rel: str, files=None, commit: bool = True) -> str:
        path = os.path.join(self.home, rel)
        os.makedirs(path, exist_ok=True)
        self.git(path, "init", "-q")
        for name, text in (files or {"README.md": "# x\n"}).items():
            self.write(os.path.join(rel, name), text)
        if commit:
            self.git(path, "add", "-A")
            self.git(path, "commit", "-q", "-m", "primeiro")
        return path

    def pushed(self, path: str) -> None:
        """As after a push to GitHub (no network: the tracking ref is created by hand)."""
        self.git(path, "remote", "add", "origin", "https://github.com/ana/x.git")
        self.git(path, "update-ref", "refs/remotes/origin/main", "HEAD")

    def commit(self, path: str, name: str) -> None:
        with open(os.path.join(path, name), "w") as fh:
            fh.write(name)
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", name)

    def find(self, **kwargs) -> projects.ScanResult:
        res = selection.resolve(self.config)
        kwargs.setdefault("table", [])
        return projects.find_projects(projects.coverage_roots(res), projects.search_roots(res),
                                      controller.make_exclusions(self.config), **kwargs)

    @staticmethod
    def by_rel(result: projects.ScanResult):
        return {paths.to_portable(p.path): p for p in result.projects}


@unittest.skipIf(GIT is None, "git is not installed")
class RepositoryStateTest(FinderTestCase):
    def test_states(self) -> None:
        self.repo("Documentos/app-sem-remoto")
        self.pushed(self.repo("Projetos/limpo"))
        p = self.repo("Projetos/adiantado")
        self.pushed(p)
        self.commit(p, "novo.txt")
        p = self.repo("Projetos/sujo")
        self.pushed(p)
        self.write("Projetos/sujo/README.md", "mudado\n")
        p = self.repo("Projetos/novos")
        self.pushed(p)
        self.write("Projetos/novos/ideia.py", "1\n")
        p = self.repo("Projetos/guardado")
        self.pushed(p)
        self.write("Projetos/guardado/README.md", "rascunho\n")
        self.git(p, "stash", "-q")
        p = self.repo("Projetos/segredos", {".gitignore": ".env\nnode_modules/\ndist/\n"})
        self.pushed(p)
        self.write("Projetos/segredos/.env", "KEY=1\n")
        self.write("Projetos/segredos/node_modules/x/index.js", "1\n")
        self.write("Projetos/segredos/dist/app.js", "1\n")

        found = self.by_rel(self.find())
        p = found["~/Documentos/app-sem-remoto"]
        self.assertEqual((projects.level(p), p.git.commits, p.covered_by),
                         (projects.LEVEL_LOCAL, 1, "Documentos"))
        self.assertEqual(projects.level(found["~/Projetos/limpo"]), projects.LEVEL_REMOTE)
        self.assertEqual(found["~/Projetos/adiantado"].git.unpushed_branches, ["main"])
        self.assertEqual(found["~/Projetos/sujo"].git.changed, 1)
        self.assertEqual(found["~/Projetos/novos"].git.untracked, 1)
        self.assertEqual(found["~/Projetos/guardado"].git.stashes, 1)
        p = found["~/Projetos/segredos"]
        self.assertEqual((projects.level(p), p.git.ignored), (projects.LEVEL_IGNORED, [".env"]))
        for rel in ("adiantado", "sujo", "novos", "guardado"):
            self.assertEqual(projects.level(found["~/Projetos/" + rel]), projects.LEVEL_LOCAL, rel)

    def test_never_changes_anything(self) -> None:
        p = self.repo("Projetos/zip", {".gitattributes": "* filter=mau\n", "a.txt": "1\n"})
        marker = os.path.join(self.tmp, "correu")
        with open(os.path.join(p, ".git", "config"), "a") as fh:
            fh.write('[filter "mau"]\n\tclean = "sh -c \'touch %s && cat\'"\n' % marker)
            fh.write("[core]\n\tfsmonitor = sh -c 'touch %s'\n" % marker)
        os.utime(os.path.join(p, "a.txt"), (1700000000, 1700000000))  # stale index

        def snapshot():
            out = {}
            for dirpath, dirnames, filenames in os.walk(self.home):
                for name in dirnames + filenames:
                    st = os.lstat(os.path.join(dirpath, name))
                    out[os.path.join(dirpath, name)] = (st.st_mtime_ns, st.st_size)
            return out

        before = snapshot()
        result = self.find()
        self.assertEqual(snapshot(), before)
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(self.by_rel(result)["~/Projetos/zip"].git.checked)

    def test_filter_that_cannot_be_disabled_never_runs(self) -> None:
        """git -c splits at the first "=": a filter named "a=b" cannot be switched off, so
        the repository must not get "git status" at all (here and through an include)."""
        marker = os.path.join(self.tmp, "correu")
        hostile = 'clean = "sh -c \'touch %s; cat\'"\n\tprocess = "sh -c \'touch %s\'"\n' % (
            marker, marker)
        p = self.repo("Projetos/direto", {".gitattributes": "* filter=a=b\n", "a.txt": "1\n"})
        with open(os.path.join(p, ".git", "config"), "a") as fh:
            fh.write('[filter "a=b"]\n\t' + hostile)
        q = self.repo("Projetos/incluido", {".gitattributes": "* filter=x=y\n", "a.txt": "1\n"})
        with open(os.path.join(self.tmp, "extra.cfg"), "w") as fh:
            fh.write('[filter "x=y"]\n\t' + hostile)
        with open(os.path.join(q, ".git", "config"), "a") as fh:
            fh.write("[include]\n\tpath = %s\n" % os.path.join(self.tmp, "extra.cfg"))
        for path in (p, q):
            self.pushed(path)
            os.utime(os.path.join(path, "a.txt"), (1700000000, 1700000000))  # stale index
        found = self.by_rel(self.find())
        self.assertFalse(os.path.exists(marker))
        for rel in ("~/Projetos/direto", "~/Projetos/incluido"):
            g = found[rel].git
            self.assertIsNone(g.changed, rel)
            self.assertIn("não é seguro", g.note, rel)
            self.assertEqual(projects.level(found[rel]), projects.LEVEL_UNKNOWN, rel)

    def test_commits_only_on_a_local_bare_remote_are_not_sent(self) -> None:
        p = self.repo("Projetos/misto")
        self.pushed(p)
        bare = os.path.join(self.tmp, "copia.git")
        self.git(self.tmp, "init", "-q", "--bare", bare)
        self.git(p, "remote", "add", "copia", bare)
        self.commit(p, "depois.txt")
        self.git(p, "push", "-q", "copia", "main")
        found = self.by_rel(self.find())["~/Projetos/misto"]
        self.assertEqual(found.git.unpushed, 1)
        self.assertEqual(projects.level(found), projects.LEVEL_LOCAL)

    def test_nested_projects(self) -> None:
        p = self.repo("Projetos/mono", {".gitignore": "tools/r/\n",
                                         "packages/web/package.json": "{}\n"})
        self.pushed(p)
        self.repo("Projetos/mono/tools/r")
        self.write("Documentos/site/package.json", "{}\n")
        self.write("Documentos/site/node_modules/dep/package.json", "{}\n")
        self.repo("Documentos/site/node_modules/clone")
        found = self.by_rel(self.find())
        self.assertNotIn("~/Projetos/mono/packages/web", found)
        self.assertEqual(found["~/Projetos/mono/tools/r"].parent, p)
        self.assertEqual(found["~/Documentos/site"].kind, projects.KIND_FOLDER)
        self.assertFalse([k for k in found if "node_modules" in k])

    def test_worktree_inside_the_repository(self) -> None:
        """Claude Code puts worktrees in <repo>/.claude/worktrees/<name>."""
        p = self.repo("Projetos/app")
        self.pushed(p)
        self.git(p, "worktree", "add", "-q", "-b", "worktree-x",
                 os.path.join(p, ".claude", "worktrees", "x"))
        self.write("Projetos/app/.claude/worktrees/x/novo.py", "1\n")
        found = self.by_rel(self.find())
        self.assertEqual(found["~/Projetos/app"].git.untracked, 0)
        self.assertEqual(projects.level(found["~/Projetos/app"]), projects.LEVEL_REMOTE)
        wt = found["~/Projetos/app/.claude/worktrees/x"]
        self.assertTrue(wt.worktree)
        self.assertEqual((wt.git.untracked, wt.parent), (1, p))

    def test_copied_worktree_with_commits_outside_the_copy(self) -> None:
        """The worktree's files are copied, its commits (in the main repository) are not."""
        main = os.path.join(self.tmp, "media", "Dados", "app")
        os.makedirs(main)
        self.git(main, "init", "-q")
        with open(os.path.join(main, "a.txt"), "w") as fh:
            fh.write("1\n")
        self.git(main, "add", "-A")
        self.git(main, "commit", "-q", "-m", "primeiro")
        self.git(main, "remote", "add", "origin", "https://github.com/ana/app.git")
        self.git(main, "update-ref", "refs/remotes/origin/main", "HEAD")
        wt = os.path.join(self.home, "Projetos", "app-wt")
        self.git(main, "worktree", "add", "-q", "-b", "feature", wt)
        self.commit(wt, "b.txt")
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, "Projetos"))
        found = self.by_rel(self.find())["~/Projetos/app-wt"]
        self.assertTrue(found.covered)
        self.assertEqual(found.commits_elsewhere, main)
        self.assertTrue(projects.history_elsewhere(found))
        self.assertFalse(projects.in_backup(found))
        self.assertTrue(any("não vai para a cópia" in r for r in projects.reasons(found)))
        result = projects.ScanResult(projects=[found])
        self.assertEqual(controller.projects_at_risk(self.config, result), [found])
        self.assertEqual(controller.projects_only_here(self.config, result), [])
        # Once pushed, the copy of the files is all that is missing: nothing to warn about.
        self.git(main, "update-ref", "refs/remotes/origin/feature", "feature")
        found = self.by_rel(self.find())["~/Projetos/app-wt"]
        self.assertFalse(projects.history_elsewhere(found))
        self.assertTrue(projects.in_backup(found))

    def test_deep_nested_repository_still_counts_for_its_parent(self) -> None:
        """A repository too deep to be reported on its own is a new folder of its parent."""
        p = self.repo("Projetos/app", {"a/b/c/d/e/x.txt": "1\n"})
        self.pushed(p)
        self.repo("Projetos/app/a/b/c/d/e/sub")
        found = self.by_rel(self.find())
        self.assertNotIn("~/Projetos/app/a/b/c/d/e/sub", found)
        self.assertEqual(found["~/Projetos/app"].git.untracked, 1)
        self.assertEqual(projects.level(found["~/Projetos/app"]), projects.LEVEL_LOCAL)

    def test_git_missing(self) -> None:
        self.pushed(self.repo("Projetos/limpo"))
        self.repo("Projetos/sem-remoto")
        found = self.by_rel(self.find(git=None))
        self.assertEqual(projects.level(found["~/Projetos/limpo"]), projects.LEVEL_UNKNOWN)
        self.assertEqual(found["~/Projetos/limpo"].git.remotes, [("origin", "github.com")])
        self.assertEqual(projects.level(found["~/Projetos/sem-remoto"]), projects.LEVEL_LOCAL)

    def test_cancel_stops_a_slow_git(self) -> None:
        self.repo("Projetos/a")
        fake = os.path.join(self.tmp, "git-lento")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nexec sleep 30\n")
        os.chmod(fake, 0o755)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        result = self.find(git=fake, cancel=cancel)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.by_rel(result)["~/Projetos/a"].git.note, "A procura foi cancelada")


class WalkTest(FinderTestCase):
    def test_coverage_and_adding(self) -> None:
        self.write("coisas/cliente/app/package.json", "{}\n")
        self.write("coisas/cliente/app/src/main.ts", "1\n")
        p = self.by_rel(self.find())["~/coisas/cliente/app"]
        self.assertFalse(p.covered)
        self.config.add_path(KIND_PROJECT, p.path)
        self.assertTrue(self.by_rel(self.find())["~/coisas/cliente/app"].covered)

    def test_excluded_by_a_custom_rule(self) -> None:
        self.write("Documentos/privado/app/Cargo.toml", "[package]\n")
        self.config.settings.custom_excludes = ["~/Documentos/privado/"]
        p = self.by_rel(self.find())["~/Documentos/privado/app"]
        self.assertEqual((p.covered, p.excluded_by), (False, "~/Documentos/privado"))

    def test_depth_limit_and_selected_deep_folder(self) -> None:
        deep = "coisas/a/b/c/d/e/f/g/fundo"
        self.write(deep + "/package.json", "{}\n")
        result = self.find()
        self.assertNotIn("~/" + deep, self.by_rel(result))
        self.assertTrue(result.too_deep_count)
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, "coisas/a/b/c/d/e"))
        self.assertTrue(self.by_rel(self.find())["~/" + deep].covered)

    def test_skip_list_is_anchored(self) -> None:
        self.write("snap-app/index.html", "<html>\n")
        self.write("snap-app/app.js", "1\n")
        self.write("snap/code/common/.vscode/x/package.json", "{}\n")
        found = self.by_rel(self.find())
        self.assertIn("~/snap-app", found)
        self.assertFalse([k for k in found if k.startswith("~/snap/")])

    def test_hidden_folders_count_only_git_repositories(self) -> None:
        self.write(".algo/app/package.json", "{}\n")
        self.assertNotIn("~/.algo/app", self.by_rel(self.find()))
        self.config.add_path(KIND_FOLDER, os.path.join(self.home, ".algo"))
        self.assertIn("~/.algo/app", self.by_rel(self.find()))

    def test_selected_folder_on_a_network_share_is_not_searched(self) -> None:
        share = os.path.join(self.tmp, "nas")
        os.makedirs(os.path.join(share, "app"))
        with open(os.path.join(share, "app", "go.mod"), "w") as fh:
            fh.write("module x\n")
        self.config.add_path(KIND_FOLDER, share)
        self.assertIn(os.path.join(share, "app"), {p.path for p in self.find().projects})
        result = self.find(table=[(share, 99, "fuse.sshfs")])
        self.assertEqual(result.network_skipped, [share])
        self.assertNotIn(os.path.join(share, "app"), {p.path for p in result.projects})

    def test_undecodable_name(self) -> None:
        base = os.fsencode(os.path.join(self.home, "Documentos"))
        os.makedirs(os.path.join(base, b"caf\xe9"))
        with open(os.path.join(base, b"caf\xe9", b"package.json"), "wb") as fh:
            fh.write(b"{}\n")
        result = self.find()
        self.assertEqual(len(result.projects), 1)
        self.assertIn("�", paths.safe_text(result.projects[0].path))
        projects.to_dict(result)  # the cache can be written

    def test_cancel_before_start(self) -> None:
        cancel = threading.Event()
        cancel.set()
        result = self.find(cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.dirs, 0)


if __name__ == "__main__":
    unittest.main()
