"""Teste manual (SÓ LEITURA) da armadilha "unidade ativa é do usuário, não do token".

Cenário: duas sessões `SeiRest` em unidades diferentes intercalando consultas a
  - P1: processo aberto SÓ na unidade A (ex.: PROC-PRFMAP-PGM)
  - P2: processo aberto na unidade B (ex.: PROC-PRFMAP-CHEFIA-PGM)
Os dois números e as duas siglas vêm da linha de comando — nenhum processo
real fica gravado no repositório.

Fases:
  1. Reproduz a armadilha com chamadas cruas (`_req`, sem reasserção): depois
     que a sessão CHEFIA troca a unidade, a sessão PRFMAP deixa de ver o P1.
  2. Mesmo intercalamento pelo caminho protegido (`consultar_processo`,
     `listar_documentos`, `listar_assinaturas`, `get` usado por sei_docs):
     tudo deve passar.
  3. Duas threads no MESMO processo martelando as duas sessões ao mesmo tempo
     (lock compartilhado → zero repetições esperadas).
  4. Um SUBPROCESSO em CHEFIA martelando enquanto este processo opera em PRFMAP
     (sem lock comum → a repetição 1x é que salva; conta quantas vezes).
  5. Custo: `verificar_pareceres` com reasserção por operação.

Rodar dentro do repositório:
  uv run python scripts/teste_unidade_concorrente.py --p1 NUP_A --p2 NUP_B \\
      [--unidade-a PROC-PRFMAP-PGM] [--unidade-b PROC-PRFMAP-CHEFIA-PGM]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time

from sei_mcp.sei_rest import SeiRest, SeiRestError

# Preenchidos pela linha de comando em main() — ver docstring.
P1 = ""       # processo aberto só na unidade A
P2 = ""       # processo aberto na unidade B
PRFMAP = ""   # sigla da unidade A
CHEFIA = ""   # sigla da unidade B


def _args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--p1", required=True, help="NUP de processo aberto SÓ na unidade A")
    ap.add_argument("--p2", required=True, help="NUP de processo aberto na unidade B")
    ap.add_argument("--unidade-a", default="PROC-PRFMAP-PGM", help="sigla da unidade A (default: PROC-PRFMAP-PGM)")
    ap.add_argument("--unidade-b", default="PROC-PRFMAP-CHEFIA-PGM", help="sigla da unidade B (default: PROC-PRFMAP-CHEFIA-PGM)")
    return ap.parse_args()

falhas: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FALHA") + " " + msg, flush=True)
    if not cond:
        falhas.append(msg)


def titulo(t: str) -> None:
    print(f"\n=== {t}", flush=True)


def main() -> int:
    global P1, P2, PRFMAP, CHEFIA
    ns = _args()
    P1, P2, PRFMAP, CHEFIA = ns.p1, ns.p2, ns.unidade_a, ns.unidade_b
    a = SeiRest(unidade_sigla=PRFMAP)
    b = SeiRest(unidade_sigla=CHEFIA)

    # ------------------------------------------------------------------ 1
    titulo("1. Reproduzir a armadilha com chamadas cruas (sem reasserção)")
    a._assegurar_unidade()
    j = a._req("GET", "/processo/consultar", params={"protocoloFormatado": P1})
    ok(bool(j.get("data", {}).get("IdProcedimento")), f"A/PRFMAP cru vê {P1}")
    b._assegurar_unidade()            # B troca a unidade do USUÁRIO para CHEFIA
    try:
        a._req("GET", "/processo/consultar", params={"protocoloFormatado": P1})
        ok(False, f"A/PRFMAP cru ainda vê {P1} após B trocar (armadilha NÃO reproduzida?)")
    except SeiRestError as e:
        ok("não encontrado" in str(e).lower(), f"A/PRFMAP cru perdeu {P1} após B trocar: {e}")

    # ------------------------------------------------------------------ 2
    titulo("2. Intercalamento pelo caminho protegido (10 rodadas)")
    t0 = time.time()
    id1 = a.consultar_processo(P1)["IdProcedimento"]
    id2 = b.consultar_processo(P2)["IdProcedimento"]
    docs1 = a.listar_documentos(id1)
    docs2 = b.listar_documentos(id2)
    ok(len(docs1) > 0, f"A listou {len(docs1)} docs de {P1}")
    ok(len(docs2) > 0, f"B listou {len(docs2)} docs de {P2}")
    interno1 = next((d["id"] for d in docs1 if ((d.get("atributos") or {}).get("tipoDocumento") or "").upper() == "I"), None)
    interno2 = next((d["id"] for d in docs2 if ((d.get("atributos") or {}).get("tipoDocumento") or "").upper() == "I"), None)
    erros = 0
    for i in range(10):
        try:
            a.consultar_processo(P1)
            b.consultar_processo(P2)
            if interno1:
                a.listar_assinaturas(interno1)
            if interno2:
                b.listar_assinaturas(interno2)
            if interno1:
                a.get("/documento/secao/listar", {"id": interno1})     # caminho do sei_docs
            if interno2:
                b.get("/documento/secao/listar", {"id": interno2})
        except SeiRestError as e:
            erros += 1
            print(f"    rodada {i}: {e}")
    ok(erros == 0, f"10 rodadas intercaladas sem erro ({time.time() - t0:.1f}s); "
                   f"repetições por unidade: A={a.estatisticas['repeticoes_por_unidade']} B={b.estatisticas['repeticoes_por_unidade']}")

    # P1 a partir da CHEFIA deve falhar com mensagem clara (não silenciosamente)
    try:
        b.consultar_processo(P1)
        ok(False, f"B/CHEFIA viu {P1} (esperava 'não encontrado')")
    except SeiRestError as e:
        ok(CHEFIA in str(e) and "não encontrado" in str(e).lower(), f"B/CHEFIA não vê {P1}, erro nomeia a unidade: {e}")

    # ------------------------------------------------------------------ 3
    titulo("3. Duas threads no mesmo processo (lock compartilhado)")
    antes = (a.estatisticas["repeticoes_por_unidade"], b.estatisticas["repeticoes_por_unidade"])
    erros_t: list[str] = []

    def martelar(sess: SeiRest, nup: str, interno: str | None, n: int) -> None:
        for _ in range(n):
            try:
                sess.consultar_processo(nup)
                if interno:
                    sess.listar_assinaturas(interno)
            except SeiRestError as e:
                erros_t.append(f"{sess.unidade_sigla}: {e}")

    t0 = time.time()
    ta = threading.Thread(target=martelar, args=(a, P1, interno1, 8))
    tb = threading.Thread(target=martelar, args=(b, P2, interno2, 8))
    ta.start(); tb.start(); ta.join(); tb.join()
    rep = (a.estatisticas["repeticoes_por_unidade"] - antes[0], b.estatisticas["repeticoes_por_unidade"] - antes[1])
    ok(not erros_t, f"2 threads × 8 rodadas sem erro ({time.time() - t0:.1f}s); repetições A={rep[0]} B={rep[1]} (esperado 0/0)")
    for e in erros_t[:5]:
        print("    ", e)

    # ------------------------------------------------------------------ 4
    titulo("4. Subprocesso em CHEFIA martelando enquanto este processo opera em PRFMAP")
    codigo = (
        "from sei_mcp.sei_rest import SeiRest\n"
        f"s = SeiRest(unidade_sigla={CHEFIA!r})\n"
        "import time\n"
        "for _ in range(12):\n"
        f"    s.consultar_processo({P2!r})\n"
        "print('SUB repeticoes', s.estatisticas['repeticoes_por_unidade'])\n"
    )
    sub = subprocess.Popen([sys.executable, "-c", codigo], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    antes_a = a.estatisticas["repeticoes_por_unidade"]
    erros_sub = 0
    t0 = time.time()
    for _ in range(12):
        try:
            a.consultar_processo(P1)
            if interno1:
                a.listar_assinaturas(interno1)
        except SeiRestError as e:
            erros_sub += 1
            print("    ", e)
    saida_sub, _ = sub.communicate(timeout=300)
    rep_a = a.estatisticas["repeticoes_por_unidade"] - antes_a
    ok(erros_sub == 0, f"12 rodadas em PRFMAP com subprocesso CHEFIA concorrente, sem erro ({time.time() - t0:.1f}s); "
                       f"repetições 1x acionadas aqui: {rep_a}; {saida_sub.strip() or 'SUB sem saída'}")

    # ------------------------------------------------------------------ 5
    titulo("5. Custo de verificar_pareceres com reasserção por operação")
    t0 = time.time()
    pareceres = a.verificar_pareceres(P1)
    dt = time.time() - t0
    print(f"  {P1}: {len(pareceres)} pareceres em {dt:.1f}s; "
          f"alterações de unidade acumuladas na sessão A: {a.estatisticas['alteracoes_unidade']}")

    print("\nRESUMO:", "TUDO OK" if not falhas else f"{len(falhas)} falha(s)")
    for f in falhas:
        print("  -", f)
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main())
