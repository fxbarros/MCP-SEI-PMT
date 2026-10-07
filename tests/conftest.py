"""Fixtures da suíte do sei-mcp.

GARANTIAS DE ISOLAMENTO (não relaxar):
  - nenhum teste lê o Keychain: `keyring.get_password` devolve credenciais
    fictícias em toda a suíte (fixture autouse);
  - nenhum teste fala com sei.teresina.pi.gov.br: o `SeiRest` dos testes usa
    `httpx.MockTransport` apontando para `FakeWssei` (tests/fake_wssei.py);
  - nenhum teste abre o Chrome: `sei_client` não é exercitado;
  - nenhum teste grava fora de `tmp_path`.
"""
from __future__ import annotations

import httpx
import pytest

from fake_wssei import CREDENCIAIS, HTML_INTERNO, ID_CHEFIA, ID_P1, ID_P2, ID_PRFMAP, P1, P2, PDF_FALSO, FakeWssei
from sei_mcp import sei_rest
from sei_mcp.sei_rest import BASE_URL, SeiRest


@pytest.fixture(autouse=True)
def keychain_falso(monkeypatch):
    monkeypatch.setattr(
        sei_rest.keyring,
        "get_password",
        lambda service, chave: CREDENCIAIS.get(chave) if service == sei_rest.SERVICE else None,
    )
    # estado global do módulo zerado entre testes
    monkeypatch.setattr(sei_rest, "_ID_UNIDADE_POR_SIGLA", {})
    monkeypatch.setattr(sei_rest, "_singleton", None)


@pytest.fixture
def fake() -> FakeWssei:
    f = FakeWssei()
    f.adicionar_processo(P1, ID_PRFMAP, ID_P1)
    f.adicionar_processo(P2, ID_CHEFIA, ID_P2)
    return f


@pytest.fixture
def fazer_rest(fake):
    """Fábrica de `SeiRest` ligados ao wssei falso (vários por teste, p/ concorrência)."""
    criados: list[SeiRest] = []

    def _fazer(unidade_sigla: str | None = None) -> SeiRest:
        r = SeiRest(unidade_sigla=unidade_sigla)
        r._http.close()
        r._http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(fake.handler))
        criados.append(r)
        return r

    yield _fazer
    for r in criados:
        r.close()


@pytest.fixture
def rest(fazer_rest) -> SeiRest:
    return fazer_rest()


@pytest.fixture
def processo_com_documentos(fake):
    """P1 com três documentos: parecer interno assinado, parecer externo (PDF) e despacho-formulário restrito."""
    fake.adicionar_documento(
        ID_P1, "17000001", protocolo="15000001", tipo="Parecer Jurídico", tipo_documento="I",
        nome_composto="Parecer Jurídico 100 (15000001)", assinado=True,
        assinaturas=[{"nome": "Fulana Procuradora", "cargo": "Procuradora do Município",
                      "unidade": "PROC-PRFMAP-PGM", "dataHora": "01/10/2026 10:30"}],
        html_render=HTML_INTERNO,
    )
    fake.definir_secoes("17000001", versao="3", secoes=[
        ("1", "300", "<p>timbre</p>", True),
        ("2", "176", "<p>PARECER JURÍDICO Nº 100/2026</p>", False),
        ("3", "177", "<p>corpo velho</p>", False),
        ("4", "301", "<p>referência</p>", True),
        ("5", "392", "<p>rodapé</p>", False),
    ])
    fake.adicionar_documento(
        ID_P1, "17000002", protocolo="15000002", tipo="Parecer", tipo_documento="X",
        nome_composto="Parecer (15000002)", informacao="Parecer da SEMPLAN", mime="application/pdf",
        nome="Parecer_externo.pdf", unidade="SEMPLAN", anexo=PDF_FALSO,
    )
    fake.adicionar_documento(
        ID_P1, "17000003", protocolo="15000003", tipo="Despacho", tipo_documento="A",
        nome_composto="Despacho 55 (15000003)", restrito=True, html_render="<html><body><p>Despacho</p></body></html>",
    )
    return fake
