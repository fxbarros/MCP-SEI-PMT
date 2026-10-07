"""Helpers puros da planilha de controle e dos modelos de minuta."""
from __future__ import annotations

import pytest

from sei_mcp.modelos import _classificar, _inferir_tipo
from sei_mcp.planilha import _resumir_anotacao


def test_resumir_anotacao_tira_o_prefixo_e_fica_na_primeira_linha():
    assert _resumir_anotacao(None) == "" and _resumir_anotacao("") == ""
    assert _resumir_anotacao("Anotação / Aguardando matrícula / Fulano em 01/02/2026 10:00") == \
        "Aguardando matrícula / Fulano em 01/02/2026 10:00"
    assert _resumir_anotacao("Marcador / Urgente\r\nsegunda linha") == "Urgente"
    assert _resumir_anotacao("x" * 400) == "x" * 300


@pytest.mark.parametrize("nome, tipo", [
    ("Modelo Despacho Perpetuidade", "despacho"),
    ("oficio padrao", "oficio"),
    ("Ofício resposta", "oficio"),
    ("estrutura parecer", "estrutura"),
    ("Parecer Aforamento", "parecer"),
    ("Minuta Genérica", "outro"),
    ("parecer com despacho", "despacho"),     # despacho tem precedência
])
def test_inferir_tipo_do_modelo(nome, tipo):
    assert _inferir_tipo(nome.lower()) == tipo


@pytest.mark.parametrize("linha, classe", [
    ("", "blank"),
    ("   ", "blank"),
    ("EMENTA: Direito administrativo.", "ementa"),
    ("I. DO RELATÓRIO", "header"),
    ("II. DA FUNDAMENTAÇÃO", "header"),
    ("II.1. Da finalidade", "subheader"),
    ("II.4.1. Detalhe", "subheader"),
    ("12. Texto do parágrafo.", "numerado"),
    ("Art. 5º Todos são iguais", "citacao"),
    ("§ 1º Texto", "citacao"),
    ("I - inciso", "citacao"),
    ("a) alínea", "citacao"),
    ("[...]", "citacao"),
    ("Processo nº 00047.000001/2026-00", "cabecalho"),
    ("Consulente: SEMF", "cabecalho"),
    ("Senhor,", "cabecalho"),
    ("Texto qualquer do corpo.", "numerado"),    # default dentro do corpo
])
def test_classificar_paragrafo(linha, classe):
    assert _classificar(linha, modo_assinatura=False) == classe


def test_classificar_em_modo_assinatura_centraliza_tudo_menos_vazio():
    assert _classificar("Fulano de Tal", modo_assinatura=True) == "centralizado"
    assert _classificar("", modo_assinatura=True) == "blank"
