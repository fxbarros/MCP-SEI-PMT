"""Cliente REST: a armadilha "unidade ativa é do usuário, não do token" e o resto do contrato."""
from __future__ import annotations

import threading

import pytest

from fake_wssei import ID_CHEFIA, ID_P1, ID_PRFMAP, P1, P2, PDF_FALSO
from sei_mcp import sei_rest
from sei_mcp.sei_rest import SeiRest, SeiRestError


def caminhos(fake) -> list[str]:
    return [p for _, p in fake.chamadas]


# -- unidade reassegurada -----------------------------------------------

def test_reassegura_a_unidade_antes_de_cada_operacao(rest, fake):
    rest.get("/versao")
    rest.get("/versao")
    assert fake.n_alteracoes_unidade == 2
    assert fake.unidade_usuario == ID_PRFMAP
    assert rest.estatisticas["alteracoes_unidade"] == 2
    assert caminhos(fake) == [
        "/autenticar", "/usuario/unidades", "/usuario/alterar/unidade", "/versao",
        "/usuario/alterar/unidade", "/versao",
    ]


def test_outro_token_troca_a_unidade_e_a_sessao_segue_enxergando_o_processo(rest, fake):
    assert rest.consultar_processo(P1)["IdProcedimento"] == ID_P1
    fake.unidade_usuario = ID_CHEFIA           # outro token do mesmo usuário trocou a unidade
    assert rest.consultar_processo(P1)["IdProcedimento"] == ID_P1
    assert rest.estatisticas["repeticoes_por_unidade"] == 0


def test_troca_entre_a_reassercao_e_a_consulta_repete_uma_vez(rest, fake):
    fake.trocar_unidade_antes_da_proxima_consulta = ID_CHEFIA   # corrida: troca DEPOIS da reasserção
    assert rest.consultar_processo(P1)["IdProcedimento"] == ID_P1
    assert rest.estatisticas["repeticoes_por_unidade"] == 1
    assert fake.n_alteracoes_unidade == 2
    assert fake.unidade_usuario == ID_PRFMAP


def test_erro_que_nao_e_de_unidade_nao_repete(rest, fake):
    fake.erro_forcado_proxima_consulta = "Parâmetro inválido"
    with pytest.raises(SeiRestError, match="Parâmetro inválido"):
        rest.consultar_processo(P1)
    assert rest.estatisticas["repeticoes_por_unidade"] == 0
    assert fake.n_alteracoes_unidade == 1


def test_processo_de_outra_unidade_falha_nomeando_a_unidade(rest):
    with pytest.raises(SeiRestError) as exc:
        rest.consultar_processo(P2)
    assert "PROC-PRFMAP-PGM" in str(exc.value)
    assert "não encontrado" in str(exc.value).lower()


def test_lock_compartilhado_serializa_sessoes_em_unidades_diferentes(fazer_rest, fake):
    a = fazer_rest("PROC-PRFMAP-PGM")
    b = fazer_rest("PROC-PRFMAP-CHEFIA-PGM")
    assert a._lock is b._lock is sei_rest._LOCK_SESSAO
    erros: list[str] = []

    def martelar(sess: SeiRest, nup: str, n: int = 25) -> None:
        for _ in range(n):
            try:
                sess.consultar_processo(nup)
            except SeiRestError as e:
                erros.append(str(e))

    ta = threading.Thread(target=martelar, args=(a, P1))
    tb = threading.Thread(target=martelar, args=(b, P2))
    ta.start(); tb.start(); ta.join(); tb.join()
    assert erros == []
    assert a.estatisticas["repeticoes_por_unidade"] == 0
    assert b.estatisticas["repeticoes_por_unidade"] == 0


# -- sessão / token -----------------------------------------------------

@pytest.mark.parametrize("modo", ["401", "mensagem"])
def test_token_expirado_reautentica_e_reaplica_a_unidade(rest, fake, modo):
    rest.consultar_processo(P1)
    fake.expiracao_como = modo
    fake.expirar_tokens()
    fake.unidade_usuario = ID_CHEFIA           # o token novo nasceria aqui
    assert rest.consultar_processo(P1)["IdProcedimento"] == ID_P1
    assert rest.estatisticas["reautenticacoes"] == 1
    assert fake.n_tokens == 2
    i = fake.chamadas.index(("POST", "/autenticar"), 1)     # 2ª autenticação
    assert fake.chamadas[i + 1] == ("POST", "/usuario/alterar/unidade")


def test_sigla_desconhecida_da_erro_claro(fazer_rest):
    r = fazer_rest("NAO-EXISTE")
    with pytest.raises(SeiRestError, match="não encontrada entre as 3 unidades"):
        r.get("/versao")


def test_id_da_unidade_e_resolvido_uma_vez_por_processo(fazer_rest, fake):
    a, b = fazer_rest(), fazer_rest()
    a.get("/versao")
    b.get("/versao")
    assert caminhos(fake).count("/usuario/unidades") == 1


def test_unidade_vem_do_keychain_quando_nao_informada(rest):
    assert rest.unidade_sigla == "PROC-PRFMAP-PGM"


def test_credenciais_ausentes_no_keychain(monkeypatch):
    monkeypatch.setattr(sei_rest.keyring, "get_password", lambda *_: None)
    with pytest.raises(SeiRestError, match="Keychain"):
        SeiRest()


# -- consultas ------------------------------------------------------------

def test_listar_documentos_pagina_ate_esgotar(rest, fake):
    for i in range(250):
        fake.adicionar_documento(ID_P1, str(17100000 + i), protocolo=str(15100000 + i), tipo="Anexo", tipo_documento="X")
    docs = rest.listar_documentos(ID_P1)
    assert len(docs) == 250
    assert caminhos(fake).count(f"/documento/listar/{ID_P1}") == 2
    assert fake.n_alteracoes_unidade == 1        # uma reasserção para a operação inteira


def test_verificar_pareceres_interno_e_externo(rest, processo_com_documentos):
    out = rest.verificar_pareceres(P1)
    assert [p["id_documento"] for p in out] == ["15000001", "15000002"]   # o Despacho fica de fora
    interno, externo = out
    assert interno["titulo_arvore"] == "Parecer Jurídico 15000001"
    assert interno["origem"] == "interno" and interno["assinado"] is True and interno["n_assinaturas"] == 1
    assert interno["assinante_nome"] == "Fulana Procuradora"
    assert interno["assinante_cargo"] == "Procuradora do Município"
    assert interno["unidade_sigla"] == "PROC-PRFMAP-PGM"
    assert (interno["data_assinatura"], interno["hora_assinatura"]) == ("01/10/2026", "10:30")
    assert externo["origem"] == "externo" and externo["assinado"] is False and externo["n_assinaturas"] == 0
    assert externo["data_assinatura"] is None


def test_baixar_conteudo_documento_binario_e_html(rest, processo_com_documentos):
    ext = rest.baixar_conteudo_documento("17000002")
    assert ext == {"conteudo": PDF_FALSO, "content_type": "application/pdf", "nome_arquivo": "Parecer_externo.pdf"}
    interno = rest.baixar_conteudo_documento("17000001")
    assert set(interno) == {"html"}
    assert "&lt;html&gt;" in interno["html"]      # ainda com entidades: quem chama faz o unescape


def test_documento_de_processo_de_outra_unidade_nao_autorizado(rest, fake):
    fake.adicionar_documento("16000002", "17200001", protocolo="15200001", tipo="Ofício", tipo_documento="X",
                             mime="application/pdf", nome="x.pdf", anexo=PDF_FALSO)
    with pytest.raises(SeiRestError, match="não autorizado"):
        rest.baixar_conteudo_documento("17200001")
    assert rest.estatisticas["repeticoes_por_unidade"] == 1   # reassegurou e repetiu 1x antes de desistir
