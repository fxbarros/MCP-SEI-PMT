"""Criação, leitura, edição e formatação de documentos internos do SEI via REST.

Toda ESCRITA passa por trava de confirmação no server.py (`confirmar=True`):
sem ela a tool devolve apenas a prévia e nada é gravado. Não existe aqui
função de assinar, tramitar, concluir ou excluir — decisão do projeto.

Mapa (26/09/2026, SEI-PMT 5.0.4 / wssei 3.0.4):
  POST /documento/{id_proc}/interno/criar     → {idDocumento, protocoloDocumentoFormatado}
  GET  /documento/secao/listar?id=            → {secoes:[{id,idSecaoModelo,conteudo,somenteLeitura}], ultimaVersaoDocumento}
  POST /documento/secao/alterar               → form documento, secoes(JSON de TODAS as seções), versao
  GET  /documento/{id}/interno/visualizar     → HTML renderizado
  GET  /documento/tipo/pesquisar?filter=      → [{id, nome}]

Seções de um Parecer Jurídico da PRFMAP: 300 timbre (só leitura), 176 cabeçalho
(título/processo/consulente/assunto), 177 corpo, 301 referência (só leitura),
392 rodapé (endereço). O SEI exige TODAS as seções no POST; as de só leitura
vão vazias (o SEI as reconstrói).
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .sei_rest import SeiRest, SeiRestError

# Tipos de documento (idSerie) mais usados pela PRFMAP — de /documento/tipo/pesquisar
TIPOS_DOCUMENTO = {
    "parecer jurídico": "806",
    "parecer juridico": "806",
    "parecer": "452",
    "parecer técnico": "805",
    "ofício": "290",
    "oficio": "290",
    "ofício-circular": "292",
    "despacho": "291",
    "despacho decisório": "1344",
    "memorando": "816",
    "memorando-circular": "692",
    "certidão": "344",
    "certidao": "344",
    "nota técnica": "300",
    "nota tecnica": "300",
}

# Estilos do SEI que valem a pena expor (os que o Fábio usa + os de estrutura)
ESTILOS_SEI = {
    "Texto_Justificado_Recuo_Primeira_Linha": "parágrafo comum do corpo (recuo 1ª linha) — default da PRFMAP",
    "Texto_Justificado": "parágrafo justificado sem recuo — títulos de tópico (I. RELATÓRIO) e linhas em branco",
    "Texto_Ementa": "ementa (recuada à direita)",
    "Citacao": "transcrição de lei/doutrina/jurisprudência (recuo 4 cm, fonte menor)",
    "Texto_Centralizado": "centralizado — bloco de assinatura",
    "Texto_Centralizado_Maiusculas": "centralizado em maiúsculas — título do documento (cabeçalho)",
    "Texto_Alinhado_Esquerda": "à esquerda — destinatário de ofício/despacho",
    "Texto_Alinhado_Direita": "à direita — local e data",
    "Paragrafo_Numerado_Nivel1": "parágrafo autonumerado 1., 2., 3. (numeração feita pelo SEI)",
    "Item_Alinea_Letra": "alínea autonumerada a), b), c)",
    "Item_Inciso_Romano": "inciso autonumerado I, II, III",
    "Tabela_Texto_Justificado": "texto dentro de célula de tabela",
}

_RE_ID_SEI = re.compile(r"\b(?:Ids?\.?|SEI\s+n[ºo°]?)\s*(\d{7,9})\b", re.IGNORECASE)
# Tópico = romano com ponto ("I. RELATÓRIO"), numérico com traço ("2.1 - Da
# competência") ou linha toda em maiúsculas. "12. Texto" é parágrafo numerado.
_RE_TOPICO = re.compile(r"^(?:[IVX]+\.\s+\S|\d+(?:\.\d+)*\s*[-–—]\s+\S)")
_RE_NUM_PARAG = re.compile(r"^(\d{1,3})\.\s+(.*)$", re.DOTALL)


def sanitize_iso8859(text: str) -> str:
    """Caracteres fora do ISO-8859-1 viram entidades numéricas (o wssei faz iconv)."""
    out = []
    for ch in text:
        try:
            ch.encode("iso-8859-1")
            out.append(ch)
        except UnicodeEncodeError:
            out.append(f"&#{ord(ch)};")
    return "".join(out)


def _inline(texto: str) -> str:
    """Escapa HTML e aplica **negrito** / *itálico*."""
    t = html.escape(texto, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", t)
    return t


def _linkar_ids(html_par: str, mapa: dict[str, str]) -> str:
    """'Id. 15559225' → âncora sei! (link interno) quando o doc está no processo."""
    if not mapa:
        return html_par

    def sub(m: re.Match) -> str:
        num = m.group(1)
        idi = mapa.get(num)
        if not idi:
            return m.group(0)
        prefixo = m.group(0)[: m.start(1) - m.start(0)]
        return (
            f'{prefixo}<span contenteditable="false" style="text-indent:0;">'
            f'<a class="ancoraSei" id="lnkSei{idi}" style="text-indent:0;">{num}</a></span>'
        )

    return _RE_ID_SEI.sub(sub, html_par)


def formatar_html_sei(texto: str, mapa_ids: dict[str, str] | None = None) -> str:
    """Converte texto simples (com marcação leve) no HTML que o SEI espera.

    Regras, por parágrafo (separados por linha em branco):
      - começa com "> "            → Citacao (transcrição)
      - começa com "EMENTA:"       → Texto_Ementa
      - "I. RELATÓRIO", "2.1 - X"  → tópico: Texto_Justificado em negrito
      - "12. Texto..."             → parágrafo numerado manualmente (número em negrito,
                                     como nos pareceres da PRFMAP)
      - começa com "|c| "          → Texto_Centralizado (assinatura)
      - começa com "|d| "          → Texto_Alinhado_Direita (local e data)
      - começa com "|e| "          → Texto_Alinhado_Esquerda (destinatário)
      - já é HTML ("<p", "<table") → passa intacto
      - demais                     → Texto_Justificado_Recuo_Primeira_Linha
    Inline: **negrito**, *itálico*, quebra de linha simples vira <br />.
    "Id. NNNNNNN" vira link sei! quando `mapa_ids` (nº SEI → id interno) o conhece.
    """
    mapa_ids = mapa_ids or {}
    blocos = re.split(r"\n\s*\n", texto.strip().replace("\r\n", "\n"))
    saida: list[str] = []
    for bloco in blocos:
        b = bloco.strip()
        if not b:
            continue
        if b.startswith(("<p", "<table", "<div", "<ul", "<ol", "<hr")):
            saida.append(b)
            continue
        cls = "Texto_Justificado_Recuo_Primeira_Linha"
        corpo = b
        if b.startswith("> "):
            cls = "Citacao"
            corpo = "\n".join(l[2:] if l.startswith("> ") else l for l in b.split("\n"))
        elif b.upper().startswith("EMENTA:"):
            cls = "Texto_Ementa"
            corpo = "**EMENTA:**" + b[7:]
        elif b.startswith("|c| "):
            cls, corpo = "Texto_Centralizado", b[4:]
        elif b.startswith("|d| "):
            cls, corpo = "Texto_Alinhado_Direita", b[4:]
        elif b.startswith("|e| "):
            cls, corpo = "Texto_Alinhado_Esquerda", b[4:]
        elif "\n" not in b and len(b) < 120 and (
            _RE_TOPICO.match(b) or (b.upper() == b and any(ch.isalpha() for ch in b))
        ):
            cls = "Texto_Justificado"
            corpo = f"**{b}**" if not b.startswith("**") else b
        else:
            m = _RE_NUM_PARAG.match(b)
            if m:
                corpo = f"**{m.group(1)}.** {m.group(2)}"
        inner = _inline(corpo).replace("\n", "<br />")
        inner = _linkar_ids(inner, mapa_ids)
        saida.append(f'<p class="{cls}">{inner}</p>')
    return "\n".join(saida)


# ---------------------------------------------------------------------------
# Operações REST (o SeiRest só tem leitura; escrita fica concentrada aqui)
# ---------------------------------------------------------------------------

def _get(rest: SeiRest, path: str, params: dict | None = None) -> Any:
    with rest._lock:
        rest._garantir_unidade()
        return rest._get(path, params)


def _post(rest: SeiRest, path: str, data: dict) -> Any:
    with rest._lock:
        rest._garantir_unidade()
        return rest._post(path, data)


def resolver_tipo(rest: SeiRest, tipo: str) -> tuple[str, str]:
    """Nome do tipo → (idSerie, nome oficial). Tabela local primeiro, depois a API."""
    chave = tipo.strip().lower()
    if chave in TIPOS_DOCUMENTO:
        return TIPOS_DOCUMENTO[chave], tipo.strip()
    if chave.isdigit():
        return chave, tipo
    data = _get(rest, "/documento/tipo/pesquisar", {"filter": tipo, "limit": 50, "start": 0}) or []
    exato = next((t for t in data if (t.get("nome") or "").strip().lower() == chave), None)
    if exato:
        return str(exato["id"]), exato["nome"]
    if len(data) == 1:
        return str(data[0]["id"]), data[0]["nome"]
    nomes = [(t.get("id"), t.get("nome")) for t in data[:10]]
    raise SeiRestError(f"tipo {tipo!r} ambíguo ou inexistente; candidatos: {nomes}")


def listar_tipos(rest: SeiRest, filtro: str) -> list[dict[str, str]]:
    data = _get(rest, "/documento/tipo/pesquisar", {"filter": filtro, "limit": 100, "start": 0}) or []
    return [{"id": str(t.get("id")), "nome": t.get("nome")} for t in data]


def mapa_ids_processo(rest: SeiRest, numero: str) -> dict[str, str]:
    """nº SEI visível (protocoloFormatado) → id interno, para os links sei!."""
    proc = rest.consultar_processo(numero)
    docs = rest.listar_documentos(proc["IdProcedimento"])
    return {
        str((d.get("atributos") or {}).get("protocoloFormatado")): str(d["id"])
        for d in docs
        if (d.get("atributos") or {}).get("protocoloFormatado")
    }


def resolver_documento(rest: SeiRest, id_documento: str, numero: str | None = None) -> str:
    """Aceita id interno (8 dígitos, ≥17M) ou nº SEI visível; devolve id interno."""
    s = str(id_documento).strip()
    if numero:
        mapa = mapa_ids_processo(rest, numero)
        if s in mapa:
            return mapa[s]
        if s in mapa.values():
            return s
        raise SeiRestError(f"documento {s} não está no processo {numero}")
    return s


def criar_documento(
    rest: SeiRest, numero: str, tipo: str, descricao: str = "", nivel_acesso: str = "0"
) -> dict[str, Any]:
    proc = rest.consultar_processo(numero)
    id_serie, nome_tipo = resolver_tipo(rest, tipo)
    j = _post(
        rest,
        f"/documento/{proc['IdProcedimento']}/interno/criar",
        {
            "idSerie": id_serie,
            "numero": "",
            "descricao": descricao,
            "dataElaboracao": "",
            "nivelAcesso": nivel_acesso,
            "idHipoteseLegal": "",
            "grauSigilo": "",
            "idUnidadeGeradoraProtocolo": "",
            "assuntos": "",
            "interessados": "",
            "remetente": "",
            "destinatarios": "",
            "observacao": "",
            "idTextoPadraoInterno": "",
            "idTipoConferencia": "",
            "protocoloDocumentoModelo": "",
        },
    )
    d = j.get("data") or {}
    return {
        "numero_processo": numero,
        "id_procedimento": proc["IdProcedimento"],
        "tipo": nome_tipo,
        "id_serie": id_serie,
        "id_documento": str(d.get("idDocumento") or d.get("IdDocumento") or ""),
        "numero_sei": str(d.get("protocoloDocumentoFormatado") or d.get("ProtocoloDocumentoFormatado") or ""),
        "mensagem": j.get("mensagem"),
    }


_RE_IMG_B64 = re.compile(r'src="data:image/[^"]{40,}"')


def listar_secoes(rest: SeiRest, id_documento: str) -> dict[str, Any]:
    data = _get(rest, "/documento/secao/listar", {"id": id_documento}) or {}
    secoes = []
    editaveis = []
    for s in data.get("secoes", []):
        conteudo = html.unescape(s.get("conteudo") or "")
        ro = str(s.get("somenteLeitura") or "").upper() == "S"
        item = {
            "id": str(s.get("id")),
            "idSecaoModelo": str(s.get("idSecaoModelo")),
            "somente_leitura": ro,
            "tamanho": len(conteudo),
            "html": _RE_IMG_B64.sub('src="data:image/...(omitido)"', conteudo),
        }
        secoes.append(item)
        if not ro:
            editaveis.append(item["idSecaoModelo"])
    # convenção PRFMAP: 1ª editável = cabeçalho, 2ª = corpo, última = rodapé
    papeis = {}
    if len(editaveis) >= 2:
        papeis = {"cabecalho": editaveis[0], "corpo": editaveis[1]}
        if len(editaveis) >= 3:
            papeis["rodape"] = editaveis[-1]
    elif editaveis:
        papeis = {"corpo": editaveis[0]}
    return {
        "id_documento": id_documento,
        "versao": str(data.get("ultimaVersaoDocumento") or "1"),
        "papeis": papeis,
        "secoes": secoes,
    }


def visualizar(rest: SeiRest, id_documento: str) -> str:
    data = _get(rest, f"/documento/{id_documento}/interno/visualizar")
    return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)


def html_para_texto(h: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", h)
    t = re.sub(r"</p\s*>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(t)).strip()


def montar_payload_secoes(
    info: dict[str, Any], alteracoes: dict[str, str]
) -> list[dict[str, str]]:
    """TODAS as seções (exigência do SEI): só leitura vazia, alteradas novas, demais originais."""
    payload = []
    for s in info["secoes"]:
        modelo = s["idSecaoModelo"]
        if s["somente_leitura"]:
            conteudo = ""
        elif modelo in alteracoes:
            conteudo = alteracoes[modelo]
        else:
            conteudo = s["html"]
        payload.append({"id": s["id"], "idSecaoModelo": modelo, "conteudo": sanitize_iso8859(conteudo)})
    return payload


def gravar_secoes(rest: SeiRest, id_documento: str, payload: list[dict[str, str]], versao: str) -> dict[str, Any]:
    j = _post(
        rest,
        "/documento/secao/alterar",
        {"documento": id_documento, "secoes": json.dumps(payload, ensure_ascii=False), "versao": versao},
    )
    return {"mensagem": j.get("mensagem"), "data": j.get("data")}
