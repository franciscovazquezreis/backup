"""Run reports (text file on the disk) and the local history of runs."""

from __future__ import annotations

import os
from typing import Any, Dict, List, TYPE_CHECKING

from . import fmt, paths
from .config import read_json, write_json_atomic
from .exclusions import CATEGORY_LABELS

if TYPE_CHECKING:  # pragma: no cover
    from .engine import RunResult

STATUS_TITLES = {
    "ok": "Cópia concluída",
    "warnings": "Cópia concluída com avisos",
    "nothing": "Tudo atualizado",
    "cancelled": "Cópia cancelada",
    "failed": "A cópia não foi concluída",
}

HISTORY_LIMIT = 200


def render_text(result: "RunResult", computer: str) -> str:
    s = result.stats
    lines: List[str] = []
    add = lines.append
    add("Salvaguarda: registo da cópia de segurança")
    add("=" * 44)
    add("")
    add("Computador:  {}".format(computer))
    add("Início:      {}".format(result.started.strftime("%d/%m/%Y %H:%M:%S")))
    add("Fim:         {} ({})".format(result.finished.strftime("%d/%m/%Y %H:%M:%S"),
                                      fmt.duration(result.duration)))
    add("Resultado:   {}".format(STATUS_TITLES.get(result.status, result.status)))
    if result.message:
        add("             {}".format(result.message))
    add("")
    add("Números")
    add("-------")
    add("Ficheiros na seleção:            {} ({})".format(fmt.number(s.source_files),
                                                       fmt.size(s.source_bytes)))
    add("Ficheiros novos copiados:        {}".format(fmt.number(s.new_files)))
    add("Ficheiros alterados copiados:    {}".format(fmt.number(s.changed_files)))
    add("Dados copiados:                  {}".format(fmt.size(s.bytes_copied)))
    add("Ligações simbólicas:             {}".format(fmt.number(s.links)))
    add("Apagados no computador:          {}".format(fmt.number(s.removed_files)))
    add("Guardados em Versões anteriores: {}".format(fmt.number(s.archived_files)))
    if s.dropped:
        add("Substituídos sem guardar versão: {} (ficheiros grandes de configurações)".format(
            fmt.number(s.dropped)))
    if s.orphans:
        add("Itens retirados da seleção:      {}".format(fmt.number(s.orphans)))
    if s.newly_excluded:
        add("Agora excluídos por regras:      {}".format(fmt.number(s.newly_excluded)))
    if s.special_skipped:
        add("Ignorados (sockets, pipes):      {}".format(fmt.number(s.special_skipped)))
    if result.version_dir:
        add("")
        add("Versões anteriores desta cópia: {}".format(paths.display_path(result.version_dir)))
    if result.pruned:
        add("Versões antigas apagadas: {}".format(", ".join(result.pruned)))
    if s.excluded_categories:
        add("")
        add("Não copiados por serem dispensáveis")
        add("-----------------------------------")
        for category, count in sorted(s.excluded_categories.items(), key=lambda kv: -kv[1]):
            add("{}: {}".format(CATEGORY_LABELS.get(category, category), fmt.number(count)))
        top = sorted(s.excluded.items(), key=lambda kv: -kv[1])[:15]
        if top:
            add("Mais frequentes: " + ", ".join("{} ({})".format(k, fmt.number(v)) for k, v in top))
    if result.notes:
        add("")
        add("Notas")
        add("-----")
        for note in result.notes:
            add("* " + note)
    if result.issues:
        add("")
        add("Avisos e erros ({})".format(fmt.number(len(result.issues))))
        add("-----------------")
        for issue in result.issues:
            tag = "ERRO " if issue.level == "error" else "AVISO"
            add("{}  {}\n       {}".format(tag, paths.safe_text(issue.path), issue.message))
    add("")
    return "\n".join(lines)


def history_path() -> str:
    return os.path.join(paths.app_state_dir(), "historico.json")


def load_history() -> List[Dict[str, Any]]:
    data = read_json(history_path())
    return data if isinstance(data, list) else []


def append_history(entry: Dict[str, Any]) -> None:
    history = load_history()
    history.append(entry)
    history = history[-HISTORY_LIMIT:]
    try:
        write_json_atomic(history_path(), history)
    except OSError:
        pass


def history_entry(result: "RunResult", destination_name: str, computer: str,
                  kind: str = "copia") -> Dict[str, Any]:
    entry = result.summary_dict()
    entry["tipo"] = kind
    entry["disco"] = destination_name
    entry["computador"] = computer
    return entry
