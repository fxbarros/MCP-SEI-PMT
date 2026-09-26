"""Geração da planilha de controle dos processos pendentes.

Saída fixa em ~/Desenvolvimento/sei-mcp/saida/controle.xlsx (sobrescrita).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .sei_client import ProcessoPendente

SAIDA_PADRAO = Path.home() / "Desenvolvimento" / "sei-mcp" / "saida" / "controle.xlsx"

_COLUNAS = [
    ("Numero_SEI", 22),
    ("Id_procedimento", 14),
    ("Tipo", 28),
    ("Especificacao", 60),
    ("Atribuido_para", 28),
    ("Tem_anotacao", 10),
    ("Anotacao_resumo", 60),
    ("Origem", 12),
    ("Materia", 22),
    ("Complexidade", 14),
    ("Acao_sugerida", 40),
    ("Status_minuta", 16),
    ("Resumo", 80),
]
# Colunas em amarelo (preenchidas pelo Claude após análise)
_PARA_PREENCHER = {"Materia", "Complexidade", "Acao_sugerida", "Status_minuta", "Resumo"}


def _resumir_anotacao(anot: str | None) -> str:
    if not anot:
        return ""
    primeira = anot.replace("\r", "\n").split("\n")[0].strip()
    if primeira.lower().startswith("anotação /"):
        primeira = primeira[len("Anotação /") :].strip()
    elif primeira.lower().startswith("marcador /"):
        primeira = primeira[len("Marcador /") :].strip()
    return primeira[:300]


def _ler_valores_amarelos_existentes(saida: Path) -> dict[str, dict[str, str]]:
    """Lê valores das colunas amarelas da planilha existente, indexado por número."""
    valores: dict[str, dict[str, str]] = {}
    if not saida.exists():
        return valores
    try:
        wb_old = load_workbook(saida)
        ws_old = wb_old["Pendentes"] if "Pendentes" in wb_old.sheetnames else wb_old.active
        headers_old = [c.value for c in ws_old[1]]
        if "Numero_SEI" not in headers_old:
            return valores
        col_numero_idx = headers_old.index("Numero_SEI")
        for r in range(2, ws_old.max_row + 1):
            num = ws_old.cell(r, col_numero_idx + 1).value
            if not num:
                continue
            vals: dict[str, str] = {}
            for col_idx, header in enumerate(headers_old, start=1):
                if header in _PARA_PREENCHER:
                    v = ws_old.cell(r, col_idx).value
                    if v not in (None, ""):
                        vals[header] = v
            if vals:
                valores[num] = vals
    except Exception:
        pass
    return valores


def gerar_planilha(
    processos: Iterable[ProcessoPendente],
    saida: Path = SAIDA_PADRAO,
) -> Path:
    saida.parent.mkdir(parents=True, exist_ok=True)
    procs = list(processos)

    # Preserva valores das colunas amarelas (Materia/Complexidade/etc.)
    # de uma execução anterior, pra não perder análises feitas.
    valores_antigos = _ler_valores_amarelos_existentes(saida)

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Pendentes"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E78")
    preencher_fill = PatternFill("solid", fgColor="FFF4CE")  # amarelo suave
    wrap = Alignment(wrap_text=True, vertical="top")
    top = Alignment(vertical="top")

    for col_idx, (nome, largura) in enumerate(_COLUNAS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=nome)
        cell.font = header_font
        cell.fill = header_fill
        ws.column_dimensions[get_column_letter(col_idx)].width = largura

    for r, p in enumerate(procs, start=2):
        antigos = valores_antigos.get(p.numero, {})
        valores = {
            "Numero_SEI": p.numero,
            "Id_procedimento": p.id_procedimento,
            "Tipo": p.tipo,
            "Especificacao": p.especificacao or "",
            "Atribuido_para": p.atribuido_para or "",
            "Tem_anotacao": "sim" if p.tem_anotacao else "",
            "Anotacao_resumo": _resumir_anotacao(p.anotacao_completa),
            "Origem": p.origem,
            "Materia": antigos.get("Materia", ""),
            "Complexidade": antigos.get("Complexidade", ""),
            "Acao_sugerida": antigos.get("Acao_sugerida", ""),
            "Status_minuta": antigos.get("Status_minuta", ""),
            "Resumo": antigos.get("Resumo", ""),
        }
        for col_idx, (nome, _) in enumerate(_COLUNAS, start=1):
            cell = ws.cell(row=r, column=col_idx, value=valores[nome])
            cell.alignment = wrap if nome in {"Especificacao", "Anotacao_resumo", "Resumo"} else top
            if nome in _PARA_PREENCHER:
                cell.fill = preencher_fill

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(_COLUNAS))}{len(procs) + 1}"

    # Aba "meta" com timestamp e contagem — útil pra você saber quando rodou
    meta = wb.create_sheet("meta")
    meta["A1"] = "gerado_em"
    meta["B1"] = datetime.now().isoformat(timespec="seconds")
    meta["A2"] = "total_processos"
    meta["B2"] = len(procs)
    meta["A3"] = "com_anotacao"
    meta["B3"] = sum(1 for p in procs if p.tem_anotacao)
    meta["A4"] = "valores_preservados_de_execucao_anterior"
    meta["B4"] = len(valores_antigos)

    wb.save(saida)
    return saida


def status_minuta_por_numero() -> dict[str, str]:
    """Lê a coluna Status_minuta da planilha existente, indexada por numero."""
    out: dict[str, str] = {}
    if not SAIDA_PADRAO.exists():
        return out
    try:
        wb = load_workbook(SAIDA_PADRAO)
        ws = wb["Pendentes"] if "Pendentes" in wb.sheetnames else wb.active
        headers = [c.value for c in ws[1]]
        if "Numero_SEI" not in headers or "Status_minuta" not in headers:
            return out
        col_num = headers.index("Numero_SEI") + 1
        col_st = headers.index("Status_minuta") + 1
        for r in range(2, ws.max_row + 1):
            num = ws.cell(r, col_num).value
            st = ws.cell(r, col_st).value or ""
            if num:
                out[num] = str(st)
    except Exception:
        pass
    return out


def atualizar_linha(numero: str, **campos: str) -> dict[str, object]:
    """Atualiza a linha de um processo na planilha controle.xlsx.

    Aceita kwargs com nomes de coluna (case-insensitive). Útil pra Claude
    gravar progresso da análise: Status_minuta, Materia, Complexidade,
    Acao_sugerida, Resumo.

    Cria a planilha se não existir (mas precisa que listar_pendentes tenha
    sido chamado antes pra ter os processos cadastrados).
    """
    if not SAIDA_PADRAO.exists():
        return {
            "erro": "controle.xlsx não existe — chame gerar_planilha_controle antes"
        }

    wb = load_workbook(SAIDA_PADRAO)
    ws = wb["Pendentes"] if "Pendentes" in wb.sheetnames else wb.active
    headers = [c.value for c in ws[1]]
    if "Numero_SEI" not in headers:
        return {"erro": "coluna Numero_SEI não encontrada na planilha"}
    col_numero = headers.index("Numero_SEI") + 1

    row = None
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, col_numero).value == numero:
            row = r
            break
    if row is None:
        return {
            "erro": f"processo {numero!r} não encontrado na planilha "
                    "— talvez a lista de pendentes tenha mudado"
        }

    atualizados: list[str] = []
    nao_encontrados: list[str] = []
    for nome_campo, valor in campos.items():
        casado = False
        for i, header in enumerate(headers, start=1):
            if header and header.lower() == nome_campo.lower():
                ws.cell(row, i, value=valor)
                atualizados.append(header)
                casado = True
                break
        if not casado:
            nao_encontrados.append(nome_campo)

    wb.save(SAIDA_PADRAO)
    return {
        "linha": row,
        "campos_atualizados": atualizados,
        "campos_nao_encontrados": nao_encontrados,
    }
