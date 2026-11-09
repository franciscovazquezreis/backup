"""Exclusion rules: what is never worth copying.

Patterns follow a small subset of the .gitignore syntax:

* ``*`` matches anything except ``/``; ``**`` also crosses folders; ``?`` one character.
* A trailing ``/`` restricts the pattern to folders.
* Without any other ``/`` the pattern matches the name at any depth (``*.iso``).
* ``~/x/y`` or ``x/y`` is relative to the home folder; ``/x/y`` is an absolute path.

Besides patterns there are marker files: a folder that contains ``pyvenv.cfg`` is a
Python virtual environment, ``conda-meta`` marks a conda environment and a valid
``CACHEDIR.TAG`` (https://bford.info/cachedir/) marks any cache folder.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .catalog import CAT_CACHES, CAT_DEV, CAT_TOOLS, PRESETS

CAT_ALWAYS = "always"
CAT_CUSTOM = "custom"

CATEGORY_LABELS = {
    CAT_ALWAYS: "Nunca copiados",
    CAT_CACHES: "Caches, lixo e temporários",
    CAT_DEV: "Dependências e ficheiros gerados de projetos",
    CAT_TOOLS: "Ferramentas e pacotes reinstaláveis",
    CAT_CUSTOM: "Exclusões personalizadas",
}

CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"

# (pattern, category). Home-relative patterns contain a slash.
_ALWAYS = (
    ("lost+found/", CAT_ALWAYS),
    ("~/.gvfs/", CAT_ALWAYS),
    (".local/state/salvaguarda/", CAT_ALWAYS),
)

_CACHES = (
    (".cache/", CAT_CACHES),
    (".Trash-*/", CAT_CACHES),
    (".thumbnails/", CAT_CACHES),
    (".~lock.*#", CAT_CACHES),
    ("~$*", CAT_CACHES),
    ("*.crdownload", CAT_CACHES),
    ("*.part", CAT_CACHES),
    (".goutputstream-*", CAT_CACHES),
    (".*.swp", CAT_CACHES),
    (".DS_Store", CAT_CACHES),
    ("Thumbs.db", CAT_CACHES),
    (".local/share/Trash/", CAT_CACHES),
    (".var/app/*/cache/", CAT_CACHES),
    ("snap/*/*/.cache/", CAT_CACHES),
    (".local/share/baloo/", CAT_CACHES),
    (".local/share/tracker/", CAT_CACHES),
    ("~/.nv/", CAT_CACHES),
    ("~/.xsession-errors", CAT_CACHES),
    ("~/.xsession-errors.old", CAT_CACHES),
)

_DEV = (
    ("node_modules/", CAT_DEV),
    ("bower_components/", CAT_DEV),
    ("__pycache__/", CAT_DEV),
    ("*.pyc", CAT_DEV),
    ("*.pyo", CAT_DEV),
    (".pytest_cache/", CAT_DEV),
    (".mypy_cache/", CAT_DEV),
    (".ruff_cache/", CAT_DEV),
    (".tox/", CAT_DEV),
    (".nox/", CAT_DEV),
    (".next/", CAT_DEV),
    (".nuxt/", CAT_DEV),
    (".svelte-kit/", CAT_DEV),
    (".astro/", CAT_DEV),
    (".turbo/", CAT_DEV),
    (".parcel-cache/", CAT_DEV),
    (".vite/", CAT_DEV),
    (".docusaurus/", CAT_DEV),
    (".expo/", CAT_DEV),
    (".dart_tool/", CAT_DEV),
    (".terraform/", CAT_DEV),
    (".angular/", CAT_DEV),
    (".pnpm-store/", CAT_DEV),
    (".nyc_output/", CAT_DEV),
    (".eslintcache", CAT_DEV),
)

_TOOLS = (
    ("~/.npm/", CAT_TOOLS),
    ("~/.npm-global/", CAT_TOOLS),
    (".cargo/registry/", CAT_TOOLS),
    (".cargo/git/", CAT_TOOLS),
    ("~/.rustup/", CAT_TOOLS),
    (".gradle/caches/", CAT_TOOLS),
    (".gradle/wrapper/dists/", CAT_TOOLS),
    (".gradle/daemon/", CAT_TOOLS),
    (".gradle/native/", CAT_TOOLS),
    (".m2/repository/", CAT_TOOLS),
    (".nvm/versions/", CAT_TOOLS),
    (".nvm/.cache/", CAT_TOOLS),
    (".pyenv/versions/", CAT_TOOLS),
    (".volta/tools/", CAT_TOOLS),
    (".sdkman/candidates/", CAT_TOOLS),
    (".asdf/installs/", CAT_TOOLS),
    (".local/share/mise/installs/", CAT_TOOLS),
    (".local/share/fnm/", CAT_TOOLS),
    (".local/share/pnpm/", CAT_TOOLS),
    (".bun/install/cache/", CAT_TOOLS),
    (".yarn/berry/cache/", CAT_TOOLS),
    ("go/pkg/mod/", CAT_TOOLS),
    (".local/lib/python*/", CAT_TOOLS),
    (".local/share/uv/python/", CAT_TOOLS),
    (".local/share/claude/versions/", CAT_TOOLS),
    (".local/share/virtualenvs/", CAT_TOOLS),
    (".conda/pkgs/", CAT_TOOLS),
    ("miniconda3/pkgs/", CAT_TOOLS),
    ("anaconda3/pkgs/", CAT_TOOLS),
    (".platformio/packages/", CAT_TOOLS),
    ("Android/Sdk/", CAT_TOOLS),
    (".android/avd/", CAT_TOOLS),
    (".android/cache/", CAT_TOOLS),
    ("~/.vscode-server/", CAT_TOOLS),
    ("~/.cursor-server/", CAT_TOOLS),
    ("~/.windsurf-server/", CAT_TOOLS),
    (".local/share/flatpak/", CAT_TOOLS),
    (".local/share/containers/", CAT_TOOLS),
    (".local/share/docker/", CAT_TOOLS),
    (".ollama/models/", CAT_TOOLS),
    (".lmstudio/models/", CAT_TOOLS),
    (".local/share/Steam/", CAT_TOOLS),
    ("~/.steam/", CAT_TOOLS),
    (".local/share/JetBrains/Toolbox/apps/", CAT_TOOLS),
)

# Folder markers: (file name inside the folder, category, description)
_MARKERS = (
    ("pyvenv.cfg", CAT_DEV, "ambiente virtual Python"),
    ("conda-meta", CAT_DEV, "ambiente conda"),
)


def glob_to_regex(pattern: str) -> str:
    """Translate a glob into a regular expression (without anchors)."""
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:j].replace("\\", "\\\\")
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class _Rule:
    __slots__ = ("pattern", "category", "dir_only", "regex", "label")

    def __init__(self, pattern: str, category: str, dir_only: bool, label: str):
        self.pattern = pattern
        self.category = category
        self.dir_only = dir_only
        self.regex = re.compile(glob_to_regex(pattern) + r"\Z", re.DOTALL)
        self.label = label


class _RuleGroup:
    """All rules of one scope, with a combined regex for the common no-match case."""

    def __init__(self) -> None:
        self.rules: List[_Rule] = []
        self._any: Optional[re.Pattern] = None
        self._all: Optional[re.Pattern] = None

    def add(self, rule: _Rule) -> None:
        self.rules.append(rule)

    def compile(self) -> None:
        files = [r.regex.pattern for r in self.rules if not r.dir_only]
        every = [r.regex.pattern for r in self.rules]
        self._any = re.compile("|".join("(?:%s)" % p for p in files), re.DOTALL) if files else None
        self._all = re.compile("|".join("(?:%s)" % p for p in every), re.DOTALL) if every else None

    def match(self, text: str, is_dir: bool) -> Optional[_Rule]:
        combined = self._all if is_dir else self._any
        if combined is None or not combined.match(text):
            return None
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(text):
                return rule
        return None


def _split_pattern(raw: str) -> Optional[Tuple[str, str, bool]]:
    """Return (scope, pattern, dir_only) for a user or built-in pattern."""
    pattern = raw.strip()
    if not pattern or pattern.startswith("#"):
        return None
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if not pattern:
        return None
    if pattern.startswith("~/"):
        return "home", pattern[2:], dir_only
    if pattern == "~":
        return None
    if pattern.startswith("/"):
        return "abs", pattern, dir_only
    if "/" in pattern:
        return "home", pattern, dir_only
    return "name", pattern, dir_only


def preset_rules() -> List[Tuple[str, str]]:
    """Exclusions declared by the settings catalogue, expressed relative to home."""
    result: List[Tuple[str, str]] = []
    for preset in PRESETS:
        for root in preset.paths:
            for pattern, category in preset.excludes:
                dir_only = pattern.endswith("/")
                body = pattern.rstrip("/")
                if "/" in body:
                    full = root + "/" + body
                else:
                    full = root + "/**/" + body
                result.append((full + ("/" if dir_only else ""), category))
        result.extend(preset.home_excludes)
    return result


class Exclusions:
    """Decides which entries are skipped. Never applied to a source root itself."""

    def __init__(
        self,
        caches: bool = True,
        dev: bool = True,
        tools: bool = True,
        custom: Sequence[str] = (),
    ) -> None:
        self.categories = {CAT_ALWAYS, CAT_CUSTOM}
        if caches:
            self.categories.add(CAT_CACHES)
        if dev:
            self.categories.add(CAT_DEV)
        if tools:
            self.categories.add(CAT_TOOLS)
        self._groups: Dict[str, _RuleGroup] = {
            "name": _RuleGroup(), "home": _RuleGroup(), "abs": _RuleGroup()
        }
        builtin: List[Tuple[str, str]] = list(_ALWAYS)
        builtin.extend(_CACHES)
        builtin.extend(_DEV)
        builtin.extend(_TOOLS)
        builtin.extend(preset_rules())
        for raw, category in builtin:
            if category in self.categories:
                self._add(raw, category)
        for raw in custom:
            self._add(raw, CAT_CUSTOM)
        for group in self._groups.values():
            group.compile()
        self.markers = [m for m in _MARKERS if m[1] in self.categories]
        self.cachedir_tags = CAT_CACHES in self.categories

    def _add(self, raw: str, category: str) -> None:
        parsed = _split_pattern(raw)
        if parsed is None:
            return
        scope, pattern, dir_only = parsed
        if scope == "home":
            label = "~/" + pattern
        else:
            label = pattern
        self._groups[scope].add(_Rule(pattern, category, dir_only, label))

    def match(self, name: str, path: str, home_rel: Optional[str], is_dir: bool
              ) -> Optional[Tuple[str, str]]:
        """Return (category, label) when the entry must be skipped."""
        rule = self._groups["name"].match(name, is_dir)
        if rule is None and home_rel is not None:
            rule = self._groups["home"].match(home_rel, is_dir)
        if rule is None:
            rule = self._groups["abs"].match(path, is_dir)
        if rule is None:
            return None
        return rule.category, rule.label

    def match_listing(self, folder: str, names: Iterable[str]) -> Optional[Tuple[str, str]]:
        """Check marker files found inside ``folder`` (already listed by the caller)."""
        if not self.markers and not self.cachedir_tags:
            return None
        present = names if isinstance(names, (set, frozenset, dict)) else set(names)
        for marker, category, description in self.markers:
            if marker in present:
                return category, "{} ({})".format(os.path.basename(folder), description)
        if self.cachedir_tags and "CACHEDIR.TAG" in present:
            try:
                with open(os.path.join(folder, "CACHEDIR.TAG"), "rb") as fh:
                    if fh.read(len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE:
                        return CAT_CACHES, "{} (pasta de cache)".format(os.path.basename(folder))
            except OSError:
                pass
        return None


def validate_custom(lines: Iterable[str]) -> List[str]:
    """Clean user patterns, dropping blanks, comments and patterns that would exclude everything."""
    result = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.strip("/*~ ") == "":
            continue
        result.append(text)
    return result
