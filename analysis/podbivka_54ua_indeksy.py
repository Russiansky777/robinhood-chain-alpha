#!/usr/bin/env python3
"""Подбивка: 54uaRuJE -- «его индекс в блоке − индекс покупателя того же минта» (обе стороны).

Сбор снайперов (podbivka_snaipery.py) брал источники только ДО него в блоке, поэтому
разность ≤ 0 там невозможна по построению. Здесь -- блок слота его покупки целиком
(getBlock jsonParsed, transactionDetails "accounts": ключи + балансы, без инструкций):
все покупатели того же минта в этом слоте и их индексы, до и после него.
Покупки -- те же 279, что в docs/podbivka_2026-09-28_snaipery.md (поправить() сводки).
Только чтение. Выход: data/podbivka/snaipery/54ua_indeksy.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_sim as S  # noqa: E402
import podbivka_snaipery_svod as SV  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
КОШ = "54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE"
ВХОД = КОРЕНЬ / "data" / "podbivka" / "snaipery" / f"{КОШ}_168ч.json"
ВЫХОД = КОРЕНЬ / "data" / "podbivka" / "snaipery" / "54ua_indeksy.json"


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    д = json.loads(ВХОД.read_text(encoding="utf-8"))
    сд, _ = SV.поправить(д["сделки"])
    пок = [с for с in сд if с["сторона"] == "buy" and not с.get("многоминтовая")]
    уз = S.Узел()
    по_слоту: dict = {}
    for с in пок:
        по_слоту.setdefault(с["slot"], []).append(с)
    рез, ошибки = [], []
    for n, (слот, сс) in enumerate(sorted(по_слоту.items())):
        with уз.на("helius"):          # Shyft getBlock отдаёт блок без транзакций v1 (проверено 29.09)
            try:
                б = уз.вызов("getBlock", [слот, {"encoding": "jsonParsed", "transactionDetails": "accounts",
                                                 "rewards": False, "maxSupportedTransactionVersion": 1,
                                                 "commitment": "confirmed"}], срок=60.0)
            except RuntimeError as exc:
                ошибки.append(f"{слот}: {S.чисто(str(exc))[:120]}")
                continue
        тт = (б or {}).get("transactions") or []
        минты = {с["mint"] for с in сс}
        покупатели: dict = {m: [] for m in минты}
        его: dict = {}
        for инд, т in enumerate(тт):
            подп = ((т.get("transaction") or {}).get("signatures") or [None])[0]
            for m in минты:
                for w in C.mint_buyers(т, m):
                    if w == КОШ:
                        его.setdefault(подп, инд)
                        continue
                    ld = C.lamport_delta(т, w)
                    покупатели[m].append({"адрес": w, "индекс": инд, "подпись": подп,
                                          "sol": -ld / 1e9 if ld is not None else None})
        for с in сс:
            i0 = его.get(с["signature"])
            рез.append({"signature": с["signature"], "slot": слот, "mint": с["mint"], "sol_экв": -с["sol_экв"],
                        "индекс_сбор": с.get("индекс"), "индекс_блок": i0, "в_блоке": len(тт),
                        "покупатели": [dict(x, разность=(i0 - x["индекс"]) if i0 is not None else None)
                                       for x in покупатели[с["mint"]]]})
        if n % 25 == 0:
            print(f"{n}/{len(по_слоту)} слотов, ошибок {len(ошибки)}", flush=True)
    ВЫХОД.write_text(json.dumps({"кошелёк": КОШ, "покупок": len(пок), "слотов": len(по_слоту), "ошибки": ошибки,
                                 "покупки": рез, "расход": уз.расход()}, ensure_ascii=False, default=str),
                     encoding="utf-8")
    R.записано(ВЫХОД)
    R.пуш("Podbivka-2: 54uaRuJE indeksy v bloke (obe storony) [automated]", [str(ВЫХОД)])
    print(json.dumps(уз.расход(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
