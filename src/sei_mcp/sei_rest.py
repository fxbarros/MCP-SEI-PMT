"""Cliente REST do SEI-PMT via módulo mod-wssei v2 (SEI 5.0.4 / wssei 3.0.4).

Caminho sem browser: autentica com usuário/senha/órgão do Keychain (service
`mcp-sei`) e consulta processos, documentos e assinaturas direto na API
oficial. Não depende do layout das telas do SEI, que já quebrou o scraper
três vezes em 2026. A escrita (criar/editar documento) fica em `sei_docs.py`,
que passa por `SeiRest.get` / `SeiRest.post`.

Descoberto em 26/09/2026: endpoints públicos `/orgao/listar` (PGM = id 6);
`/autenticar` com form usuario/senha/orgao; demais chamadas levam header
`token`. Assinaturas vêm em `/documento/listar/assinaturas/{id_documento}`
com nome, cargo, unidade e dataHora.

ARMADILHA — unidade ativa é do USUÁRIO, não do token (validado 02/10/2026)
--------------------------------------------------------------------------
`POST /usuario/alterar/unidade` muda a unidade corrente do usuário no servidor.
Todos os tokens do mesmo usuário passam a enxergar a nova unidade, e um token
recém-autenticado já nasce nela (`loginData.IdUnidadeAtual`). Consequência:
se um script paralelo (outro processo, outro `SeiRest(unidade_sigla=...)`)
troca para PROC-PRFMAP-CHEFIA-PGM ou CAPSC-PGM, o servidor MCP — que acreditava
estar em PROC-PRFMAP-PGM — passa a receber "Processo não encontrado." e
"Acesso ao documento não autorizado" nos processos abertos só na PRFMAP, sem
aviso nenhum. Trocar a unidade "1x por token" (comportamento até 02/10/2026)
é, portanto, insuficiente.

Como este módulo lida com isso:
- a unidade alvo é REASSEGURADA (`_assegurar_unidade`) antes de CADA operação,
  dentro do lock e imediatamente antes da chamada protegida;
- o id da sigla é resolvido 1x por processo (`/usuario/unidades` tem ~1.900
  linhas, 1,6 s) e cacheado; a reasserção é só o POST leve (~1 s);
- se mesmo assim o servidor acusar unidade errada ("não encontrado",
  "não autorizado"), a operação reassegura e repete uma única vez;
- após reautenticar (token expirado) a unidade é reaplicada antes de repetir;
- o lock é compartilhado por todas as instâncias do processo: dentro de um
  mesmo processo, duas sessões em unidades diferentes nunca intercalam
  troca e consulta. Entre processos distintos vale só a repetição 1x.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
import keyring

# httpx loga cada request em INFO (formatado pelo rich) — ruído no stderr do MCP
logging.getLogger("httpx").setLevel(logging.WARNING)

SERVICE = "mcp-sei"
BASE_URL = (
    "https://sei.teresina.pi.gov.br/sei/modulos/wssei/controlador_ws.php/api/v2"
)
ORGAO_PGM = "6"
UNIDADE_PADRAO = "PROC-PRFMAP-PGM"

# Mensagens do wssei que denunciam unidade ativa trocada por outro token.
_RE_ERRO_UNIDADE = re.compile(r"não encontrado|não autorizado|nao encontrado|nao autorizado", re.I)

# sigla → id, resolvido 1x por processo (a lista de /usuario/unidades é enorme)
_ID_UNIDADE_POR_SIGLA: dict[str, str] = {}
_cache_lock = threading.Lock()

# Um único lock para TODAS as sessões do processo: a unidade ativa é estado do
# usuário no servidor, então duas instâncias em unidades diferentes precisam
# serializar "trocar unidade + chamada" entre si, não só internamente.
_LOCK_SESSAO = threading.Lock()

T = TypeVar("T")


def _log(tag: str, msg: str) -> None:
    print(f"[REST:{tag}] {msg}", file=sys.stderr, flush=True)


class SeiRestError(RuntimeError):
    pass


class SeiRest:
    """Sessão REST persistente; re-autentica sozinha quando o token expira e
    reassegura a unidade alvo antes de cada operação (ver docstring do módulo)."""

    def __init__(self, unidade_sigla: str | None = None) -> None:
        self._usuario = keyring.get_password(SERVICE, "usuario")
        self._senha = keyring.get_password(SERVICE, "senha")
        if not self._usuario or not self._senha:
            raise SeiRestError("credenciais ausentes no Keychain (service mcp-sei)")
        self._unidade_sigla = (
            unidade_sigla or keyring.get_password(SERVICE, "unidade") or UNIDADE_PADRAO
        )
        self._http = httpx.Client(base_url=BASE_URL, timeout=60)
        self._token: str | None = None
        self._id_unidade: str | None = None
        self._unidade_logada = False
        self._lock = _LOCK_SESSAO
        # contadores p/ diagnóstico (teste manual, logs)
        self.estatisticas = {"alteracoes_unidade": 0, "repeticoes_por_unidade": 0, "reautenticacoes": 0}

    @property
    def unidade_sigla(self) -> str:
        return self._unidade_sigla

    # -- sessão ---------------------------------------------------------
    def _autenticar(self) -> None:
        r = self._http.post(
            "/autenticar",
            data={
                "usuario": self._usuario,
                "senha": self._senha,
                "orgao": ORGAO_PGM,
                "contexto": "",
            },
        )
        r.raise_for_status()
        j = r.json()
        if not j.get("sucesso"):
            raise SeiRestError(f"falha no login REST: {j.get('mensagem')}")
        d = j["data"]
        self._token = d["token"]
        atual = (d.get("loginData") or {}).get("IdUnidadeAtual")
        _log("AUTH", f"token obtido (IdUnidadeAtual do usuário no login: {atual})")

    def _req(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        _retry: bool = True,
    ) -> dict[str, Any]:
        """Chamada crua: SEM lock e SEM garantia de unidade. Devolve o JSON inteiro.

        Reautentica uma vez se o token foi rejeitado (HTTP 401/403 ou
        "Acesso negado!") e, como o token novo nasce na unidade corrente do
        usuário, reaplica a unidade alvo antes de repetir.
        """
        if not self._token:
            self._autenticar()
        r = self._http.request(method, path, params=params, data=data, headers={"token": self._token})
        if r.status_code in (401, 403):
            if not _retry:
                r.raise_for_status()
            return self._reautenticar_e_repetir(method, path, params, data, f"HTTP {r.status_code}")
        r.raise_for_status()
        j = r.json()
        if j.get("sucesso"):
            return j
        msg = str(j.get("mensagem") or "")
        if _retry and "acesso negado" in msg.lower():
            return self._reautenticar_e_repetir(method, path, params, data, msg)
        raise SeiRestError(f"{path}: {msg}")

    def _reautenticar(self, path: str, motivo: str) -> None:
        """Token novo + unidade alvo reaplicada (o token nasce na unidade corrente do usuário)."""
        _log("AUTH", f"{motivo} em {path}; reautenticando")
        self.estatisticas["reautenticacoes"] += 1
        self._autenticar()
        if self._id_unidade and path != "/usuario/alterar/unidade":
            self._req("POST", "/usuario/alterar/unidade", data={"unidade": self._id_unidade}, _retry=False)
            self.estatisticas["alteracoes_unidade"] += 1

    def _reautenticar_e_repetir(
        self, method: str, path: str, params: dict | None, data: dict | None, motivo: str
    ) -> dict[str, Any]:
        self._reautenticar(path, motivo)
        return self._req(method, path, params=params, data=data, _retry=False)

    def _req_arquivo(self, path: str, *, _retry: bool = True) -> httpx.Response:
        """GET cru de endpoint que devolve ARQUIVO: SEM lock e SEM garantia de unidade.

        O wssei responde com o binário (Content-Type do arquivo) quando dá certo
        e com JSON quando não há arquivo: `sucesso=false` (erro) ou
        `sucesso=true` + `data.html` (documento interno). Erro vira
        SeiRestError; o resto volta como Response pra quem chamou decidir.
        """
        if not self._token:
            self._autenticar()
        r = self._http.request("GET", path, headers={"token": self._token}, timeout=300)
        if r.status_code in (401, 403):
            if not _retry:
                r.raise_for_status()
            self._reautenticar(path, f"HTTP {r.status_code}")
            return self._req_arquivo(path, _retry=False)
        r.raise_for_status()
        if "json" not in (r.headers.get("content-type") or "").lower():
            return r
        j = r.json()
        if j.get("sucesso"):
            return r
        msg = str(j.get("mensagem") or "")
        if _retry and "acesso negado" in msg.lower():
            self._reautenticar(path, msg)
            return self._req_arquivo(path, _retry=False)
        raise SeiRestError(f"{path}: {msg}")

    # -- unidade --------------------------------------------------------
    def _resolver_id_unidade(self) -> str:
        """Id da sigla alvo, resolvido 1x por processo via /usuario/unidades."""
        if self._id_unidade:
            return self._id_unidade
        with _cache_lock:
            idu = _ID_UNIDADE_POR_SIGLA.get(self._unidade_sigla)
        if idu is None:
            unidades = self._req("GET", "/usuario/unidades").get("data") or []
            alvo = next(
                (u for u in unidades if (u.get("sigla") or "").strip() == self._unidade_sigla),
                None,
            )
            if alvo is None:
                raise SeiRestError(
                    f"unidade {self._unidade_sigla!r} não encontrada entre as "
                    f"{len(unidades)} unidades do usuário"
                )
            idu = str(alvo["id"])
            with _cache_lock:
                _ID_UNIDADE_POR_SIGLA[self._unidade_sigla] = idu
            _log("UNIDADE", f"{self._unidade_sigla} = id {idu} ({len(unidades)} unidades listadas)")
        self._id_unidade = idu
        return idu

    def _assegurar_unidade(self) -> None:
        """Reaplica a unidade alvo no servidor. Chamar COM o lock, logo antes da operação.

        Não há como pular: outro token do mesmo usuário pode ter trocado a
        unidade entre duas chamadas (ver docstring do módulo).
        """
        idu = self._resolver_id_unidade()
        self._req("POST", "/usuario/alterar/unidade", data={"unidade": idu})
        self.estatisticas["alteracoes_unidade"] += 1
        if not self._unidade_logada:
            _log("UNIDADE", f"ativa: {self._unidade_sigla} ({idu}); reassegurada antes de cada chamada")
            self._unidade_logada = True

    def _protegido(self, fn: Callable[[], T]) -> T:
        """lock → unidade → fn(). Se o servidor acusar unidade errada, reassegura e repete 1x.

        `fn` deve usar só `self._req` (nunca `get`/`post`, que pegariam o lock de novo).
        """
        with self._lock:
            self._assegurar_unidade()
            try:
                return fn()
            except SeiRestError as e:
                if not _RE_ERRO_UNIDADE.search(str(e)):
                    raise
                _log("UNIDADE", f"{e} — reassegurando {self._unidade_sigla} e repetindo 1x")
                self.estatisticas["repeticoes_por_unidade"] += 1
                self._assegurar_unidade()
                return fn()

    # -- chamadas protegidas (uso externo: sei_docs.py) ------------------
    def get(self, path: str, params: dict | None = None) -> Any:
        """GET com lock + unidade reassegurada. Devolve `data`."""
        return self._protegido(lambda: self._req("GET", path, params=params).get("data"))

    def post(self, path: str, data: dict) -> dict[str, Any]:
        """POST com lock + unidade reassegurada. Devolve o JSON inteiro."""
        return self._protegido(lambda: self._req("POST", path, data=data))

    # -- compatibilidade com scripts anteriores a 02/10/2026 --------------
    # Nomes antigos (`_garantir_unidade`, `_get`, `_post`) continuam existindo,
    # mas agora caem no caminho protegido. `r._unidade_ok = False` em scripts
    # velhos vira atributo inerte — não precisa mais.
    def _garantir_unidade(self) -> None:
        with self._lock:
            self._assegurar_unidade()

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self.get(path, params)

    def _post(self, path: str, data: dict) -> dict[str, Any]:
        return self.post(path, data)

    # -- consultas ------------------------------------------------------
    def versao(self) -> dict[str, Any]:
        return self._req("GET", "/versao").get("data") or {}

    def consultar_processo(self, numero: str) -> dict[str, Any]:
        """Número NUP → {IdProcedimento, ProtocoloProcedimentoFormatado, NomeTipoProcedimento}."""
        try:
            data = self.get("/processo/consultar", {"protocoloFormatado": numero})
        except SeiRestError as e:
            raise SeiRestError(f"processo {numero} na unidade {self._unidade_sigla}: {e}") from e
        if not data or not data.get("IdProcedimento"):
            raise SeiRestError(
                f"processo {numero} não encontrado via REST (unidade {self._unidade_sigla})"
            )
        return data

    def listar_documentos(self, id_procedimento: str) -> list[dict[str, Any]]:
        """Todos os documentos do processo (pagina até esgotar; `start` = nº da página)."""

        def _paginar() -> list[dict[str, Any]]:
            docs: list[dict[str, Any]] = []
            for pagina in range(25):
                data = self._req(
                    "GET",
                    f"/documento/listar/{id_procedimento}",
                    params={"limit": 200, "start": pagina},
                ).get("data")
                lote = data if isinstance(data, list) else (data or {}).get("documentos", [])
                if not lote:
                    break
                docs.extend(lote)
                if len(lote) < 200:
                    break
            return docs

        return self._protegido(_paginar)

    def listar_assinaturas(self, id_documento: str) -> list[dict[str, Any]]:
        data = self.get(f"/documento/listar/assinaturas/{id_documento}")
        return data if isinstance(data, list) else []

    def baixar_conteudo_documento(self, id_documento: str) -> dict[str, Any]:
        """Conteúdo de UM documento (id interno), sem baixar o processo.

        Externo (PDF, DOCX, imagem): `{"conteudo": bytes, "content_type",
        "nome_arquivo"}` com o arquivo original. Interno ou formulário
        (Despacho, E-mail, Recibo): `{"html": str}` com a página renderizada,
        ainda com entidades (`&lt;p&gt;`) — quem chama faz `html.unescape`.
        Validado em 05/10/2026: bytes idênticos (sha256) aos do ZIP do processo.
        """

        def _baixar() -> dict[str, Any]:
            r = self._req_arquivo(f"/documento/baixar/anexo/{id_documento}")
            tipo = (r.headers.get("content-type") or "").split(";")[0].strip()
            if "json" in tipo.lower():
                pagina = (r.json().get("data") or {}).get("html")
                if not isinstance(pagina, str):
                    raise SeiRestError(f"documento {id_documento}: resposta sem arquivo e sem html")
                return {"html": pagina}
            m = re.search(r'filename="?([^";]+)"?', r.headers.get("content-disposition") or "")
            return {
                "conteudo": r.content,
                "content_type": tipo or None,
                "nome_arquivo": m.group(1).strip() if m else None,
            }

        return self._protegido(_baixar)

    def verificar_pareceres(self, numero: str) -> list[dict[str, Any]]:
        """Pareceres do processo com assinaturas, sem baixar nada.

        Cobre documentos internos (tipoDocumento=I) e externos (X). Externos
        são PDFs anexados: a API não lista assinatura pra eles, então saem
        como `assinado=False` com `origem="externo"`.
        """
        t0 = time.time()
        proc = self.consultar_processo(numero)
        docs = self.listar_documentos(proc["IdProcedimento"])
        out: list[dict[str, Any]] = []
        for d in docs:
            a = d.get("atributos") or {}
            tipo = (a.get("tipo") or "").strip()
            if not tipo.lower().startswith("parecer"):
                continue
            externo = (a.get("tipoDocumento") or "").upper() == "X"
            assinaturas = [] if externo else self.listar_assinaturas(d["id"])
            primeira = assinaturas[0] if assinaturas else {}
            data_hora = (primeira.get("dataHora") or "").split(" ")
            out.append(
                {
                    "titulo_arvore": f"{tipo} {a.get('protocoloFormatado') or ''}".strip(),
                    "id_documento": a.get("protocoloFormatado"),
                    "id_interno": d.get("id"),
                    "origem": "externo" if externo else "interno",
                    "unidade_sigla": primeira.get("unidade"),
                    "unidade_descricao": None,
                    "assinado": bool(assinaturas),
                    "assinante_nome": primeira.get("nome"),
                    "assinante_cargo": primeira.get("cargo"),
                    "data_assinatura": data_hora[0] or None,
                    "hora_assinatura": data_hora[1] if len(data_hora) > 1 else None,
                    "n_assinaturas": len(assinaturas),
                }
            )
        _log("PARECER", f"{numero}: {len(out)} pareceres em {time.time() - t0:.1f}s")
        return out

    def close(self) -> None:
        self._http.close()


_singleton: SeiRest | None = None
_singleton_lock = threading.Lock()


def get_rest() -> SeiRest:
    """Instância única por processo do servidor (sessão e token reaproveitados)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = SeiRest()
        return _singleton
