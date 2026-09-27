#!/usr/bin/env python3
"""Кошелёк DBot по цепи: круги, свопы, чаевые, приоритет, итог по суткам.

ЗАЧЕМ (задание владельца 27.09). Свод по журналу DBot и свод по цепи могут
расходиться: журнал знает о сделках задачи, а цепь -- о том, что реально ушло с
кошелька. Здесь считается ЦЕПЬ и только цепь.

ЧТО СЧИТАЕТСЯ. Все транзакции кошелька за окно; свопом считается транзакция, где
кошелёк подписант и у него менялся остаток токена (не USDC и не WSOL). Покупка --
токен пришёл и SOL ушёл, продажа -- наоборот. Круг -- покупка и ближайшая
следующая продажа того же минта (FIFO); круг считается в те сутки, когда была
ПОКУПКА (слово владельца).

Разложение круга:
  вход     = сколько SOL ушло из кошелька на покупке МИНУС её чаевые и тариф;
  выход    = сколько SOL пришло на продаже ПЛЮС её чаевые и тариф;
  свопы    = выход - вход (чистая разница цен, без издержек доставки);
  чаевые   = переводы характерных размеров (маркеры DBot) в обеих сделках;
  приоритет= тариф транзакции минус базовые 5000 лампортов на подпись;
  итог     = сумма изменений SOL кошелька в обеих сделках (то есть свопы минус
             чаевые, приоритет и базовый тариф). Он же -- перекрёстная проверка.

Только чтение цепи. Ни ключей в отчёте, ни сделок.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

БАЗОВЫЙ_ТАРИФ_ЛАМПОРТЫ = 5_000
СМЕЩЕНИЕ_МАДРИДА_Ч = 2          # CEST
ЛАМПОРТОВ_В_SOL = 1_000_000_000


def сутки_мадрида(время_utc: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(время_utc + СМЕЩЕНИЕ_МАДРИДА_Ч * 3600))


def приоритет_sol(tx: dict) -> float:
    """Тариф минус базовые 5000 лампортов на каждую подпись."""
    мета = (tx or {}).get("meta") or {}
    тариф = int(мета.get("fee") or 0)
    подписей = len(((tx or {}).get("transaction") or {}).get("signatures") or []) or 1
    return max(0, тариф - БАЗОВЫЙ_ТАРИФ_ЛАМПОРТЫ * подписей) / ЛАМПОРТОВ_В_SOL


def базовый_sol(tx: dict) -> float:
    подписей = len(((tx or {}).get("transaction") or {}).get("signatures") or []) or 1
    return БАЗОВЫЙ_ТАРИФ_ЛАМПОРТЫ * подписей / ЛАМПОРТОВ_В_SOL


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--wallet", required=True)
    р.add_argument("--s", default="2026-09-24T22:00:00Z",
                   help="начало окна UTC (сутки 25.09 по Мадриду начинаются здесь)")
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "dbot_batch5_po_cepi.json"))
    р.add_argument("--okno-s", default="", help="окно ночи UTC для списка покупок")
    р.add_argument("--okno-po", default="")
    р.add_argument("--imena", default="yes", help="'yes' -- запросить имена минтов")
    а = р.parse_args()

    import calendar  # noqa: PLC0415

    import solana_buyer200_fast_price as fp  # noqa: PLC0415
    import solana_dbot_realized_ledger as LG  # noqa: PLC0415

    def метка(т: str) -> float:
        return calendar.timegm(time.strptime(т.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))

    с_ = метка(а.s)
    окно = (метка(а.okno_s), метка(а.okno_po)) if а.okno_s and а.okno_po else None

    подписи = LG.get_full_history(а.wallet)
    подписи = [s for s in подписи
               if (s.get("blockTime") or 0) >= с_ and s.get("err") is None]
    подписи.sort(key=lambda s: s.get("blockTime") or 0)
    print(f"подписей в окне: {len(подписи)}")

    события, все_tx = [], {}
    for и, s in enumerate(подписи, 1):
        tx = fp.get_transaction(s["signature"])
        if tx is None:
            continue
        все_tx[s["signature"]] = tx
        дельты = LG.wallet_mint_deltas(tx, а.wallet)
        sol = LG.wallet_sol_delta(tx, а.wallet) or 0.0
        тронуто = [m for m in дельты["increased"] + дельты["decreased"]
                   if m not in (LG.USDC_MINT, LG.SOL_MINT)]
        if not (тронуто and LG.is_signer(tx, а.wallet)):
            continue
        for минт in дельты["increased"]:
            if минт in (LG.USDC_MINT, LG.SOL_MINT) or sol >= -0.001:
                continue
            события.append({"signature": s["signature"], "block_time": s.get("blockTime"),
                             "mint": минт, "direction": "buy", "tx": tx, "sol": sol})
        for минт in дельты["decreased"]:
            if минт in (LG.USDC_MINT, LG.SOL_MINT) or sol <= 0.001:
                continue
            события.append({"signature": s["signature"], "block_time": s.get("blockTime"),
                             "mint": минт, "direction": "sell", "tx": tx, "sol": sol})
        if и % 50 == 0:
            print(f"  разобрано {и}/{len(подписи)}")

    пары = LG.pair_trades(события)
    по_суткам: dict = {}
    круги = []
    for п in пары:
        b, s_ = п["buy"], п.get("sell")
        день = сутки_мадрида(b["block_time"] or 0)
        в = по_суткам.setdefault(день, {
            "кругов": 0, "покупок": 0, "незакрытых": 0, "свопы_sol": 0.0,
            "чаевые_sol": 0.0, "приоритет_sol": 0.0, "базовый_sol": 0.0,
            "итог_sol": 0.0})
        в["покупок"] += 1
        чаевые_b = LG.tips_and_fee(b["tx"], а.wallet)["tip_sol"]
        приор_b = приоритет_sol(b["tx"])
        баз_b = базовый_sol(b["tx"])
        if s_ is None:
            в["незакрытых"] += 1
            continue
        чаевые_s = LG.tips_and_fee(s_["tx"], а.wallet)["tip_sol"]
        приор_s = приоритет_sol(s_["tx"])
        баз_s = базовый_sol(s_["tx"])
        вход = -b["sol"] - чаевые_b - приор_b - баз_b
        выход = s_["sol"] + чаевые_s + приор_s + баз_s
        итог = b["sol"] + s_["sol"]
        в["кругов"] += 1
        в["свопы_sol"] += выход - вход
        в["чаевые_sol"] += чаевые_b + чаевые_s
        в["приоритет_sol"] += приор_b + приор_s
        в["базовый_sol"] += баз_b + баз_s
        в["итог_sol"] += итог
        круги.append({"день": день, "mint": п["mint"],
                       "покупка_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime(b["block_time"] or 0)),
                       "вход_sol": round(вход, 6), "выход_sol": round(выход, 6),
                       "свопы_sol": round(выход - вход, 6),
                       "чаевые_sol": round(чаевые_b + чаевые_s, 6),
                       "приоритет_sol": round(приор_b + приор_s, 6),
                       "итог_sol": round(итог, 6)})
    for в in по_суткам.values():
        for к in ("свопы_sol", "чаевые_sol", "приоритет_sol", "базовый_sol", "итог_sol"):
            в[к] = round(в[к], 6)

    покупки_окна = []
    if окно:
        for е in события:
            if е["direction"] != "buy":
                continue
            т = е["block_time"] or 0
            if not (окно[0] <= т < окно[1]):
                continue
            покупки_окна.append({
                "mint": е["mint"], "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(т)),
                "вход_sol": round(-е["sol"], 6), "signature": е["signature"]})
        if а.imena == "yes":
            try:
                import bloom_token_name as TN  # noqa: PLC0415

                for п_ in покупки_окна:
                    п_["имя"] = TN.имя(п_["mint"], lambda m, p=None, **kw:
                                        fp.rpc_call(m, p if isinstance(p, list) else [p]))
            except Exception as exc:  # noqa: BLE001
                print(f"имена минтов не получены: {type(exc).__name__}", file=sys.stderr)

    свод = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "кошелёк": а.wallet, "окно_с": а.s,
             "подписей_разобрано": len(подписи),
             "событий": len(события), "пар": len(пары),
             "по_суткам_мадрида": dict(sorted(по_суткам.items())),
             "круги": круги,
             "покупки_окна": покупки_окна,
             "как_считано": ("круг -- покупка и ближайшая следующая продажа того же "
                              "минта; сутки -- по времени ПОКУПКИ (Мадрид, CEST); "
                              "итог = сумма изменений SOL кошелька в обеих сделках")}
    Path(а.out).write_text(json.dumps(свод, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    печать = {к: v for к, v in свод.items() if к not in ("круги",)}
    print(json.dumps(печать, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
