"""What the app knows how to back up: personal folders, app settings, projects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import paths

XDG_LABELS = {
    "DESKTOP": "Ambiente de trabalho",
    "DOCUMENTS": "Documentos",
    "DOWNLOAD": "Transferências",
    "MUSIC": "Música",
    "PICTURES": "Imagens",
    "VIDEOS": "Vídeos",
    "TEMPLATES": "Modelos",
    "PUBLICSHARE": "Público",
}

XDG_ICONS = {
    "DESKTOP": "user-desktop-symbolic",
    "DOCUMENTS": "folder-documents-symbolic",
    "DOWNLOAD": "folder-download-symbolic",
    "MUSIC": "folder-music-symbolic",
    "PICTURES": "folder-pictures-symbolic",
    "VIDEOS": "folder-videos-symbolic",
    "TEMPLATES": "folder-templates-symbolic",
    "PUBLICSHARE": "folder-publicshare-symbolic",
}

# Exclusion categories, also used by the exclusion engine and the reports.
CAT_CACHES = "caches"
CAT_DEV = "dev"
CAT_TOOLS = "tools"

# Browser data that Chromium-based browsers keep inside ~/.config but can
# download or rebuild at any time.
_CHROMIUM_CACHES = tuple(
    (name, CAT_CACHES)
    for name in (
        "Cache/", "Code Cache/", "GPUCache/", "DawnCache/", "DawnGraphiteCache/",
        "DawnWebGPUCache/", "GraphiteDawnCache/", "GrShaderCache/", "ShaderCache/",
        "component_crx_cache/", "extensions_crx_cache/", "optimization_guide_model_store/",
        "Crashpad/", "Safe Browsing/", "OnDeviceHeadSuggestModel/", "screen_ai/",
        "SODALanguagePacks/", "WasmTtsEngine/",
        "**/Service Worker/CacheStorage/", "**/Service Worker/ScriptCache/",
    )
)

_MOZILLA_JUNK = (
    ("*/*/storage/default/*/cache/", CAT_CACHES),
    ("*/*/crashes/", CAT_CACHES),
    ("*/*/minidumps/", CAT_CACHES),
    ("*/*/datareporting/", CAT_CACHES),
    ("*/*/saved-telemetry-pings/", CAT_CACHES),
    ("*/*/startupCache/", CAT_CACHES),
    ("*/*/shader-cache/", CAT_CACHES),
    ("*/Crash Reports/", CAT_CACHES),
    ("*/Pending Pings/", CAT_CACHES),
)

_THUNDERBIRD_JUNK = (
    ("*/crashes/", CAT_CACHES),
    ("*/minidumps/", CAT_CACHES),
    ("*/datareporting/", CAT_CACHES),
    ("*/saved-telemetry-pings/", CAT_CACHES),
    ("Crash Reports/", CAT_CACHES),
    ("Pending Pings/", CAT_CACHES),
)

_EDITOR_EXTENSIONS = (("extensions/*/", CAT_TOOLS),)


@dataclass(frozen=True)
class Preset:
    id: str
    group: str
    label: str
    description: str
    icon: str
    paths: Tuple[str, ...]  # relative to the home folder
    excludes: Tuple[Tuple[str, str], ...] = ()  # (pattern relative to each path, category)
    processes: Tuple[str, ...] = ()  # names as in /proc/<pid>/comm
    extra: Optional[str] = None
    default_on: bool = True
    home_excludes: Tuple[Tuple[str, str], ...] = ()  # (pattern relative to home, category)


GROUPS = (
    ("browsers", "Navegadores"),
    ("email", "E-mail"),
    ("security", "Chaves e palavras-passe"),
    ("dev", "Programação e assistentes de IA"),
    ("desktop", "Ambiente de trabalho"),
    ("office", "Escritório"),
    ("advanced", "Avançado"),
)

PRESETS: Tuple[Preset, ...] = (
    # Browsers
    Preset(
        "firefox", "browsers", "Firefox",
        "Marcadores, palavras-passe, histórico e extensões",
        "web-browser-symbolic",
        (".mozilla", ".config/mozilla", "snap/firefox/common/.mozilla",
         ".var/app/org.mozilla.firefox/.mozilla"),
        _MOZILLA_JUNK, ("firefox", "firefox-bin", "firefox-esr"),
    ),
    Preset(
        "librewolf", "browsers", "LibreWolf",
        "Marcadores, palavras-passe, histórico e extensões",
        "web-browser-symbolic",
        (".librewolf", ".var/app/io.gitlab.librewolf-community/.librewolf"),
        (("*/storage/default/*/cache/", CAT_CACHES), ("*/crashes/", CAT_CACHES),
         ("*/minidumps/", CAT_CACHES), ("*/startupCache/", CAT_CACHES)),
        ("librewolf",),
    ),
    Preset(
        "chrome", "browsers", "Google Chrome",
        "Marcadores, palavras-passe, histórico e extensões",
        "web-browser-symbolic",
        (".config/google-chrome",), _CHROMIUM_CACHES, ("chrome",),
    ),
    Preset(
        "chromium", "browsers", "Chromium",
        "Marcadores, palavras-passe, histórico e extensões",
        "web-browser-symbolic",
        (".config/chromium", "snap/chromium/common/chromium",
         ".var/app/org.chromium.Chromium/config/chromium"),
        _CHROMIUM_CACHES, ("chromium", "chromium-browse"),
    ),
    Preset(
        "brave", "browsers", "Brave",
        "Marcadores, palavras-passe, histórico e extensões",
        "web-browser-symbolic",
        (".config/BraveSoftware", ".var/app/com.brave.Browser/config/BraveSoftware"),
        _CHROMIUM_CACHES, ("brave",),
    ),
    Preset(
        "edge", "browsers", "Microsoft Edge",
        "Marcadores, palavras-passe, histórico e extensões",
        "web-browser-symbolic",
        (".config/microsoft-edge",), _CHROMIUM_CACHES, ("msedge",),
    ),
    Preset(
        "vivaldi", "browsers", "Vivaldi",
        "Marcadores, palavras-passe, histórico e extensões",
        "web-browser-symbolic",
        (".config/vivaldi",), _CHROMIUM_CACHES, ("vivaldi-bin",),
    ),
    Preset(
        "opera", "browsers", "Opera",
        "Marcadores, palavras-passe, histórico e extensões",
        "web-browser-symbolic",
        (".config/opera",), _CHROMIUM_CACHES, ("opera",),
    ),
    # E-mail
    Preset(
        "thunderbird", "email", "Thunderbird",
        "Contas, mensagens guardadas no computador, contactos e filtros",
        "mail-unread-symbolic",
        (".thunderbird", "snap/thunderbird/common/.thunderbird",
         ".var/app/org.mozilla.Thunderbird/.thunderbird"),
        _THUNDERBIRD_JUNK, ("thunderbird", "thunderbird-bin"),
    ),
    Preset(
        "evolution", "email", "Evolution",
        "Contas, mensagens, contactos e calendários",
        "mail-unread-symbolic",
        (".local/share/evolution", ".config/evolution"),
        (), ("evolution",),
    ),
    # Security
    Preset(
        "ssh", "security", "Chaves SSH",
        "Chaves de acesso a servidores, ao GitHub e afins",
        "network-server-symbolic", (".ssh",),
    ),
    Preset(
        "gpg", "security", "Chaves GPG",
        "Chaves para assinar e cifrar",
        "channel-secure-symbolic", (".gnupg",),
    ),
    Preset(
        "keyrings", "security", "Porta-chaves do sistema",
        "Palavras-passe guardadas por apps e pelo ambiente de trabalho",
        "dialog-password-symbolic", (".local/share/keyrings",),
    ),
    Preset(
        "keepassxc", "security", "KeePassXC",
        "Definições (a base de dados .kdbx fica onde a guardaste)",
        "dialog-password-symbolic",
        (".config/keepassxc", ".var/app/org.keepassxc.KeePassXC/config/keepassxc"),
        (), ("keepassxc",),
    ),
    # Development and AI assistants
    Preset(
        "git", "dev", "Git e GitHub CLI",
        "Identidade, aliases e sessão do gh",
        "emblem-shared-symbolic",
        (".gitconfig", ".config/git", ".git-credentials", ".config/gh"),
    ),
    Preset(
        "ai", "dev", "Assistentes de IA",
        "Claude Code, Codex, Gemini CLI, Copilot, Continue e OpenCode: "
        "definições, servidores MCP e histórico",
        "dialog-information-symbolic",
        (".claude", ".claude.json", ".codex", ".gemini", ".config/github-copilot",
         ".continue", ".config/opencode", ".local/share/opencode", ".aider.conf.yml"),
        home_excludes=((".continue/index/", CAT_CACHES),),
    ),
    Preset(
        "vscode", "dev", "Visual Studio Code",
        "Definições, atalhos, snippets e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/Code/User", ".vscode",
         ".var/app/com.visualstudio.code/config/Code/User"),
        _EDITOR_EXTENSIONS, ("code",),
    ),
    Preset(
        "vscodium", "dev", "VSCodium",
        "Definições, atalhos, snippets e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/VSCodium/User", ".vscode-oss",
         ".var/app/com.vscodium.codium/config/VSCodium/User"),
        _EDITOR_EXTENSIONS, ("codium",),
    ),
    Preset(
        "cursor", "dev", "Cursor",
        "Definições, atalhos, regras, servidores MCP e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/Cursor/User", ".cursor"),
        _EDITOR_EXTENSIONS, ("cursor",),
    ),
    Preset(
        "windsurf", "dev", "Windsurf",
        "Definições, memórias, servidores MCP e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/Windsurf/User", ".windsurf", ".codeium/windsurf"),
        _EDITOR_EXTENSIONS, ("windsurf",),
    ),
    Preset(
        "zed", "dev", "Zed",
        "Definições, atalhos e temas",
        "accessories-text-editor-symbolic", (".config/zed",), (), ("zed", "zed-editor"),
    ),
    Preset(
        "jetbrains", "dev", "IDEs JetBrains",
        "Definições do IntelliJ, PyCharm, WebStorm e afins",
        "accessories-text-editor-symbolic", (".config/JetBrains",),
    ),
    Preset(
        "shell", "dev", "Terminal e shell",
        "bash, zsh, fish, tmux, vim, neovim e emuladores de terminal",
        "utilities-terminal-symbolic",
        (".bashrc", ".bash_aliases", ".bash_profile", ".bash_logout", ".profile",
         ".bash_history", ".zshrc", ".zprofile", ".zshenv", ".zsh_history", ".p10k.zsh",
         ".oh-my-zsh", ".config/fish", ".inputrc", ".tmux.conf", ".config/tmux",
         ".vimrc", ".vim", ".config/nvim", ".config/starship.toml", ".config/alacritty",
         ".config/kitty", ".config/terminator", ".config/tilix"),
        (("cache/", CAT_CACHES),),
    ),
    Preset(
        "scripts", "dev", "Scripts pessoais",
        "Programas e scripts em ~/bin e ~/.local/bin",
        "application-x-executable-symbolic", ("bin", ".local/bin"),
    ),
    Preset(
        "pkgconf", "dev", "npm, pip, Cargo e Docker",
        "Configuração e credenciais dos gestores de pacotes",
        "package-x-generic-symbolic",
        (".npmrc", ".yarnrc", ".yarnrc.yml", ".config/pip", ".pypirc",
         ".cargo/config.toml", ".cargo/credentials.toml", ".docker/config.json"),
    ),
    # Desktop
    Preset(
        "desktop", "desktop", "Definições do ambiente de trabalho",
        "Painel, atalhos de teclado, aparência, fundo, applets e marcadores do "
        "gestor de ficheiros",
        "preferences-desktop-appearance-symbolic",
        (".local/share/cinnamon", ".config/cinnamon", ".cinnamon", ".config/autostart",
         ".local/share/applications", ".config/menus", ".config/gtk-3.0",
         ".config/gtk-4.0", ".config/mimeapps.list", ".config/nemo", ".local/share/nemo",
         ".config/caja", ".config/xfce4", ".local/share/gnome-shell/extensions"),
        (), (), extra="dconf",
    ),
    Preset(
        "appearance", "desktop", "Tipos de letra, temas e ícones",
        "Instalados por ti na pasta pessoal",
        "preferences-desktop-font-symbolic",
        (".local/share/fonts", ".fonts", ".themes", ".local/share/themes", ".icons",
         ".local/share/icons"),
    ),
    # Office
    Preset(
        "libreoffice", "office", "LibreOffice",
        "Dicionário pessoal, correção automática, modelos e definições",
        "x-office-document-symbolic",
        (".config/libreoffice", "snap/libreoffice/current/.config/libreoffice",
         ".var/app/org.libreoffice.LibreOffice/config/libreoffice"),
        (), ("soffice.bin",),
    ),
    # Advanced
    Preset(
        "config_all", "advanced", "Toda a pasta .config",
        "Definições de todas as apps. Ocupa mais espaço e pode trazer definições "
        "antigas que já não interessam",
        "emblem-system-symbolic", (".config",), default_on=False,
    ),
)

PRESETS_BY_ID: Dict[str, Preset] = {p.id: p for p in PRESETS}

# Folder names that usually hold programming projects.
PROJECT_FOLDER_NAMES = {
    "projetos", "projectos", "projects", "project", "dev", "code", "src", "repos",
    "repositorios", "repositórios", "git", "github", "gitlab", "workspace",
    "workspaces", "vibe", "vibecoding", "vibe-coding", "vibe_coding", "programação",
    "programacao", "sites", "apps", "coding",
}

PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt",
                   "Cargo.toml", "go.mod", "composer.json", "Gemfile", "deno.json")

# Home subfolders never suggested as "other folders".
_NOT_SUGGESTED = {"snap", "lost+found"}


def preset_existing_paths(preset: Preset) -> List[str]:
    """Absolute paths of a preset that exist on this computer."""
    result = []
    user_home = paths.home()
    for rel in preset.paths:
        candidate = os.path.join(user_home, rel)
        if os.path.lexists(candidate):
            result.append(candidate)
    return result


def detected_presets() -> List[Preset]:
    found = []
    for preset in PRESETS:
        if preset_existing_paths(preset) or preset.extra == "dconf" and _has_dconf():
            found.append(preset)
    return found


def _has_dconf() -> bool:
    from shutil import which

    return which("dconf") is not None


def _looks_like_project(path: str) -> bool:
    return any(os.path.exists(os.path.join(path, marker)) for marker in PROJECT_MARKERS)


def _contains_projects(path: str, limit: int = 300) -> bool:
    try:
        with os.scandir(path) as it:
            for index, entry in enumerate(it):
                if index >= limit:
                    break
                try:
                    if entry.is_dir(follow_symlinks=False) and _looks_like_project(entry.path):
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


@dataclass
class HomeScan:
    project_dirs: List[str] = field(default_factory=list)
    other_dirs: List[str] = field(default_factory=list)
    loose_files: int = 0


def scan_home() -> HomeScan:
    """Classify the visible folders of the home folder that are not XDG folders."""
    result = HomeScan()
    user_home = paths.home()
    xdg = {os.path.normpath(p) for p in paths.xdg_user_dirs().values()}
    try:
        entries = sorted(os.scandir(user_home), key=lambda e: e.name.lower())
    except OSError:
        return result
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=True)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            continue
        if is_file:
            result.loose_files += 1
            continue
        if not is_dir or entry.name in _NOT_SUGGESTED:
            continue
        if os.path.normpath(entry.path) in xdg:
            continue
        if entry.name in ("bin",):
            continue  # covered by the "Scripts pessoais" preset
        name = entry.name.lower()
        if (name in PROJECT_FOLDER_NAMES or _looks_like_project(entry.path)
                or _contains_projects(entry.path)):
            result.project_dirs.append(entry.path)
        else:
            result.other_dirs.append(entry.path)
    return result


def loose_home_files() -> List[str]:
    """Visible regular files (and symlinks) directly inside the home folder."""
    result = []
    try:
        for entry in os.scandir(paths.home()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                    result.append(entry.path)
            except OSError:
                continue
    except OSError:
        pass
    return sorted(result)
