"""Files written next to the backups so they can be used without the app installed."""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import stat

from .. import VERSION
from . import fmt
from .layout import APP_DIR, README_NAME

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
    Definições do ambiente de trabalho (painel, atalhos, aparência), exportadas com
    o comando "dconf dump /".

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

Para aplicar as definições do ambiente de trabalho (Cinnamon, GNOME, MATE):

    dconf load / < "<...>/Cópia atual/Extras/definicoes-do-ambiente-de-trabalho.ini"

Depois, termina a sessão e volta a entrar.


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
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def package_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ignore(folder: str, names):
    return [n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))]


def copy_app(root: str) -> bool:
    """Put a runnable copy of the app on the disk. Returns True when it was updated."""
    target = os.path.join(root, APP_DIR)
    version_file = os.path.join(target, "VERSAO")
    try:
        with open(version_file, encoding="utf-8") as fh:
            if fh.read().strip() == VERSION:
                return False
    except OSError:
        pass
    staging = target + ".nova"
    old = target + ".antiga"
    for leftover in (staging, old):
        if os.path.lexists(leftover):
            shutil.rmtree(leftover, ignore_errors=True)
    os.makedirs(staging)
    shutil.copytree(package_dir(), os.path.join(staging, "salvaguarda"), ignore=_ignore)
    launcher = os.path.join(staging, LAUNCHER_NAME)
    with open(launcher, "w", encoding="utf-8") as fh:
        fh.write(_LAUNCHER)
    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    with open(os.path.join(staging, "VERSAO"), "w", encoding="utf-8") as fh:
        fh.write(VERSION + "\n")
    if os.path.lexists(target):
        os.rename(target, old)
    os.rename(staging, target)
    shutil.rmtree(old, ignore_errors=True)
    return True
