<h1 align="center">
    <img alt="MCP SEI-PMT" src="https://raw.githubusercontent.com/fxbarros/MCP-SEI-PMT/main/docs/assets/banner.svg?sanitize=true">
    <br>
    <small>A caixa do SEI, os PDFs dos processos, a verificação de pareceres e a redação de minutas em linguagem natural — com escrita no SEI só mediante confirmação</small>
</h1>

<p align="center">
    <img alt="Python" src="https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white">
    <img alt="Ferramentas" src="https://img.shields.io/badge/ferramentas-18-brightgreen">
    <img alt="MCP" src="https://img.shields.io/badge/MCP-Claude%20Desktop-d97757">
    <img alt="REST" src="https://img.shields.io/badge/API%20REST-mod--wssei%20v2-blue">
    <img alt="Escrita" src="https://img.shields.io/badge/escrita-s%C3%B3%20com%20confirma%C3%A7%C3%A3o-8b0000">
</p>

<p align="center">
    <a href="#-funcionalidades"><strong>Funcionalidades</strong></a>
    &middot;
    <a href="#%EF%B8%8F-as-18-ferramentas"><strong>Ferramentas</strong></a>
    &middot;
    <a href="#-instala%C3%A7%C3%A3o"><strong>Instalação</strong></a>
    &middot;
    <a href="#-exemplos-de-uso"><strong>Exemplos</strong></a>
    &middot;
    <a href="#-como-funciona-por-dentro"><strong>Por dentro</strong></a>
    &middot;
    <a href="#-seguran%C3%A7a"><strong>Segurança</strong></a>
    &middot;
    <a href="#%EF%B8%8F-avisos-importantes"><strong>Avisos</strong></a>
</p>

Servidor [MCP](https://modelcontextprotocol.io) que liga o Claude Desktop ao **SEI — Sistema Eletrônico de Informações** da Prefeitura Municipal de Teresina (`sei.teresina.pi.gov.br`, SEI 5.0.4). Foi construído para a rotina de uma procuradoria: ver o que está na caixa, baixar o processo inteiro em PDF, saber se já existe parecer assinado, redigir a minuta a partir de modelos e, quando o usuário confirmar, criar e preencher o documento dentro do próprio SEI.

> ⚡ **Dois caminhos**: a **API REST oficial** do SEI (módulo `mod-wssei`, a mesma do app móvel) para consultas, assinaturas e documentos — rápida e imune a mudanças de layout; e o **Chrome headless** (patchright) apenas para o que a API não oferece, como o PDF consolidado do processo.

## ✨ Funcionalidades

- 🔐 **Login com as credenciais do Keychain** do macOS (SIP usuário/senha), com relogin automático e troca automática para a unidade alvo
- 📥 **Caixa da unidade**: todos os processos recebidos, com filtro por matéria (tipo, especificação e anotação) e `apenas_meus`
- 📄 **PDF consolidado ou ZIP** de cada processo, salvo no iCloud Drive — um ou todos em lote
- ✅ **Parecer já assinado?** Verificação em ~2 s pela API: nome, cargo, unidade, data e hora de cada assinatura
- 📊 **Planilha de controle** (`controle.xlsx`) com colunas preenchidas pelo Claude (matéria, complexidade, ação sugerida, status da minuta) preservadas entre regenerações
- 📝 **Modelos e minutas**: lê os modelos `.docx` da pasta de modelos e salva a minuta pronta na pasta do processo, clonando a formatação do modelo
- 🧾 **Documentos no SEI**: cria documento interno, lê as seções, converte texto para o HTML com as classes do SEI (`Texto_Justificado_Recuo_Primeira_Linha`, `Citacao`, `Texto_Ementa`…) e grava — **sempre com `confirmar=true` na mesma chamada**; sem isso só devolve a prévia
- 🌙 **Lote noturno**: `proximo_pendente_nao_analisado` entrega um processo por vez, consultando a planilha para pular os já analisados
- 🛡️ **Nunca assina, tramita, conclui ou exclui** — essas tools não existem, por decisão de projeto

## 🛠️ As 18 ferramentas

**Caixa e planilha** (Chrome headless)

| Ferramenta | O que faz |
|---|---|
| `listar_pendentes` | processos da unidade, com `filtro` por matéria, `limite` e `apenas_meus` |
| `gerar_planilha_controle` | gera/atualiza o `controle.xlsx` preservando as colunas preenchidas à mão ou pelo Claude |
| `atualizar_planilha_processo` | grava status da minuta, matéria, complexidade, ação sugerida e resumo de um processo |
| `proximo_pendente_nao_analisado` | o próximo processo do filtro que ainda não tem análise na planilha |

**Processo e parecer**

| Ferramenta | O que faz |
|---|---|
| `baixar_processo` | PDF consolidado (default), ZIP ou ambos, em `Processos SEI/{numero}/` |
| `baixar_todos_processos` | o mesmo em lote, pulando os já baixados |
| `preparar_processo` | baixa, verifica parecer e decide: texto direto para o Claude (≤ 15 MB) ou NotebookLM |
| `verificar_parecer_juridico_rapido` | **REST**: pareceres do processo com as assinaturas — sem browser, sem download |
| `verificar_parecer_juridico` | a mesma verificação lendo o PDF baixado (fallback) |

**Modelos e minutas** (arquivos locais)

| Ferramenta | O que faz |
|---|---|
| `listar_modelos_parecer` | modelos disponíveis na pasta `Modelos parecer/` |
| `ler_modelo_parecer` | texto de um modelo |
| `salvar_minuta_processo` | salva a minuta (`.docx`/`.md`/`.txt`) na pasta do processo, clonando o modelo se indicado |

**Documentos no SEI** (REST)

| Ferramenta | Escreve? | O que faz |
|---|---|---|
| `listar_tipos_documento_sei` | não | tipos de documento (idSerie) — Parecer Jurídico, Ofício, Despacho, Memorando, Certidão… |
| `listar_estilos_sei` | não | classes CSS do SEI e a marcação leve aceita por `formatar_html_sei` |
| `formatar_html_sei` | não | texto simples → HTML do SEI; `Id. NNNNNNN` vira link *sei!* quando o documento está no processo |
| `ler_documento_sei` | não | seções (cabeçalho/corpo/rodapé), versão e texto do corpo de um documento interno |
| `criar_documento_sei` | **sim, com `confirmar=true`** | cria documento interno vazio no processo |
| `editar_documento_sei` | **sim, com `confirmar=true`** | substitui o conteúdo de uma seção |

## 🧰 Requisitos

- macOS (credenciais no Keychain) com Google Chrome instalado
- Python 3.12+ e [uv](https://docs.astral.sh/uv/)
- Claude Desktop
- Conta no SEI-PMT com login por usuário e senha do SIP

## 📦 Instalação

### 1) Clone o repositório

```bash
git clone https://github.com/fxbarros/MCP-SEI-PMT.git sei-mcp
cd sei-mcp
```

### 2) Instale as dependências

```bash
uv sync
```

### 3) Salve as credenciais no Keychain

```bash
uv run setup_credenciais.py
```

Pergunta usuário, senha, órgão (`PGM`) e a **unidade alvo** (sigla, ex. `PROC-PRFMAP-PGM`). Tudo fica no Keychain (service `mcp-sei`), nunca em arquivo.

### 4) Registre o MCP no Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (ajuste o caminho):

```json
{
  "mcpServers": {
    "sei": {
      "command": "/CAMINHO/sei-mcp/.venv/bin/python",
      "args": ["-m", "sei_mcp.server"]
    }
  }
}
```

Reinicie o Claude Desktop. Para ver a janela do Chrome durante o uso, adicione `"env": {"SEI_HEADLESS": "0"}`.

## 💬 Exemplos de uso

- *"Lista meus pendentes do SEI de aforamento"*
- *"Quais desses têm parecer assinado?"* → `verificar_parecer_juridico_rapido` em cada um, em segundos
- *"Baixa o processo 00047.001613/2026-14 em PDF"*
- *"Analisa o próximo pendente de perpetuidade e redige a minuta com o modelo de perpetuidade"*
- *"Cria um despacho no processo X"* → prévia → **"pode criar"** → documento criado, sem assinatura
- *"Coloca esse texto no corpo do despacho"* → prévia com o HTML → **"pode gravar"** → gravado; a assinatura continua sendo sua, no SEI

## 🔬 Como funciona por dentro

- **API REST (`sei_rest.py`, `sei_docs.py`)**: `POST /autenticar` → token → `POST /usuario/alterar/unidade` → consultas (`/processo/consultar`, `/documento/listar/{id}`, `/documento/listar/assinaturas/{id}`, `/documento/secao/listar`, `/documento/secao/alterar`). Token expirado é renovado sozinho.
- **Chrome headless (`sei_client.py`)**: patchright com o Chrome real e perfil persistente em `~/.mcp-sei-chrome-profile`; cliente único mantido vivo entre chamadas, thread dedicada e reset automático em erro. Usado só onde a API não chega (Controle de Processos completo, PDF/ZIP consolidado).
- **Formatação (`formatar_html_sei`)**: parágrafos numerados com o número em negrito, tópicos (`I. RELATÓRIO`) em negrito, `> texto` vira `Citacao`, `EMENTA:` vira `Texto_Ementa`, `|c|`/`|d|`/`|e|` alinham; caracteres fora do ISO-8859-1 viram entidades (exigência do módulo).
- **Seções**: o SEI exige todas as seções no POST de alteração; as de só leitura (timbre, referência) vão vazias e são reconstruídas pelo sistema.

## 🔒 Segurança

- Credenciais **só no Keychain**; nada em arquivo, variável de ambiente ou log
- `stdout` é exclusivo do protocolo MCP; todo log vai para `stderr`
- As duas ferramentas que escrevem no SEI devolvem apenas a **prévia** sem `confirmar=true` — o modelo precisa mostrar ao usuário e receber aprovação explícita antes de repetir a chamada
- Não há ferramenta de assinar, tramitar, concluir, excluir ou dar ciência, e não haverá

## ⚠️ Avisos importantes

- Feito para a instância da PMT (órgão PGM). Outras instâncias do SEI com `mod-wssei` devem funcionar na parte REST trocando a URL e o id do órgão; a parte Chrome depende do layout da tela.
- O PDF consolidado gerado pelo SEI **omite documentos externos não renderizáveis** (Word etc.); para preservá-los use `formato="zip"`.
- Documentos externos (PDF anexado) não têm assinatura eletrônica listável — aparecem como não assinados na verificação rápida.
- Uso pessoal, sem vínculo com a Prefeitura de Teresina ou com o Ministério da Gestão (mantenedor do SEI).

## 📄 Licença

MIT
