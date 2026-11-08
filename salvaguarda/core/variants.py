"""Same app, different packaging (deb, snap, Flatpak).

Linux Mint ships Firefox as a .deb (~/.mozilla); Ubuntu ships it as a snap
(~/snap/firefox/common/.mozilla), others as a Flatpak. A profile restored to the old
place is never seen by the new app. This module knows where each packaging keeps its
data, which one is installed on this computer, and where a backed-up folder should go.
Only file checks: no subprocesses, no network, no root.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .layout import HOME_DIR

DEB = "deb"          # distro .deb, Mozilla's own .deb, or a tarball in /opt
SNAP = "snap"
FLATPAK = "flatpak"

KIND_LABELS = {DEB: "pacote .deb", SNAP: "snap", FLATPAK: "Flatpak"}



@dataclass(frozen=True)
class Variant:
    kind: str
    # (role, home-relative folder). The first folder of each role is where this
    # packaging reads today; later ones of the same role are older locations.
    roots: Tuple[Tuple[str, str], ...]
    probes: Tuple[str, ...] = ()    # paths ("~/" = home) whose existence means "installed"
    app_ini: Tuple[str, ...] = ()   # Mozilla application.ini, to read the installed version
    snap_wrapper: str = ""          # /usr/bin/<x> that may be Ubuntu's wrapper for the snap

    def target_for(self, role: str) -> Optional[str]:
        return next((path for r, path in self.roots if r == role), None)


def _fp(app_id: str) -> Tuple[str, str]:
    return ("~/.local/share/flatpak/app/" + app_id, "/var/lib/flatpak/app/" + app_id)


# Home-relative folders MUST match catalog.PRESETS paths, so mirror rels line up.
APP_VARIANTS: Dict[str, Tuple[Variant, ...]] = {
    "firefox": (
        Variant(DEB, (("profiles", ".mozilla"), ("profiles", ".config/mozilla")),
                probes=("/usr/lib/firefox/firefox", "/usr/lib/firefox-esr/firefox-esr",
                        "/opt/firefox/firefox"),
                app_ini=("/usr/lib/firefox/application.ini",
                         "/usr/lib/firefox-esr/application.ini",
                         "/opt/firefox/application.ini"),
                snap_wrapper="/usr/bin/firefox"),
        Variant(SNAP, (("profiles", "snap/firefox/common/.mozilla"),),
                probes=("/snap/firefox/current",),
                app_ini=("/snap/firefox/current/usr/lib/firefox/application.ini",)),
        Variant(FLATPAK, (("profiles", ".var/app/org.mozilla.firefox/.mozilla"),),
                probes=_fp("org.mozilla.firefox"),
                app_ini=tuple(p + "/current/active/files/lib/firefox/application.ini"
                              for p in _fp("org.mozilla.firefox"))),
    ),
    "thunderbird": (
        # The current Thunderbird snap reads ~/.thunderbird (personal-files plug
        # "dot-thunderbird"); older snap revisions used ~/snap/thunderbird/common.
        Variant(DEB, (("profiles", ".thunderbird"),
                      ("profiles", "snap/thunderbird/common/.thunderbird")),
                probes=("/usr/lib/thunderbird/thunderbird", "/opt/thunderbird/thunderbird",
                        "/snap/thunderbird/current"),
                app_ini=("/usr/lib/thunderbird/application.ini",
                         "/snap/thunderbird/current/usr/lib/thunderbird/application.ini")),
        Variant(FLATPAK, (("profiles", ".var/app/org.mozilla.Thunderbird/.thunderbird"),),
                probes=_fp("org.mozilla.Thunderbird")),
    ),
    "chromium": (
        Variant(DEB, (("profiles", ".config/chromium"),),
                probes=("/usr/lib/chromium/chromium",),
                snap_wrapper="/usr/bin/chromium-browser"),
        Variant(SNAP, (("profiles", "snap/chromium/common/chromium"),),
                probes=("/snap/chromium/current",)),
        Variant(FLATPAK, (("profiles", ".var/app/org.chromium.Chromium/config/chromium"),),
                probes=_fp("org.chromium.Chromium")),
    ),
    "brave": (
        Variant(DEB, (("profiles", ".config/BraveSoftware"),),
                probes=("/opt/brave.com/brave/brave",)),
        Variant(FLATPAK, (("profiles", ".var/app/com.brave.Browser/config/BraveSoftware"),),
                probes=_fp("com.brave.Browser")),
    ),
    "vscode": (
        # The VS Code snap uses classic confinement: same folders as the .deb.
        Variant(DEB, (("user", ".config/Code/User"), ("ext", ".vscode")),
                probes=("/usr/share/code/code", "/snap/code/current")),
        Variant(FLATPAK, (("user", ".var/app/com.visualstudio.code/config/Code/User"),
                          ("ext", ".var/app/com.visualstudio.code/data/vscode")),
                probes=_fp("com.visualstudio.code")),
    ),
    "libreoffice": (
        Variant(DEB, (("profile", ".config/libreoffice"),),
                probes=("/usr/lib/libreoffice/program/soffice",)),
        # Strict snap: data lives in ~/snap/libreoffice/<revision>, reached through the
        # "current" symlink that snapd creates the first time the snap runs.
        Variant(SNAP, (("profile", "snap/libreoffice/current/.config/libreoffice"),),
                probes=("/snap/libreoffice/current",)),
        Variant(FLATPAK, (("profile", ".var/app/org.libreoffice.LibreOffice/config/libreoffice"),),
                probes=_fp("org.libreoffice.LibreOffice")),
    ),
}

# When several packagings are installed and none matches the copy.
_PRIORITY = {DEB: 0, SNAP: 1, FLATPAK: 2}


class Probe:
    """File-system questions, with a root prefix so tests can fake a whole system."""

    def __init__(self, home: str, root: str = "/"):
        self.home = home
        self.root = root

    def resolve(self, path: str) -> str:
        if path.startswith("~/"):
            return os.path.join(self.home, path[2:])
        return os.path.join(self.root, path.lstrip("/"))

    def exists(self, path: str) -> bool:
        return os.path.lexists(self.resolve(path))

    def read_head(self, path: str, size: int = 4096) -> bytes:
        try:
            with open(self.resolve(path), "rb") as fh:
                return fh.read(size)
        except OSError:
            return b""

    def readlink(self, path: str) -> str:
        try:
            return os.readlink(self.resolve(path))
        except OSError:
            return ""


def _is_snap_wrapper(probe: Probe, path: str) -> bool:
    """Ubuntu's transitional .debs put a script in /usr/bin that ends in
    ``exec /snap/bin/<app> "$@"``; some systems link /usr/bin/<app> to /snap/bin."""
    if not path:
        return False
    if "/snap/" in probe.readlink(path):
        return True
    head = probe.read_head(path)
    return head.startswith(b"#!") and b"/snap/bin/" in head


def installed_kinds(app: str, probe: Probe) -> List[str]:
    variants = APP_VARIANTS.get(app, ())
    kinds = [v.kind for v in variants if any(probe.exists(p) for p in v.probes)]
    has_snap = any(v.kind == SNAP for v in variants)
    for v in variants:
        if v.snap_wrapper and has_snap and SNAP not in kinds and _is_snap_wrapper(probe, v.snap_wrapper):
            kinds.append(SNAP)
    return kinds


def locate(app: str, home_rel: str) -> Optional[Tuple[Variant, str, str]]:
    """(variant, role, matched root) for a home-relative folder of a preset."""
    for variant in APP_VARIANTS.get(app, ()):
        for role, root in variant.roots:
            if home_rel == root or home_rel.startswith(root + "/"):
                return variant, role, root
    return None


@dataclass
class Remap:
    app: str
    rel: str            # mirror rel, "Pasta pessoal/.mozilla"
    source: str         # packaging of the copy
    target: str         # packaging installed here
    destination: str    # home-relative destination root on this computer
    warnings: List[str] = field(default_factory=list)


def plan_remaps(app: str, rels: Sequence[str], probe: Probe) -> List[Remap]:
    """Where the rels of one restore item should go instead. Empty: restore as usual.

    * app not installed here: nothing moves (the user may install the same packaging);
    * the copy has data of a packaging that is installed here: nothing moves;
    * otherwise each role goes to the installed packaging (deb > snap > flatpak),
      taking only the first old folder of each role so two profiles never mix.
    """
    installed = installed_kinds(app, probe)
    if not installed:
        return []
    found = []
    for rel in rels:
        parts = rel.split("/", 1)
        if parts[0] != HOME_DIR or len(parts) < 2:
            continue
        hit = locate(app, parts[1])
        if hit is not None:
            found.append((rel, parts[1]) + hit)
    if {variant.kind for _, _, variant, _, _ in found} & set(installed):
        return []
    target_kind = sorted(installed, key=lambda k: _PRIORITY.get(k, 9))[0]
    target = next(v for v in APP_VARIANTS[app] if v.kind == target_kind)
    found.sort(key=lambda f: _PRIORITY.get(f[2].kind, 9))
    result: List[Remap] = []
    roles_done = set()
    for rel, home_rel, variant, role, root in found:
        dest_root = target.target_for(role)
        if dest_root is None or role in roles_done:
            continue
        roles_done.add(role)
        result.append(Remap(app, rel, variant.kind, target_kind,
                            dest_root + home_rel[len(root):]))
    for remap in result:
        snap = snap_user_dir_problem(remap.destination, probe)
        if snap:
            remap.warnings.append(
                "Abre o {0} uma vez e fecha-o antes de restaurar: a pasta ~/snap/{0}/current "
                "ainda não existe.".format(snap))
    return result


def snap_user_dir_problem(destination: str, probe: Probe) -> Optional[str]:
    """Restoring into ~/snap/<x>/current before the snap ever ran would create a real
    folder where snapd expects a symlink. Returns the snap name in that case."""
    match = re.match(r"snap/([^/]+)/current(/|$)", destination)
    if not match:
        return None
    if probe.readlink("~/snap/{}/current".format(match.group(1))):
        return None
    return match.group(1)


# --------------------------------------------------------- Mozilla profile checks
_VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: str) -> Optional[Tuple[int, int, int]]:
    match = _VERSION.match(text.strip())
    if not match:
        return None
    return tuple(int(g or 0) for g in match.groups())  # type: ignore[return-value]


def ini_value(text: str, section: str, key: str) -> str:
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
        elif current == section and "=" in line and line.split("=", 1)[0].strip() == key:
            return line.split("=", 1)[1].strip()
    return ""


def installed_mozilla_version(app: str, kind: str, probe: Probe) -> Optional[Tuple[int, int, int]]:
    for variant in APP_VARIANTS.get(app, ()):
        if variant.kind != kind:
            continue
        for ini in variant.app_ini:
            text = probe.read_head(ini, 65536).decode("utf-8", "replace")
            version = parse_version(ini_value(text, "App", "Version"))
            if version:
                return version
    return None


def profile_last_version(profile_dir: str) -> Optional[Tuple[int, int, int]]:
    """Version that last opened the profile (compatibility.ini, LastVersion=145.0.1_2025...)."""
    try:
        with open(os.path.join(profile_dir, "compatibility.ini"), encoding="utf-8",
                  errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return None
    return parse_version(ini_value(text, "Compatibility", "LastVersion").split("_")[0])


def default_profiles(base: str) -> List[str]:
    """Profile folders that profiles.ini names as default ([ProfileN] Default=1 and
    [Install<hash>] Default=<path>)."""
    try:
        with open(os.path.join(base, "profiles.ini"), encoding="utf-8", errors="replace") as fh:
            text = fh.read(262144)
    except OSError:
        return []
    result: List[str] = []
    name = ""
    section: Dict[str, str] = {}
    for line in text.splitlines() + ["[end]"]:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            if name.startswith("Profile") and section.get("Default") == "1" and section.get("Path"):
                relative = section.get("IsRelative", "1") == "1"
                result.append(os.path.join(base, section["Path"]) if relative else section["Path"])
            elif name.startswith("Install") and section.get("Default"):
                result.append(os.path.join(base, section["Default"]))
            name, section = line[1:-1], {}
        elif "=" in line:
            key, value = line.split("=", 1)
            section[key.strip()] = value.strip()
    unique: List[str] = []
    for path in result:
        if path not in unique:
            unique.append(path)
    return unique


def downgrade_warning(label: str, installed: Optional[Tuple[int, int, int]],
                      last: Optional[Tuple[int, int, int]]) -> Optional[str]:
    if installed is None or last is None or installed[:2] >= last[:2]:
        return None
    return ("O perfil do {app} foi usado pela última vez na versão {old}, mais recente do que "
            "a instalada neste computador ({new}). O {app} vai recusar abri-lo e propor um "
            "perfil novo. Instala uma versão igual ou mais recente antes de o abrires.").format(
                app=label, old=".".join(map(str, last[:2])), new=".".join(map(str, installed[:2])))
