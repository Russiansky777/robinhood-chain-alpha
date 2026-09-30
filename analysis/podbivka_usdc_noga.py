#!/usr/bin/env python3
"""Образцы для Code-3: сигналы «USDC на других типах» (котировки денежных групп 28–30.09) с сырыми транзакциями.

Вход -- data/podbivka/kotirovki_grupp_2026-10-01.json (analysis/podbivka_kotirovki_arhiv.py --svod): сигналы по
правилу pyg7 с котировкой USDC в пулах НЕ Raydium CPMM и НЕ Pump AMM. По каждому: подпись, группа, источник, программа
пула (адрес программы по типу пула архива), адрес пула, минт токена, сумма в USDC и SOL-экв., слот -- и сырая
транзакция покупки getTransaction (encoding json, maxSupportedTransactionVersion 1, Helius; частота -- PODB_HELIUS_RPS).
Отдельно -- число сигналов по типам: CLMM / DLMM / DAMM v2 / Whirlpool / AMM v4 / прочее (Whirlpool и AMM v4 в архиве
PumpApi нет -- там 0 по построению, не по факту).
Выход: data/podbivka/usdc_noga_dlya_code3.json.
"""
from __future__ import annotations

import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ДВУХШАГОВЫЕ = {"raydium-cpmm", "pump-amm"}
ПРОГРАММЫ = {"raydium-clmm": ("CLMM", "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"),
             "meteora-dlmm": ("DLMM", "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"),
             "meteora-damm-v2": ("DAMM v2", "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"),
             "orca-whirlpool": ("Whirlpool", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"),
             "raydium-amm": ("AMM v4", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"),
             "meteora-damm-v1": ("прочее", "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB"),
             "meteora-launchpad": ("прочее", "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"),
             "raydium-launchpad": ("прочее", "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"),
             "pump": ("прочее", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")}
ТИПЫ = ("CLMM", "DLMM", "DAMM v2", "Whirlpool", "AMM v4", "прочее")


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    д = json.loads((П / "kotirovki_grupp_2026-10-01.json").read_text(encoding="utf-8"))
    сс = [с for с in д["сигналы"] if с["quoteMint"] == USDC and с["pool"] not in ДВУХШАГОВЫЕ]
    уз = S.Узел()
    строки = []
    with уз.на("helius"):
        for с in сс:
            тип, прог = ПРОГРАММЫ.get(с["pool"], ("прочее", None))
            try:
                tx = уз.вызов("getTransaction", [с["signature"], {"encoding": "json", "commitment": "confirmed",
                                                                   "maxSupportedTransactionVersion": 1}], срок=40.0)
                why = None if tx else "узел вернул пусто"
            except RuntimeError as exc:
                tx, why = None, str(exc)[:160]
            строки.append({"signature": с["signature"], "группа": с["группа"], "источник": с["trader"],
                           "тип": тип, "пул_архива": с["pool"], "программа_пула": прог, "пул": с["poolId"],
                           "минт_токена": с["mint"], "usdc": с["quoteAmount"], "sol_экв": с["sol_экв"],
                           "слот": с["block"], "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((с["timestamp"] or 0) / 1000)),
                           "tx": tx, "tx_why_not": why})
    по_типам = collections.Counter(x["тип"] for x in строки)
    out = П / "usdc_noga_dlya_code3.json"
    out.write_text(json.dumps({
        "что": "сигналы «USDC на других типах» (не Raydium CPMM / Pump AMM) денежных групп 28–30.09, правило pyg7, "
               "с сырыми транзакциями покупки (getTransaction, encoding json, maxSupportedTransactionVersion 1)",
        "источник": "docs/podbivka_2026-10-01_kotirovki_grupp.md, data/podbivka/kotirovki_grupp_2026-10-01.json",
        "по_типам": {т: по_типам.get(т, 0) for т in ТИПЫ},
        "оговорка": "Whirlpool и Raydium AMM v4 в архиве PumpApi нет -- их 0 по построению, не по факту",
        "сигналов": len(строки), "без_транзакции": sum(1 for x in строки if not x["tx"]),
        "сигналы": строки, "расход": уз.расход()}, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    print("сигналов", len(строки), "по типам", dict(по_типам), "без tx", sum(1 for x in строки if not x["tx"]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
