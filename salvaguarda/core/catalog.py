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
        "Palavras-passe guardadas por apps e pelo ambiente de trabalho, certificados e "
        "a carteira do KDE",
        "dialog-password-symbolic",
        (".local/share/keyrings", ".local/share/kwalletd", ".config/kwalletrc", ".pki/nssdb",
         ".local/share/mkcert"),
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
        "Claude Code, Codex, Gemini, Copilot, Qwen, Continue, OpenCode, Cline, Roo, Goose, "
        "Amp, Crush e Aider: definições, regras, skills, servidores MCP, chaves e histórico "
        "de conversas",
        "dialog-information-symbolic",
        (".claude", ".claude.json", ".config/anthropic", ".agents",
         ".codex", ".gemini", ".qwen", ".copilot", ".config/github-copilot",
         ".continue", ".config/opencode", ".local/share/opencode",
         ".cline", ".roo", ".kilocode", ".config/kilo",
         ".config/goose", ".local/share/goose", ".config/amp",
         ".config/crush", ".local/share/crush",
         ".aider", ".aider.conf.yml", ".aider.model.settings.yml", ".aider.model.metadata.json"),
        home_excludes=(
            (".continue/index/", CAT_CACHES),
            (".continue/logs/", CAT_CACHES),
            (".claude/debug/", CAT_CACHES),
            (".claude/shell-snapshots/", CAT_CACHES),
            (".claude/session-env/", CAT_CACHES),
            (".codex/packages/", CAT_TOOLS),
            (".codex/log/", CAT_CACHES),
            (".copilot/logs/", CAT_CACHES),
            (".gemini/bin/", CAT_TOOLS),
            (".qwen/bin/", CAT_TOOLS),
            (".local/share/opencode/log/", CAT_CACHES),
            (".aider/caches/", CAT_CACHES),
        ),
    ),
    Preset(
        "claude_desktop", "dev", "Claude (app de computador)",
        "Servidores MCP, preferências e resultados do Cowork. A máquina virtual do Cowork "
        "e a cópia interna do Claude Code não são copiadas",
        "dialog-information-symbolic",
        (".config/Claude", ".config/claude-desktop-debian"),
        _CHROMIUM_CACHES,
        home_excludes=(
            (".config/Claude/vm_bundles/", CAT_TOOLS),
            (".config/Claude/claude-code/", CAT_TOOLS),
            (".config/Claude/claude-code-vm/", CAT_TOOLS),
            (".config/Claude/logs/", CAT_CACHES),
        ),
    ),
    Preset(
        "local_ai", "dev", "Modelos locais (Ollama e LM Studio)",
        "Conversas, histórico, definições e chave do Ollama. Os modelos não são copiados: "
        "ficam na lista de programas para voltar a descarregar",
        "dialog-information-symbolic", (".ollama", ".lmstudio"),
    ),
    Preset(
        "vscode", "dev", "Visual Studio Code",
        "Definições, atalhos, snippets, conversas do Copilot e de extensões de IA (Cline, "
        "Roo, Kilo), ficheiros por guardar e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/Code/User", ".config/Code/Backups", ".vscode",
         ".var/app/com.visualstudio.code/config/Code/User"),
        _EDITOR_EXTENSIONS, ("code",),
    ),
    Preset(
        "vscodium", "dev", "VSCodium",
        "Definições, atalhos, snippets, ficheiros por guardar e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/VSCodium/User", ".config/VSCodium/Backups", ".vscode-oss",
         ".var/app/com.vscodium.codium/config/VSCodium/User"),
        _EDITOR_EXTENSIONS, ("codium",),
    ),
    Preset(
        "cursor", "dev", "Cursor",
        "Definições, atalhos, regras, conversas, servidores MCP e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/Cursor/User", ".config/Cursor/Backups", ".cursor", ".config/cursor"),
        _EDITOR_EXTENSIONS, ("cursor",),
    ),
    Preset(
        "windsurf", "dev", "Windsurf",
        "Definições, memórias, conversas, servidores MCP e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/Windsurf/User", ".config/Windsurf/Backups", ".windsurf", ".codeium/windsurf"),
        _EDITOR_EXTENSIONS, ("windsurf",),
    ),
    Preset(
        "kiro", "dev", "Kiro",
        "Definições, steering, hooks, servidores MCP, conversas e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/Kiro/User", ".kiro", ".local/share/kiro-cli"),
        _EDITOR_EXTENSIONS,
    ),
    Preset(
        "ai_editors", "dev", "Trae e Void",
        "Definições, regras, servidores MCP e lista de extensões",
        "accessories-text-editor-symbolic",
        (".config/Trae/User", ".trae", ".config/Trae CN/User", ".trae-cn",
         ".config/Void/User", ".void-editor"),
        _EDITOR_EXTENSIONS,
    ),
    Preset(
        "zed", "dev", "Zed",
        "Definições, atalhos, temas, conversas do agente e biblioteca de prompts",
        "accessories-text-editor-symbolic",
        (".config/zed", ".local/share/zed",
         ".var/app/dev.zed.Zed/config/zed", ".var/app/dev.zed.Zed/data/zed"),
        (), ("zed", "zed-editor"),
        home_excludes=tuple(
            (root + "/" + sub + "/", category)
            for root in (".local/share/zed", ".var/app/dev.zed.Zed/data/zed")
            for sub, category in (
                ("extensions", CAT_TOOLS), ("remote_extensions", CAT_TOOLS),
                ("languages", CAT_TOOLS), ("node", CAT_TOOLS), ("copilot", CAT_TOOLS),
                ("prettier", CAT_TOOLS), ("debug_adapters", CAT_TOOLS),
                ("external_agents", CAT_TOOLS), ("remote_servers", CAT_TOOLS),
                ("devcontainer", CAT_TOOLS), ("logs", CAT_CACHES),
                ("hang_traces", CAT_CACHES), ("embeddings", CAT_CACHES),
            )
        ),
    ),
    Preset(
        "jetbrains", "dev", "IDEs JetBrains",
        "Definições do IntelliJ, PyCharm, WebStorm e afins",
        "accessories-text-editor-symbolic", (".config/JetBrains",),
    ),
    Preset(
        "jupyter", "dev", "Jupyter e IPython",
        "Definições, kernels registados, área de trabalho do JupyterLab e histórico do IPython",
        "utilities-terminal-symbolic",
        (".jupyter", ".local/share/jupyter", ".ipython"),
        home_excludes=((".local/share/jupyter/runtime/", CAT_CACHES),),
    ),
    Preset(
        "n8n", "dev", "n8n",
        "Fluxos, credenciais e a chave que as decifra (pasta ~/.n8n)",
        "emblem-system-symbolic", (".n8n",),
    ),
    Preset(
        "cloud", "dev", "Contas de nuvem e publicação",
        "Sessões e chaves de AWS, Google Cloud, Azure, Vercel, Netlify, Cloudflare, "
        "Firebase, Supabase, Fly.io, Railway, Stripe e ngrok",
        "network-server-symbolic",
        (".aws", ".config/gcloud", ".azure", ".local/share/com.vercel.cli", ".now",
         ".config/netlify", ".netlify", ".config/.wrangler", ".wrangler",
         ".config/configstore", ".supabase", ".fly", ".railway",
         ".config/stripe", ".config/ngrok"),
        home_excludes=(
            (".config/gcloud/logs/", CAT_CACHES),
            (".azure/logs/", CAT_CACHES),
            (".azure/cliextensions/", CAT_TOOLS),
            (".fly/bin/", CAT_TOOLS),
        ),
    ),
    Preset(
        "shell", "dev", "Terminal e shell",
        "bash, zsh, fish, tmux, vim, neovim, Warp e outros emuladores de terminal",
        "utilities-terminal-symbolic",
        (".bashrc", ".bash_aliases", ".bash_profile", ".bash_logout", ".profile",
         ".bash_history", ".zshrc", ".zprofile", ".zshenv", ".zsh_history", ".p10k.zsh",
         ".oh-my-zsh", ".config/fish", ".inputrc", ".tmux.conf", ".config/tmux",
         ".vimrc", ".vim", ".config/nvim", ".config/starship.toml", ".config/alacritty",
         ".config/kitty", ".config/terminator", ".config/tilix", ".config/environment.d",
         ".config/warp-terminal", ".local/share/warp-terminal", ".local/state/warp-terminal"),
        (("cache/", CAT_CACHES),),
    ),
    Preset(
        "scripts", "dev", "Scripts pessoais",
        "Programas e scripts em ~/bin e ~/.local/bin",
        "application-x-executable-symbolic", ("bin", ".local/bin"),
    ),
    Preset(
        "user_services", "dev", "Serviços do utilizador",
        "Serviços que arrancam sozinhos (systemd), como servidores MCP, n8n ou Ollama",
        "emblem-system-symbolic",
        (".config/systemd/user", ".local/share/systemd/user"),
    ),
    Preset(
        "pkgconf", "dev", "npm, pip, Cargo, uv e Docker",
        "Configuração e credenciais dos gestores de pacotes, de versões e de contentores",
        "package-x-generic-symbolic",
        (".npmrc", ".yarnrc", ".yarnrc.yml", ".bunfig.toml", ".config/pnpm", ".config/pip",
         ".pip", ".pypirc", ".config/uv", ".cargo/config.toml", ".cargo/config",
         ".cargo/credentials.toml", ".cargo/credentials", ".cargo/.crates.toml",
         ".cargo/.crates2.json", ".config/go/env", ".docker/config.json", ".docker/contexts",
         ".config/containers", ".netrc", ".tool-versions", ".asdfrc", ".config/mise",
         ".config/direnv"),
    ),
    # Desktop
    Preset(
        "desktop", "desktop", "Definições do ambiente de trabalho",
        "Painel, atalhos de teclado, aparência, fundo, applets, web apps e marcadores do "
        "gestor de ficheiros (Cinnamon, GNOME, MATE, Xfce, KDE, COSMIC e LXQt)",
        "preferences-desktop-appearance-symbolic",
        (".local/share/cinnamon", ".config/cinnamon", ".cinnamon", ".config/autostart",
         ".local/share/applications", ".config/menus", ".config/gtk-3.0",
         ".config/gtk-4.0", ".config/mimeapps.list", ".config/nemo", ".local/share/nemo",
         ".config/caja", ".config/xfce4", ".local/share/gnome-shell/extensions",
         ".local/share/ice",
         ".config/kdeglobals", ".config/kwinrc", ".config/kwinrulesrc",
         ".config/kglobalshortcutsrc", ".config/khotkeysrc", ".config/kxkbrc",
         ".config/plasma-org.kde.plasma.desktop-appletsrc", ".config/plasmashellrc",
         ".config/plasmarc", ".config/kscreenlockerrc", ".config/ksmserverrc",
         ".config/krunnerrc", ".config/dolphinrc", ".config/konsolerc",
         ".local/share/konsole", ".local/share/plasma",
         ".config/cosmic", ".config/lxqt"),
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
    Preset(
        "sandboxed", "advanced", "Dados de apps Snap e Flatpak",
        "Definições e dados de todas as apps instaladas como snap ou Flatpak (sem caches). "
        "Pode ocupar bastante espaço",
        "package-x-generic-symbolic", ("snap", ".var/app"), default_on=False,
    ),
)

# Preset ids are stored in config.json: never rename or remove one. If a preset must
# change id, map the old id to the new one here, so the user's choice is kept and its
# copy does not leave «Cópia atual».
PRESET_ALIASES: Dict[str, str] = {}

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
