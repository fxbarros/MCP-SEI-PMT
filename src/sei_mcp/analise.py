"""Extração de texto e detecção de parecer assinado em PDFs do SEI."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader

# Padrão de assinatura eletrônica do SEI-PMT (Teresina).
# Formato visto em produção (2026-04-30):
#   "Documento assinado eletronicamente por <NOME>, <CARGO>, em <DATA>,
#    às <HORA>, com fundamento no Decreto nº <X>/<Y> - PMT."
# O `com fundamento no Decreto` é o marcador que diferencia uma assinatura
# real (com força legal) de uma simples menção do texto.
RE_ASSINATURA_SEI = re.compile(
    r"Documento\s+assinado\s+eletronicamente\s+por\s+"
    r"(?P<nome>[^,\n]+?)\s*,\s*"
    r"(?P<cargo>[^,\n]+?)\s*,\s*"
    r"em\s+(?P<data>\d{2}/\d{2}/\d{4})\s*,\s*"
    r"(?:às|as)\s+(?P<hora>\d{1,2}:\d{2})\s*,\s*"
    r"com\s+fundamento\s+no\s+Decreto",
    re.IGNORECASE | re.DOTALL,
)
# Identificadores do documento no rodapé da assinatura
RE_SEI_ID = re.compile(r"SEI\s*n[ºo]?\s*(\d+)", re.IGNORECASE)
RE_VERIFICADOR = re.compile(
    r"c[óo]digo\s+verificador\s+(\d+)", re.IGNORECASE
)
RE_CRC = re.compile(r"c[óo]digo\s+CRC\s+([A-Fa-f0-9]+)", re.IGNORECASE)

# Marca de bookmark/título de parecer na árvore do SEI
RE_TITULO_PARECER = re.compile(r"^\s*parecer\b", re.IGNORECASE)


def extrair_texto_pdf(path: Path, max_chars: int | None = None) -> dict[str, Any]:
    """Extrai texto de um PDF.

    Returns:
        {"n_paginas": N, "texto": str, "n_chars": int, "outline": [...]}
    """
    reader = PdfReader(str(path))
    n_paginas = len(reader.pages)

    paginas_texto: list[str] = []
    for p in reader.pages:
        try:
            t = p.extract_text() or ""
        except Exception:
            t = ""
        paginas_texto.append(t.strip())
    texto_completo = "\n\n".join(paginas_texto).strip()
    truncado = False
    if max_chars and len(texto_completo) > max_chars:
        texto_completo = texto_completo[:max_chars]
        truncado = True

    # tenta extrair outline/bookmarks (cada documento do SEI vira um bookmark)
    outline: list[dict[str, Any]] = []
    try:
        for entry in _flatten_outline(reader.outline, reader):
            outline.append(entry)
    except Exception:
        pass

    return {
        "n_paginas": n_paginas,
        "n_chars": len(texto_completo),
        "texto": texto_completo,
        "truncado": truncado,
        "outline": outline,
    }


def verificar_parecer_juridico_pdf(path: Path) -> dict[str, Any]:
    """Verifica se o processo tem parecer jurídico, e se está assinado.

    Estratégia:
      1. Lê o outline (bookmarks) do PDF — cada documento do SEI é um bookmark
      2. Filtra os que começam com "Parecer"
      3. Pra cada um, extrai o texto das páginas correspondentes
      4. Busca padrão de assinatura eletrônica do SEI

    Returns:
        {
          "n_pareceres_encontrados": int,
          "pareceres": [{"titulo", "pagina_inicio", "pagina_fim",
                         "assinado": bool, "assinante": str|None}],
          "tem_parecer_assinado": bool,
          "recomendacao": "sem_parecer" | "parecer_nao_assinado_pode_substituir"
                           | "ja_existe_parecer_assinado_pular_minuta",
        }
    """
    reader = PdfReader(str(path))
    n_paginas = len(reader.pages)

    outline: list[dict[str, Any]] = []
    try:
        outline = _flatten_outline(reader.outline, reader)
    except Exception:
        pass

    pareceres_brutos = [
        item for item in outline if RE_TITULO_PARECER.match(item["titulo"])
    ]

    resultados: list[dict[str, Any]] = []
    for p in pareceres_brutos:
        pagina_inicio = p["pagina"]
        # próximo bookmark (qualquer um) define o fim do parecer
        proxima = next(
            (o["pagina"] for o in outline if o["pagina"] > pagina_inicio),
            n_paginas + 1,
        )
        pagina_fim = proxima - 1

        textos: list[str] = []
        for pn in range(pagina_inicio - 1, min(pagina_fim, n_paginas)):
            try:
                t = reader.pages[pn].extract_text() or ""
            except Exception:
                t = ""
            textos.append(t)
        texto_parecer = "\n".join(textos)

        m = RE_ASSINATURA_SEI.search(texto_parecer)
        assinado = bool(m)
        # pypdf separa palavras com \t em alguns PDFs do SEI → normaliza espaços
        assinante = " ".join(m.group(1).split()) if m else None

        resultados.append(
            {
                "titulo": p["titulo"],
                "pagina_inicio": pagina_inicio,
                "pagina_fim": pagina_fim,
                "assinado": assinado,
                "assinante": assinante,
            }
        )

    tem_assinado = any(r["assinado"] for r in resultados)
    if tem_assinado:
        recomendacao = "ja_existe_parecer_assinado_pular_minuta"
    elif resultados:
        recomendacao = "parecer_nao_assinado_pode_substituir"
    else:
        recomendacao = "sem_parecer"

    return {
        "n_pareceres_encontrados": len(resultados),
        "pareceres": resultados,
        "tem_parecer_assinado": tem_assinado,
        "recomendacao": recomendacao,
    }


def _flatten_outline(items: Any, reader: PdfReader, nivel: int = 0) -> list[dict[str, Any]]:
    """Achata o outline aninhado do PDF em uma lista linear."""
    out: list[dict[str, Any]] = []
    if not items:
        return out
    for item in items:
        if isinstance(item, list):
            out.extend(_flatten_outline(item, reader, nivel + 1))
            continue
        try:
            titulo = getattr(item, "title", None) or str(item)
            pagina = reader.get_destination_page_number(item) + 1  # 1-based
            out.append({"titulo": titulo, "pagina": pagina, "nivel": nivel})
        except Exception:
            continue
    return out
