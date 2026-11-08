"""Small fixes and checks after restoring on a new system.

* relink(): absolute symlink targets inside the old home folder follow the new one.
* shell_findings(): lines of restored shell start-up files that will fail here (old home
  path, sourced files that do not exist, tools that are not installed yet).
* old_home_mentions(): restored settings files that still name the old home folder.

The user's files are never edited: rewriting them would change their size and break the
"restoring twice changes nothing" property. Problems are only reported.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from typing import Callable, Dict, List, Optional, Tuple

SHELL_FILES = (".bashrc", ".bash_profile", ".bash_aliases", ".profile", ".zshrc", ".zprofile",
               ".zshenv", ".config/fish/config.fish")

# Settings files worth checking for paths into the old home folder (home-relative globs).
SETTINGS_FILES = (".ssh/config", ".gitconfig", ".config/git/config", ".npmrc",
                  ".config/gtk-3.0/bookmarks", ".local/share/applications/*.desktop",
                  ".config/autostart/*.desktop", ".config/Code/User/settings.json",
                  ".config/Cursor/User/settings.json", ".claude.json") + SHELL_FILES


def relink(target: str, old_home: str, new_home: str) -> str:
    if not old_home or old_home == new_home or not os.path.isabs(target):
        return target
    if target == old_home or target.startswith(old_home + "/"):
        return new_home + target[len(old_home):]
    return target


_SOURCE = re.compile(r"""^\s*(?:source|\.)\s+["']?([^"'\s;|&]+)""")
_EVAL = re.compile(r"""eval\s+["']?\$\(\s*([\w.-]+)""")
_GUARD = re.compile(r"\[\s+-[sfe]\s|\[\[\s+-[sfe]\s|command -v|type -p|which |2>\s*/dev/null|\|\|")


def _expand(path: str, home: str, env: Dict[str, str]) -> str:
    path = path.replace("${HOME}", home).replace("$HOME", home)
    if path.startswith("~/"):
        path = home + path[1:]
    return re.sub(r"\$\{?(\w+)\}?", lambda m: env.get(m.group(1), m.group(0)), path)


def shell_findings(home: str, old_home: str = "",
                   which: Callable[[str], Optional[str]] = shutil.which,
                   env: Optional[Dict[str, str]] = None) -> List[Tuple[str, int, str, str]]:
    """(file, line number, line, problem) for lines that will probably fail here."""
    env = dict(os.environ if env is None else env)
    result = []
    for rel in SHELL_FILES:
        path = os.path.join(home, rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read(1 << 20).splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            if not code.strip():
                continue
            if old_home and old_home != home and re.search(re.escape(old_home) + r"(/|\b)", code):
                result.append(("~/" + rel, number, line.strip(),
                               "usa a pasta pessoal antiga ({}); troca por $HOME".format(old_home)))
                continue
            guarded = bool(_GUARD.search(code))
            match = _SOURCE.match(code)
            if match and not guarded:
                target = _expand(match.group(1), home, env)
                if "$" not in target and os.path.isabs(target) and not os.path.exists(target):
                    result.append(("~/" + rel, number, line.strip(),
                                   "o ficheiro {} não existe neste computador".format(
                                       target.replace(home, "~", 1))))
                    continue
            match = _EVAL.search(code)
            if match and not guarded and which(match.group(1)) is None:
                result.append(("~/" + rel, number, line.strip(),
                               "o programa «{}» ainda não está instalado".format(match.group(1))))
    return result


def old_home_mentions(home: str, old_home: str, restored: List[str]) -> List[str]:
    """Home-relative settings files among ``restored`` that still contain ``old_home``."""
    if not old_home or old_home == home:
        return []
    needle = (old_home.rstrip("/") + "/").encode("utf-8", "surrogateescape")
    found = []
    for rel in restored:
        if not any(fnmatch.fnmatchcase(rel, pattern) for pattern in SETTINGS_FILES):
            continue
        try:
            with open(os.path.join(home, rel), "rb") as fh:
                if needle in fh.read(256 * 1024):
                    found.append(rel)
        except OSError:
            continue
    return found
