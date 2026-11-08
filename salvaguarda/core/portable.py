"""Files written next to the backups so they can be used without the app installed."""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import stat
from typing import Tuple

from .. import VERSION
from . import fmt
from .layout import (APP_DIR, COMPUTER_META, CURRENT_DIR, README_NAME, TMP_PREFIX,
                     VERSIONS_DIR)

LAUNCHER_NAME = "Abrir a Salvaguarda.sh"

_LAUNCHER = """#!/bin/sh
# Abre a Salvaguarda a partir do disco de cópias, sem a instalar.
# Precisa de python3 e GTK 3, que já vêm no Linux Mint, no Ubuntu e no Debian com
# ambiente gráfico.
DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
cd "$DIR" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    echo "É preciso o python3 para abrir a Salvaguarda." >&2
    exit 1
fi
exec python3 -m salvaguarda "$@"
"""

_README = """SALVAGUARDA: CÓPIAS DE SEGURANÇA
================================

Esta pasta foi criada pela app Salvaguarda e guarda cópias de segurança dos
ficheiros de um ou mais computadores. Os ficheiros estão tal e qual, sem
compressão nem formatos especiais: podes abri-los diretamente, com as mesmas
pastas, datas e permissões que tinham no computador.


COMO ESTÁ ORGANIZADO
--------------------

<computador>/Cópia atual/Pasta pessoal/
    Cópia exata do que foi escolhido dentro da pasta pessoal (/home/<utilizador>),
    incluindo ficheiros e pastas ocultos (os que começam por ponto).

<computador>/Cópia atual/Outras localizações/
    Pastas de fora da pasta pessoal, com o caminho completo (ex.: media/dados/Fotos).

<computador>/Cópia atual/Extras/
    definicoes-do-ambiente-de-trabalho.ini: definições do ambiente de trabalho
    (painel, atalhos, aparência), exportadas com o comando "dconf dump /".
    programas-instalados.md: os programas que tinhas instalados (pacotes, snaps,
    Flatpaks, ferramentas de programação, modelos de IA, extensões dos editores) e
    os comandos para os voltares a instalar.

<computador>/Versões anteriores/<data>/
    Ficheiros que foram apagados ou substituídos no computador nessa data.
    São apagados automaticamente ao fim do prazo definido na app.

<computador>/Registos/
    O relatório de cada cópia (o que foi copiado, avisos e erros).

Aplicação/
    Uma cópia da própria app, para a usares sem instalar nada.


RESTAURAR COM A APP (RECOMENDADO)
---------------------------------

1. Abre a pasta "Aplicação", faz duplo clique em "{launcher}" e escolhe
   "Executar". Em alternativa, no terminal:

       sh "<caminho deste disco>/Salvaguarda/Aplicação/{launcher}"

   Também podes instalar a app com o ficheiro .deb, se o tiveres.

2. Na app, abre "Restaurar", escolhe o computador e o que queres recuperar e
   carrega em "Restaurar". Por omissão, cada ficheiro volta ao sítio de onde veio.


RESTAURAR À MÃO (SEM A APP)
---------------------------

Copia o conteúdo de "<computador>/Cópia atual/Pasta pessoal" para a tua pasta
pessoal. No terminal, para manter datas e permissões:

    cp -a "<caminho deste disco>/Salvaguarda/<computador>/Cópia atual/Pasta pessoal/." ~/

Para aplicar as definições do ambiente de trabalho, só no mesmo ambiente em que
foram guardadas (por exemplo, Cinnamon para Cinnamon):

    dconf load / < "<...>/Cópia atual/Extras/definicoes-do-ambiente-de-trabalho.ini"

Depois, termina a sessão e volta a entrar. A app faz isto com mais cuidado: guarda
primeiro as definições em uso e, noutro ambiente, aplica só as das apps.


SE MUDARES DE DISTRIBUIÇÃO
--------------------------

* Ao instalar o sistema novo, usa o mesmo nome de utilizador e a mesma palavra-passe:
  o porta-chaves abre sozinho e os caminhos guardados nas definições continuam
  válidos (por exemplo, o histórico de projetos do Claude Code).
* Restaura com a app: o Firefox, o Thunderbird, o Chromium, o VS Code e o
  LibreOffice vão para a versão instalada no sistema novo (.deb, snap ou Flatpak),
  que guarda os dados noutra pasta.
* Os programas não são copiados. Reinstala-os com a lista em
  Extras/programas-instalados.md.
* Os volumes do Docker e os modelos do Ollama ou do LM Studio também não são
  copiados: a lista diz quais eram e como os voltar a obter.


SE O TEU UTILIZADOR NÃO CONSEGUIR LER ALGUNS FICHEIROS
------------------------------------------------------

Acontece quando o sistema foi reinstalado e o utilizador ficou com outro número
interno. A app propõe corrigir isto sozinha; à mão, no terminal:

    sudo chown -R "$USER": "<caminho deste disco>/Salvaguarda/<computador>"


Não apagues nem alteres o ficheiro disco.json: é ele que identifica este disco.

Gerado pela Salvaguarda {version} em {date}.
"""


def write_readme(root: str) -> None:
    text = _README.format(launcher=LAUNCHER_NAME, version=VERSION,
                          date=fmt.date_long(_dt.datetime.now()))
    path = os.path.join(root, README_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
        # Only the generation line changes between runs of the same version.
        if current.split("Gerado pela")[0] == text.split("Gerado pela")[0]:
            return
        # Written by a newer version (another computer, or after a downgrade): keep it.
        match = re.search(r"Gerado pela Salvaguarda (\S+) em", current)
        if match and version_key(match.group(1)) > version_key(VERSION):
            return
    except (OSError, UnicodeError):
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def package_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_VERSION_LINE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.M)
# Files that dpkg leaves next to the real ones while it is installing an update.
_TRANSIENT = (".pyc", ".pyo", ".dpkg-new", ".dpkg-tmp", ".dpkg-old", ".dpkg-dist")


def source_version(pkg_dir: str) -> str:
    """VERSION written in ``<pkg_dir>/__init__.py`` (the files, not the running code)."""
    try:
        with open(os.path.join(pkg_dir, "__init__.py"), encoding="utf-8") as fh:
            match = _VERSION_LINE.search(fh.read())
    except (OSError, UnicodeError):
        return ""
    return match.group(1).strip() if match else ""


def version_key(text: str) -> Tuple[Tuple[int, ...], int, str]:
    """Order versions like dpkg does for X.Y.Z and X.Y.Z~rcN; garbage sorts first."""
    text = (text or "").strip()
    main, tilde, suffix = text.partition("~")
    try:
        numbers = tuple(int(part) for part in main.split("."))
    except ValueError:
        return ((), 0, "")
    return (numbers, 0 if tilde else 1, suffix)


def _ignore(folder: str, names):
    return [n for n in names if n == "__pycache__" or n.endswith(_TRANSIENT)]


def _is_app_copy(path: str) -> bool:
    """Only a copy of the app itself: never a folder that holds a computer's backups."""
    return (os.path.isdir(os.path.join(path, "salvaguarda"))
            and not os.path.lexists(os.path.join(path, COMPUTER_META))
            and not os.path.lexists(os.path.join(path, CURRENT_DIR))
            and not os.path.lexists(os.path.join(path, VERSIONS_DIR)))


def copy_app(root: str) -> bool:
    """Put a runnable copy of the app on the disk. Returns True when it was updated.

    Never replaces a newer copy (a disk shared with a computer that has a newer
    version, or after installing an older .deb), and never copies files that are
    not the running version (the .deb was updated while the app was open).
    """
    target = os.path.join(root, APP_DIR)
    version_file = os.path.join(target, "VERSAO")
    source = package_dir()
    if source_version(source) != VERSION:
        return False  # the next run, with the new version, makes the copy
    try:
        with open(version_file, encoding="utf-8") as fh:
            on_disk = fh.read().strip()
    except (OSError, UnicodeError):
        on_disk = ""
    if on_disk and version_key(on_disk) >= version_key(VERSION):
        return False
    # Hidden working names: a computer folder can never be called like this (names that
    # start with a dot are refused), so these folders are never someone's backups.
    staging = os.path.join(root, TMP_PREFIX + "aplicacao-nova")
    old = os.path.join(root, TMP_PREFIX + "aplicacao-antiga")
    for leftover in (staging, old):
        if os.path.lexists(leftover):
            shutil.rmtree(leftover, ignore_errors=True)
    for legacy in (target + ".nova", target + ".antiga"):  # left by version 1.0.0
        if _is_app_copy(legacy):
            shutil.rmtree(legacy, ignore_errors=True)
    os.makedirs(staging)
    try:
        shutil.copytree(source, os.path.join(staging, "salvaguarda"), ignore=_ignore)
        if source_version(os.path.join(staging, "salvaguarda")) != VERSION:
            raise OSError("a app foi atualizada durante a cópia")
        launcher = os.path.join(staging, LAUNCHER_NAME)
        with open(launcher, "w", encoding="utf-8") as fh:
            fh.write(_LAUNCHER)
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with open(os.path.join(staging, "VERSAO"), "w", encoding="utf-8") as fh:
            fh.write(VERSION + "\n")
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if os.path.lexists(target):
        os.rename(target, old)
    os.rename(staging, target)
    shutil.rmtree(old, ignore_errors=True)
    return True
