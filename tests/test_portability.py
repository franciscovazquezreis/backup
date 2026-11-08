"""Moving to another Debian-based distribution: packaging, desktop settings, inventory."""

import datetime as _dt
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

from salvaguarda.core import desktop as ds
from salvaguarda.core import inventory as inv
from salvaguarda.core import postrestore
from salvaguarda.core import variants as va


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sg-proto-")
        self.root = os.path.join(self.tmp, "root")
        self.home = os.path.join(self.tmp, "home", "bruno")
        os.makedirs(self.root)
        os.makedirs(self.home)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def touch(self, path, content="", base=None):
        full = os.path.join(base or self.root, path.lstrip("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(content)
        return full


# ----------------------------------------------------------------- variants
class VariantTests(TmpCase):
    def probe(self):
        return va.Probe(self.home, self.root)

    def test_mint_deb_profile_goes_to_ubuntu_snap(self):
        os.makedirs(os.path.join(self.root, "snap/firefox/current"))
        # Ubuntu's /usr/bin/firefox is a wrapper script, not a real Firefox.
        self.touch("/usr/bin/firefox", '#!/bin/sh\nexec /snap/bin/firefox "$@"\n')
        plan = va.plan_remaps("firefox", ["Pasta pessoal/.mozilla"], self.probe())
        self.assertEqual([(r.source, r.target, r.destination) for r in plan],
                         [("deb", "snap", "snap/firefox/common/.mozilla")])

    def test_remap_onto_a_folder_that_another_item_restores_is_skipped(self):
        """The old snap profile comes back with «Apps em snap e Flatpak»: the deb profile
        must not be poured into the same folder."""
        from salvaguarda.core import restore

        os.makedirs(os.path.join(self.root, "snap/firefox/current"))
        tree = os.path.join(self.tmp, "copia")
        os.makedirs(os.path.join(tree, "Pasta pessoal/.mozilla"))
        firefox = restore.RestoreItem("Firefox", "preset", "firefox", ["Pasta pessoal/.mozilla"])
        sandboxed = restore.RestoreItem("Apps", "preset", "sandboxed", ["Pasta pessoal/snap"])
        loose = restore.RestoreItem("Soltos", "loose", "home_files", ["Pasta pessoal"])
        both = [firefox, sandboxed, loose]
        # The copy has no old snap profile: the deb profile goes to the snap.
        self.assertTrue(restore.plan_variants(both, self.probe(), tree))
        os.makedirs(os.path.join(tree, "Pasta pessoal/snap/firefox/common/.mozilla"))
        self.assertEqual(restore.plan_variants(both, self.probe(), tree), {})
        # Loose files alone never block a remap.
        self.assertTrue(restore.plan_variants([firefox, loose], self.probe(), tree))

    def test_wrapper_alone_counts_as_snap(self):
        self.touch("/usr/bin/firefox", '#!/bin/sh\nexec /snap/bin/firefox "$@"\n')
        self.assertEqual(va.installed_kinds("firefox", self.probe()), ["snap"])

    def test_same_packaging_does_not_move(self):
        self.touch("/usr/lib/firefox/firefox")
        self.assertEqual(va.plan_remaps("firefox", ["Pasta pessoal/.mozilla"], self.probe()), [])

    def test_app_not_installed_does_not_move(self):
        self.assertEqual(va.plan_remaps("firefox", ["Pasta pessoal/.mozilla"], self.probe()), [])

    def test_flatpak_user_install(self):
        os.makedirs(os.path.join(self.home, ".local/share/flatpak/app/org.mozilla.firefox"))
        plan = va.plan_remaps("firefox", ["Pasta pessoal/.mozilla"], self.probe())
        self.assertEqual(plan[0].destination, ".var/app/org.mozilla.firefox/.mozilla")

    def test_snap_back_to_deb(self):
        self.touch("/usr/lib/firefox/firefox")
        plan = va.plan_remaps("firefox", ["Pasta pessoal/snap/firefox/common/.mozilla"], self.probe())
        self.assertEqual(plan[0].destination, ".mozilla")

    def test_vscode_roles_map_separately(self):
        os.makedirs(os.path.join(self.root, "var/lib/flatpak/app/com.visualstudio.code"))
        plan = va.plan_remaps("vscode", ["Pasta pessoal/.config/Code/User", "Pasta pessoal/.vscode"],
                              self.probe())
        self.assertEqual(sorted(r.destination for r in plan),
                         [".var/app/com.visualstudio.code/config/Code/User",
                          ".var/app/com.visualstudio.code/data/vscode"])

    def test_two_old_profiles_never_mix(self):
        os.makedirs(os.path.join(self.root, "snap/firefox/current"))
        rels = ["Pasta pessoal/.var/app/org.mozilla.firefox/.mozilla", "Pasta pessoal/.mozilla"]
        plan = va.plan_remaps("firefox", rels, self.probe())
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].rel, "Pasta pessoal/.mozilla")  # deb wins over flatpak

    def test_libreoffice_snap_never_ran(self):
        os.makedirs(os.path.join(self.root, "snap/libreoffice/current"))
        plan = va.plan_remaps("libreoffice", ["Pasta pessoal/.config/libreoffice"], self.probe())
        self.assertTrue(plan[0].warnings)
        os.makedirs(os.path.join(self.home, "snap/libreoffice/335"))
        os.symlink("335", os.path.join(self.home, "snap/libreoffice/current"))
        plan = va.plan_remaps("libreoffice", ["Pasta pessoal/.config/libreoffice"], self.probe())
        self.assertFalse(plan[0].warnings)

    def test_downgrade_detection(self):
        self.touch("/usr/lib/firefox-esr/application.ini", "[App]\nVendor=Mozilla\nVersion=140.5.0\n")
        profile = os.path.join(self.home, ".mozilla/firefox/abc.default-release")
        self.touch("compatibility.ini", "[Compatibility]\nLastVersion=146.0.1_20251120/20251120\n",
                   base=profile)
        installed = va.installed_mozilla_version("firefox", "deb", self.probe())
        last = va.profile_last_version(profile)
        self.assertEqual((installed, last), ((140, 5, 0), (146, 0, 1)))
        self.assertIn("140.5", va.downgrade_warning("Firefox", installed, last))
        self.assertIsNone(va.downgrade_warning("Firefox", (147, 0, 0), last))

    def test_default_profiles(self):
        base = os.path.join(self.home, ".mozilla/firefox")
        self.touch("profiles.ini", "[Install4F96D1932A9F858E]\nDefault=abc.default-release\nLocked=1\n\n"
                   "[Profile0]\nName=default-release\nIsRelative=1\nPath=abc.default-release\nDefault=1\n\n"
                   "[General]\nStartWithLastProfile=1\nVersion=2\n", base=base)
        self.assertEqual(va.default_profiles(base), [os.path.join(base, "abc.default-release")])


# --------------------------------------------------------- desktop settings
CINNAMON_DUMP = """[org/cinnamon]
enabled-applets=['panel1:left:0:menu@cinnamon.org:0']

[org/cinnamon/desktop/interface]
gtk-theme='Mint-Y-Dark-Aqua'
icon-theme='Mint-Y-Sand'

[org/gnome/desktop/interface]
gtk-theme='Mint-Y-Dark-Aqua'
icon-theme='Mint-Y-Sand'
color-scheme='prefer-dark'

[org/gnome/desktop/background]
picture-uri='file:///home/ana/Imagens/F%C3%A9rias/praia.jpg'

[org/x/editor/preferences/editor]
scheme='oblivion'
tabs-size=uint32 4

[org/gnome/terminal/legacy/profiles:/:b1dcc9dd-5262-4d8d-a863-c897e6d979b9]
custom-command='/home/ana/.local/bin/fish'

[org/nemo/window-state]
sidebar-bookmark-breakpoint=0
"""


class DesktopTests(TmpCase):
    def test_detect_desktop(self):
        self.assertEqual(ds.current_desktop({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}), ds.CINNAMON)
        self.assertEqual(ds.current_desktop({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}), ds.GNOME)
        self.assertEqual(ds.current_desktop({"XDG_CURRENT_DESKTOP": "Budgie:GNOME"}), ds.BUDGIE)
        self.assertEqual(ds.current_desktop({"XDG_CURRENT_DESKTOP": "KDE"}), ds.KDE)
        self.assertEqual(ds.current_desktop({}, ["bash", "plasmashell"]), ds.KDE)
        self.assertEqual(ds.current_desktop({}), ds.UNKNOWN)

    def test_dump_desktop_guess(self):
        self.assertEqual(ds.desktop_of_dump(ds.split_dump(CINNAMON_DUMP)), ds.CINNAMON)

    def test_same_desktop_keeps_everything_but_missing_themes(self):
        os.makedirs(os.path.join(self.root, "usr/share/themes/Mint-Y-Dark-Aqua"))
        plan = ds.DesktopPlan(CINNAMON_DUMP, ds.CINNAMON, ds.CINNAMON, self.home, root=self.root)
        self.assertFalse(plan.skipped)
        self.assertIn("gtk-theme='Mint-Y-Dark-Aqua'", plan.text)
        self.assertNotIn("Mint-Y-Sand", plan.text)  # icon theme not installed here
        self.assertIn("enabled-applets", plan.text)

    def test_other_desktop_gets_only_app_settings(self):
        plan = ds.DesktopPlan(CINNAMON_DUMP, "", ds.GNOME, self.home, root=self.root)
        self.assertEqual(plan.source, ds.CINNAMON)
        self.assertNotIn("[org/gnome/desktop/interface]", plan.text)
        self.assertNotIn("[org/gnome/desktop/background]", plan.text)
        self.assertNotIn("[org/cinnamon]", plan.text)
        self.assertIn("[org/x/editor/preferences/editor]", plan.text)
        self.assertIn("tabs-size=uint32 4", plan.text)
        self.assertTrue(any("GNOME" in n for n in plan.notes()))

    def test_paths_follow_new_user_and_folder_names(self):
        pairs = ds.path_pairs("/home/ana", "/home/bruno", {"PICTURES": "~/Imagens"},
                              {"PICTURES": "/home/bruno/Pictures"})
        plan = ds.DesktopPlan(CINNAMON_DUMP, ds.CINNAMON, ds.CINNAMON, self.home, pairs, self.root)
        self.assertIn("file:///home/bruno/Pictures/F%C3%A9rias/praia.jpg", plan.text)
        self.assertIn("custom-command='/home/bruno/.local/bin/fish'", plan.text)
        self.assertEqual(plan.rewritten, 2)

    def test_folders_outside_the_old_home_keep_their_paths(self):
        pairs = ds.path_pairs("/home/ana", "/home/ana", {"PICTURES": "/mnt/dados/Fotos"},
                              {"PICTURES": "/home/ana/Imagens"})
        self.assertEqual(pairs, [])

    def test_export_header_wins_over_the_metadata(self):
        """An old export in «Versões anteriores» says itself which desktop wrote it."""
        text = ds.dump_header(ds.CINNAMON) + CINNAMON_DUMP
        self.assertEqual(ds.desktop_in_header(text), ds.CINNAMON)
        plan = ds.DesktopPlan(text, ds.GNOME, ds.GNOME, self.home, (), self.root)
        self.assertFalse(plan.same_desktop)
        self.assertNotIn("[org/cinnamon", plan.text)
        self.assertNotIn("salvaguarda-ambiente", plan.text)
        unknown = ds.DesktopPlan(ds.dump_header("") + CINNAMON_DUMP, "", "", self.home, (),
                                 self.root)
        self.assertTrue(any("Não foi possível confirmar" in n for n in unknown.notes()))

    def test_rewrite_respects_boundaries(self):
        text, n = ds.rewrite_paths("a='/home/anabela/x' b='/home/ana'", [("/home/ana", "/home/bruno")])
        self.assertEqual(text, "a='/home/anabela/x' b='/home/bruno'")
        self.assertEqual(n, 1)

    def test_roundtrip_keeps_unknown_sections(self):
        text = ds.join_dump(ds.split_dump(CINNAMON_DUMP))
        self.assertEqual(ds.split_dump(text), ds.split_dump(CINNAMON_DUMP))


# --------------------------------------------------------------- inventory
POLICY = """code:
  Installed: 1.95.3-1731513102
  Candidate: 1.95.3-1731513102
  Version table:
 *** 1.95.3-1731513102 500
        500 https://packages.microsoft.com/repos/code stable/main amd64 Packages
        100 /var/lib/dpkg/status
git:
  Installed: 1:2.43.0-1ubuntu7.3
  Candidate: 1:2.43.0-1ubuntu7.3
  Version table:
 *** 1:2.43.0-1ubuntu7.3 500
        500 http://ftp.rnl.tecnico.ulisboa.pt/pub/ubuntu noble-updates/main amd64 Packages
        100 /var/lib/dpkg/status
     1:2.43.0-1ubuntu7 500
        500 http://ftp.rnl.tecnico.ulisboa.pt/pub/ubuntu noble/main amd64 Packages
discord:
  Installed: 0.0.75
  Candidate: 0.0.75
  Version table:
 *** 0.0.75 100
        100 /var/lib/dpkg/status
libfoo1:
  Installed: 1.2-3ubuntu0.2
  Candidate: 1.2-3ubuntu0.2
  Version table:
 *** 1.2-3ubuntu0.2 100
        100 /var/lib/dpkg/status
     1.2-3 500
        500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages
"""


class FakeRunner(inv.Runner):
    def __init__(self, outputs):
        super().__init__()
        self.outputs = outputs
        self.calls = []

    def which(self, name):
        return "/usr/bin/" + name if any(k[0] == name for k in self.outputs) else None

    def run(self, cmd, timeout=inv.DEFAULT_TIMEOUT, ok_codes=(0,)):
        self.calls.append(list(cmd))
        for key, value in self.outputs.items():
            if tuple(cmd[:len(key)]) == key:
                if isinstance(value, Exception):
                    self.problems.append("{}: {}".format(cmd[0], value))
                    return None
                return value
        return None


class InventoryTests(TmpCase):
    def test_apt_policy_classification(self):
        found = inv.classify_manual_packages(POLICY, ["ftp.rnl.tecnico.ulisboa.pt"])
        self.assertEqual(found["extra"], [("code", "https://packages.microsoft.com/repos/code")])
        self.assertEqual(found["local"], [("discord", "0.0.75")])  # libfoo1: stale lists, official

    def test_files_only_sections(self):
        h = self.home
        self.touch(".nvm/versions/node/v22.11.0/lib/node_modules/@anthropic-ai/claude-code/package.json",
                   json.dumps({"name": "@anthropic-ai/claude-code", "version": "2.0.1"}), base=h)
        self.touch(".nvm/versions/node/v22.11.0/lib/node_modules/npm/package.json", "{}", base=h)
        self.touch(".bun/install/global/package.json", json.dumps({"dependencies": {"vercel": "^39"}}), base=h)
        self.touch(".local/share/pipx/venvs/httpie/pipx_metadata.json",
                   json.dumps({"main_package": {"package_or_url": "httpie"}}), base=h)
        os.makedirs(os.path.join(h, ".local/share/uv/tools/ruff"))
        self.touch(".cargo/.crates.toml", '[v1]\n"ripgrep 14.1.0 (registry+https://github.com/rust-lang/crates.io-index)" = ["rg"]\n'
                   '"tool 0.1.0 (git+https://github.com/me/tool#abc)" = ["tool"]\n', base=h)
        self.touch(".ollama/models/manifests/registry.ollama.ai/library/llama3.2/3b", "{}", base=h)
        self.touch(".ollama/models/manifests/registry.ollama.ai/me/coder/latest", "{}", base=h)
        self.touch(".vscode/extensions/extensions.json",
                   json.dumps([{"identifier": {"id": "ms-python.python"}, "version": "1"}]), base=h)
        self.touch(".config/systemd/user/bot.service", "[Service]\n", base=h)
        os.makedirs(os.path.join(h, ".config/systemd/user/default.target.wants"))
        os.symlink(os.path.join(h, ".config/systemd/user/bot.service"),
                   os.path.join(h, ".config/systemd/user/default.target.wants/bot.service"))
        self.touch("Applications/Obsidian-1.7.7.AppImage", "x", base=h)
        runner = FakeRunner({
            ("snap", "list"): "Name Version Rev Tracking Publisher Notes\ncore22 1 1 latest/stable canonical** base\n"
                              "code 1.95 177 latest/stable vscode** classic\n",
            ("flatpak", "list"): "com.spotify.Client\tflathub\tsystem\n",
            ("crontab", "-l"): "0 3 * * * /home/ana/bin/limpar.sh\n",
            ("apt-mark",): RuntimeError("não existe"),
        })
        sections = inv.build(h, {}, runner)
        text = inv.render(sections, "portatil", "Linux Mint 22.2", "Cinnamon", "ana", h, "1.1.0",
                          _dt.datetime(2026, 11, 8, 14, 0))
        for expected in ("npm install -g @anthropic-ai/claude-code", "bun add -g vercel",
                         "pipx install httpie", "uv tool install ruff", "cargo install ripgrep",
                         "cargo install --git https://github.com/me/tool tool",
                         "ollama pull llama3.2:3b", "ollama pull me/coder:latest",
                         "code --install-extension ms-python.python",
                         "systemctl --user enable --now bot.service", "Obsidian-1.7.7.AppImage",
                         "sudo snap install code --classic", "flatpak install --system flathub com.spotify.Client",
                         "0 3 * * * /home/ana/bin/limpar.sh", "## Não foi possível verificar"):
            self.assertIn(expected, text)
        self.assertNotIn("core22", text)
        self.assertNotIn("install -g npm", text)
        # Docker: no socket here, so the docker CLI is never called.
        self.assertFalse(any(c[0] == "docker" for c in runner.calls))

    def test_lm_studio_models_and_readable_problems(self):
        h = self.home
        self.touch(".lmstudio/models/lmstudio-community/Qwen3-8B-GGUF/q4.gguf", "x", base=h)
        self.touch(".bun/install/global/package.json", json.dumps({"dependencies": ["x"]}), base=h)
        with unittest.mock.patch.object(inv, "section_docker", side_effect=RuntimeError("avaria")):
            sections = inv.build(h, {}, FakeRunner({}))
        text = inv.render(sections, "portatil", "Ubuntu", "GNOME", "ana", h, "1.1.0",
                          _dt.datetime(2026, 11, 8, 14, 0))
        self.assertIn("lms get lmstudio-community/Qwen3-8B-GGUF", text)
        self.assertIn("Docker: não foi possível ler (avaria)", text)
        self.assertNotIn("<lambda>", text)

    def test_missing_tools_give_an_empty_but_valid_file(self):
        runner = FakeRunner({})
        sections = inv.build(self.home, {}, runner)
        text = inv.render(sections, "pc", "Debian", "", "u", self.home, "1.1.0", _dt.datetime.now())
        self.assertTrue(text.startswith("# Programas instalados em «pc»"))

    def test_generation_line_alone_does_not_rewrite(self):
        target = os.path.join(self.tmp, "x.md")
        a = inv.render([], "pc", "Debian", "", "u", self.home, "1.1.0", _dt.datetime(2026, 1, 1))
        b = inv.render([], "pc", "Debian", "", "u", self.home, "1.1.0", _dt.datetime(2026, 1, 2))
        self.assertTrue(inv.write_if_changed(target, a))
        self.assertFalse(inv.write_if_changed(target, b))

    def test_real_runner_timeout_is_reported(self):
        runner = inv.Runner()
        self.assertIsNone(runner.run(["sleep", "5"], timeout=1.5))
        self.assertTrue(any("não respondeu" in p for p in runner.problems))
        self.assertIsNone(runner.run(["comando-que-nao-existe"]))


class PostRestoreTests(TmpCase):
    def test_relink(self):
        self.assertEqual(postrestore.relink("/home/ana/bin/x", "/home/ana", "/home/bruno"),
                         "/home/bruno/bin/x")
        self.assertEqual(postrestore.relink("/home/anabela/x", "/home/ana", "/home/bruno"),
                         "/home/anabela/x")
        self.assertEqual(postrestore.relink("../x", "/home/ana", "/home/bruno"), "../x")
        self.assertEqual(postrestore.relink("/usr/bin/x", "/home/ana", "/home/bruno"), "/usr/bin/x")
        moved = [("/home/ana/Documentos", "/home/bruno/Documents")]
        self.assertEqual(postrestore.relink("/home/ana/Documentos/a.sh", "/home/ana",
                                            "/home/bruno", moved), "/home/bruno/Documents/a.sh")
        self.assertEqual(postrestore.relink("/home/ana/bin/x", "/home/ana", "/home/bruno", moved),
                         "/home/bruno/bin/x")

    def test_shell_findings(self):
        self.touch(".bashrc", "alias ll='ls -l'\n"
                   ". \"$HOME/.cargo/env\"\n"
                   "[ -s \"$HOME/.nvm/nvm.sh\" ] && . \"$HOME/.nvm/nvm.sh\"\n"
                   "eval \"$(starship init bash)\"\n"
                   "command -v zoxide >/dev/null && eval \"$(zoxide init bash)\"\n"
                   "export PATH=/home/ana/bin:$PATH\n", base=self.home)
        found = postrestore.shell_findings(self.home, "/home/ana", which=lambda name: None, env={})
        self.assertEqual([number for _f, number, _l, _p in found], [2, 4, 6])

    def test_old_home_mentions(self):
        self.touch(".ssh/config", "IdentityFile /home/ana/.ssh/id_ed25519\n", base=self.home)
        self.touch(".gitconfig", "[user]\nname = Ana\n", base=self.home)
        found = postrestore.old_home_mentions(self.home, "/home/ana", [".ssh/config", ".gitconfig",
                                                                         "Documentos/nota.txt"])
        self.assertEqual(found, [".ssh/config"])
        self.assertEqual(postrestore.old_home_mentions(self.home, self.home, [".ssh/config"]), [])


class VenvInventoryTests(TmpCase):
    def test_reads_requirements_without_running_anything(self):
        venv = os.path.join(self.home, "Projetos", "app", ".venv")
        site = os.path.join(venv, "lib", "python3.12", "site-packages")
        self.touch("pyvenv.cfg", "home = /usr/bin\n", base=venv)
        self.touch("requests-2.32.3.dist-info/METADATA", "Name: requests\nVersion: 2.32.3\n\nbody",
                   base=site)
        self.touch("pip-24.0.dist-info/METADATA", "Name: pip\nVersion: 24.0\n", base=site)
        self.touch("mylib-0.1.dist-info/METADATA", "Name: mylib\nVersion: 0.1\n", base=site)
        self.touch("mylib-0.1.dist-info/direct_url.json",
                   json.dumps({"url": "file:///home/ana/mylib", "dir_info": {"editable": True}}),
                   base=site)
        os.makedirs(os.path.join(self.home, "Projetos", "app", "node_modules", "x", ".venv"))
        found = inv.find_venvs(os.path.join(self.home, "Projetos"))
        self.assertEqual(found, [venv])
        reqs = inv.requirements(venv)
        self.assertIn("requests==2.32.3", reqs)
        self.assertFalse([r for r in reqs if r.startswith("pip")])
        self.assertTrue(any("mylib" in r and "editável" in r for r in reqs))
        section = inv.section_venvs(self.home, [os.path.join(self.home, "Projetos")])
        self.assertIn("requests==2.32.3", section.raw)

    def test_data_outside_the_copy_is_reported(self):
        os.makedirs(os.path.join(self.home, ".local/share/containers/storage/volumes/n8n_data"))
        section = inv.section_outside_data(self.home)
        self.assertTrue(any("n8n_data" in n for n in section.notes))

    def test_generate_writes_once_and_respects_the_refresh_interval(self):
        target = os.path.join(self.tmp, "extras", inv.INVENTORY_NAME)
        args = dict(computer="pc", system="Debian", desktop="", user="u", home=self.home,
                    version="1.1.0", xdg={}, runner=FakeRunner({}))
        self.assertIsNone(inv.generate(target, **args))
        self.assertTrue(os.path.isfile(target))
        before = os.stat(target).st_mtime_ns
        os.unlink(target + ".verificado")
        args["runner"] = FakeRunner({})
        self.assertIsNone(inv.generate(target, **args))
        self.assertEqual(os.stat(target).st_mtime_ns, before, "unchanged content keeps its date")
        broken = dict(args, home=None)
        self.assertIsNone(inv.generate(target, **broken), "too recent: nothing is rebuilt")


if __name__ == "__main__":
    unittest.main()
