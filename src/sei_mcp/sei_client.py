"""Cliente SEI-PMT — automação do navegador via patchright + Chrome real.

Responsabilidades atuais:
  - login() validado contra SIP/SEI da Prefeitura de Teresina

Use via context manager:

    with SeiClient() as c:
        c.login()
        # ... próximas operações
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

import keyring
from patchright.sync_api import (
    BrowserContext,
    Frame,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)


@dataclass(frozen=True, slots=True)
class ProcessoPendente:
    id_procedimento: str
    numero: str
    tipo: str
    especificacao: str | None
    atribuido_para: str | None
    sigla_responsavel: str | None
    tem_anotacao: bool
    anotacao_completa: str | None
    url_processo: str
    origem: Literal["recebidos", "gerados"]


_RE_TIPO_ESPEC = re.compile(
    r"^Tipo\s+(?P<tipo>.+?)(?:\s*/\s*Especificação\s+(?P<espec>.+))?$"
)
_RE_ATRIBUIDO = re.compile(r"^Atribuído para\s+(?P<nome>.+?)\s*$")

SERVICE = "mcp-sei"
PROFILE_DIR = Path.home() / ".mcp-sei-chrome-profile"
SEI_URL = "https://sei.teresina.pi.gov.br/sei/"
SIP_HOST = "sip.teresina.pi.gov.br"
SEI_HOST = "sei.teresina.pi.gov.br"

# iCloud Drive → Processos SEI (visível como pasta no Finder, sincronizada)
DESTINO_DOWNLOAD = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "Processos SEI"
)


def _log(tag: str, msg: str) -> None:
    # Vai pra stderr — stdout é reservado pro protocolo JSON-RPC do MCP.
    print(f"[{tag}] {msg}", flush=True, file=sys.stderr)


def _carregar_credenciais() -> tuple[str, str, str, str]:
    _log("SETUP", f"lendo credenciais do Keychain (service={SERVICE})")
    usuario = keyring.get_password(SERVICE, "usuario")
    senha = keyring.get_password(SERVICE, "senha")
    orgao = keyring.get_password(SERVICE, "orgao") or "PGM"
    unidade = keyring.get_password(SERVICE, "unidade") or "PROC-PATR-PGM"
    if not usuario or not senha:
        print(
            "ERRO: credenciais ausentes. Rode "
            "setup_credenciais.py do projeto sei-mcp.",
            file=sys.stderr,
        )
        sys.exit(1)
    _log("SETUP", f"usuário={usuario}  órgão={orgao}  unidade alvo={unidade}")
    return usuario, senha, orgao, unidade


def _garantir_perfil_livre() -> None:
    """Falha cedo, com mensagem clara, se outro Chrome já usa o perfil.

    O Chrome cria PROFILE_DIR/SingletonLock como symlink "host-pid". Se o pid
    ainda existe, outro processo (ex.: o sei-mcp de outra janela do Claude)
    está com o browser aberto — lançar de novo aborta com erro críptico de
    ProcessSingleton. Se o pid morreu, o lock é órfão e pode ser removido.
    """
    lock = PROFILE_DIR / "SingletonLock"
    if not lock.is_symlink():
        return
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[-1])
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        lock.unlink(missing_ok=True)
        _log("BROWSER", f"SingletonLock órfão removido (pid {pid} já morreu)")
        return
    except PermissionError:
        pass
    raise RuntimeError(
        f"Outro Chrome (pid {pid}) já está usando o perfil {PROFILE_DIR} — "
        "provavelmente o sei-mcp de outra janela (Claude Desktop e Claude Code "
        "abertos ao mesmo tempo). Feche a outra instância ou aguarde ela "
        "liberar o browser."
    )


def _esta_no_sip(url: str) -> bool:
    return SIP_HOST in url


def _esta_no_sei(url: str) -> bool:
    return SEI_HOST in url and SIP_HOST not in url


class SeiClient:
    """Cliente persistente para o SEI-PMT.

    Headed por padrão (Cloudflare/SIP detectam headless mais facilmente, e o
    debug fica muito mais fácil com a janela visível). Sessão persistida em
    PROFILE_DIR para reaproveitar cookies entre runs.
    """

    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> Self:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _garantir_perfil_livre()
        _log(
            "BROWSER",
            f"abrindo Chrome real (channel=chrome) "
            f"{'headless' if self.headless else 'headed'}, perfil={PROFILE_DIR}",
        )
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=self.headless,
            no_viewport=True,
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._ctx is not None:
            self._ctx.close()
        if self._pw is not None:
            self._pw.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("SeiClient não inicializado — use com `with`.")
        return self._page

    def login(self) -> str:
        """Garante que estamos logados no SEI E na unidade alvo.

        Retorna a unidade ativa após eventual troca.
        """
        usuario, senha, orgao, unidade_alvo = _carregar_credenciais()
        page = self.page

        _log("NAV", f"indo para {SEI_URL}")
        page.goto(SEI_URL, wait_until="domcontentloaded")
        time.sleep(1.5)

        if _esta_no_sip(page.url):
            self._submeter_login_sip(usuario, senha, orgao)
        else:
            _log("NAV", f"sessão já válida — url={page.url}")

        try:
            page.wait_for_url(lambda u: _esta_no_sei(u), timeout=20_000)
        except PWTimeout:
            _log("LOGIN", f"ERRO: não chegou no SEI. url={page.url}")
            raise

        _log("CHECK", f"estamos no SEI — url={page.url}")
        unidade = self._detectar_unidade_ativa() or "?"
        _log("OK", f"login validado, unidade ativa: {unidade}")

        if unidade != unidade_alvo:
            _log("UNIDADE", f"trocando de {unidade!r} para {unidade_alvo!r}")
            self._trocar_unidade(unidade_alvo)
            unidade = self._detectar_unidade_ativa() or "?"
            _log("OK", f"unidade ativa após troca: {unidade}")
        return unidade

    def _trocar_unidade(self, sigla_alvo: str) -> None:
        """Vai pra tela 'Trocar Unidade' e clica na unidade alvo."""
        page = self.page

        # O layout novo do SEI (2026) tem DOIS elementos #lnkInfraUnidade —
        # um na barra mobile (invisível no desktop) e um na barra desktop.
        # query_selector pega o primeiro (invisível) e ElementHandle.click()
        # trava esperando visibilidade. Clique via JS dispara o onclick
        # (window.location.href=...) independente de visibilidade.
        clicou_header = page.evaluate(
            "() => { const el = document.querySelector('#lnkInfraUnidade');"
            " if (!el) return false; el.click(); return true; }"
        )
        if not clicou_header:
            raise RuntimeError("link da unidade (#lnkInfraUnidade) não encontrado no header")

        try:
            page.wait_for_url(lambda u: "infra_unidade" in u, timeout=15_000)
        except PWTimeout:
            pass
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
        time.sleep(0.6)

        # Na tela de troca, seleciona a unidade alvo.
        # Layout novo (2026): cada unidade é um <input type="radio"
        # class="infraRadioInput" title="SIGLA"> com onclick=selecionarUnidade(id);
        # a sigla vira texto de <td data-label="Sigla">, e NÃO existe mais <a> com
        # a sigla — o seletor antigo por <a> dava "unidade não disponível" mesmo
        # com a unidade presente na lista. Tenta radio → linha da tabela → <a>.
        clicked = page.evaluate(
            "sigla => {"
            " const norm = s => (s || '').replace(/\\s+/g, '');"
            " const alvo = norm(sigla);"
            " const radios = Array.from(document.querySelectorAll("
            "  'input.infraRadioInput, input[name=chkInfraItem]'));"
            " for (const r of radios) {"
            "  if (norm(r.getAttribute('title')) === alvo) { r.click(); return true; }"
            " }"
            " for (const tr of Array.from(document.querySelectorAll('tr'))) {"
            "  const td = tr.querySelector('td[data-label=\"Sigla\"]');"
            "  if (td && norm(td.innerText) === alvo) {"
            "   const r = tr.querySelector('input[type=radio]');"
            "   if (r) { r.click(); return true; }"
            "  }"
            " }"
            " for (const a of Array.from(document.querySelectorAll('a'))) {"
            "  if (norm(a.innerText) === alvo) { a.click(); return true; }"
            " }"
            " return false;"
            " }",
            sigla_alvo,
        )
        if not clicked:
            raise RuntimeError(
                f"unidade {sigla_alvo!r} não está disponível pra este login. "
                "Confere a lista em 'Trocar Unidade' no SEI."
            )

        try:
            page.wait_for_url(lambda u: "procedimento_controlar" in u or "principal" in u, timeout=15_000)
        except PWTimeout:
            pass
        page.wait_for_load_state("domcontentloaded", timeout=15_000)

    def _submeter_login_sip(self, usuario: str, senha: str, orgao: str) -> None:
        page = self.page
        _log("LOGIN", f"detectado SIP — preenchendo formulário (url={page.url})")
        page.wait_for_selector("#frmLogin", timeout=15_000)

        # Ordem é crítica: usuário → órgão → senha → Enter.
        # Selecionar o órgão dispara JS que apaga a senha; por isso senha é a última.
        page.locator("#txtUsuario").fill(usuario)
        _log("LOGIN", "usuário preenchido em #txtUsuario")

        try:
            page.locator("#selOrgao").select_option(label=orgao)
            _log("LOGIN", f"órgão selecionado por label: {orgao}")
        except Exception:
            page.locator("#selOrgao").select_option(value=orgao)
            _log("LOGIN", f"órgão selecionado por value: {orgao}")

        time.sleep(0.4)

        # O input visível #pwdSenha (type=text, classe `masked`) não tem name.
        # O JS da classe `masked` propaga as TECLAS para um irmão hidden
        # type=password name=pwdSenha — esse sim vai no submit.
        # press_sequentially dispara keydown/keypress/keyup; fill() não funciona aqui.
        senha_loc = page.locator("#pwdSenha")
        senha_loc.click()
        senha_loc.press_sequentially(senha, delay=25)
        _log("LOGIN", f"senha digitada tecla-a-tecla ({len(senha)} chars)")

        # Submit via Enter no campo de senha — fluxo natural.
        # Invocar acaoLogin(2) via JS direto NÃO funciona (alguma pré-condição falha).
        senha_loc.press("Enter")
        _log("LOGIN", "submetendo form via Enter")

    def listar_pendentes_pagina_atual(
        self,
        incluir_gerados: bool = False,
        apenas_meus: bool = True,
    ) -> list[ProcessoPendente]:
        """Parseia a tela 'Controle de Processos' (página atual, sem navegar paginação).

        Se `apenas_meus=True` (default), aplica o filtro "Ver atribuídos a mim"
        antes de parsear (chama verMeusProcessos('M') no JS do SEI). Sem isso, a
        tabela mostra todos os processos da unidade (308 em PROC-PATR-PGM, não 155).

        Retorna no máximo 100 recebidos + (opcional) os gerados visíveis.
        Paginação será implementada num passo seguinte.
        """
        page = self.page

        if "procedimento_controlar" not in page.url:
            _log("LISTAR", "navegando para Controle de Processos")
            page.evaluate(
                "() => { const a = document.querySelector("
                "\"a[href*='acao=procedimento_controlar']\"); "
                "if (a) a.click(); }"
            )
            page.wait_for_url(lambda u: "procedimento_controlar" in u, timeout=15_000)

        page.wait_for_selector("#tblProcessosRecebidos", timeout=15_000)

        if apenas_meus:
            self._aplicar_filtro_meus()

        recebidos = self._parsear_tabela("tblProcessosRecebidos", "recebidos")
        _log("LISTAR", f"recebidos: {len(recebidos)} processos parseados")

        if incluir_gerados:
            gerados = self._parsear_tabela("tblProcessosGerados", "gerados")
            _log("LISTAR", f"gerados: {len(gerados)} processos parseados")
            return recebidos + gerados
        return recebidos

    def listar_pendentes(
        self,
        apenas_meus: bool = True,
        incluir_gerados: bool = False,
        max_paginas: int = 20,
        filtro: str = "",
    ) -> list[ProcessoPendente]:
        """Lista TODOS os processos pendentes, percorrendo todas as páginas.

        Usa o controle nativo do SEI: infraAcaoPaginar('+', N, 'Recebidos', null).
        Para a iteração quando: chega no total informado, max_paginas atingido,
        ou hdnRecebidosItens não muda mais.

        Args:
            filtro: se passado, filtra processos cujo `tipo + especificacao`
                contenha o termo (case-insensitive). Útil pra
                'perpetuidade', 'aforamento', 'desmembramento', etc.
        """
        page = self.page

        # SEMPRE navega pra Controle de Processos COM reset (zera paginação).
        # Necessário porque o cliente é singleton — entre chamadas a tabela
        # pode estar numa página diferente da 0, e parsear dali deixaria
        # processos de fora.
        href_controle = page.evaluate(
            "() => {"
            " const a = document.querySelector("
            "  \"a[href*='acao=procedimento_controlar'][href*='reset=1']\")"
            "  || document.querySelector(\"a[href*='acao=procedimento_controlar']\");"
            " return a ? a.getAttribute('href') : null;"
            " }"
        )
        if href_controle:
            if not href_controle.startswith("http"):
                href_controle = SEI_URL + href_controle.lstrip("/")
            _log("LISTAR", "navegando para Controle de Processos (reset paginação)")
            page.goto(href_controle, wait_until="domcontentloaded")
        elif "procedimento_controlar" not in page.url:
            _log("LISTAR", "fallback: SEI_URL home")
            page.goto(SEI_URL, wait_until="domcontentloaded")

        page.wait_for_selector("#tblProcessosRecebidos", timeout=15_000)

        if apenas_meus:
            self._aplicar_filtro_meus()
        else:
            self._remover_filtro_meus()

        total = self._ler_total("Recebidos")
        _log("LISTAR", f"total recebidos informado pelo SEI: {total}")

        todos: list[ProcessoPendente] = []
        vistos: set[str] = set()
        pagina = 0
        while pagina < max_paginas:
            atuais = self._parsear_tabela("tblProcessosRecebidos", "recebidos")
            novos = [p for p in atuais if p.id_procedimento not in vistos]
            for p in novos:
                vistos.add(p.id_procedimento)
            todos.extend(novos)
            _log(
                "LISTAR",
                f"página {pagina}: {len(atuais)} na tabela, "
                f"{len(novos)} novos, total acumulado={len(todos)}",
            )
            if not novos:
                break
            if total and len(todos) >= total:
                break
            if not self._avancar_pagina("Recebidos", pagina):
                _log("LISTAR", "não conseguiu avançar página — parando")
                break
            pagina += 1

        if incluir_gerados:
            gerados = self._parsear_tabela("tblProcessosGerados", "gerados")
            _log("LISTAR", f"gerados (página única): {len(gerados)}")
            todos.extend(gerados)

        if filtro:
            termo = filtro.lower().strip()
            antes = len(todos)
            # Inclui a anotação (post-it): muitas vezes é o único lugar onde a
            # matéria aparece ("perpetuidade" não estava em tipo/especificação).
            todos = [
                p for p in todos
                if termo in " ".join(
                    (p.tipo, p.especificacao or "", p.anotacao_completa or "")
                ).lower()
            ]
            _log(
                "LISTAR",
                f"filtro={filtro!r}: {len(todos)}/{antes} match",
            )
        return todos

    def _ler_total(self, qual: Literal["Recebidos", "Gerados"]) -> int:
        rotulo = "recebidos" if qual == "Recebidos" else "gerados"
        return self.page.evaluate(
            f"""() => {{
                if (!document.body) return 0;
                const m = document.body.innerText.match(
                    /Processos {rotulo}\\s*\\((\\d+)\\s+registros/i
                );
                return m ? parseInt(m[1], 10) : 0;
            }}"""
        ) or 0

    def _avancar_pagina(
        self, qual: Literal["Recebidos", "Gerados"], pagina_atual: int
    ) -> bool:
        page = self.page
        hdn_id = "hdnRecebidosItens" if qual == "Recebidos" else "hdnGeradosItens"

        antes = page.evaluate(
            "id => { const h = document.getElementById(id); return h ? h.value : null; }",
            hdn_id,
        )
        if antes is None:
            return False

        existe_proxima = page.evaluate(
            "qual => {"
            " const a = document.getElementById('lnk' + qual + 'ProximaPaginaSuperior')"
            " || document.getElementById('lnk' + qual + 'ProximaPaginaInferior');"
            " return !!a && a.offsetParent !== null;"
            " }",
            qual,
        )
        if not existe_proxima:
            _log("LISTAR", f"link 'Próxima Página' ({qual}) não disponível")
            return False

        page.evaluate(
            "qual => {"
            " const a = document.getElementById('lnk' + qual + 'ProximaPaginaSuperior')"
            " || document.getElementById('lnk' + qual + 'ProximaPaginaInferior');"
            " if (a) a.click();"
            " }",
            qual,
        )
        try:
            page.wait_for_function(
                "args => { const h = document.getElementById(args.id);"
                " return h && h.value !== args.prev; }",
                arg={"id": hdn_id, "prev": antes},
                timeout=15_000,
            )
            return True
        except PWTimeout:
            _log("LISTAR", f"timeout esperando atualização de {hdn_id}")
            return False

    def _remover_filtro_meus(self) -> None:
        """Desativa o filtro 'Ver atribuídos a mim' se estiver ativo.

        SEI sinaliza filtro ativo com div#divFiltroMeusProcessos. Pra remover,
        clica em #ancLiberarMeusProcessos (link 'Remover filtro').
        """
        page = self.page
        ja_ativo = page.evaluate(
            "() => !!document.getElementById('divFiltroMeusProcessos')"
        )
        if not ja_ativo:
            _log("LISTAR", "filtro 'atribuídos a mim' já está inativo")
            return

        _log("LISTAR", "removendo filtro 'atribuídos a mim'")
        page.evaluate(
            "() => { const a = document.getElementById('ancLiberarMeusProcessos'); "
            "if (a) a.click(); }"
        )
        try:
            page.wait_for_function(
                "() => !document.getElementById('divFiltroMeusProcessos')",
                timeout=10_000,
            )
            # A remoção dispara reload — espera a tabela voltar antes de
            # qualquer evaluate (senão document.body pode estar nulo).
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            page.wait_for_selector("#tblProcessosRecebidos", timeout=15_000)
            _log("LISTAR", "filtro removido (vendo todos os processos do setor)")
        except PWTimeout:
            _log("LISTAR", "AVISO: filtro pode não ter sido removido")

    def _aplicar_filtro_meus(self) -> None:
        """Ativa o filtro 'Ver atribuídos a mim' se ainda não estiver ativo.

        SEI sinaliza o filtro ativo com div#divFiltroMeusProcessos contendo
        o link #ancLiberarMeusProcessos. Quando inativo, existe o link
        #lnkAtribuidosMim que chama verMeusProcessos('M').
        """
        page = self.page
        ja_ativo = page.evaluate(
            "() => !!document.getElementById('divFiltroMeusProcessos')"
        )
        if ja_ativo:
            _log("LISTAR", "filtro 'atribuídos a mim' já ativo")
            return

        _log("LISTAR", "ativando filtro 'atribuídos a mim' (verMeusProcessos('M'))")
        page.evaluate(
            "() => { if (typeof verMeusProcessos === 'function') "
            "verMeusProcessos('M'); else { "
            "const a = document.getElementById('lnkAtribuidosMim'); if (a) a.click(); "
            "} }"
        )

        try:
            page.wait_for_selector("#divFiltroMeusProcessos", timeout=10_000)
            # A aplicação dispara reload — espera a tabela voltar antes de
            # qualquer evaluate (senão document.body pode estar nulo).
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            page.wait_for_selector("#tblProcessosRecebidos", timeout=15_000)
            _log("LISTAR", "filtro confirmado ativo")
        except PWTimeout:
            _log("LISTAR", "AVISO: filtro pode não ter sido aplicado")

    def _parsear_tabela(
        self, tabela_id: str, origem: Literal["recebidos", "gerados"]
    ) -> list[ProcessoPendente]:
        page = self.page
        linhas = page.evaluate(
            """(tableId) => {
                const t = document.getElementById(tableId);
                if (!t) return [];
                return Array.from(t.rows).slice(1).map(tr => {
                    const tds = Array.from(tr.cells);
                    const chk = tr.querySelector("input.infraCheckboxInput");
                    const linkProc = tr.querySelector("a.processoVisualizado")
                                   || tr.querySelector("td:nth-child(3) a[href*='id_procedimento']");
                    const linkAtrib = tr.querySelector("td:last-child a");
                    const imgAnotacao = tr.querySelector("td:nth-child(2) img.imagemStatus");
                    const aAnotacao = tr.querySelector("td:nth-child(2) a[aria-label]");
                    return {
                        tr_id: tr.id || "",
                        chk_value: chk ? chk.value : "",
                        chk_title: chk ? chk.getAttribute("title") || "" : "",
                        chk_aria: chk ? chk.getAttribute("aria-label") || "" : "",
                        link_proc_href: linkProc ? linkProc.getAttribute("href") || "" : "",
                        link_proc_text: linkProc ? (linkProc.innerText || "").trim() : "",
                        link_atrib_title: linkAtrib ? linkAtrib.getAttribute("title") || "" : "",
                        link_atrib_text: linkAtrib ? (linkAtrib.innerText || "").trim() : "",
                        tem_anotacao: !!imgAnotacao,
                        anotacao_aria: aAnotacao ? aAnotacao.getAttribute("aria-label") || "" : "",
                    };
                });
            }""",
            tabela_id,
        )

        out: list[ProcessoPendente] = []
        for r in linhas:
            id_proc = r["chk_value"] or r["tr_id"].lstrip("P")
            numero = r["chk_title"] or r["link_proc_text"].replace("​", "")
            tipo, espec = self._parse_aria(r["chk_aria"])
            atribuido = self._parse_atribuido(r["link_atrib_title"])

            out.append(
                ProcessoPendente(
                    id_procedimento=id_proc,
                    numero=numero,
                    tipo=tipo,
                    especificacao=espec,
                    atribuido_para=atribuido,
                    sigla_responsavel=r["link_atrib_text"] or None,
                    tem_anotacao=r["tem_anotacao"],
                    anotacao_completa=r["anotacao_aria"] or None,
                    url_processo=r["link_proc_href"],
                    origem=origem,
                )
            )
        return out

    @staticmethod
    def _parse_aria(aria: str) -> tuple[str, str | None]:
        if not aria:
            return ("", None)
        m = _RE_TIPO_ESPEC.match(aria.strip())
        if not m:
            return (aria.strip(), None)
        return (m.group("tipo").strip(), (m.group("espec") or "").strip() or None)

    @staticmethod
    def _parse_atribuido(title: str) -> str | None:
        if not title:
            return None
        m = _RE_ATRIBUIDO.match(title.strip())
        return m.group("nome").strip() if m else title.strip()

    def diagnosticar_paginacao(self) -> dict:
        """Inspeciona controles de paginação da tabela de Recebidos.

        Versão 2 — busca por:
          * elementos com id/name contendo 'agina' (Pagina, hdnPagina, selPagina)
          * links/imagens com title/onmouseover contendo 'página', 'próxima', 'última'
          * inputs hidden em qualquer form
          * outerHTML de divs irmãs da tabela (onde geralmente está o controle)
        """
        return self.page.evaluate(
            """() => {
                const out = {};

                // 1) contagem
                const all = document.body.innerText;
                const m = all.match(/Processos recebidos\\s*\\(([^)]+)\\)/);
                out.contagem = m ? m[1] : null;

                // 2) qualquer elemento com id/name contendo 'agina'
                out.por_id_pagina = Array.from(document.querySelectorAll('*'))
                    .filter(el => /agina/i.test(el.id || '') || /agina/i.test(el.getAttribute('name') || ''))
                    .slice(0, 30)
                    .map(el => ({tag: el.tagName, id: el.id, name: el.getAttribute('name'),
                                  type: el.type || '', value: (el.value || '').slice(0, 40)}));

                // 3) imagens/links com title falando em página
                out.por_title = Array.from(document.querySelectorAll('a, img, input[type=image], button'))
                    .filter(el => /(p[áa]gina|pr[oó]xima|[uú]ltima|primeira|anterior)/i.test(
                        (el.getAttribute('title') || '') + ' '
                      + (el.getAttribute('aria-label') || '') + ' '
                      + (el.getAttribute('onmouseover') || '')))
                    .slice(0, 20)
                    .map(el => ({tag: el.tagName,
                                  id: el.id || '',
                                  title: el.getAttribute('title') || '',
                                  aria: el.getAttribute('aria-label') || '',
                                  onclick: el.getAttribute('onclick') || '',
                                  href: el.getAttribute('href') || '',
                                  src: el.getAttribute('src') || ''}));

                // 4) hidden inputs em todo form
                out.hidden_inputs = Array.from(document.querySelectorAll('input[type=hidden]'))
                    .filter(el => el.name)
                    .slice(0, 40)
                    .map(el => ({name: el.name, value: (el.value || '').slice(0, 60)}));

                // 5) outerHTML dos irmãos imediatos da tabela de recebidos
                const t = document.getElementById('tblProcessosRecebidos');
                if (t) {
                    const parent = t.parentElement;
                    out.parent_tag = parent ? parent.tagName + '#' + parent.id : null;
                    if (parent) {
                        out.irmaos_da_tabela = Array.from(parent.children)
                            .filter(c => c !== t)
                            .map(c => ({tag: c.tagName, id: c.id || '',
                                         classes: c.className || '',
                                         html: (c.outerHTML || '').slice(0, 600)}))
                            .slice(0, 10);
                    }
                    // 6) outerHTML do avô
                    const grand = parent ? parent.parentElement : null;
                    if (grand) {
                        out.avo_tag = grand.tagName + '#' + grand.id;
                        out.avo_filhos_resumo = Array.from(grand.children)
                            .map(c => ({tag: c.tagName, id: c.id, classes: (c.className || '').slice(0, 60)}));
                    }
                }
                return out;
            }"""
        )

    # -----------------------------------------------------------------
    # Download de processo
    # -----------------------------------------------------------------

    def baixar_processo(
        self,
        numero: str,
        formato: Literal["pdf", "zip", "ambos"] = "pdf",
        destino_base: Path = DESTINO_DOWNLOAD,
        forcar: bool = False,
    ) -> Path | dict[str, Path]:
        """Baixa o processo no formato escolhido.

        - `formato="pdf"` (default): consolida em um único PDF — melhor pra análise pelo Claude
        - `formato="zip"`: ZIP com cada documento no formato original (Word, PDF, etc.) — melhor pra preservar Word editável
        - `formato="ambos"`: baixa os dois (retorna dict com 'pdf' e 'zip')

        Pasta: `{destino_base}/{numero_safe}/`. Arquivo: `{numero_safe}.{ext}`.
        Se já existir e forcar=False, retorna o caminho existente sem rebaixar.
        """
        if formato == "ambos":
            zip_path = self._baixar_um_formato(numero, "zip", destino_base, forcar)
            pdf_path = self._baixar_um_formato(numero, "pdf", destino_base, forcar)
            return {"pdf": pdf_path, "zip": zip_path}
        return self._baixar_um_formato(numero, formato, destino_base, forcar)

    def _baixar_um_formato(
        self,
        numero: str,
        formato: Literal["pdf", "zip"],
        destino_base: Path,
        forcar: bool,
    ) -> Path:
        numero_safe = numero.replace("/", "-")
        pasta = destino_base / numero_safe
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / f"{numero_safe}.{formato}"

        if destino.exists() and not forcar:
            _log("BAIXAR", f"{numero}: {formato.upper()} já existe em {destino}, pulando")
            return destino

        _log("BAIXAR", f"{numero}: abrindo processo via pesquisa rápida")
        self._abrir_processo_por_numero(numero)

        _log("BAIXAR", f"{numero}: clicando em 'Gerar Arquivo {formato.upper()} do Processo'")
        frame_geracao = self._clicar_gerar(formato)

        _log("BAIXAR", f"{numero}: confirmando 'Gerar' no modal e capturando download")
        page = self.page
        try:
            with page.expect_download(timeout=120_000) as dl_info:
                clicked = frame_geracao.evaluate(
                    "() => {"
                    " const b = document.querySelector("
                    "  \"button[name='btnGerar'], input[name='btnGerar']\");"
                    " if (b) { b.click(); return 'btnGerar'; }"
                    " const b2 = Array.from(document.querySelectorAll("
                    "  'button, input[type=button], input[type=submit]'))"
                    "  .find(el => /^gerar$/i.test((el.value || el.innerText || '').trim()));"
                    " if (b2) { b2.click(); return 'fallback-text'; }"
                    " const f = document.querySelector("
                    "  \"form[action*='gerar_zip'], form[action*='gerar_pdf'], "
                    "   #frmProcedimentoZip, #frmProcedimentoPdf, #frmGerarZip\");"
                    " if (f && typeof f.submit === 'function') { f.submit(); return 'form'; }"
                    " return null;"
                    " }"
                )
                if not clicked:
                    raise RuntimeError(
                        f"não achei botão 'Gerar' nem form de gerar_{formato} no frame"
                    )
                _log("BAIXAR", f"submit via {clicked!r}")
            dl = dl_info.value
        except PWTimeout as e:
            raise RuntimeError(
                f"timeout esperando download do {formato.upper()} de {numero}"
            ) from e

        dl.save_as(destino)
        _log("BAIXAR", f"{numero}: salvo em {destino} ({destino.stat().st_size} bytes)")
        return destino

    def verificar_pareceres_processo(self, numero: str) -> list[dict[str, Any]]:
        """Verifica pareceres do processo SEM baixar PDF — lê HTML da árvore.

        Abre o processo via pesquisa rápida, itera links da árvore que casam
        com 'Parecer', navega cada um no ifrVisualizacao e extrai do HTML:
          - título do documento (ex: 'Parecer Jurídico 15212040')
          - id do documento (SEI nº)
          - unidade emissora (ex: 'PROC-PATR-PGM' + nome completo se aparecer)
          - se está assinado, nome/cargo do assinante, data/hora
        """
        from .analise import (
            RE_ASSINATURA_SEI,
            RE_SEI_ID,
            RE_TITULO_PARECER,
        )

        page = self.page
        self._abrir_processo_por_numero(numero)

        # Aguarda ifrArvore carregar com lista de documentos
        deadline = time.time() + 20.0
        arvore_frame: Frame | None = None
        while time.time() < deadline:
            arvore_frame = next(
                (f for f in page.frames if f.name == "ifrArvore"), None
            )
            if arvore_frame and "procedimento_visualizar" in (arvore_frame.url or ""):
                break
            time.sleep(0.3)
        if arvore_frame is None:
            _log("PARECER", "ifrArvore não encontrado")
            return []

        try:
            arvore_frame.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PWTimeout:
            pass

        # Coleta todos os links que parecem ser pareceres + tag de unidade ao lado
        pareceres_brutos = arvore_frame.evaluate(
            """() => {
                const links = Array.from(document.querySelectorAll('a'));
                const candidatos = links.filter(a =>
                    /^\\s*parecer\\b/i.test((a.innerText || '').trim())
                );
                return candidatos.map(a => {
                    // Procura tag de unidade no nó-irmão (.infraArvoreUnidade ou similar)
                    let unidade_sigla = '';
                    let parent = a.parentElement;
                    for (let lvl = 0; lvl < 4 && parent; lvl++) {
                        const tag = parent.querySelector(
                            '.infraArvoreUnidade, [class*=Unidade], span[title*=PGM], '
                          + 'span[title*=PMT], span[title*=SEMAM]'
                        );
                        if (tag) {
                            unidade_sigla = (tag.innerText || tag.getAttribute('title') || '').trim();
                            break;
                        }
                        parent = parent.parentElement;
                    }
                    return {
                        titulo: (a.innerText || '').trim(),
                        href: a.getAttribute('href') || '',
                        onclick: a.getAttribute('onclick') || '',
                        unidade_sigla,
                    };
                });
            }"""
        )
        _log("PARECER", f"{len(pareceres_brutos)} pareceres na árvore")

        if not pareceres_brutos:
            return []

        # Acha (ou aguarda) o ifrVisualizacao
        deadline = time.time() + 10.0
        vis_frame: Frame | None = None
        while time.time() < deadline:
            vis_frame = next(
                (f for f in page.frames if f.name == "ifrVisualizacao"), None
            )
            if vis_frame:
                break
            time.sleep(0.3)
        if vis_frame is None:
            _log("PARECER", "ifrVisualizacao não disponível")
            return []

        resultados: list[dict[str, Any]] = []
        for p in pareceres_brutos:
            href = p["href"]
            if not href:
                continue
            if not href.startswith("http"):
                href = SEI_URL + href.lstrip("/")

            try:
                vis_frame.goto(href, wait_until="domcontentloaded", timeout=20_000)
            except PWTimeout:
                _log("PARECER", f"timeout abrindo {p['titulo']!r}")
                continue

            try:
                texto = vis_frame.evaluate("() => document.body.innerText || ''")
            except Exception:
                texto = ""

            # Tenta também pegar do <head><title> ou de um header com nome da unidade
            try:
                header_unidade = vis_frame.evaluate(
                    """() => {
                        const candidatos = document.querySelectorAll(
                          'p, div, h1, h2, h3, span'
                        );
                        for (const el of candidatos) {
                          const t = (el.innerText || '').trim();
                          if (/(procuradoria|secretaria|gabinete)/i.test(t)
                              && t.length < 200) {
                            return t;
                          }
                        }
                        return '';
                    }"""
                )
            except Exception:
                header_unidade = ""

            m_sig = RE_ASSINATURA_SEI.search(texto)
            if m_sig:
                assinado = True
                nome = m_sig.group("nome").strip()
                cargo = m_sig.group("cargo").strip()
                data = m_sig.group("data")
                hora = m_sig.group("hora")
            else:
                assinado = False
                nome = cargo = data = hora = None

            m_id = RE_SEI_ID.search(texto)
            sei_id = m_id.group(1) if m_id else None

            # Captura sigla da unidade do título (ex: "Parecer Jurídico 15212040" + "PROC-PATR-PGM")
            unidade_descricao = header_unidade or None

            resultados.append(
                {
                    "titulo_arvore": p["titulo"],
                    "id_documento": sei_id,
                    "unidade_sigla": p["unidade_sigla"] or None,
                    "unidade_descricao": unidade_descricao,
                    "assinado": assinado,
                    "assinante_nome": nome,
                    "assinante_cargo": cargo,
                    "data_assinatura": data,
                    "hora_assinatura": hora,
                }
            )

        _log(
            "PARECER",
            f"verificados {len(resultados)} pareceres "
            f"({sum(1 for r in resultados if r['assinado'])} assinados)",
        )
        return resultados

    def baixar_todos_processos(
        self,
        formato: Literal["pdf", "zip", "ambos"] = "pdf",
        destino_base: Path = DESTINO_DOWNLOAD,
        forcar: bool = False,
        limite: int | None = None,
    ) -> dict:
        """Baixa todos os pendentes atribuídos a você no formato escolhido.

        - `formato="pdf"` (default): PDF consolidado — melhor pra análise
        - `formato="zip"`: ZIP com originais
        - `formato="ambos"`: ambos
        - `forcar=False` (default): pula processos cujo arquivo alvo já existe
        - `limite`: se passado, baixa apenas os primeiros N (útil pra testar)

        Retorna dict com contagens e lista de erros (não interrompe em erro
        individual — segue baixando os próximos).
        """
        pendentes = self.listar_pendentes(incluir_gerados=False)
        if limite:
            pendentes = pendentes[:limite]

        baixados: list[str] = []
        pulados: list[str] = []
        erros: list[dict] = []
        formatos = ("pdf", "zip") if formato == "ambos" else (formato,)

        total = len(pendentes)
        for i, p in enumerate(pendentes, 1):
            _log("LOTE", f"=== [{i}/{total}] {p.numero} — {p.tipo} ===")
            numero_safe = p.numero.replace("/", "-")
            todos_existem = all(
                (destino_base / numero_safe / f"{numero_safe}.{f}").exists()
                for f in formatos
            )
            if todos_existem and not forcar:
                _log("LOTE", f"{p.numero}: arquivo(s) já existe(m), pulando")
                pulados.append(p.numero)
                continue
            try:
                self.baixar_processo(
                    p.numero,
                    formato=formato,
                    destino_base=destino_base,
                    forcar=forcar,
                )
                baixados.append(p.numero)
            except Exception as e:
                _log("LOTE", f"{p.numero}: ERRO — {e}")
                erros.append({"numero": p.numero, "erro": str(e)[:300]})

        resumo = {
            "total": total,
            "formato": formato,
            "baixados": len(baixados),
            "pulados": len(pulados),
            "erros": len(erros),
            "lista_erros": erros,
        }
        _log("LOTE", f"FIM: {resumo}")
        return resumo

    def _abrir_processo_por_numero(self, numero: str) -> None:
        """Abre o processo via barra de pesquisa rápida do header.

        Mais simples e não depende de paginação. Funciona pra qualquer processo
        ao qual a unidade tem acesso. A URL fica com `id_protocolo=` em vez de
        `id_procedimento=`, mas o `ifrConteudoVisualizacao` carrega
        `arvore_visualizar` igual e a barra ZIP aparece normalmente.
        """
        page = self.page

        _log("BAIXAR", f"pesquisando {numero!r} via #txtPesquisaRapida")
        pesquisa = page.locator("#txtPesquisaRapida")
        try:
            pesquisa.wait_for(state="attached", timeout=10_000)
        except PWTimeout as e:
            raise RuntimeError(
                "campo de pesquisa rápida (#txtPesquisaRapida) não encontrado"
            ) from e

        pesquisa.fill(numero)
        pesquisa.press("Enter")

        try:
            page.wait_for_url(
                lambda u: "id_protocolo=" in u
                or "id_procedimento=" in u
                or "procedimento_pesquisar" in u,
                timeout=15_000,
            )
        except PWTimeout:
            pass

        if "procedimento_pesquisar" in page.url:
            raise RuntimeError(
                f"pesquisa por {numero!r} retornou múltiplos resultados "
                f"(url={page.url})"
            )
        if (
            "id_protocolo=" not in page.url
            and "id_procedimento=" not in page.url
        ):
            raise RuntimeError(
                f"pesquisa por {numero!r} não abriu nenhum processo "
                f"(url={page.url})"
            )

        # Aguarda iframes filhos terminarem de carregar
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            _log("BAIXAR", "networkidle timeout, seguindo")
        time.sleep(1.0)

    def _clicar_gerar(self, formato: Literal["pdf", "zip"]) -> Frame:
        """Clica em 'Gerar Arquivo {formato.upper()} do Processo'.

        Estrutura de frames descoberta empiricamente em 2026-04-30:
          - `ifrConteudoVisualizacao` recebe a tela "arvore_visualizar" que
            contém a BARRA DE AÇÕES com os ícones PDF e ZIP. Carregamento é
            tardio: primeiro fica about:blank, depois muda pra
            controlador.php?acao=arvore_visualizar&...&id_procedimento=N
          - Ao clicar no ícone, um NOVO frame `ifrVisualizacao` (sem
            "Conteudo") é criado e recebe o form de geração em
            controlador.php?acao=procedimento_gerar_{formato}&acao_origem=arvore_visualizar
        Retorna o frame onde está o form (pra clicar 'Gerar' depois).
        """
        page = self.page

        # 1) Aguarda ifrConteudoVisualizacao carregar com arvore_visualizar
        deadline = time.time() + 20.0
        barra_frame: Frame | None = None
        while time.time() < deadline:
            barra_frame = next(
                (fr for fr in page.frames
                 if fr.name == "ifrConteudoVisualizacao"
                 and "arvore_visualizar" in (fr.url or "")),
                None,
            )
            if barra_frame is not None:
                break
            time.sleep(0.3)

        if barra_frame is None:
            frames_info = [(f.name, (f.url or "")[:90]) for f in page.frames]
            raise RuntimeError(
                "ifrConteudoVisualizacao não carregou com arvore_visualizar. "
                f"Frames: {frames_info}"
            )

        try:
            barra_frame.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PWTimeout:
            pass

        # 2) Pega o href do link do formato escolhido — o target='ifrVisualizacao'
        # do <a> não navega via Playwright; vamos navegar o iframe diretamente
        # via evaluate. Localizamos pela imagem (processo_gerar_pdf.svg ou
        # processo_gerar_zip.svg) e subimos pro <a> ancestral.
        href_alvo = barra_frame.evaluate(
            "padrao => {"
            " const img = document.querySelector(\"img[src*='\" + padrao + \"']\");"
            " if (!img) return null;"
            " const a = img.closest('a');"
            " return a ? a.getAttribute('href') : null;"
            " }",
            f"processo_gerar_{formato}",
        )
        if not href_alvo:
            raise RuntimeError(
                f"link 'Gerar Arquivo {formato.upper()} do Processo' não encontrado "
                "em ifrConteudoVisualizacao"
            )
        if not href_alvo.startswith("http"):
            href_alvo = SEI_URL + href_alvo.lstrip("/")
        _log("BAIXAR", f"href do {formato.upper()}: {href_alvo[:120]}")

        # 3) Aguarda o ifrVisualizacao existir (child de ifrConteudoVisualizacao)
        deadline = time.time() + 15.0
        form_frame: Frame | None = None
        while time.time() < deadline:
            form_frame = next(
                (f for f in page.frames if f.name == "ifrVisualizacao"), None
            )
            if form_frame is not None:
                break
            time.sleep(0.3)

        if form_frame is None:
            frames_info = [(f.name, (f.url or "")[:90]) for f in page.frames]
            raise RuntimeError(
                f"frame ifrVisualizacao não existe ainda. Frames: {frames_info}"
            )

        # Navega o ifrVisualizacao diretamente pra URL de geração
        _log("BAIXAR", f"navegando ifrVisualizacao direto pra URL do {formato.upper()}")
        form_frame.goto(href_alvo, wait_until="domcontentloaded")

        _log("BAIXAR",
             f"form de gerar_zip em {form_frame.name!r}: {form_frame.url[:120]}")
        try:
            form_frame.wait_for_load_state("domcontentloaded", timeout=15_000)
            form_frame.wait_for_selector(
                "button[name='btnGerar'], input[name='btnGerar'], "
                "input[value='Gerar'], button:has-text('Gerar')",
                timeout=15_000,
            )
        except PWTimeout:
            _log("BAIXAR", f"AVISO: form de gerar_{formato} sem botão Gerar visível")
        return form_frame

    def _detectar_unidade_ativa(self) -> str | None:
        page = self.page
        candidatos = [
            "#lnkInfraUnidade",
            "header a:has-text('-PGM')",
            "button:has-text('-PGM')",
            "a[onclick*='infra_unidade']",
        ]
        for sel in candidatos:
            try:
                el = page.query_selector(sel)
                if el:
                    txt = (el.inner_text() or "").strip()
                    if txt:
                        _log("CHECK", f"unidade detectada via {sel!r}: {txt}")
                        return txt
            except Exception:
                pass
        body = page.inner_text("body")
        for tag in ("PROC-PATR-PGM", "PROC-GERAL-PGM"):
            if tag in body:
                _log("CHECK", f"unidade detectada por busca em body: {tag}")
                return tag
        return None
