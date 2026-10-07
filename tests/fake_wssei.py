"""Servidor wssei FALSO em memória para a suíte offline.

Reproduz o que foi validado contra o SEI-PMT em 02/10/2026 e importa para o
cliente REST:
  - a unidade ativa é estado do USUÁRIO no servidor, compartilhado por todos
    os tokens (`unidade_usuario`); um token novo "nasce" nela;
  - processo aberto em outra unidade responde "Processo não encontrado." e
    anexo de processo de outra unidade "Acesso ao documento não autorizado";
  - token expirado responde HTTP 401 ou, conforme a instalação, HTTP 200 com
    `sucesso=false` e "Acesso negado!" (`expiracao_como`);
  - `/documento/listar/{id}` pagina por `limit`/`start` (start = nº da página);
  - `/documento/baixar/anexo/{id}` devolve o binário do externo ou JSON com
    `data.html` (ainda com entidades) para interno/formulário.

Ganchos para simular corridas: `trocar_unidade_antes_da_proxima_consulta`
(outro token troca a unidade DEPOIS da reasserção e ANTES da consulta) e
`erro_forcado_proxima_consulta` (erro que não é de unidade).
"""
from __future__ import annotations

import html
import re
import threading
from urllib.parse import parse_qs

import httpx

from sei_mcp.sei_rest import BASE_URL

PREFIXO_API = httpx.URL(BASE_URL).path

CREDENCIAIS = {"usuario": "usuario.teste", "senha": "segredo-de-teste", "unidade": "PROC-PRFMAP-PGM"}

ID_PRFMAP = "110001096"
ID_CHEFIA = "110002000"
UNIDADES = [
    {"id": ID_PRFMAP, "sigla": "PROC-PRFMAP-PGM", "descricao": "Procuradoria de Regularização Fundiária, Meio Ambiente e Patrimonial"},
    {"id": ID_CHEFIA, "sigla": "PROC-PRFMAP-CHEFIA-PGM", "descricao": "Chefia da PRFMAP"},
    {"id": "110003000", "sigla": "CAPSC-PGM", "descricao": "Câmara de Prevenção e Solução de Conflitos"},
]

# NUPs fictícios (dígitos verificadores inventados de propósito)
P1 = "00000.000001/2026-11"   # aberto só na PRFMAP
P2 = "00000.000002/2026-22"   # aberto só na CHEFIA
ID_P1 = "16000001"
ID_P2 = "16000002"

PDF_FALSO = b"%PDF-1.4\n% arquivo de teste, nao e um PDF de verdade\n%%EOF\n"
HTML_INTERNO = (
    '<html><head><meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
    "<title>SEI</title></head><body><p>Parecer: ação</p></body></html>"
)


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _erro(msg: str) -> httpx.Response:
    return _json({"sucesso": False, "mensagem": msg})


def _ok(data=None, mensagem: str | None = None) -> httpx.Response:
    j: dict = {"sucesso": True, "data": data}
    if mensagem:
        j["mensagem"] = mensagem
    return _json(j)


class FakeWssei:
    def __init__(self) -> None:
        self.unidade_usuario = ID_CHEFIA          # o usuário começa FORA da unidade alvo
        self.tokens_validos: set[str] = set()
        self.n_tokens = 0
        self.expiracao_como = "401"               # ou "mensagem"
        self.processos: dict[str, dict] = {}      # nup -> {"id", "unidade"}
        self.documentos: dict[str, list[dict]] = {}   # id_proc -> docs (formato do wssei)
        self.assinaturas: dict[str, list[dict]] = {}  # id_doc -> assinaturas
        self.anexos: dict[str, dict] = {}         # id_doc -> {"bytes","content_type","nome"} | {"html"}
        self.secoes: dict[str, dict] = {}         # id_doc -> {"versao", "secoes": [{id, idSecaoModelo, conteudo, somenteLeitura}]}
        self.tipos_documento = [{"id": "1344", "nome": "Despacho Decisório"}, {"id": "291", "nome": "Despacho"}]
        self.documentos_criados: list[dict] = []
        self.gravacoes: list[dict] = []
        self.chamadas: list[tuple[str, str]] = []
        self.trocar_unidade_antes_da_proxima_consulta: str | None = None
        self.erro_forcado_proxima_consulta: str | None = None
        self._lock = threading.Lock()

    # -- montagem de cenário -------------------------------------------
    def adicionar_processo(self, nup: str, id_unidade: str, id_proc: str) -> str:
        self.processos[nup] = {"id": id_proc, "unidade": id_unidade}
        self.documentos.setdefault(id_proc, [])
        return id_proc

    def adicionar_documento(
        self,
        id_proc: str,
        id_doc: str,
        *,
        protocolo: str,
        tipo: str,
        tipo_documento: str = "I",
        nome_composto: str | None = None,
        informacao: str = "",
        mime: str = "",
        nome: str = "",
        unidade: str = "PROC-PRFMAP-PGM",
        assinado: bool = False,
        restrito: bool = False,
        cancelado: bool = False,
        assinaturas: list[dict] | None = None,
        anexo: bytes | None = None,
        html_render: str | None = None,
    ) -> None:
        self.documentos.setdefault(id_proc, []).append(
            {
                "id": id_doc,
                "atributos": {
                    "tipo": tipo,
                    "tipoDocumento": tipo_documento,
                    "protocoloFormatado": protocolo,
                    "nomeComposto": nome_composto or f"{tipo} ({protocolo})",
                    "informacao": informacao,
                    "mimeType": mime,
                    "nome": nome,
                    "tamanho": str(len(anexo)) if anexo is not None else "",
                    "siglaUnidade": unidade,
                    "status": {
                        "documentoAssinado": "S" if assinado else "N",
                        "documentoRestrito": "S" if restrito else "N",
                        "documentoCancelado": "S" if cancelado else "N",
                    },
                },
            }
        )
        if assinaturas:
            self.assinaturas[id_doc] = assinaturas
        if anexo is not None:
            self.anexos[id_doc] = {"bytes": anexo, "content_type": mime or "application/octet-stream", "nome": nome}
        elif html_render is not None:
            self.anexos[id_doc] = {"html": html_render}

    def definir_secoes(self, id_doc: str, versao: str, secoes: list[tuple[str, str, str, bool]]) -> None:
        """secoes = [(id, idSecaoModelo, html, somente_leitura), ...] na ordem do documento."""
        self.secoes[id_doc] = {
            "versao": versao,
            "secoes": [{"id": i, "idSecaoModelo": m, "conteudo": h, "somenteLeitura": "S" if ro else "N"} for i, m, h, ro in secoes],
        }

    def expirar_tokens(self) -> None:
        self.tokens_validos.clear()

    @property
    def n_alteracoes_unidade(self) -> int:
        return self.chamadas.count(("POST", "/usuario/alterar/unidade"))

    # -- roteamento ------------------------------------------------------
    def _processo_visivel(self, id_proc: str) -> bool:
        return any(p["id"] == id_proc and p["unidade"] == self.unidade_usuario for p in self.processos.values())

    def _processo_do_documento(self, id_doc: str) -> str | None:
        for id_proc, docs in self.documentos.items():
            if any(d["id"] == id_doc for d in docs):
                return id_proc
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path.startswith(PREFIXO_API), path
        path = path[len(PREFIXO_API):]
        form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()} if request.content else {}
        params = dict(request.url.params)
        with self._lock:
            self.chamadas.append((request.method, path))

            if path == "/autenticar":
                if form.get("usuario") != CREDENCIAIS["usuario"] or form.get("senha") != CREDENCIAIS["senha"]:
                    return _erro("Usuário ou senha inválidos")
                self.n_tokens += 1
                tok = f"tok{self.n_tokens}"
                self.tokens_validos.add(tok)
                return _ok({"token": tok, "loginData": {"IdUnidadeAtual": self.unidade_usuario}})

            if request.headers.get("token") not in self.tokens_validos:
                if self.expiracao_como == "401":
                    return _json({"sucesso": False, "mensagem": "Token inválido"}, 401)
                return _erro("Acesso negado!")

            if path == "/versao":
                return _ok({"versao": "3.0.4"})
            if path == "/usuario/unidades":
                return _ok(UNIDADES)
            if path == "/usuario/alterar/unidade":
                self.unidade_usuario = form["unidade"]
                return _ok(None, "Unidade alterada")

            if path == "/processo/consultar":
                if self.trocar_unidade_antes_da_proxima_consulta:
                    self.unidade_usuario = self.trocar_unidade_antes_da_proxima_consulta
                    self.trocar_unidade_antes_da_proxima_consulta = None
                if self.erro_forcado_proxima_consulta:
                    msg, self.erro_forcado_proxima_consulta = self.erro_forcado_proxima_consulta, None
                    return _erro(msg)
                nup = params.get("protocoloFormatado", "")
                proc = self.processos.get(nup)
                if not proc or proc["unidade"] != self.unidade_usuario:
                    return _erro("Processo não encontrado.")
                return _ok({"IdProcedimento": proc["id"], "ProtocoloProcedimentoFormatado": nup, "NomeTipoProcedimento": "PGM: Parecer"})

            if path.startswith("/documento/listar/assinaturas/"):
                return _ok(self.assinaturas.get(path.rsplit("/", 1)[1], []))

            if path.startswith("/documento/listar/"):
                id_proc = path.rsplit("/", 1)[1]
                if not self._processo_visivel(id_proc):
                    return _erro("Processo não encontrado.")
                docs = self.documentos.get(id_proc, [])
                limit, start = int(params.get("limit", 200)), int(params.get("start", 0))
                return _ok(docs[start * limit : (start + 1) * limit])

            if path.startswith("/documento/baixar/anexo/"):
                id_doc = path.rsplit("/", 1)[1]
                dono = self._processo_do_documento(id_doc)
                if dono is None or not self._processo_visivel(dono):
                    return _erro("Acesso ao documento não autorizado")
                a = self.anexos.get(id_doc)
                if a is None:
                    return _erro("Documento sem anexo")
                if "html" in a:
                    return _ok({"html": html.escape(a["html"])})
                return httpx.Response(
                    200,
                    content=a["bytes"],
                    headers={"content-type": a["content_type"], "content-disposition": f'attachment; filename="{a["nome"]}"'},
                )

            if path == "/documento/tipo/pesquisar":
                f = (params.get("filter") or "").lower()
                return _ok([t for t in self.tipos_documento if f in t["nome"].lower()])

            m = re.fullmatch(r"/documento/(\d+)/interno/criar", path)
            if m and request.method == "POST":
                id_proc = m.group(1)
                if not self._processo_visivel(id_proc):
                    return _erro("Processo não encontrado.")
                n = len(self.documentos_criados) + 1
                id_doc, protocolo = f"1790000{n}", f"1590000{n}"
                self.documentos_criados.append({"id_proc": id_proc, "idSerie": form.get("idSerie"),
                                                "descricao": form.get("descricao"), "nivelAcesso": form.get("nivelAcesso")})
                return _ok({"idDocumento": id_doc, "protocoloDocumentoFormatado": protocolo}, "Documento criado")

            if path == "/documento/secao/listar":
                doc = self.secoes.get(params.get("id", ""))
                if doc is None:
                    return _erro("Documento não encontrado.")
                # o wssei devolve o HTML das seções com entidades (&lt;p&gt;...)
                return _ok({"secoes": [{**sec, "conteudo": html.escape(sec["conteudo"])} for sec in doc["secoes"]],
                            "ultimaVersaoDocumento": doc["versao"]})

            if path == "/documento/secao/alterar":
                self.gravacoes.append(dict(form))
                return _ok({"versao": str(int(form.get("versao", "1")) + 1)}, "Seções alteradas")

            return _json({"sucesso": False, "mensagem": f"rota desconhecida: {path}"}, 404)
