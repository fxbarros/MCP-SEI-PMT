"""Salva credenciais do SEI-PMT no Keychain do macOS.

Service name: mcp-sei
Itens armazenados:
  - usuario  (login do SIP)
  - senha    (senha do SIP)
  - orgao    (sigla do órgão, ex: PGM)
  - unidade  (sigla da unidade alvo, ex: PROC-PRFMAP-PGM) — usada pra
              garantir que estamos na caixa certa antes de listar/baixar

Rode uma vez: uv run setup_credenciais.py
"""

from __future__ import annotations

import getpass
import sys

import keyring

SERVICE = "mcp-sei"


def _set(key: str, value: str) -> None:
    keyring.set_password(SERVICE, key, value)
    print(f"  [{key}] salvo no Keychain (service={SERVICE})")


def main() -> int:
    print(f"Configuração de credenciais para o SEI-PMT (service={SERVICE})")
    print("Os dados ficam no Keychain do macOS, nunca em arquivo.\n")

    usuario = input("Usuário do SIP: ").strip()
    if not usuario:
        print("ERRO: usuário vazio.", file=sys.stderr)
        return 1

    senha = getpass.getpass("Senha do SIP: ")
    if not senha:
        print("ERRO: senha vazia.", file=sys.stderr)
        return 1

    orgao = input("Órgão [PGM]: ").strip() or "PGM"
    unidade = input("Unidade alvo [PROC-PRFMAP-PGM]: ").strip() or "PROC-PRFMAP-PGM"

    _set("usuario", usuario)
    _set("senha", senha)
    _set("orgao", orgao)
    _set("unidade", unidade)

    print("\nOK. Reinicie o Claude Desktop e peça: \"lista meus pendentes do SEI\".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
