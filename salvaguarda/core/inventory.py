"""List of installed software, to reinstall it on a new system (or another distribution).

Written before each backup into Extras/programas-instalados.md, like the desktop
settings export. Programs themselves are not copied (they are reinstallable and often
tied to one distribution), but the list says what was there and how to get it back.

Rules: no network, no root, no shell, every command with a timeout, every section
optional (a missing tool or a refusal never fails the backup). Files are read directly
whenever possible. Nothing from the user's projects is ever executed.
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

INVENTORY_NAME = "programas-instalados.md"
STAMP_PREFIX = "Gerado pela Salvaguarda"
TOTAL_BUDGET = 90.0        # seconds for the whole inventory
DEFAULT_TIMEOUT = 15.0
REFRESH_SECONDS = 12 * 3600  # the list is rebuilt at most twice a day


# ------------------------------------------------------------------ running
@dataclass
class Runner:
    """Runs local commands safely. ``which`` and ``run`` can be replaced in tests."""

    budget: float = TOTAL_BUDGET
    started: float = field(default_factory=time.monotonic)
    problems: List[str] = field(default_factory=list)

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def left(self) -> float:
        return self.budget - (time.monotonic() - self.started)

    def run(self, cmd: Sequence[str], timeout: float = DEFAULT_TIMEOUT,
            ok_codes: Tuple[int, ...] = (0,)) -> Optional[str]:
        exe = self.which(cmd[0])
        if exe is None:
            return None
        timeout = min(timeout, self.left())
        if timeout <= 1:
            self.problems.append("{}: sem tempo (a lista ficou incompleta)".format(cmd[0]))
            return None
        env = dict(os.environ)
        env.update({"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "NO_COLOR": "1",
                    "TERM": "dumb", "PAGER": "cat", "SYSTEMD_PAGER": "cat"})
        for var in ("DOCKER_HOST", "DOCKER_CONTEXT"):  # never a remote daemon
            env.pop(var, None)
        try:
            out = subprocess.run([exe] + list(cmd[1:]), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=timeout, env=env, check=False)
        except subprocess.TimeoutExpired:
            self.problems.append("{}: não respondeu em {} s".format(cmd[0], int(timeout)))
            return None
        except (OSError, ValueError) as exc:
            self.problems.append("{}: {}".format(cmd[0], exc))
            return None
        if out.returncode not in ok_codes:
            first = out.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
            self.problems.append("{}: {}".format(" ".join(cmd[:2]), first[0] if first else
                                                 "terminou com erro {}".format(out.returncode)))
            return None
        return out.stdout.decode("utf-8", "replace")


# ------------------------------------------------------------- small helpers
def _read(path: str, limit: int = 1 << 20) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _json(path: str) -> Optional[object]:
    text = _read(path, 8 << 20)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _listdir(path: str) -> List[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _md(text: str) -> str:
    """Escape Markdown table cells."""
    return str(text).replace("|", "\\|").replace("\n", " ")


@dataclass
class Section:
    title: str
    intro: str = ""
    rows: List[Tuple[str, ...]] = field(default_factory=list)
    header: Tuple[str, ...] = ()
    commands: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    raw: str = ""  # verbatim block (crontab)

    @property
    def empty(self) -> bool:
        return not (self.rows or self.commands or self.raw or self.notes)


# ------------------------------------------------------------------- APT
OFFICIAL_SOURCE_FILES = {
    "/etc/apt/sources.list",
    "/etc/apt/sources.list.d/official-package-repositories.list",  # Linux Mint
    "/etc/apt/sources.list.d/official-source-repositories.list",   # Linux Mint
    "/etc/apt/sources.list.d/ubuntu.sources",                      # Ubuntu 24.04+
    "/etc/apt/sources.list.d/debian.sources",                      # Debian 13+
    "/etc/apt/sources.list.d/system.sources",                      # Pop!_OS
}


def apt_sources(root: str = "/") -> List[Tuple[str, str]]:
    """(file, "uri suite components") of third-party APT repositories."""
    result = []
    base = os.path.join(root, "etc/apt/sources.list.d")
    for name in _listdir(base):
        path = "/etc/apt/sources.list.d/" + name
        if path in OFFICIAL_SOURCE_FILES or not name.endswith((".list", ".sources")):
            continue
        text = _read(os.path.join(base, name)) or ""
        if name.endswith(".list"):
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line.startswith("deb "):
                    words = re.sub(r"\[[^\]]*\]", "", line).split()[1:]
                    result.append((path, " ".join(words)))
        else:  # deb822
            for block in re.split(r"\n\s*\n", text):
                fields = {}
                for line in block.splitlines():
                    if ":" in line and not line.startswith((" ", "#")):
                        key, value = line.split(":", 1)
                        fields[key.strip().lower()] = value.strip()
                if fields.get("enabled", "yes").lower() == "no" or "uris" not in fields:
                    continue
                result.append((path, " ".join(
                    [fields.get("uris", ""), fields.get("suites", ""), fields.get("components", "")]).strip()))
    return result


_POLICY_PKG = re.compile(r"^(\S+):$")
_POLICY_VER = re.compile(r"^ (\*\*\*| {3}) (\S+) (-?\d+)$")
_POLICY_ORIGIN = re.compile(r"^ {8}-?\d+ (\S+)(?: (\S+))?")
_OFFICIAL_HOST = re.compile(
    r"(^|\.)(ubuntu\.com|debian\.org|linuxmint\.com|pop-os\.org|elementary\.io|zorinos\.com)$")


def _host(uri: str) -> str:
    match = re.match(r"^[a-z+]+://([^/:]+)", uri)
    return match.group(1) if match else ""


def classify_manual_packages(policy: str, official_hosts: Iterable[str] = ()) -> Dict[str, List[Tuple[str, str]]]:
    """Parse ``apt-cache policy <pkgs>``: {'extra': [(pkg, origin)], 'local': [(pkg, version)]}."""
    official = set(official_hosts)
    result: Dict[str, List[Tuple[str, str]]] = {"extra": [], "local": []}
    pkg, installed_version = "", ""
    installed_origins: List[str] = []
    other_origins: List[str] = []
    in_installed = False

    def is_official(url: str) -> bool:
        host = _host(url)
        return bool(_OFFICIAL_HOST.search(host)) or host in official

    def flush() -> None:
        if not pkg or not installed_version:
            return
        urls = [o for o in installed_origins if o != "/var/lib/dpkg/status"]
        if urls:
            if not any(is_official(u) for u in urls):
                result["extra"].append((pkg, urls[0]))
            return
        # Installed version is in no repository list. Stale lists or a superseded
        # update are common: call it "local" only when no version of the package
        # comes from any repository.
        if not other_origins:
            result["local"].append((pkg, installed_version))
        elif not any(is_official(u) for u in other_origins):
            result["extra"].append((pkg, other_origins[0]))

    for line in policy.splitlines():
        m = _POLICY_PKG.match(line)
        if m:
            flush()
            pkg, installed_version, installed_origins, other_origins = m.group(1), "", [], []
            in_installed = False
            continue
        m = _POLICY_VER.match(line)
        if m:
            in_installed = m.group(1) == "***"
            if in_installed:
                installed_version = m.group(2)
            continue
        m = _POLICY_ORIGIN.match(line)
        if m:
            if in_installed:
                installed_origins.append(m.group(1))
            elif m.group(1) != "/var/lib/dpkg/status":
                other_origins.append(m.group(1))
    flush()
    return result


def official_mirror_hosts(root: str = "/") -> List[str]:
    """Hosts of the official source files (Mint and Ubuntu let users pick any mirror)."""
    hosts = []
    for path in OFFICIAL_SOURCE_FILES:
        text = _read(os.path.join(root, path.lstrip("/"))) or ""
        hosts.extend(_host(u) for u in re.findall(r"[a-z+]+://[^\s\]]+", text))
    return sorted(set(h for h in hosts if h))


def section_apt(r: Runner) -> List[Section]:
    sections = []
    repos = apt_sources()
    if repos:
        s = Section("Repositórios APT extra",
                    "Repositórios acrescentados por ti ou por instaladores (Chrome, VS Code, "
                    "Docker...). Noutra distribuição, o nome da versão (por exemplo «noble») "
                    "pode ser diferente: segue as instruções oficiais de cada programa.",
                    header=("Ficheiro", "Endereço"))
        s.rows = [(f, d) for f, d in repos]
        sections.append(s)
    manual = r.run(["apt-mark", "showmanual"], timeout=20)
    if manual is None:
        return sections
    names = [n for n in manual.split() if re.match(r"^[a-z0-9][a-z0-9+.:-]*$", n)]
    policy_parts = []
    for i in range(0, len(names), 400):  # keep the command line short
        out = r.run(["apt-cache", "policy"] + names[i:i + 400], timeout=30)
        if out is None:
            break
        policy_parts.append(out)
    found = classify_manual_packages("".join(policy_parts), official_mirror_hosts())
    if found["extra"]:
        s = Section("Pacotes de repositórios extra", header=("Pacote", "Origem"))
        s.rows = sorted(found["extra"])
        s.commands = ["sudo apt install " + " ".join(p for p, _ in s.rows)]
        s.notes = ["Primeiro volta a acrescentar os repositórios da secção anterior."]
        sections.append(s)
    if found["local"]:
        s = Section("Pacotes instalados a partir de ficheiros .deb",
                    "Não vêm de nenhum repositório: guarda o ficheiro .deb ou volta a "
                    "descarregá-lo do site de cada programa.", header=("Pacote", "Versão"))
        s.rows = sorted(found["local"])
        sections.append(s)
    s = Section("Outros pacotes instalados por ti",
                "Lista completa dos pacotes marcados como instalados à mão (inclui os que vieram "
                "com o sistema).")
    s.raw = " ".join(names)
    sections.append(s)
    return sections


# ------------------------------------------------------------ snap / flatpak
_SNAP_SKIP = re.compile(r"^(core\d*|snapd|bare|gtk-common-themes|gnome-\d+-\d+|kf5-.*|kde-.*-sdk|mesa-.*|gpu-.*)$")


def section_snap(r: Runner) -> Optional[Section]:
    out = r.run(["snap", "list"], timeout=15)
    if not out:
        return None
    s = Section("Snaps", header=("Snap", "Canal", "Notas"))
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 4 or _SNAP_SKIP.match(cols[0]):
            continue
        notes = cols[5] if len(cols) > 5 else ""
        if "base" in notes or notes == "snapd":
            continue
        s.rows.append((cols[0], cols[3], notes))
        s.commands.append("sudo snap install {}{}{}".format(
            cols[0], " --classic" if "classic" in notes else "",
            "" if cols[3] in ("latest/stable", "-") else " --channel=" + cols[3]))
    return s


def section_flatpak(r: Runner) -> Optional[Section]:
    out = r.run(["flatpak", "list", "--app", "--columns=application,origin,installation"], timeout=20)
    if not out:
        return None
    s = Section("Flatpak", header=("App", "Origem", "Instalação"))
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) < 3 or not cols[0] or cols[0].lower() == "application id":
            continue
        s.rows.append((cols[0], cols[1], cols[2]))
        s.commands.append("flatpak install {} {} {}".format(
            "--user" if cols[2] == "user" else "--system", cols[1], cols[0]))
    s.notes = ["Os dados destas apps ficam em ~/.var/app/<app>."]
    return s


# ---------------------------------------------------------------- Node.js
def _node_modules(folder: str) -> List[Tuple[str, str]]:
    result = []
    for name in _listdir(folder):
        if name.startswith("."):
            continue
        names = [name + "/" + sub for sub in _listdir(os.path.join(folder, name))] \
            if name.startswith("@") else [name]
        for pkg in names:
            data = _json(os.path.join(folder, pkg, "package.json"))
            version = data.get("version", "") if isinstance(data, dict) else ""
            if pkg not in ("npm", "corepack"):
                result.append((pkg, str(version)))
    return result


def section_node(home: str) -> Optional[Section]:
    s = Section("Node.js: pacotes globais", header=("Pacote", "Versão", "Onde"))
    places = [("/usr/lib/node_modules", "npm do sistema (sudo npm -g)"),
              ("/usr/local/lib/node_modules", "npm em /usr/local"),
              (os.path.join(home, ".npm-global/lib/node_modules"), "npm em ~/.npm-global"),
              (os.path.join(home, ".local/lib/node_modules"), "npm em ~/.local")]
    for d in glob.glob(os.path.join(home, ".nvm/versions/node/*/lib/node_modules")):
        places.append((d, "nvm " + d.split(os.sep)[-3]))
    for d in glob.glob(os.path.join(home, ".local/share/fnm/node-versions/*/installation/lib/node_modules")):
        places.append((d, "fnm " + d.split(os.sep)[-4]))
    seen = set()
    for folder, where in places:
        for pkg, version in _node_modules(folder):
            s.rows.append((pkg, version, where))
            seen.add(pkg)
    for path, where in ((os.path.join(home, ".bun/install/global/package.json"), "bun"),
                        (os.path.join(home, ".local/share/pnpm/global/5/package.json"), "pnpm")):
        data = _json(path)
        if isinstance(data, dict):
            for pkg, version in sorted((data.get("dependencies") or {}).items()):
                s.rows.append((pkg, str(version), where))
    if not s.rows:
        return None
    npm = sorted({row[0] for row in s.rows if row[2].startswith(("npm", "nvm", "fnm"))})
    if npm:
        s.commands.append("npm install -g " + " ".join(npm))
    for tool in ("bun", "pnpm"):
        pkgs = sorted({row[0] for row in s.rows if row[2] == tool})
        if pkgs:
            s.commands.append("{} add -g {}".format(tool, " ".join(pkgs)))
    return s


# ----------------------------------------------------------------- Python
def section_python(home: str) -> Optional[Section]:
    s = Section("Python: ferramentas", header=("Pacote", "Instalado com"))
    for base in (os.path.join(home, ".local/share/pipx/venvs"), os.path.join(home, ".local/pipx/venvs")):
        for name in _listdir(base):
            meta = _json(os.path.join(base, name, "pipx_metadata.json"))
            spec = name
            if isinstance(meta, dict):
                main = meta.get("main_package") or {}
                spec = main.get("package_or_url") or main.get("package") or name
            s.rows.append((str(spec), "pipx"))
            s.commands.append("pipx install " + str(spec))
    uv_tools = os.path.join(home, ".local/share/uv/tools")
    for name in _listdir(uv_tools):
        if name.startswith(".") or not os.path.isdir(os.path.join(uv_tools, name)):
            continue
        s.rows.append((name, "uv tool"))
        s.commands.append("uv tool install " + name)
    for site in glob.glob(os.path.join(home, ".local/lib/python3*/site-packages")):
        for entry in _listdir(site):
            if entry.endswith(".dist-info"):
                s.rows.append((entry[:-10].rsplit("-", 1)[0], "pip --user (" + site.split(os.sep)[-2] + ")"))
    return s if s.rows else None


# ------------------------------------------------------------------ Rust/Go
_CRATE = re.compile(r'^"(\S+) (\S+) \(([^)]*)\)"\s*=')


def section_cargo(home: str) -> Optional[Section]:
    text = _read(os.path.join(home, ".cargo/.crates.toml"))
    if not text:
        return None
    s = Section("Rust: programas instalados com cargo", header=("Crate", "Versão", "Origem"))
    for line in text.splitlines():
        m = _CRATE.match(line.strip())
        if not m:
            continue
        name, version, source = m.groups()
        s.rows.append((name, version, source.split("+", 1)[0]))
        if source.startswith("git+"):
            s.commands.append("cargo install --git {} {}".format(source[4:].split("#")[0].split("?")[0], name))
        else:
            s.commands.append("cargo install " + name)
    return s if s.rows else None


def section_go(r: Runner, home: str) -> Optional[Section]:
    gobin = os.environ.get("GOBIN") or os.path.join(os.environ.get("GOPATH") or os.path.join(home, "go"), "bin")
    bins = [b for b in _listdir(gobin) if os.path.isfile(os.path.join(gobin, b))][:60]
    if not bins:
        return None
    s = Section("Go: programas em " + gobin.replace(home, "~", 1), header=("Programa", "Módulo"))
    for b in bins:
        module = ""
        if r.which("go") and r.left() > 5:
            out = r.run(["go", "version", "-m", os.path.join(gobin, b)], timeout=5) or ""
            m = re.search(r"^\s*path\s+(\S+)", out, re.M)
            module = m.group(1) if m else ""
        s.rows.append((b, module))
        if module:
            s.commands.append("go install {}@latest".format(module))
    return s


# ------------------------------------------------------ AppImages, /opt...
def section_appimages(home: str, xdg: Dict[str, str]) -> Optional[Section]:
    folders = [os.path.join(home, n) for n in ("Applications", "Aplicações", "AppImages", "Apps",
                                               "bin", ".local/bin", "opt")]
    folders += [xdg[k] for k in ("DOWNLOAD", "DESKTOP") if k in xdg]
    found = []
    for folder in folders:
        for name in _listdir(folder):
            if name.lower().endswith(".appimage"):
                path = os.path.join(folder, name)
                try:
                    size = os.stat(path).st_size
                except OSError:
                    continue
                found.append((path.replace(home, "~", 1), "{:.0f} MB".format(size / 1e6)))
    if not found:
        return None
    s = Section("AppImages", "Se estiverem numa pasta que a Salvaguarda copia, já estão no disco.",
                header=("Ficheiro", "Tamanho"))
    s.rows = sorted(set(found))
    return s


def _unowned(r: Runner, paths_: List[str]) -> List[str]:
    """Paths that no .deb package installed (dpkg -S answers for all in one call)."""
    if not paths_ or not r.which("dpkg-query"):
        return paths_
    out = r.run(["dpkg-query", "-S"] + paths_, timeout=15, ok_codes=(0, 1)) or ""
    owned = set()
    for line in out.splitlines():
        if ": " in line:
            for p in line.split(": ", 1)[1].split(", "):
                owned.add(p.strip())
    return [p for p in paths_ if p not in owned]


def section_manual_installs(r: Runner) -> Optional[Section]:
    candidates = ["/opt/" + n for n in _listdir("/opt")]
    candidates += ["/usr/local/bin/" + n for n in _listdir("/usr/local/bin")][:300]
    rows = [(p, "") for p in _unowned(r, candidates)]
    if not rows:
        return None
    s = Section("Programas instalados à mão em /opt e /usr/local/bin",
                "Não pertencem a nenhum pacote: foram instalados com um script ou um ficheiro "
                "comprimido. Anota de onde vieram.", header=("Caminho", ""))
    s.rows = rows
    return s


# ----------------------------------------------------------------- Docker
def section_docker(r: Runner) -> Optional[Section]:
    sockets = ["/var/run/docker.sock"]
    if os.environ.get("XDG_RUNTIME_DIR"):
        sockets.insert(0, os.path.join(os.environ["XDG_RUNTIME_DIR"], "docker.sock"))
    sock = next((p for p in sockets if _is_socket(p)), None)
    if sock is None or not r.which("docker"):
        return None
    host = ["docker", "-H", "unix://" + sock]
    volumes = r.run(host + ["volume", "ls", "--format", "{{.Name}}\t{{.Driver}}"], timeout=10)
    if volumes is None:
        return Section("Docker", notes=[
            "Não foi possível falar com o Docker (o teu utilizador pode não estar no grupo "
            "«docker»). Os volumes ficam em /var/lib/docker e a Salvaguarda não os consegue copiar."])
    images = r.run(host + ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"], timeout=10) or ""
    s = Section("Docker", header=("Tipo", "Nome", "Detalhe"))
    for line in volumes.splitlines():
        if line.strip():
            name, _, driver = line.partition("\t")
            s.rows.append(("volume", name, driver))
    for line in images.splitlines():
        if line.strip() and not line.startswith("<none>"):
            name, _, size = line.partition("\t")
            s.rows.append(("imagem", name, size))
    if any(row[0] == "volume" for row in s.rows):
        s.notes.append(
            "Os volumes podem ter bases de dados com dados teus e NÃO estão nesta cópia. "
            "Para exportar um volume para a pasta atual (precisa de uma imagem já descarregada, "
            "por exemplo busybox ou alpine):")
        s.commands.append('docker run --rm -v NOME_DO_VOLUME:/dados -v "$PWD":/saida busybox '
                          'tar -C /dados -czf /saida/NOME_DO_VOLUME.tar.gz .')
    return s


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


# ----------------------------------------------------------------- Ollama
def section_ollama(home: str) -> Optional[Section]:
    bases = [os.environ.get("OLLAMA_MODELS", ""), os.path.join(home, ".ollama/models"),
             "/usr/share/ollama/.ollama/models", "/var/lib/ollama/models"]
    models = []
    for base in [b for b in bases if b]:
        root = os.path.join(base, "manifests")
        for dirpath, _dirs, files in os.walk(root):
            for tag in files:
                parts = os.path.relpath(os.path.join(dirpath, tag), root).split(os.sep)
                if len(parts) != 4:
                    continue
                registry, namespace, model, tag_ = parts
                name = model if namespace == "library" else namespace + "/" + model
                if registry != "registry.ollama.ai":
                    name = registry + "/" + name
                models.append((name + ":" + tag_, base.replace(home, "~", 1)))
    if not models:
        return None
    s = Section("Modelos do Ollama", "Voltam a ser descarregados com «ollama pull». Os modelos "
                "criados por ti com «ollama create» precisam do Modelfile original.",
                header=("Modelo", "Pasta"))
    s.rows = sorted(set(models))
    s.commands = ["ollama pull " + m for m, _ in s.rows]
    return s


# -------------------------------------------------------- editor extensions
_EDITORS = (
    ("VS Code", "code", ".vscode/extensions"),
    ("VS Code (Flatpak)", "flatpak run com.visualstudio.code", ".var/app/com.visualstudio.code/data/vscode/extensions"),
    ("VSCodium", "codium", ".vscode-oss/extensions"),
    ("Cursor", "cursor", ".cursor/extensions"),
    ("Windsurf", "windsurf", ".windsurf/extensions"),
)


def editor_extensions(folder: str) -> List[str]:
    data = _json(os.path.join(folder, "extensions.json"))
    ids = []
    if isinstance(data, list):
        for entry in data:
            ident = (entry.get("identifier") or {}).get("id") if isinstance(entry, dict) else None
            if ident:
                ids.append(str(ident))
    if not ids:  # older layout: publisher.name-1.2.3 folders
        for name in _listdir(folder):
            m = re.match(r"^([\w-]+\.[\w.-]+?)-\d+\.\d+", name)
            if m:
                ids.append(m.group(1))
    return sorted(set(ids), key=str.lower)


def section_editors(home: str) -> List[Section]:
    result = []
    for label, cli, rel in _EDITORS:
        ids = editor_extensions(os.path.join(home, rel))
        if ids:
            s = Section("Extensões do " + label)
            s.commands = ["{} --install-extension {}".format(cli, i) for i in ids]
            result.append(s)
    return result


# ------------------------------------------------- crontab, systemd, hosts
def section_crontab(r: Runner) -> Optional[Section]:
    out = r.run(["crontab", "-l"], timeout=5, ok_codes=(0, 1))
    if not out or not out.strip() or out.startswith("no crontab"):
        return None
    s = Section("Tarefas agendadas (crontab)",
                "Para as repor: guarda o bloco num ficheiro e corre «crontab ficheiro». "
                "Isto substitui as tarefas que lá estiverem.")
    s.raw = out
    return s


def section_systemd_user(home: str) -> Optional[Section]:
    base = os.path.join(home, ".config/systemd/user")
    units = [n for n in _listdir(base) if n.endswith((".service", ".timer", ".path", ".socket"))]
    if not units:
        return None
    enabled = set()
    for wants in glob.glob(os.path.join(base, "*.wants")):
        enabled.update(_listdir(wants))
    s = Section("Serviços do utilizador (systemd)", header=("Unidade", "Ativa no arranque"))
    s.rows = [(u, "sim" if u in enabled else "não") for u in units]
    s.commands = ["systemctl --user enable --now " + u for u in units if u in enabled]
    return s


_DEFAULT_HOSTS = re.compile(r"^(127\.0\.0\.1\s+localhost|127\.0\.1\.1\s|::1\s|fe00::|ff0[0-2]::)")


def section_hosts() -> Optional[Section]:
    text = _read("/etc/hosts") or ""
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")
             and not _DEFAULT_HOSTS.match(l.strip())]
    if not lines:
        return None
    s = Section("Entradas acrescentadas em /etc/hosts",
                "Para as repor: «sudo nano /etc/hosts» e acrescenta estas linhas.")
    s.raw = "\n".join(lines) + "\n"
    return s


# ------------------------------------------------------------------ render
def build(home: str, xdg: Dict[str, str], runner: Optional[Runner] = None,
          project_roots: Sequence[str] = ()) -> List[Section]:
    r = runner or Runner()
    sections: List[Optional[Section]] = []
    steps: List[Callable[[], object]] = [
        lambda: section_apt(r), lambda: section_snap(r), lambda: section_flatpak(r),
        lambda: section_node(home), lambda: section_python(home), lambda: section_cargo(home),
        lambda: section_go(r, home), lambda: section_appimages(home, xdg),
        lambda: section_manual_installs(r), lambda: section_docker(r),
        lambda: section_ollama(home), lambda: section_editors(home),
        lambda: section_crontab(r), lambda: section_systemd_user(home), section_hosts,
        lambda: section_venvs(home, project_roots), lambda: section_outside_data(home),
    ]
    for step in steps:
        try:
            got = step()
        except Exception as exc:  # one broken section never stops the backup
            r.problems.append("{}: {}".format(getattr(step, "__name__", "secção"), exc))
            continue
        if isinstance(got, list):
            sections.extend(got)
        else:
            sections.append(got)  # type: ignore[arg-type]
    result = [s for s in sections if s is not None and not s.empty]
    if r.problems:
        result.append(Section("Não foi possível verificar", notes=list(r.problems)))
    return result


def render(sections: List[Section], computer: str, system: str, desktop: str, user: str,
           home: str, version: str, now: _dt.datetime) -> str:
    out = ["# Programas instalados em «{}»".format(computer), "",
           "{} {} em {}.".format(STAMP_PREFIX, version, now.strftime("%d/%m/%Y, às %H:%M")),
           "Sistema: {}{} · utilizador {} · pasta pessoal {}".format(
               system, " ({})".format(desktop) if desktop else "", user, home), "",
           "Esta lista serve para voltares a instalar os teus programas noutro sistema. "
           "Nada disto é instalado automaticamente. Os comandos precisam de internet e os "
           "que começam por «sudo» pedem a palavra-passe de administrador.", ""]
    for s in sections:
        out += ["## " + s.title, ""]
        if s.intro:
            out += [s.intro, ""]
        if s.rows:
            header = s.header or tuple("" for _ in s.rows[0])
            out.append("| " + " | ".join(header) + " |")
            out.append("|" + "---|" * len(header))
            out += ["| " + " | ".join(_md(c) for c in row) + " |" for row in s.rows]
            out.append("")
        for note in s.notes:
            out += ["- " + note]
        if s.notes:
            out.append("")
        if s.commands:
            out += ["```sh"] + s.commands + ["```", ""]
        if s.raw:
            out += ["```", s.raw.rstrip("\n"), "```", ""]
    return "\n".join(out).rstrip() + "\n"


def write_if_changed(target: str, text: str) -> bool:
    """Like system.dconf_dump: keep the date stable when only the generation line changed."""
    def body(t: str) -> str:
        return "\n".join(l for l in t.splitlines() if not l.startswith(STAMP_PREFIX))
    current = _read(target, 64 << 20)
    if current is not None and body(current) == body(text):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    try:
        # surrogateescape: a folder name that is not valid UTF-8 keeps its own bytes
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


# ------------------------------------------------- Python environments of projects
VENV_SKIP = {"node_modules", ".git", "__pycache__", ".cache"}


def find_venvs(root: str, max_depth: int = 4, limit: int = 50) -> List[str]:
    found: List[str] = []
    stack: List[Tuple[str, int]] = [(root, 0)]
    while stack and len(found) < limit:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        names = {e.name for e in entries}
        if "pyvenv.cfg" in names:
            found.append(folder)
            continue
        if depth >= max_depth:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False) and e.name not in VENV_SKIP:
                    stack.append((e.path, depth + 1))
            except OSError:
                continue
    return sorted(found)


def _meta(dist_info: str) -> Optional[Tuple[str, str]]:
    name = version = ""
    try:
        with open(os.path.join(dist_info, "METADATA"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    break  # end of headers
                if line.startswith("Name:"):
                    name = line[5:].strip()
                elif line.startswith("Version:"):
                    version = line[8:].strip()
    except OSError:
        return None
    return (name, version) if name else None


def requirements(venv: str) -> List[str]:
    """pip-freeze-like lines: name==version, or the URL for VCS / local installs."""
    lines = []
    for dist in sorted(glob.glob(os.path.join(venv, "lib", "python3*", "site-packages", "*.dist-info"))):
        meta = _meta(dist)
        if meta is None or meta[0].lower() in ("pip", "setuptools", "wheel"):
            continue
        direct = None
        try:
            with open(os.path.join(dist, "direct_url.json"), encoding="utf-8") as fh:
                direct = json.load(fh)
        except (OSError, ValueError):
            pass
        if isinstance(direct, dict) and direct.get("url"):
            url = str(direct["url"])
            vcs = direct.get("vcs_info") or {}
            if vcs.get("vcs") and vcs.get("commit_id"):
                lines.append("{} @ {}+{}@{}".format(meta[0], vcs["vcs"], url, vcs["commit_id"]))
            elif (direct.get("dir_info") or {}).get("editable"):
                lines.append("# {} (editável, a partir de {})".format(meta[0], url))
            else:
                lines.append("{} @ {}".format(meta[0], url))
        else:
            lines.append("{}=={}".format(*meta))
    return lines


def section_venvs(home: str, project_roots: Sequence[str]) -> Optional[Section]:
    s = Section("Ambientes Python dos projetos",
                "As pastas .venv não são copiadas (recriam-se), mas muitas vezes são o único "
                "registo das dependências de um projeto. Para recriar um ambiente: «uv venv» e "
                "«uv pip install -r ficheiro», com as linhas abaixo guardadas num ficheiro.")
    seen = set()
    for root in project_roots:
        for venv in find_venvs(root):
            if venv in seen:
                continue
            seen.add(venv)
            reqs = requirements(venv)
            if not reqs:
                continue
            shown = venv.replace(home, "~", 1)
            s.notes.append("{} ({} pacotes)".format(shown, len(reqs)))
            s.raw += "# {}\n{}\n\n".format(shown, "\n".join(reqs))
    return s if s.notes else None


# --------------------------------------------------- data the copy cannot hold
def section_outside_data(home: str) -> Optional[Section]:
    """Data that lives in places the copy leaves out on purpose."""
    notes = []
    for base, what in ((os.path.join(home, ".local/share/containers/storage/volumes"), "Podman"),
                       (os.path.join(home, ".local/share/docker/volumes"), "Docker sem root")):
        volumes = [n for n in _listdir(base) if os.path.isdir(os.path.join(base, n))]
        if volumes:
            notes.append("Volumes do {} em {}: {}. Podem ter bases de dados e não estão na "
                         "cópia; exporta-os antes de reinstalar o sistema.".format(
                             what, base.replace(home, "~", 1), ", ".join(volumes[:20])))
    patterns = (".local/lib/python3*/site-packages/open_webui/data/webui.db",
                ".local/share/uv/tools/*/lib/python3*/site-packages/open_webui/data/webui.db",
                ".local/share/pipx/venvs/*/lib/python3*/site-packages/open_webui/data/webui.db")
    for pattern in patterns:
        for path in glob.glob(os.path.join(home, pattern)):
            notes.append("O Open WebUI guarda as conversas em {}, dentro de uma instalação "
                         "Python que não é copiada. Exporta as conversas na própria app ou "
                         "define DATA_DIR para uma pasta que a Salvaguarda copie.".format(
                             path.replace(home, "~", 1)))
    old_lmstudio = os.path.join(home, ".cache/lm-studio/conversations")
    if os.path.isdir(old_lmstudio):
        notes.append("Há conversas antigas do LM Studio em ~/.cache/lm-studio/conversations, "
                     "uma pasta de cache que não é copiada. Move-as para "
                     "~/.lmstudio/conversations.")
    if not notes:
        return None
    return Section("Dados que não estão na cópia", notes=notes)


# ------------------------------------------------------------------ entry point
def generate(target: str, *, computer: str, system: str, desktop: str, user: str, home: str,
             version: str, xdg: Dict[str, str], project_roots: Sequence[str] = (),
             runner: Optional[Runner] = None, now: Optional[_dt.datetime] = None,
             force: bool = False) -> Optional[str]:
    """Write the inventory to ``target``. Returns an error message or None.

    Skipped when it was checked less than REFRESH_SECONDS ago: installing software is
    rare and listing the APT packages takes a few seconds. The content file keeps its
    date when nothing changed, so it is not copied again; a separate stamp file
    remembers when it was last checked.
    """
    stamp = target + ".verificado"
    try:
        if not force and os.path.exists(target):
            try:
                if time.time() - os.stat(stamp).st_mtime < REFRESH_SECONDS:
                    return None
            except OSError:
                pass
        sections = build(home, xdg, runner, project_roots)
        text = render(sections, computer, system, desktop, user, home, version,
                      now or _dt.datetime.now())
        write_if_changed(target, text)
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write("")
        return None
    except Exception as exc:  # never fails the backup
        return str(exc) or exc.__class__.__name__
