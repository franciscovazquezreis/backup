# Instruções para agentes (Codex, Claude e outros)

A Salvaguarda é uma app de cópias de segurança para Linux Mint e outras distribuições
baseadas em Debian: Python 3 + GTK 3, empacotada em `.deb`, sem acesso à internet. O que
está em jogo são os ficheiros do utilizador, por isso a segurança dos dados vem antes de
tudo o resto.

## Comandos

```sh
python3 -m unittest discover -s tests -t .   # testes (núcleo, sem GTK)
./packaging/build-deb.sh                     # cria dist/salvaguarda_<versão>_all.deb
python3 -m salvaguarda                       # abre a app a partir do código-fonte
```

Os testes de permissões só correm com um utilizador sem privilégios (como root são saltados).

## Organização

- `salvaguarda/core/`: motor sem dependências de GTK (seleção, exclusões, cópia, restauro,
  relatórios, migração da configuração). Tudo o que se puder testar fica aqui.
- `salvaguarda/ui/`: interface GTK 3. As tarefas longas correm numa thread; a interface só é
  alterada na thread principal (`GLib.idle_add`), nunca a partir da thread de trabalho.
- `packaging/`: pacote `.deb` e scripts de manutenção.

## Regras que não se quebram

1. **Nada se perde na cópia.** O que sai da «Cópia atual» passa para «Versões anteriores»
   (exceção única: ficheiros de configurações de apps acima do limite definido). Uma origem em
   falta, ilegível ou noutro disco nunca esvazia a cópia que já existe.
2. **Escrita atómica.** Ficheiros escritos com nome temporário (`.sgtmp-*`) e renomeados no
   fim; a cópia nunca fica com ficheiros a meio.
3. **O restauro nunca apaga** ficheiros do computador; o que for substituído é guardado à parte.
4. **Atualizações por cima.** Cada versão nova instala-se sobre a anterior sem perder nada:
   - não mudar o nome de chaves de `config.json`, de ficheiros do disco (`disco.json`,
     `salvaguarda.json`, `estado.json`) nem de pastas do disco («Cópia atual», «Versões
     anteriores», «Pasta pessoal»...) sem uma migração e um teste;
   - chaves desconhecidas da configuração são preservadas (uma versão antiga não as apaga);
   - imports no topo dos módulos (uma app aberta durante a atualização não pode carregar
     módulos novos a meio);
   - subir `VERSION` em `salvaguarda/__init__.py`, `packaging/debian/changelog` e o
     `<release>` em `data/*.metainfo.xml`.
5. **Compatível com Python 3.8** (Linux Mint 20): nada de `match`, `list[str]` fora de
   anotações com `from __future__ import annotations`, `str.removeprefix`, `dict | dict`.
6. **Sem rede e sem root.** Privilégios só com `pkexec`, sempre com confirmação explícita.

## Texto visível ao utilizador

Português europeu pós-AO90 (atual, direto, ficheiro, ecrã, utilizador, guardar), nunca
português do Brasil (arquivo, tela, usuário, salvar). Sem travessões (—). Frases curtas, voz
ativa, sem jargão.

## Review guidelines

Prioridades, por esta ordem:

1. Perda ou corrupção de dados (cópia, versões, restauro, limpeza de versões antigas).
2. Quebra da atualização por cima de uma versão anterior (configuração, disco, pacote).
3. Incompatibilidade com Python 3.8 ou com GTK 3.22+.
4. Alterações à interface feitas fora da thread principal; bloqueios da interface.
5. Texto visível em português do Brasil, com erros ou com travessões.

Assinala só problemas com um caminho concreto até à falha (entrada, estado, resultado errado).
Não assinales preferências de estilo quando o código segue o padrão já usado no ficheiro.
