#!/usr/bin/env python3
"""Подбивка: первые покупки лидера с котировкой пула «другое» -- чем котируются
и что дала бы наша 0.5 SOL через котировочный токен.

Шаг 1 (этот файл, --shag 1): по каждой первой покупке лидера от 2 SOL-экв из
data/podbivka/p1b2_sverka.json -- транзакция, пул (c2_common.identify_pool),
минт котировки; для «другого» -- какие встречные плечи этого минта есть в той
же транзакции (к WSOL, к USDC/USDT): это цена котировочного на тот же слот.
Выход: data/podbivka/lider_kotirovka.json, топ-5 котировочных минтов.
Узел -- по скользящей грани (старше «сейчас - 2.4 суток» -- Helius).
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ЛИДЕР = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"


def плечи(tx: dict, q: str) -> dict:
    """Встречные плечи минта q в инструкциях DEX (счета не подписантов)."""
    sg = C.signers(tx)
    ряды = [r for r in C.token_rows(tx).values() if r["account"] and r["owner"] not in sg]
    из_: dict = {}
    for s in C.instruction_account_sets(tx):
        qq = [r for r in ряды if r["mint"] == q and r["account"] in s and r["post"] != r["pre"]]
        for a in qq:
            for b in ряды:
                if b["account"] not in s or b["mint"] == q or b["post"] == b["pre"]:
                    continue
                da, db = a["post"] - a["pre"], b["post"] - b["pre"]
                if (da > 0) == (db > 0):
                    continue
                if b["mint"] in (C.WSOL, C.USDC, C.USDT):
                    к = "WSOL" if b["mint"] == C.WSOL else "USD"
                    # цена q в котировке b: |db|/|da| в сырых единицах
                    из_.setdefault(к, []).append({"b_за_q_raw": abs(db) / abs(da), "dec_q": a.get("dec"),
                                                   "dec_b": b.get("dec")})
    return из_


def main() -> int:
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "p1b2_sverka.json").read_text(encoding="utf-8"))
    L = д["по_источнику"]["leader"]
    покупки = [{"sig": x["source_sig"], "mint": x["mint"], "utc": x["utc"]} for x in L["список_совпало"]] + \
              [{"sig": x["source_sig"], "mint": x["mint"], "utc": x["utc"]} for x in L["список_у_симулятора_без_DBot"]]
    уз = S.Узел()
    ряды, счёт = [], collections.Counter()
    import calendar
    import time
    for п in покупки:
        bt = calendar.timegm(time.strptime(п["utc"], "%Y-%m-%dT%H:%M:%SZ"))
        with уз.на(S.узел_по_времени(bt)):
            try:
                tx = уз.tx(п["sig"])
            except RuntimeError as exc:
                ряды.append({**п, "why_not": S.чисто(str(exc))[:120]})
                continue
        уз._кэш.clear()  # noqa: SLF001
        if not tx:
            ряды.append({**п, "why_not": "узел не отдал"})
            continue
        пул = C.identify_pool(tx, ЛИДЕР, п["mint"])
        q = пул.get("quote_mint")
        р = {**п, "quote_mint": q, "pool_vault": пул.get("pool_vault"), "slot": tx.get("slot"),
             "узел": S.узел_по_времени(bt)}
        if q and q not in (C.WSOL, C.NATIVE_QUOTE, C.USDC, C.USDT):
            счёт[q] += 1
            пл = плечи(tx, q)
            р["плечи_котировочного"] = {к: len(v) for к, v in пл.items()}
        ряды.append(р)
    топ = [{"quote_mint": q, "покупок": n} for q, n in счёт.most_common(5)]
    со_плечом = collections.Counter()
    for р in ряды:
        for к in (р.get("плечи_котировочного") or {}):
            со_плечом[к] += 1
    итог = {"покупок": len(покупки), "другое": sum(счёт.values()), "топ5": топ,
            "с_плечом_в_той_же_сделке": dict(со_плечом), "ряды": ряды, "расход": уз.расход()}
    (КОРЕНЬ / "data" / "podbivka" / "lider_kotirovka.json").write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                                                        encoding="utf-8")
    print("лидер, котировка «другое»:", json.dumps({к: итог[к] for к in ("покупок", "другое", "топ5",
                                                                           "с_плечом_в_той_же_сделке")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
