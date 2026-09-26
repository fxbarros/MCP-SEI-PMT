"""Cliente REST do SEI-PMT via módulo mod-wssei v2 (SEI 5.0.4 / wssei 3.0.4).

Caminho SÓ DE LEITURA e sem browser: autentica com usuário/senha/órgão do
Keychain (service `mcp-sei`) e consulta processos, documentos e assinaturas
direto na API oficial. Não depende do layout das telas do SEI, que já
quebrou o scraper três vezes em 2026.

Descoberto em 26/09/2026: endpoints públicos `/orgao/listar` (PGM = id 6);
`/autenticar` com form usuario/senha/orgao; demais chamadas levam header
`token`. Assinaturas vêm em `/documento/listar/assinaturas/{id_documento}`
com nome, cargo, unidade e dataHora.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

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


def _log(tag: str, msg: str) -> None:
    print(f"[REST:{tag}] {msg}", file=sys.stderr, flush=True)


class SeiRestError(RuntimeError):
    pass


class SeiRest:
    """Sessão REST persistente; re-autentica sozinha quando o token expira."""

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
        self._unidade_ok = False
        self._lock = threading.Lock()

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
        self._token = j["data"]["token"]
        self._unidade_ok = False
        _log("AUTH", "token obtido")

    def _get(self, path: str, params: dict | None = None, _retry: bool = True) -> Any:
        if not self._token:
            self._autenticar()
        r = self._http.get(path, params=params, headers={"token": self._token})
        if r.status_code in (401, 403) and _retry:
            _log("AUTH", f"HTTP {r.status_code} em {path}; reautenticando")
            self._autenticar()
            return self._get(path, params, _retry=False)
        r.raise_for_status()
        j = r.json()
        if not j.get("sucesso"):
            msg = str(j.get("mensagem") or "")
            if _retry and "acesso negado" in msg.lower():
                self._autenticar()
                return self._get(path, params, _retry=False)
            raise SeiRestError(f"{path}: {msg}")
        return j.get("data")

    def _post(self, path: str, data: dict) -> Any:
        if not self._token:
            self._autenticar()
        r = self._http.post(path, data=data, headers={"token": self._token})
        r.raise_for_status()
        j = r.json()
        if not j.get("sucesso"):
            raise SeiRestError(f"{path}: {j.get('mensagem')}")
        return j

    def _garantir_unidade(self) -> None:
        """Troca a unidade ativa do token pra sigla alvo (só 1x por token)."""
        if self._unidade_ok:
            return
        unidades = self._get("/usuario/unidades") or []
        alvo = next(
            (u for u in unidades if (u.get("sigla") or "").strip() == self._unidade_sigla),
            None,
        )
        if alvo is None:
            raise SeiRestError(
                f"unidade {self._unidade_sigla!r} não encontrada entre as do usuário"
            )
        self._post("/usuario/alterar/unidade", {"unidade": alvo["id"]})
        self._unidade_ok = True
        _log("UNIDADE", f"ativa: {self._unidade_sigla} ({alvo['id']})")

    # -- consultas ------------------------------------------------------
    def versao(self) -> dict[str, Any]:
        return self._get("/versao") or {}

    def consultar_processo(self, numero: str) -> dict[str, Any]:
        """Número NUP → {IdProcedimento, ProtocoloProcedimentoFormatado, NomeTipoProcedimento}."""
        with self._lock:
            self._garantir_unidade()
            data = self._get("/processo/consultar", {"protocoloFormatado": numero})
        if not data or not data.get("IdProcedimento"):
            raise SeiRestError(f"processo {numero} não encontrado via REST")
        return data

    def listar_documentos(self, id_procedimento: str) -> list[dict[str, Any]]:
        """Todos os documentos do processo (pagina até esgotar; `start` = nº da página)."""
        docs: list[dict[str, Any]] = []
        with self._lock:
            self._garantir_unidade()
            for pagina in range(25):
                data = self._get(
                    f"/documento/listar/{id_procedimento}",
                    {"limit": 200, "start": pagina},
                )
                lote = data if isinstance(data, list) else (data or {}).get("documentos", [])
                if not lote:
                    break
                docs.extend(lote)
                if len(lote) < 200:
                    break
        return docs

    def listar_assinaturas(self, id_documento: str) -> list[dict[str, Any]]:
        with self._lock:
            data = self._get(f"/documento/listar/assinaturas/{id_documento}")
        return data if isinstance(data, list) else []

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
