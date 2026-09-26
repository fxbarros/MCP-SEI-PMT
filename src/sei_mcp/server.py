"""Servidor MCP do SEI-PMT.

Expõe 18 tools ao Claude Desktop (12 de leitura/planilha/minutas via browser ou REST + 6 de documentos via REST, das quais 2 escrevem no SEI mediante confirmar=true):
  - listar_pendentes
  - gerar_planilha_controle
  - baixar_processo
  - baixar_todos_processos

Cada chamada de tool abre um SeiClient (Chrome real headed com perfil
persistente). O perfil persistido faz com que o login seja rápido após o
primeiro uso na sessão (cookies do SEI ficam cacheados).
"""

from __future__ import annotations

import asyncio
import atexit
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .analise import extrair_texto_pdf, verificar_parecer_juridico_pdf
from .modelos import listar_modelos, ler_modelo, salvar_minuta
from .planilha import (
    SAIDA_PADRAO,
    atualizar_linha,
    gerar_planilha,
    status_minuta_por_numero,
)
from .sei_client import SeiClient
from .sei_rest import get_rest

mcp = FastMCP("sei-mcp")

# Threshold pra decidir entre leitura direta e NotebookLM
TAMANHO_MAX_PDF_DIRETO_MB = 15.0


# patchright/playwright sync NÃO pode rodar dentro do event loop do FastMCP.
# Cada tool é async e despacha o trabalho síncrono pra uma thread separada
# via asyncio.to_thread() (que cria sua própria thread sem loop ativo).

# ---------------------------------------------------------------------------
# Cliente SEI singleton — abre 1x, reusa entre chamadas, fecha no shutdown.
# Patchright sync NÃO é thread-safe; lock global serializa o acesso.
# Pra debug/manualmente, exporte SEI_HEADLESS=0 (default = headless).
# ---------------------------------------------------------------------------
_HEADLESS = os.environ.get("SEI_HEADLESS", "1") not in ("0", "false", "no")
_lock = threading.Lock()
_client: SeiClient | None = None


def _ensure_client() -> SeiClient:
    """Lazy-init do cliente SEI. Reusa se já existe."""
    global _client
    if _client is not None:
        return _client
    print(
        f"[SERVER] iniciando SeiClient singleton (headless={_HEADLESS})",
        flush=True,
        file=sys.stderr,
    )
    c = SeiClient(headless=_HEADLESS)
    try:
        c.__enter__()
        c.login()
    except Exception:
        # __exit__ fecha o que chegou a abrir (ctx e/ou playwright). Sem isso,
        # um launch falho vaza o driver do patchright e deixa o event loop dele
        # preso na thread — que fica inutilizável ("Sync API inside asyncio loop").
        try:
            c.__exit__(None, None, None)
        except Exception:
            pass
        raise
    _client = c
    return _client


def _reset_client() -> None:
    """Fecha o cliente atual; próxima chamada recria."""
    global _client
    if _client is None:
        return
    try:
        _client.__exit__(None, None, None)
    except Exception:
        pass
    _client = None


@atexit.register
def _shutdown_client() -> None:
    _reset_client()


# Playwright sync tem afinidade de thread E, quando crasha, pode deixar o
# event loop interno "rodando" na thread — o que envenena as threads
# compartilhadas do asyncio.to_thread (erro "Sync API inside the asyncio
# loop" em TODA chamada seguinte). Por isso todo trabalho de browser roda
# numa thread dedicada única; se algo der errado, a thread é descartada
# junto com o cliente e uma nova é criada na próxima chamada.
_browser_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sei-browser")


def _run_with_client(fn, *args, **kwargs):
    """Serializa acesso ao cliente + recria em caso de erro de browser.

    Sempre executa na thread dedicada do browser (_browser_executor).
    """
    global _browser_executor

    def _job():
        try:
            c = _ensure_client()
            return fn(c, *args, **kwargs)
        except Exception:
            # Erros (browser morto, timeout grave) → descarta cliente
            _reset_client()
            raise

    with _lock:
        try:
            return _browser_executor.submit(_job).result()
        except Exception:
            # A thread pode ter ficado com o loop do playwright preso —
            # descarta e cria uma nova pro próximo _ensure_client.
            _browser_executor.shutdown(wait=False)
            _browser_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="sei-browser"
            )
            raise


def _listar_impl(
    c: SeiClient,
    incluir_gerados: bool,
    filtro: str,
    limite: int | None,
    apenas_meus: bool,
) -> dict[str, Any]:
    pendentes = c.listar_pendentes(
        incluir_gerados=incluir_gerados, filtro=filtro, apenas_meus=apenas_meus
    )
    if limite:
        pendentes = pendentes[:limite]
    return {
        "total": len(pendentes),
        "apenas_meus": apenas_meus,
        "filtro_aplicado": filtro or None,
        "limite_aplicado": limite,
        "processos": [asdict(p) for p in pendentes],
    }


def _listar_sync(
    incluir_gerados: bool, filtro: str, limite: int | None, apenas_meus: bool
) -> dict[str, Any]:
    return _run_with_client(
        _listar_impl, incluir_gerados, filtro, limite, apenas_meus
    )


def _gerar_planilha_impl(c: SeiClient, saida_path: Path) -> dict[str, Any]:
    pendentes = c.listar_pendentes(incluir_gerados=False)
    path = gerar_planilha(pendentes, saida=saida_path)
    return {"arquivo": str(path), "total": len(pendentes)}


def _gerar_planilha_sync(saida: str | None) -> dict[str, Any]:
    saida_path = Path(saida) if saida else SAIDA_PADRAO
    return _run_with_client(_gerar_planilha_impl, saida_path)


def _baixar_um_impl(
    c: SeiClient, numero: str, formato: str, forcar: bool
) -> dict[str, Any]:
    result = c.baixar_processo(numero, formato=formato, forcar=forcar)
    if isinstance(result, dict):  # formato="ambos"
        return {
            "numero": numero,
            "formato": formato,
            "arquivos": {k: str(v) for k, v in result.items()},
            "tamanhos_bytes": {k: v.stat().st_size for k, v in result.items()},
        }
    return {
        "numero": numero,
        "formato": formato,
        "arquivo": str(result),
        "tamanho_bytes": result.stat().st_size,
    }


def _baixar_um_sync(numero: str, formato: str, forcar: bool) -> dict[str, Any]:
    return _run_with_client(_baixar_um_impl, numero, formato, forcar)


def _baixar_todos_impl(
    c: SeiClient, limite: int | None, formato: str, forcar: bool
) -> dict[str, Any]:
    return c.baixar_todos_processos(formato=formato, forcar=forcar, limite=limite)


def _baixar_todos_sync(
    limite: int | None, formato: str, forcar: bool
) -> dict[str, Any]:
    return _run_with_client(_baixar_todos_impl, limite, formato, forcar)


def _preparar_processo_impl(c: SeiClient, numero: str) -> dict[str, Any]:
    """Garante PDF em cache, verifica parecer assinado e decide estratégia."""
    result = c.baixar_processo(numero, formato="pdf")
    path = result if not isinstance(result, dict) else result["pdf"]

    tamanho_bytes = path.stat().st_size
    tamanho_mb = round(tamanho_bytes / 1024 / 1024, 2)
    numero_safe = numero.replace("/", "-")

    # Verifica parecer jurídico ANTES de qualquer análise — se já há um
    # assinado, o Claude pula a redação.
    try:
        parecer = verificar_parecer_juridico_pdf(path)
    except Exception as e:
        parecer = {
            "erro": f"falha ao verificar parecer: {e}",
            "recomendacao": "sem_parecer",
            "tem_parecer_assinado": False,
            "pareceres": [],
            "n_pareceres_encontrados": 0,
        }

    base = {
        "numero": numero,
        "path": str(path),
        "tamanho_mb": tamanho_mb,
        "parecer_status": parecer,
    }

    # Se já tem parecer ASSINADO, sinaliza pro Claude pular a redação,
    # independentemente do tamanho/estratégia.
    if parecer.get("tem_parecer_assinado"):
        base["estrategia"] = "pular_minuta"
        base["instrucao"] = (
            f"Processo {numero} já possui parecer jurídico ASSINADO "
            f"(página {parecer['pareceres'][0]['pagina_inicio']}, "
            f"assinante: {parecer['pareceres'][0].get('assinante') or '?'}). "
            f"NÃO redija nova minuta. Apenas registre na planilha "
            f"(atualizar_planilha_processo: status_minuta='já existe parecer "
            f"assinado'). Avise o usuário."
        )
        return base

    if tamanho_mb <= TAMANHO_MAX_PDF_DIRETO_MB:
        info = extrair_texto_pdf(path)
        base.update(
            {
                "estrategia": "pdf_direto",
                "n_paginas": info["n_paginas"],
                "n_chars": info["n_chars"],
                "outline": info["outline"],
                "texto": info["texto"],
            }
        )
        return base

    base.update(
        {
            "estrategia": "notebooklm",
            "nome_sugerido_notebook": f"Processo {numero_safe}",
            "instrucao": (
                f"PDF do processo {numero} tem {tamanho_mb} MB (acima de "
                f"{TAMANHO_MAX_PDF_DIRETO_MB} MB). Use as tools do servidor "
                f"`notebooklm-mcp`:\n"
                f"  1) notebook_create(name='Processo {numero_safe}')\n"
                f"  2) source_add(notebook_id=<id>, source_type='file', "
                f"file_path='{path}')\n"
                f"  3) Aguarde processamento e use notebook_query pra investigar "
                f"o conteúdo.\n"
                f"  4) Pra fundamentação na legislação municipal, use "
                f"cross_notebook_query incluindo o notebook 'LegislacaoTeresina'."
            ),
        }
    )
    return base


# Status que indicam que o processo já foi tratado (não precisa reanalisar)
_STATUS_FINAIS_KEYWORDS = (
    "minuta gerada",
    "já existe parecer assinado",
    "ja existe parecer assinado",
    "pular_minuta",
)


def _preparar_processo_sync(numero: str) -> dict[str, Any]:
    return _run_with_client(_preparar_processo_impl, numero)


def _proximo_pendente_impl(c: SeiClient, filtro: str) -> dict[str, Any]:
    """Devolve o próximo processo pendente NÃO analisado."""
    pendentes = c.listar_pendentes(filtro=filtro)

    if not pendentes:
        return {
            "acabou": True,
            "razao": f"sem pendentes com filtro={filtro!r}" if filtro
                     else "sem pendentes na caixa",
        }

    status = status_minuta_por_numero()

    nao_analisados = []
    for p in pendentes:
        s = (status.get(p.numero, "") or "").lower().strip()
        if not any(k in s for k in _STATUS_FINAIS_KEYWORDS):
            nao_analisados.append(p)

    if not nao_analisados:
        return {
            "acabou": True,
            "razao": "todos os pendentes do filtro já foram analisados",
            "total_no_filtro": len(pendentes),
        }

    p = nao_analisados[0]
    return {
        "acabou": False,
        "numero": p.numero,
        "id_procedimento": p.id_procedimento,
        "tipo": p.tipo,
        "especificacao": p.especificacao,
        "tem_anotacao": p.tem_anotacao,
        "anotacao_completa": p.anotacao_completa,
        "restantes_neste_filtro": len(nao_analisados),
        "ja_analisados": len(pendentes) - len(nao_analisados),
        "total_no_filtro": len(pendentes),
    }


def _proximo_pendente_sync(filtro: str) -> dict[str, Any]:
    return _run_with_client(_proximo_pendente_impl, filtro)


def _verificar_parecer_impl(c: SeiClient, numero: str) -> dict[str, Any]:
    """Apenas verifica parecer (baixa PDF se preciso, não extrai texto inteiro)."""
    result = c.baixar_processo(numero, formato="pdf")
    path = result if not isinstance(result, dict) else result["pdf"]
    parecer = verificar_parecer_juridico_pdf(path)
    return {"numero": numero, "path": str(path), **parecer}


def _verificar_parecer_sync(numero: str) -> dict[str, Any]:
    return _run_with_client(_verificar_parecer_impl, numero)


def _verificar_parecer_rapido_sync(numero: str) -> dict[str, Any]:
    """Verifica parecer SEM baixar PDF — via API REST mod-wssei (sem browser).

    Substitui (26/09/2026) a leitura da árvore HTML, que no layout novo do
    SEI devolvia todo parecer como "não assinado" e em triplicata.
    """
    pareceres = get_rest().verificar_pareceres(numero)

    tem_assinado = any(p["assinado"] for p in pareceres)
    if tem_assinado:
        recomendacao = "ja_existe_parecer_assinado_pular_minuta"
    elif pareceres:
        recomendacao = "parecer_nao_assinado_pode_substituir"
    else:
        recomendacao = "sem_parecer"

    return {
        "numero": numero,
        "fonte": "rest",
        "n_pareceres_encontrados": len(pareceres),
        "pareceres": pareceres,
        "tem_parecer_assinado": tem_assinado,
        "recomendacao": recomendacao,
    }


def _salvar_minuta_sync(
    numero: str, conteudo: str, tipo: str, formato: str, modelo: str | None
) -> dict[str, Any]:
    path = salvar_minuta(
        numero, conteudo=conteudo, tipo=tipo, formato=formato, modelo=modelo
    )
    return {
        "arquivo": str(path),
        "tamanho_bytes": path.stat().st_size,
        "tipo": tipo,
        "formato": formato,
        "modelo": modelo,
    }


@mcp.tool()
async def listar_pendentes(
    incluir_gerados: bool = False,
    filtro: str = "",
    limite: int | None = None,
    apenas_meus: bool = True,
) -> dict[str, Any]:
    """Lista os processos pendentes na caixa do setor (PROC-PATR-PGM).

    Por default mostra apenas os atribuídos a você. Para ver TODOS os
    processos do setor (atribuídos a outros procuradores também), passe
    `apenas_meus=False`.

    Args:
        incluir_gerados: se True, também inclui processos gerados. Default False.
        filtro: termo (case-insensitive) — só retorna processos cujo
            tipo+especificacao+anotação contenha esse termo. Ex: 'perpetuidade',
            'aforamento', 'desmembramento', 'TAC'. Default '' (sem filtro).
        limite: se passado, devolve apenas os primeiros N (após filtro). Útil
            pra "analise 4 processos de perpetuidade" → limite=4.
        apenas_meus: True (default) → só os atribuídos a você (~155 em PROC-PATR-PGM).
            False → todos os processos do setor (~308 em PROC-PATR-PGM).

    Returns:
        dict com `total`, `apenas_meus`, `filtro_aplicado`, `limite_aplicado`
        e `processos`.
    """
    return await asyncio.to_thread(
        _listar_sync, incluir_gerados, filtro, limite, apenas_meus
    )


@mcp.tool()
async def gerar_planilha_controle(saida: str | None = None) -> dict[str, Any]:
    """Gera a planilha de controle (.xlsx) com todos os pendentes.

    Sobrescreve sempre o mesmo arquivo (default: `~/Desenvolvimento/sei-mcp/
    saida/controle.xlsx`). Tem 11 colunas, sendo 3 em amarelo (Materia,
    Complexidade, Acao_sugerida) que devem ser preenchidas após análise.

    Args:
        saida: caminho .xlsx custom. Default usa o padrão do projeto.

    Returns:
        dict com `arquivo` (path absoluto) e `total` (nº de processos gravados).
    """
    return await asyncio.to_thread(_gerar_planilha_sync, saida)


@mcp.tool()
async def baixar_processo(
    numero: str,
    formato: str = "pdf",
    forcar: bool = False,
) -> dict[str, Any]:
    """Baixa um processo específico do SEI-PMT.

    Salvo em `~/Library/Mobile Documents/com~apple~CloudDocs/Processos SEI/
    {numero}/{numero}.{ext}` (iCloud Drive, sincronizado).

    Args:
        numero: número do processo no formato NUP, ex: '00047.001613/2026-14'
        formato: 'pdf' (default — melhor pra análise), 'zip' (preserva Word
            editável original) ou 'ambos'.
        forcar: se True, baixa mesmo que o arquivo já exista. Default False.

    Returns:
        dict com `numero`, `formato` e `arquivo` (path) + `tamanho_bytes`
        — ou `arquivos`/`tamanhos_bytes` (dict por formato) se formato='ambos'.
    """
    return await asyncio.to_thread(_baixar_um_sync, numero, formato, forcar)


@mcp.tool()
async def baixar_todos_processos(
    limite: int | None = None,
    formato: str = "pdf",
    forcar: bool = False,
) -> dict[str, Any]:
    """Baixa TODOS os processos pendentes em lote.

    Pula processos cujo arquivo no formato escolhido já existe
    (a menos que forcar=True). Continua mesmo se algum falhar
    (registra na lista_erros).

    Args:
        limite: se passado, baixa apenas os primeiros N processos. Útil pra
            testar antes de rodar em todos.
        formato: 'pdf' (default), 'zip' ou 'ambos'.
        forcar: se True, re-baixa todos mesmo que já existam.

    Returns:
        dict com `total`, `formato`, `baixados`, `pulados`, `erros` (count)
        e `lista_erros` (lista de {numero, erro}).
    """
    return await asyncio.to_thread(_baixar_todos_sync, limite, formato, forcar)


@mcp.tool()
async def preparar_processo(numero: str) -> dict[str, Any]:
    """Prepara um processo pra análise — garante PDF em cache e decide estratégia.

    - PDF ≤ 15 MB: retorna o texto completo do processo, pronto pra Claude analisar
    - PDF > 15 MB: retorna instrução pra criar notebook NotebookLM e usar Q&A

    Use SEMPRE no início ao analisar um processo. Em seguida:
    - Se estrategia='pdf_direto': use o `texto` retornado pra fazer a análise
    - Se estrategia='notebooklm': siga a `instrucao` retornada (tools do `notebooklm-mcp`)

    Args:
        numero: número NUP do processo (ex: '00047.001613/2026-14')

    Returns:
        dict com estrategia, path do PDF, tamanho_mb e:
        - se pdf_direto: n_paginas, n_chars, outline (lista de bookmarks), texto
        - se notebooklm: nome_sugerido_notebook, instrucao
    """
    return await asyncio.to_thread(_preparar_processo_sync, numero)


@mcp.tool()
async def proximo_pendente_nao_analisado(filtro: str = "") -> dict[str, Any]:
    """Devolve UM processo pendente que ainda não foi analisado.

    Otimizado pra loop noturno: chame, processe, chame de novo. Quando
    devolver `{"acabou": true}`, terminou — você pode encerrar.

    Lê a coluna Status_minuta da planilha controle.xlsx pra saber o que
    já foi feito. Considera "minuta gerada" e "já existe parecer assinado"
    como estados finais.

    Args:
        filtro: termo da matéria (case-insensitive). Ex: 'perpetuidade',
            'aforamento', 'desmembramento'. Vazio = qualquer pendente.

    Returns:
        Se acabou:
            {"acabou": true, "razao": "...", "total_no_filtro": N}
        Caso contrário:
            {"acabou": false, "numero", "tipo", "especificacao",
             "tem_anotacao", "anotacao_completa", "restantes_neste_filtro",
             "ja_analisados", "total_no_filtro"}
    """
    return await asyncio.to_thread(_proximo_pendente_sync, filtro)


@mcp.tool()
async def verificar_parecer_juridico_rapido(numero: str) -> dict[str, Any]:
    """Verifica parecer jurídico SEM baixar PDF (rápido, ~2s/processo).

    Consulta a API REST oficial do SEI (mod-wssei): lista os documentos do
    processo, filtra os do tipo "Parecer*" e lê as assinaturas de cada um.
    Não abre browser nem consome espaço em disco. Pareceres anexados como
    PDF externo (origem="externo") não têm assinatura eletrônica listável
    — saem como não assinados.

    Use esta tool quando o usuário perguntar "quais desses processos têm
    parecer assinado" — é viável iterar 5-10 processos em sequência.

    Args:
        numero: número NUP do processo

    Returns:
        dict com:
          - n_pareceres_encontrados (int)
          - pareceres: lista com {titulo_arvore, id_documento, unidade_sigla,
            unidade_descricao, assinado: bool, assinante_nome, assinante_cargo,
            data_assinatura, hora_assinatura}
          - tem_parecer_assinado: bool
          - recomendacao: 'sem_parecer' | 'parecer_nao_assinado_pode_substituir'
            | 'ja_existe_parecer_assinado_pular_minuta'
    """
    return await asyncio.to_thread(_verificar_parecer_rapido_sync, numero)


@mcp.tool()
async def verificar_parecer_juridico(numero: str) -> dict[str, Any]:
    """Verifica se o processo já tem parecer jurídico assinado.

    Estratégia:
      - Se houver parecer ASSINADO → não redija nova minuta. Avise o usuário
        e marque na planilha (status_minuta='já existe parecer assinado').
      - Se houver parecer NÃO assinado → pode desconsiderar e redigir nova.
      - Se não houver parecer → redija normalmente.

    Esta tool baixa o PDF do processo (cache se já tiver) e procura por
    bookmarks que comecem com 'Parecer' + busca o padrão de assinatura
    eletrônica do SEI dentro das páginas de cada um.

    Args:
        numero: número NUP do processo

    Returns:
        dict com:
          - n_pareceres_encontrados (int)
          - pareceres: lista com {titulo, pagina_inicio, pagina_fim,
            assinado: bool, assinante: str|None}
          - tem_parecer_assinado: bool
          - recomendacao: 'sem_parecer' | 'parecer_nao_assinado_pode_substituir'
            | 'ja_existe_parecer_assinado_pular_minuta'
    """
    return await asyncio.to_thread(_verificar_parecer_sync, numero)


@mcp.tool()
async def listar_modelos_parecer() -> dict[str, Any]:
    """Lista os modelos de parecer/despacho/ofício disponíveis.

    Modelos ficam em iCloud Drive → 'Modelos parecer'. Cada modelo tem nome
    descritivo, ex: 'Modelo parecer concessão direito real de uso',
    'Modelo despacho declínio de competência', 'Modelo despacho geral'.

    Use isto pra escolher o modelo apropriado depois de identificar a matéria.

    Returns:
        dict com `modelos` (lista) — cada item tem arquivo, stem, extensao,
        tipo_inferido (parecer/despacho/oficio/outro), tamanho_bytes.
    """
    modelos = await asyncio.to_thread(listar_modelos)
    return {"total": len(modelos), "modelos": modelos}


@mcp.tool()
async def ler_modelo_parecer(arquivo: str) -> dict[str, Any]:
    """Lê o texto de um modelo de parecer/despacho/ofício.

    Aceita o nome do arquivo (com ou sem extensão), ex:
        'Modelo parecer concessão direito real de uso'
        'Modelo despacho geral.docx'

    Args:
        arquivo: nome do modelo (de listar_modelos_parecer)

    Returns:
        dict com arquivo, texto (estrutura/conteúdo do modelo), tipo_inferido.
    """
    return await asyncio.to_thread(ler_modelo, arquivo)


@mcp.tool()
async def atualizar_planilha_processo(
    numero: str,
    status_minuta: str | None = None,
    materia: str | None = None,
    complexidade: str | None = None,
    acao_sugerida: str | None = None,
    resumo: str | None = None,
) -> dict[str, Any]:
    """Atualiza a linha de um processo na planilha controle.xlsx.

    Use durante o workflow de análise pra registrar progresso visível na
    planilha:
      - Ao COMEÇAR a analisar: status_minuta='em análise'
      - Ao TERMINAR: status_minuta='minuta gerada', materia, complexidade,
        acao_sugerida, resumo preenchidos
      - Em ERRO: status_minuta='erro: <descrição>'

    Args:
        numero: número do processo (ex: '00047.001613/2026-14')
        status_minuta: 'em análise' / 'minuta gerada' / 'erro: ...' / etc.
        materia: matéria identificada (ex: 'Aforamento', 'Perpetuidade')
        complexidade: 'auto' (rotina) ou 'manual' (parecer detalhado)
        acao_sugerida: 1 frase concreta da ação proposta
        resumo: 2-3 frases resumindo o processo

    Apenas os campos passados são atualizados; demais ficam intactos.
    Requer que controle.xlsx exista (chame gerar_planilha_controle antes).
    """
    campos: dict[str, str] = {}
    if status_minuta is not None:
        campos["Status_minuta"] = status_minuta
    if materia is not None:
        campos["Materia"] = materia
    if complexidade is not None:
        campos["Complexidade"] = complexidade
    if acao_sugerida is not None:
        campos["Acao_sugerida"] = acao_sugerida
    if resumo is not None:
        campos["Resumo"] = resumo
    if not campos:
        return {"erro": "nenhum campo passado pra atualizar"}
    return await asyncio.to_thread(atualizar_linha, numero, **campos)


@mcp.tool()
async def salvar_minuta_processo(
    numero: str,
    conteudo: str,
    tipo: str = "Parecer",
    formato: str = "docx",
    modelo: str | None = None,
) -> dict[str, Any]:
    """Salva a minuta gerada (parecer/despacho/ofício) na pasta do processo.

    A minuta vai pro mesmo diretório onde está o PDF do processo (iCloud).
    Sobrescreve se já existir — cada análise gera versão fresca.

    IMPORTANTE: quando você gerou a minuta a partir de um modelo de
    `listar_modelos_parecer`, SEMPRE passe esse modelo no parâmetro `modelo` —
    o template `.docx` é clonado e a formatação visual (fontes, alinhamento,
    recuos das citações de lei, parágrafos numerados com indent de primeira
    linha, ementa, bloco de assinatura centralizado) é preservada. Sem o
    parâmetro `modelo` o conteúdo é gravado como texto puro e perde toda a
    formatação do modelo de referência.

    A classificação dos parágrafos é automática por heurística sobre o texto:
    cabeçalho (Processo nº/Consulente:/Assunto:), EMENTA, headers romanos
    (I., II., III.), subheaders (II.1., II.4.1.), parágrafos numerados
    (1., 12., 29.), citações de lei (Art./§/incisos romanos/[...]) e bloco
    final centralizado (a partir de "Teresina-PI"/"Atenciosamente"/
    "Respeitosamente").

    Args:
        numero: número do processo (ex: '00047.001613/2026-14')
        conteudo: texto completo da minuta (com quebras de linha)
        tipo: 'Parecer' (default), 'Despacho', 'Ofício', 'Minuta', etc.
            Vira prefixo do nome do arquivo.
        formato: 'docx' (default), 'md' ou 'txt'
        modelo: nome do template em "Modelos parecer" (ex.:
            'MODELO PARECER CONCESSAO DIREITO REAL DE USO.docx'). Quando
            passado e formato='docx', preserva a formatação do template.

    Returns:
        dict com arquivo (path absoluto), tamanho_bytes, tipo, formato, modelo.
    """
    return await asyncio.to_thread(
        _salvar_minuta_sync, numero, conteudo, tipo, formato, modelo
    )


# ===========================================================================
# Documentos no SEI via API REST (26/09/2026)
#
# Leitura: listar_tipos_documento_sei, listar_estilos_sei, formatar_html_sei,
#          ler_documento_sei.
# ESCRITA (única exceção à regra "só leitura" do projeto): criar_documento_sei
#          e editar_documento_sei. Ambas exigem `confirmar=True` NA MESMA
#          CHAMADA; sem isso devolvem só a prévia e nada é gravado. Não há e
#          não haverá tool de assinar, tramitar, concluir ou excluir.
# ===========================================================================

_SEM_CONFIRMACAO = (
    "NADA FOI GRAVADO. Esta é só a prévia. Mostre ao usuário e, se ele "
    "aprovar explicitamente, repita a chamada com confirmar=true."
)


@mcp.tool()
async def listar_tipos_documento_sei(filtro: str = "") -> dict[str, Any]:
    """Lista tipos de documento interno do SEI (idSerie) para `criar_documento_sei`.

    Os mais usados já são resolvidos por nome sem consulta: 'Parecer Jurídico',
    'Parecer', 'Ofício', 'Despacho', 'Memorando', 'Certidão', 'Nota Técnica'.
    Use o filtro pra achar outros (ex.: 'Despacho Decisório').
    """
    from . import sei_docs

    def _f() -> dict[str, Any]:
        tipos = sei_docs.listar_tipos(get_rest(), filtro) if filtro else []
        return {"conhecidos": sei_docs.TIPOS_DOCUMENTO, "filtro": filtro or None, "tipos": tipos}

    return await asyncio.to_thread(_f)


@mcp.tool()
async def listar_estilos_sei() -> dict[str, Any]:
    """Estilos (classes CSS) do SEI aceitos em `editar_documento_sei` e a marcação
    leve que `formatar_html_sei` entende (>, EMENTA:, |c|, |d|, |e|, **negrito**).
    """
    from . import sei_docs

    return {
        "estilos": sei_docs.ESTILOS_SEI,
        "marcacao_leve": {
            "> texto": "Citacao (transcrição de lei/doutrina)",
            "EMENTA: texto": "Texto_Ementa",
            "I. RELATÓRIO / 2.1 - Título": "tópico em negrito (Texto_Justificado)",
            "12. Texto": "parágrafo numerado manualmente (nº em negrito, padrão PRFMAP)",
            "|c| texto": "Texto_Centralizado (bloco de assinatura)",
            "|d| texto": "Texto_Alinhado_Direita (local e data)",
            "|e| texto": "Texto_Alinhado_Esquerda (destinatário)",
            "**x** / *x*": "negrito / itálico",
            "Id. 15559225": "vira link sei! se o documento estiver no processo (passe `numero`)",
            "linha em branco": "separa parágrafos",
        },
    }


@mcp.tool()
async def formatar_html_sei(texto: str, numero: str | None = None) -> dict[str, Any]:
    """Converte texto simples (com a marcação leve de `listar_estilos_sei`) no HTML
    com as classes do SEI. Só prévia: não toca no SEI. Passe `numero` (NUP do
    processo) para que "Id. NNNNNNN" vire link sei! nos documentos do processo.
    """
    from . import sei_docs

    def _f() -> dict[str, Any]:
        mapa = sei_docs.mapa_ids_processo(get_rest(), numero) if numero else {}
        h = sei_docs.formatar_html_sei(texto, mapa)
        return {"html": h, "paragrafos": h.count("<p "), "ids_linkados": sum(1 for _ in re.finditer("ancoraSei", h))}

    return await asyncio.to_thread(_f)


@mcp.tool()
async def ler_documento_sei(id_documento: str, numero: str | None = None) -> dict[str, Any]:
    """Lê um documento INTERNO do SEI via API: seções (com papel cabeçalho/corpo/
    rodapé), versão atual e o texto do corpo. Só leitura.

    Args:
        id_documento: nº SEI visível (ex. '15559225') ou id interno. Se passar o
            nº visível, informe também `numero` (NUP) para resolver o id.
    """
    from . import sei_docs

    def _f() -> dict[str, Any]:
        rest = get_rest()
        idi = sei_docs.resolver_documento(rest, id_documento, numero)
        info = sei_docs.listar_secoes(rest, idi)
        corpo_modelo = info["papeis"].get("corpo")
        corpo_html = next((s["html"] for s in info["secoes"] if s["idSecaoModelo"] == corpo_modelo), "")
        info["texto_corpo"] = sei_docs.html_para_texto(corpo_html)
        info["id_interno"] = idi
        return info

    return await asyncio.to_thread(_f)


@mcp.tool()
async def criar_documento_sei(
    numero: str,
    tipo: str,
    descricao: str = "",
    confirmar: bool = False,
) -> dict[str, Any]:
    """Cria um documento INTERNO vazio no processo (ESCRITA no SEI).

    Sem `confirmar=true` devolve apenas a prévia (processo resolvido, tipo e id
    de série) e NÃO cria nada. Só chame com confirmar=true depois que o usuário
    aprovar explicitamente. O documento nasce sem assinatura e sem tramitação —
    isso continua sendo feito por ele no SEI.

    Args:
        numero: NUP do processo, ex. '00047.003379/2026-56'
        tipo: 'Parecer Jurídico', 'Ofício', 'Despacho', 'Memorando', 'Certidão',
            'Nota Técnica' ou id de série (ver listar_tipos_documento_sei)
        descricao: descrição/assunto opcional do documento
    """
    from . import sei_docs

    def _f() -> dict[str, Any]:
        rest = get_rest()
        proc = rest.consultar_processo(numero)
        id_serie, nome_tipo = sei_docs.resolver_tipo(rest, tipo)
        previa = {
            "acao": "criar_documento",
            "processo": proc.get("ProtocoloProcedimentoFormatado"),
            "tipo_processo": proc.get("NomeTipoProcedimento"),
            "tipo_documento": nome_tipo,
            "id_serie": id_serie,
            "descricao": descricao,
            "unidade": "PROC-PRFMAP-PGM",
        }
        if not confirmar:
            return {"gravado": False, "previa": previa, "instrucao": _SEM_CONFIRMACAO}
        r = sei_docs.criar_documento(rest, numero, tipo, descricao)
        return {"gravado": True, **r, "proximo_passo": "editar_documento_sei(id_documento=..., conteudo=...)"}

    return await asyncio.to_thread(_f)


@mcp.tool()
async def editar_documento_sei(
    id_documento: str,
    conteudo: str,
    numero: str | None = None,
    secao: str = "corpo",
    formato: str = "texto",
    confirmar: bool = False,
) -> dict[str, Any]:
    """Substitui o conteúdo de uma seção de documento INTERNO do SEI (ESCRITA).

    Sem `confirmar=true` devolve a prévia (HTML final, seção alvo, versão) e NÃO
    grava. Só chame com confirmar=true depois que o usuário aprovar.

    Args:
        id_documento: nº SEI visível ou id interno (com nº visível, passe `numero`)
        conteudo: texto com a marcação leve (formato='texto', default — vira HTML
            via formatar_html_sei) ou HTML pronto com classes do SEI (formato='html')
        numero: NUP do processo — necessário para links sei! ("Id. NNNN") e para
            resolver o nº visível
        secao: 'corpo' (default), 'cabecalho', 'rodape' ou um idSecaoModelo
            (ver ler_documento_sei → papeis)
        formato: 'texto' | 'html'
    """
    from . import sei_docs

    def _f() -> dict[str, Any]:
        rest = get_rest()
        idi = sei_docs.resolver_documento(rest, id_documento, numero)
        info = sei_docs.listar_secoes(rest, idi)
        modelo = info["papeis"].get(secao, secao)
        if modelo not in {s["idSecaoModelo"] for s in info["secoes"] if not s["somente_leitura"]}:
            raise ValueError(
                f"seção {secao!r} não é editável neste documento; papéis: {info['papeis']}"
            )
        if formato == "html":
            html_novo = conteudo
        else:
            mapa = sei_docs.mapa_ids_processo(rest, numero) if numero else {}
            html_novo = sei_docs.formatar_html_sei(conteudo, mapa)
        atual = next(s for s in info["secoes"] if s["idSecaoModelo"] == modelo)
        previa = {
            "acao": "editar_documento",
            "id_documento": idi,
            "secao": secao,
            "idSecaoModelo": modelo,
            "versao_atual": info["versao"],
            "tamanho_atual": atual["tamanho"],
            "tamanho_novo": len(html_novo),
            "html_novo": html_novo,
        }
        if not confirmar:
            return {"gravado": False, "previa": previa, "instrucao": _SEM_CONFIRMACAO}
        payload = sei_docs.montar_payload_secoes(info, {modelo: html_novo})
        r = sei_docs.gravar_secoes(rest, idi, payload, info["versao"])
        return {"gravado": True, "id_documento": idi, "secao": secao, "idSecaoModelo": modelo, **r}

    return await asyncio.to_thread(_f)


def main() -> None:
    """Entry point pro `sei-mcp` via stdio (Claude Desktop)."""
    mcp.run()


if __name__ == "__main__":
    main()
