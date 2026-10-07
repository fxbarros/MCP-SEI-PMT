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
import unicodedata
from pathlib import Path
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
    # lock + unidade reassegurada a cada chamada ficam dentro do SeiRest
    # (a unidade ativa é do usuário no servidor; ver docstring de sei_rest.py)
    return rest.get(path, params)


def _post(rest: SeiRest, path: str, data: dict) -> Any:
    return rest.post(path, data)


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


# ---------------------------------------------------------------------------
# Árvore do processo e download de UM documento (só leitura, 05/10/2026)
#
# Motivo: para ler um único anexo não se baixa o processo inteiro. A árvore
# vem de /documento/listar e o conteúdo de /documento/baixar/anexo/{id}, que
# serve tanto externo (binário) quanto interno/formulário (HTML renderizado).
# ---------------------------------------------------------------------------

# tipoDocumento do wssei: X = externo (arquivo anexado), I = interno (editor),
# A = formulário gerado pelo SEI (E-mail, Recibo Eletrônico de Protocolo)
_ORIGEM = {"X": "externo", "I": "interno", "A": "formulario"}


def arvore_processo(rest: SeiRest, numero: str) -> list[dict[str, Any]]:
    """Documentos do processo na ordem da árvore, sem baixar nada."""
    proc = rest.consultar_processo(numero)
    saida: list[dict[str, Any]] = []
    for ordem, d in enumerate(rest.listar_documentos(proc["IdProcedimento"]), 1):
        a = d.get("atributos") or {}
        st = a.get("status") or {}
        tamanho = str(a.get("tamanho") or "")
        tipo_doc = (a.get("tipoDocumento") or "").upper()
        saida.append(
            {
                "ordem": ordem,
                "id_documento": str(a.get("protocoloFormatado") or ""),
                "id_interno": str(d.get("id") or ""),
                "titulo": a.get("nomeComposto") or "",
                "tipo": (a.get("tipo") or "").strip(),
                "descricao": a.get("informacao") or "",
                "origem": _ORIGEM.get(tipo_doc, tipo_doc),
                "formato": a.get("mimeType") or "",
                "arquivo": a.get("nome") or "",
                "tamanho_bytes": int(tamanho) if tamanho.isdigit() else None,
                "unidade": a.get("siglaUnidade") or "",
                "assinado_no_sei": st.get("documentoAssinado") == "S",
                "restrito": st.get("documentoRestrito") == "S",
                "cancelado": st.get("documentoCancelado") == "S",
            }
        )
    return saida


def sem_acento(texto: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(ch)
    )


def filtrar_arvore(arvore: list[dict[str, Any]], filtro: str) -> list[dict[str, Any]]:
    """Mantém os documentos cujo título, descrição, arquivo ou nº SEI contém `filtro`."""
    alvo = sem_acento(filtro.strip())
    if not alvo:
        return arvore
    return [
        d
        for d in arvore
        if alvo in sem_acento(" ".join((d["titulo"], d["descricao"], d["arquivo"], d["id_documento"])))
    ]


def pagina_para_texto(h: str) -> str:
    """HTML de página inteira (documento renderizado) → texto, sem <head>, CSS e scripts."""
    t = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", "", h)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</t[dh]\s*>", "\t", t)
    t = re.sub(r"(?i)</p\s*>", "\n\n", t)
    t = re.sub(r"(?i)</(div|tr|li|h[1-6]|table)\s*>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    linhas = [re.sub(r"[ \t]+", " ", linha).strip() for linha in t.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


def _nome_seguro(nome: str) -> str:
    nome = unicodedata.normalize("NFC", nome)
    nome = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", nome).strip(" .")
    return nome[:150] or "documento"


def _pagina_utf8(pagina: str) -> str:
    """O SEI declara iso-8859-1; o arquivo local é gravado em UTF-8."""
    pagina, n = re.subn(r"""(?i)charset=(["']?)iso-8859-1""", r"charset=\1utf-8", pagina)
    if not n and "charset=" not in pagina[:3000].lower():
        pagina = re.sub(r"(?i)(<head[^>]*>)", r'\1<meta charset="utf-8">', pagina, count=1)
    return pagina


def baixar_documento(
    rest: SeiRest, numero: str, id_documento: str, pasta_processo: Path, forcar: bool = False
) -> dict[str, Any]:
    """Baixa UM documento (nº SEI visível ou id interno) para `pasta_processo/documentos/`.

    Externo: arquivo original, reaproveitado do disco se já estiver lá com o
    mesmo tamanho (a não ser com `forcar`). Interno/formulário: HTML
    renderizado, sempre rebaixado (minuta muda a cada edição).
    """
    s = str(id_documento).strip()
    doc = next(
        (d for d in arvore_processo(rest, numero) if s in (d["id_documento"], d["id_interno"])),
        None,
    )
    if doc is None:
        raise SeiRestError(f"documento {s} não está no processo {numero}")
    pasta = pasta_processo / "documentos"
    prefixo = doc["id_documento"] or doc["id_interno"]
    do_cache = False

    if doc["origem"] == "externo":
        destino = None
        if doc["arquivo"]:
            destino = pasta / _nome_seguro(f"{prefixo}_{doc['arquivo']}")
            do_cache = (
                not forcar
                and destino.exists()
                and doc["tamanho_bytes"] is not None
                and destino.stat().st_size == doc["tamanho_bytes"]
            )
        if not do_cache:
            r = rest.baixar_conteudo_documento(doc["id_interno"])
            if "conteudo" not in r:
                raise SeiRestError(f"documento {s}: o SEI não devolveu o arquivo do anexo")
            if destino is None:
                nome = r.get("nome_arquivo") or f"{doc['tipo'] or 'documento'}.{doc['formato'] or 'bin'}"
                destino = pasta / _nome_seguro(f"{prefixo}_{nome}")
            pasta.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(r["conteudo"])
    else:
        r = rest.baixar_conteudo_documento(doc["id_interno"])
        if "html" not in r:
            raise SeiRestError(f"documento {s}: o SEI não devolveu o HTML do documento")
        base = re.sub(rf"\s*\(?{re.escape(prefixo)}\)?\s*$", "", doc["titulo"]).strip() or doc["tipo"]
        destino = pasta / _nome_seguro(f"{prefixo}_{base}.html")
        pasta.mkdir(parents=True, exist_ok=True)
        destino.write_text(_pagina_utf8(html.unescape(r["html"])), encoding="utf-8")

    return {
        **doc,
        "numero": numero,
        "arquivo_local": str(destino),
        "tamanho_bytes": destino.stat().st_size,
        "do_cache": do_cache,
    }
