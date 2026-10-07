"""Tools do servidor: a trava de confirmação das duas tools que ESCREVEM no SEI e a saída enxuta da árvore."""
from __future__ import annotations

import asyncio
import json

import pytest

from fake_wssei import ID_P1, P1
from sei_mcp import server

COMUM = "Texto_Justificado_Recuo_Primeira_Linha"


@pytest.fixture
def rest_no_server(rest, monkeypatch):
    monkeypatch.setattr(server, "get_rest", lambda: rest)
    return rest


def criou(fake) -> bool:
    return any(p.endswith("/interno/criar") for _, p in fake.chamadas)


# -- criar_documento_sei --------------------------------------------------

def test_criar_documento_sem_confirmar_so_devolve_a_previa(rest_no_server, fake):
    out = asyncio.run(server.criar_documento_sei(P1, "Despacho", descricao="teste"))
    assert out["gravado"] is False
    assert out["previa"]["tipo_documento"] == "Despacho" and out["previa"]["id_serie"] == "291"
    assert out["previa"]["processo"] == P1 and out["previa"]["unidade"] == "PROC-PRFMAP-PGM"
    assert "NADA FOI GRAVADO" in out["instrucao"]
    assert not criou(fake) and fake.documentos_criados == []


def test_criar_documento_com_confirmar_grava(rest_no_server, fake):
    out = asyncio.run(server.criar_documento_sei(P1, "Despacho", descricao="teste", confirmar=True))
    assert out["gravado"] is True
    assert (out["id_documento"], out["numero_sei"], out["tipo"]) == ("17900001", "15900001", "Despacho")
    assert fake.documentos_criados == [{"id_proc": ID_P1, "idSerie": "291", "descricao": "teste", "nivelAcesso": "0"}]


def test_criar_documento_resolve_tipo_pela_api_quando_nao_esta_na_tabela(rest_no_server, fake):
    fake.tipos_documento.append({"id": "999", "nome": "Termo de Compromisso"})
    out = asyncio.run(server.criar_documento_sei(P1, "Termo de Compromisso"))
    assert out["gravado"] is False and out["previa"]["id_serie"] == "999"
    assert ("GET", "/documento/tipo/pesquisar") in fake.chamadas


# -- editar_documento_sei --------------------------------------------------

def test_editar_documento_sem_confirmar_so_devolve_a_previa(rest_no_server, fake, processo_com_documentos):
    out = asyncio.run(server.editar_documento_sei(
        "15000001", "I. RELATÓRIO\n\n1. Trata-se do Id. 15000002.", numero=P1,
    ))
    assert out["gravado"] is False and "NADA FOI GRAVADO" in out["instrucao"]
    previa = out["previa"]
    assert (previa["id_documento"], previa["secao"], previa["idSecaoModelo"], previa["versao_atual"]) == ("17000001", "corpo", "177", "3")
    assert previa["tamanho_atual"] == len("<p>corpo velho</p>")
    assert '<p class="Texto_Justificado"><strong>I. RELATÓRIO</strong></p>' in previa["html_novo"]
    assert 'id="lnkSei17000002"' in previa["html_novo"]        # "Id. 15000002" virou link sei! pelo mapa do processo
    assert fake.gravacoes == []


def test_editar_documento_com_confirmar_grava_todas_as_secoes(rest_no_server, fake, processo_com_documentos):
    out = asyncio.run(server.editar_documento_sei("15000001", "Novo corpo — ok", numero=P1, confirmar=True))
    assert out["gravado"] is True and out["idSecaoModelo"] == "177"
    [g] = fake.gravacoes
    assert (g["documento"], g["versao"]) == ("17000001", "3")
    secoes = json.loads(g["secoes"])
    assert [s["idSecaoModelo"] for s in secoes] == ["300", "176", "177", "301", "392"]
    assert secoes[0]["conteudo"] == "" and secoes[3]["conteudo"] == ""                 # só leitura vazias
    assert secoes[1]["conteudo"] == "<p>PARECER JURÍDICO Nº 100/2026</p>"              # intocada: original
    assert secoes[2]["conteudo"] == f'<p class="{COMUM}">Novo corpo &#8212; ok</p>'   # nova + ISO-8859-1
    assert secoes[4]["conteudo"] == "<p>rodapé</p>"


def test_editar_documento_por_papel_e_html_pronto(rest_no_server, fake, processo_com_documentos):
    out = asyncio.run(server.editar_documento_sei(
        "17000001", '<p class="Texto_Alinhado_Esquerda">Rua X</p>', secao="rodape", formato="html",
    ))
    assert out["previa"]["idSecaoModelo"] == "392"
    assert out["previa"]["html_novo"] == '<p class="Texto_Alinhado_Esquerda">Rua X</p>'


def test_editar_secao_somente_leitura_e_recusada(rest_no_server, processo_com_documentos):
    with pytest.raises(ValueError, match="não é editável"):
        asyncio.run(server.editar_documento_sei("17000001", "x", secao="300", confirmar=True))


def test_editar_documento_que_nao_esta_no_processo(rest_no_server, processo_com_documentos):
    from sei_mcp.sei_rest import SeiRestError
    with pytest.raises(SeiRestError, match="não está no processo"):
        asyncio.run(server.editar_documento_sei("15999999", "x", numero=P1, confirmar=True))


# -- listar_documentos_sei ---------------------------------------------------

def test_listar_documentos_sei_filtra_recorta_e_enxuga(rest_no_server, processo_com_documentos):
    out = asyncio.run(server.listar_documentos_sei(P1, filtro="parecer", ultimos=1))
    assert (out["total_no_processo"], out["n_listados"]) == (3, 1)
    [doc] = out["documentos"]
    assert doc["id_documento"] == "15000002" and doc["descricao"] == "Parecer da SEMPLAN"
    assert doc["assinado_no_sei"] is False            # a única flag falsa que fica
    assert "restrito" not in doc and "cancelado" not in doc and "tamanho_bytes" in doc
