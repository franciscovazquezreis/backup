"""Desktop environments and a safe way to apply a ``dconf dump /`` on another system.

At backup time the desktop in use is recorded in the computer's metadata. At restore
time the exported settings are filtered before ``dconf load``:

* on another desktop (Cinnamon to GNOME, for example) only the apps' own settings are
  applied, never the look and behaviour of the old desktop;
* keys naming a theme that is not installed here are dropped;
* paths into the old home folder (and old XDG folder names) follow the new ones.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

CINNAMON, GNOME, MATE, XFCE, KDE, BUDGIE, PANTHEON, COSMIC, LXQT, UNKNOWN = (
    "cinnamon", "gnome", "mate", "xfce", "kde", "budgie", "pantheon", "cosmic", "lxqt", "")

DESKTOP_LABELS = {
    CINNAMON: "Cinnamon", GNOME: "GNOME", MATE: "MATE", XFCE: "Xfce", KDE: "KDE Plasma",
    BUDGIE: "Budgie", PANTHEON: "Pantheon (elementary OS)", COSMIC: "COSMIC", LXQT: "LXQt",
}

# Tokens of XDG_CURRENT_DESKTOP, colon separated; the first known one wins
# ("Budgie:GNOME" is Budgie; "ubuntu:GNOME", "zorin:GNOME" and "pop:GNOME" are GNOME).
_TOKENS = {
    "x-cinnamon": CINNAMON, "cinnamon": CINNAMON, "gnome": GNOME, "gnome-classic": GNOME,
    "mate": MATE, "xfce": XFCE, "kde": KDE, "budgie": BUDGIE, "pantheon": PANTHEON,
    "cosmic": COSMIC, "lxqt": LXQT,
}
# Fallback without a session (command line, cron): process names as in /proc/<pid>/comm.
_PROCESSES = (
    ("cinnamon", CINNAMON), ("gnome-shell", GNOME), ("mate-session", MATE),
    ("xfce4-session", XFCE), ("plasmashell", KDE), ("budgie-wm", BUDGIE),
    ("budgie-panel", BUDGIE), ("gala", PANTHEON), ("cosmic-comp", COSMIC),
    ("lxqt-session", LXQT),
)


def current_desktop(environ: Optional[Dict[str, str]] = None,
                    processes: Iterable[str] = ()) -> str:
    environ = os.environ if environ is None else environ
    for name in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
        for token in (environ.get(name) or "").lower().replace(";", ":").split(":"):
            token = token.strip()
            if token in _TOKENS:
                return _TOKENS[token]
    running = set(processes)
    for process, desktop in _PROCESSES:
        if process in running:
            return desktop
    return UNKNOWN


def label(desktop: str) -> str:
    return DESKTOP_LABELS.get(desktop, "outro ambiente de trabalho")


# First line of the export written by the backup: which desktop it comes from.
HEADER_PREFIX = "# salvaguarda-ambiente:"
HOME_PREFIX = "# salvaguarda-pasta-pessoal:"
XDG_PREFIX = "# salvaguarda-pastas-xdg:"


def dump_header(desktop: str, home: str = "", xdg: Optional[Dict[str, str]] = None) -> str:
    """Comment lines (``dconf load`` ignores them) that say where an export comes from, so
    an old export restored from «Versões anteriores» does not depend on newer metadata."""
    lines = []
    if desktop:  # unknown (a run outside a session): say nothing, the metadata decides
        lines.append("{} {}".format(HEADER_PREFIX, desktop))
    if home:
        lines.append("{} {}".format(HOME_PREFIX, home))
    if xdg:
        lines.append("{} {}".format(XDG_PREFIX, json.dumps(xdg, ensure_ascii=True,
                                                           sort_keys=True)))
    return "".join(line + "\n" for line in lines)


def _header_value(text: str, prefix: str) -> Optional[str]:
    for line in text.splitlines()[:8]:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def desktop_in_header(text: str) -> Optional[str]:
    """Desktop recorded in the export itself, or None when it does not say."""
    return _header_value(text, HEADER_PREFIX) or None


def home_in_header(text: str) -> Tuple[str, Dict[str, str]]:
    """(old home folder, old XDG folders as "~/...") recorded in the export, or ("", {})."""
    home = _header_value(text, HOME_PREFIX) or ""
    xdg: Dict[str, str] = {}
    raw = _header_value(text, XDG_PREFIX)
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            xdg = {str(k): v for k, v in data.items() if isinstance(v, str)}
    return home, xdg


def export_source(text: str, recorded: str = "") -> str:
    """Desktop an export comes from: its own header first, then ``recorded`` (the
    metadata of the latest backup), then a guess from its sections. "" when unknown."""
    header = desktop_in_header(text)
    if header is not None:
        recorded = header
    return recorded or desktop_of_dump(split_dump(text))


def desktop_of_dump(sections: Sequence[Tuple[str, List[str]]]) -> str:
    """Guess the desktop that wrote a dump (disks from version 1.0.0 do not record it)."""
    names = {name for name, _ in sections}
    if any(n == "org/cinnamon" or n.startswith("org/cinnamon/") for n in names):
        return CINNAMON
    if any(n.startswith("org/mate/panel") for n in names):
        return MATE
    if any(n.startswith("org/gnome/shell") for n in names):
        return GNOME
    return UNKNOWN


# ------------------------------------------------------------------ dump parsing
_SECTION = re.compile(r"^\[(.*)\]\s*$")


def split_dump(text: str) -> List[Tuple[str, List[str]]]:
    sections: List[Tuple[str, List[str]]] = []
    current: Optional[Tuple[str, List[str]]] = None
    for line in text.splitlines():
        match = _SECTION.match(line)
        if match:
            current = (match.group(1).strip("/"), [])
            sections.append(current)
        elif current is not None and line.strip():
            current[1].append(line)
    return sections


def join_dump(sections: Sequence[Tuple[str, List[str]]]) -> str:
    out = []
    for name, lines in sections:
        if not lines:
            continue
        out.append("[{}]".format(name or "/"))
        out.extend(lines)
        out.append("")
    return "\n".join(out)


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + "/")


# Shared GNOME schemas that the running desktop reads for its own look and behaviour.
SHARED_DESKTOP = (
    "org/gnome/desktop/interface", "org/gnome/desktop/background",
    "org/gnome/desktop/screensaver", "org/gnome/desktop/wm", "org/gnome/desktop/sound",
    "org/gnome/desktop/input-sources", "org/gnome/desktop/session",
    "org/gnome/settings-daemon", "org/gnome/shell", "org/gnome/mutter",
)

# Settings owned by one desktop: harmless elsewhere, but meaningless.
OWN_DESKTOP = {
    CINNAMON: ("org/cinnamon", "org/gnome/libgnomekbd"),
    MATE: ("org/mate",),
    GNOME: ("org/gnome/shell", "org/gnome/mutter"),
    BUDGIE: ("com/solus-project", "org/ubuntubudgie"),
    PANTHEON: ("io/elementary",),
}


def classify(name: str, source_desktop: str) -> str:
    """'desktop' (look and behaviour of a desktop) or 'app' (an app's own settings)."""
    for prefix in OWN_DESKTOP.get(source_desktop, ()):
        if _under(name, prefix):
            return "desktop"
    for prefix in SHARED_DESKTOP:
        if _under(name, prefix):
            return "desktop"
    return "app"


# ------------------------------------------------------------------- themes
_THEME_KEYS = {"gtk-theme": "themes", "icon-theme": "icons", "cursor-theme": "icons",
               "theme-name": "themes"}
_THEME_SECTIONS = ("org/gnome/desktop/interface", "org/cinnamon/desktop/interface",
                   "org/mate/interface", "org/mate/peripherals-mouse", "org/cinnamon/theme",
                   "org/cinnamon/desktop/wm/preferences", "org/gnome/desktop/wm/preferences")


def theme_dirs(home: str, kind: str, root: str = "/") -> List[str]:
    system_dir = os.path.join(root, "usr", "share", kind)
    if kind == "themes":
        return [os.path.join(home, ".themes"), os.path.join(home, ".local", "share", "themes"),
                system_dir]
    return [os.path.join(home, ".icons"), os.path.join(home, ".local", "share", "icons"),
            system_dir]


def _gvariant_string(value: str) -> Optional[str]:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return None


def drop_missing_themes(sections: List[Tuple[str, List[str]]], home: str,
                        root: str = "/") -> List[str]:
    """Remove keys that name a theme not installed here. Returns the removed 'key=value'."""
    removed = []
    for name, lines in sections:
        if name not in _THEME_SECTIONS:
            continue
        keep = []
        for line in lines:
            key, _, value = line.partition("=")
            kind = _THEME_KEYS.get(key.strip())
            if name == "org/cinnamon/theme" and key.strip() == "name":
                kind = "themes"
            theme = _gvariant_string(value) if kind else None
            if kind and theme and not any(os.path.isdir(os.path.join(d, theme))
                                          for d in theme_dirs(home, kind, root)):
                removed.append("{}/{}={}".format(name, key.strip(), value.strip()))
                continue
            keep.append(line)
        lines[:] = keep
    return removed


# ------------------------------------------------------------- path rewriting
def path_pairs(old_home: str, new_home: str, old_xdg: Dict[str, str],
               new_xdg: Dict[str, str]) -> List[Tuple[str, str]]:
    """(old, new) absolute path prefixes, longest first.

    ``old_xdg`` maps XDG keys to portable paths ("~/Imagens") recorded at backup time,
    ``new_xdg`` comes from paths.xdg_user_dirs() on this computer.
    """
    pairs = []
    for key, old in old_xdg.items():
        new = new_xdg.get(key)
        # Only folders inside the old home are moved by the restore (see Destinations):
        # a folder on a data partition (/mnt/dados/Fotos) keeps its place and its paths.
        if not new or not old or not old.startswith("~/") or len(old) <= 2:
            continue
        old_abs = old_home + old[1:]
        if old_home and old_abs != new:
            pairs.append((old_abs, new))
    if old_home and old_home != new_home:
        pairs.append((old_home, new_home))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def rewrite_paths(text: str, pairs: Sequence[Tuple[str, str]]) -> Tuple[str, int]:
    """Replace path prefixes in plain form and in file:// URI form (percent-encoded)."""
    count = 0
    for old, new in pairs:
        for o, n in ((old, new), (quote(old), quote(new))):
            # Only at a path boundary: '/home/ana' must not touch '/home/anabela'.
            pattern = re.compile(re.escape(o) + r"(?=[/'\"\s,\]]|$)")
            text, done = pattern.subn(lambda _m, n=n: n, text)
            count += done
    return text, count


class DesktopPlan:
    """The text to give to ``dconf load /`` on this computer, and what was left out."""

    def __init__(self, text: str, source_desktop: str, here: str, home: str,
                 pairs: Sequence[Tuple[str, str]] = (), root: str = "/"):
        self.sections = split_dump(text)
        self.source = export_source(text, source_desktop)
        self.here = here
        self.same_desktop = bool(self.source) and self.source == here
        self.unknown = not self.source or not here
        self.skipped: List[str] = []
        if not self.same_desktop:
            kept = []
            for name, lines in self.sections:
                if classify(name, self.source) == "desktop":
                    self.skipped.append(name)
                else:
                    kept.append((name, lines))
            self.sections = kept
        self.removed_themes = drop_missing_themes(self.sections, home, root)
        self.text, self.rewritten = rewrite_paths(join_dump(self.sections), pairs)

    def notes(self) -> List[str]:
        notes = []
        if self.skipped and self.unknown:
            notes.append(
                "Não foi possível confirmar que este computador usa o mesmo ambiente de "
                "trabalho da cópia: foram aplicadas só as definições das apps. O ficheiro "
                "completo continua no disco, em «Extras».")
        elif self.skipped:
            notes.append(
                "As definições do ambiente de trabalho ({old}) não foram aplicadas, porque "
                "este computador usa {new}. Foram aplicadas só as definições das apps. O "
                "ficheiro completo continua no disco, em «Extras».".format(
                    old=label(self.source), new=label(self.here)))
        if self.removed_themes:
            count = len(self.removed_themes)
            if count == 1:
                notes.append("Uma definição de aparência não foi aplicada porque o tema não "
                             "está instalado neste computador.")
            else:
                notes.append("{} definições de aparência não foram aplicadas porque os temas "
                             "não estão instalados neste computador.".format(count))
        return notes
