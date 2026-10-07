"""Documentos: conversões puras (texto → HTML do SEI etc.), árvore e download de um documento."""
from __future__ import annotations

from pathlib import Path

import pytest

from fake_wssei import P1, PDF_FALSO
from sei_mcp import sei_docs
from sei_mcp.sei_docs import (
    _inline,
    _linkar_ids,
    _nome_seguro,
    _pagina_utf8,
    filtrar_arvore,
    formatar_html_sei,
    html_para_texto,
    montar_payload_secoes,
    pagina_para_texto,
    sanitize_iso8859,
    sem_acento,
)
from sei_mcp.sei_rest import SeiRestError

COMUM = "Texto_Justificado_Recuo_Primeira_Linha"


# -- inline / entidades ---------------------------------------------------

def test_sanitize_iso8859_mantem_latim_e_converte_o_resto():
    assert sanitize_iso8859("ação, ç, ã, é, ª, º") == "ação, ç, ã, é, ª, º"
    assert sanitize_iso8859("a — b") == "a &#8212; b"
    assert sanitize_iso8859("“aspas” → ok") == "&#8220;aspas&#8221; &#8594; ok"


def test_inline_escapa_html_e_aplica_negrito_e_italico():
    assert _inline("a < b & c") == "a &lt; b &amp; c"
    assert _inline("**forte** e *leve*") == "<strong>forte</strong> e <i>leve</i>"
    assert _inline("2 * 3 * 4") == "2 * 3 * 4"          # asterisco solto não vira itálico


def test_linkar_ids_so_quando_o_documento_esta_no_processo():
    out = _linkar_ids("ver Id. 15559225 e Id. 15559226", {"15559225": "17001234"})
    assert out.startswith("ver Id. <span")                # prefixo "Id. " preservado
    assert 'class="ancoraSei" id="lnkSei17001234"' in out and ">15559225</a>" in out
    assert "Id. 15559226" in out                          # desconhecido fica como texto
    assert _linkar_ids("Id. 15559225", {}) == "Id. 15559225"


def test_linkar_ids_aceita_ids_e_sei_n():
    out = _linkar_ids("Ids. 15559225 e SEI nº 15559226", {"15559225": "1", "15559226": "2"})
    assert out.count('class="ancoraSei"') == 2


# -- formatar_html_sei ----------------------------------------------------

def test_formatar_paragrafo_comum():
    assert formatar_html_sei("Texto comum.") == f'<p class="{COMUM}">Texto comum.</p>'


def test_formatar_citacao_multilinha():
    assert formatar_html_sei("> Art. 1º Texto.\n> Parágrafo único.") == \
        '<p class="Citacao">Art. 1º Texto.<br />Parágrafo único.</p>'


def test_formatar_ementa():
    assert formatar_html_sei("EMENTA: Direito administrativo.") == \
        '<p class="Texto_Ementa"><strong>EMENTA:</strong> Direito administrativo.</p>'


@pytest.mark.parametrize("topico", ["I. RELATÓRIO", "2.1 - Da competência", "CONCLUSÃO"])
def test_formatar_topicos_em_negrito_sem_recuo(topico):
    assert formatar_html_sei(topico) == f'<p class="Texto_Justificado"><strong>{topico}</strong></p>'


def test_formatar_paragrafo_numerado_nao_e_topico():
    assert formatar_html_sei("12. Texto do parágrafo.") == \
        f'<p class="{COMUM}"><strong>12.</strong> Texto do parágrafo.</p>'


def test_formatar_alinhamentos():
    assert formatar_html_sei("|c| Fulano de Tal") == '<p class="Texto_Centralizado">Fulano de Tal</p>'
    assert formatar_html_sei("|d| Teresina, 6 de outubro de 2026") == \
        '<p class="Texto_Alinhado_Direita">Teresina, 6 de outubro de 2026</p>'
    assert formatar_html_sei("|e| Ao Senhor Secretário") == '<p class="Texto_Alinhado_Esquerda">Ao Senhor Secretário</p>'


def test_formatar_html_pronto_passa_intacto():
    for bruto in ('<p class="Citacao">já formatado</p>', "<table><tr><td>a</td></tr></table>"):
        assert formatar_html_sei(bruto) == bruto


def test_formatar_separa_blocos_normaliza_crlf_e_ignora_vazios():
    assert formatar_html_sei("Primeiro.\r\n\r\n\r\n\r\nSegundo.") == \
        f'<p class="{COMUM}">Primeiro.</p>\n<p class="{COMUM}">Segundo.</p>'


def test_formatar_escapa_html_e_linka_ids():
    out = formatar_html_sei("Ver Id. 15559225 (<ofício>).", {"15559225": "17001234"})
    assert "(&lt;ofício&gt;)" in out and 'id="lnkSei17001234"' in out


# -- HTML → texto ------------------------------------------------------------

def test_html_para_texto():
    assert html_para_texto("<p>a<br/>b</p><p>c &amp; d</p>") == "a\nb\n\nc & d"


def test_pagina_para_texto_remove_head_css_script_e_achata_tabela():
    pagina = (
        "<html><head><title>x</title><style>p{color:red}</style></head>"
        "<body><script>alert(1)</script><p>Primeiro&nbsp;par&aacute;grafo</p>"
        "<table><tr><td>A</td><td>B</td></tr></table><div>fim</div></body></html>"
    )
    assert pagina_para_texto(pagina) == "Primeiro parágrafo\n\nA B\n\nfim"


# -- árvore / filtro / nomes ---------------------------------------------------

def test_sem_acento():
    assert sem_acento("Ofício Água ÇÃO") == "oficio agua cao"


def _doc(**kw):
    base = {"titulo": "", "descricao": "", "arquivo": "", "id_documento": ""}
    base.update(kw)
    return base


def test_filtrar_arvore_por_titulo_descricao_arquivo_ou_numero_sem_acento():
    arvore = [
        _doc(titulo="Parecer Jurídico 100 (15559225)", id_documento="15559225"),
        _doc(titulo="Anexo", descricao="Certidão de inteiro teor", id_documento="15559226"),
        _doc(titulo="Anexo", arquivo="MATRICULA_6964.pdf", id_documento="15559227"),
    ]
    ids = lambda filtro: [d["id_documento"] for d in filtrar_arvore(arvore, filtro)]
    assert ids("parecer juridico") == ["15559225"]
    assert ids("CERTIDAO") == ["15559226"]
    assert ids("matrícula") == ["15559227"]
    assert ids("5559226") == ["15559226"]
    assert filtrar_arvore(arvore, "   ") == arvore


def test_nome_seguro():
    assert _nome_seguro('Ofício: 12/2026 "final"?.pdf') == "Ofício_ 12_2026 _final__.pdf"
    assert _nome_seguro("  ...  ") == "documento"
    assert len(_nome_seguro("a" * 300)) == 150


def test_pagina_utf8_troca_o_charset_declarado_ou_insere_meta():
    assert "charset=utf-8" in _pagina_utf8('<meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">')
    assert _pagina_utf8('<meta charset="iso-8859-1">') == '<meta charset="utf-8">'
    sem = _pagina_utf8("<html><head><title>x</title></head><body>a</body></html>")
    assert sem.startswith('<html><head><meta charset="utf-8">')
    ja = '<html><head><meta charset="utf-8"></head></html>'
    assert _pagina_utf8(ja) == ja


def test_montar_payload_secoes_envia_todas_as_secoes():
    info = {"secoes": [
        {"id": "1", "idSecaoModelo": "300", "somente_leitura": True, "html": "<p>timbre</p>"},
        {"id": "2", "idSecaoModelo": "176", "somente_leitura": False, "html": "<p>cabeçalho</p>"},
        {"id": "3", "idSecaoModelo": "177", "somente_leitura": False, "html": "<p>corpo velho</p>"},
        {"id": "4", "idSecaoModelo": "392", "somente_leitura": False, "html": "<p>rodapé</p>"},
    ]}
    payload = montar_payload_secoes(info, {"177": "<p>corpo novo — ok</p>"})
    assert [p["idSecaoModelo"] for p in payload] == ["300", "176", "177", "392"]
    assert payload[0]["conteudo"] == ""                                 # só leitura vai vazia
    assert payload[1]["conteudo"] == "<p>cabeçalho</p>"                 # inalterada: original
    assert payload[2]["conteudo"] == "<p>corpo novo &#8212; ok</p>"     # alterada + ISO-8859-1
    assert payload[3] == {"id": "4", "idSecaoModelo": "392", "conteudo": "<p>rodapé</p>"}


# -- com o wssei falso ---------------------------------------------------------

def test_arvore_processo_mapeia_origem_tamanho_e_status(rest, processo_com_documentos):
    parecer, anexo, despacho = sei_docs.arvore_processo(rest, P1)
    assert [parecer["ordem"], anexo["ordem"], despacho["ordem"]] == [1, 2, 3]
    assert parecer["titulo"] == "Parecer Jurídico 100 (15000001)"
    assert parecer["origem"] == "interno" and parecer["assinado_no_sei"] is True and parecer["tamanho_bytes"] is None
    assert anexo["origem"] == "externo" and anexo["arquivo"] == "Parecer_externo.pdf"
    assert anexo["tamanho_bytes"] == len(PDF_FALSO) and anexo["unidade"] == "SEMPLAN"
    assert despacho["origem"] == "formulario" and despacho["restrito"] is True and despacho["cancelado"] is False


def test_baixar_documento_externo_grava_e_reaproveita_do_disco(rest, fake, processo_com_documentos, tmp_path):
    download = ("GET", "/documento/baixar/anexo/17000002")
    r1 = sei_docs.baixar_documento(rest, P1, "15000002", tmp_path)
    destino = Path(r1["arquivo_local"])
    assert destino == tmp_path / "documentos" / "15000002_Parecer_externo.pdf"
    assert destino.read_bytes() == PDF_FALSO and r1["do_cache"] is False
    assert r1["numero"] == P1 and r1["tamanho_bytes"] == len(PDF_FALSO)
    n = fake.chamadas.count(download)

    r2 = sei_docs.baixar_documento(rest, P1, "17000002", tmp_path)       # pelo id interno
    assert r2["do_cache"] is True and fake.chamadas.count(download) == n

    r3 = sei_docs.baixar_documento(rest, P1, "15000002", tmp_path, forcar=True)
    assert r3["do_cache"] is False and fake.chamadas.count(download) == n + 1


def test_baixar_documento_interno_grava_html_em_utf8(rest, processo_com_documentos, tmp_path):
    r = sei_docs.baixar_documento(rest, P1, "15000001", tmp_path)
    destino = Path(r["arquivo_local"])
    assert destino.name == "15000001_Parecer Jurídico 100.html"
    conteudo = destino.read_text(encoding="utf-8")
    assert "charset=utf-8" in conteudo and "charset=iso-8859-1" not in conteudo
    assert "<p>Parecer: ação</p>" in conteudo and "&lt;" not in conteudo


def test_baixar_documento_que_nao_esta_no_processo(rest, processo_com_documentos, tmp_path):
    with pytest.raises(SeiRestError, match="não está no processo"):
        sei_docs.baixar_documento(rest, P1, "99999999", tmp_path)
    assert not (tmp_path / "documentos").exists()
