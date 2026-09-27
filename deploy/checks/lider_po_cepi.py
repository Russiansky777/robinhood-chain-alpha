#!/usr/bin/env python3
"""Лидер по цепи: что он покупал в окне и есть ли среди этого наши минты.

ЗАЧЕМ (пункт 0 владельца, утро 27.09). По журналу решений видно только то, что
детектор записал. Чтобы честно ответить "почему по лидеру нет строки решения",
надо сначала узнать, была ли у него вообще покупка этого минта. Это и считается
здесь -- по цепи, без журнала.

ЧТО СЧИТАЕТСЯ. Все транзакции кошелька в окне; покупка -- токен (не USDC и не
WSOL) пришёл, а SOL ушёл. Подпись -- пометка, а не условие: у части
снайперов подписывает отдельный плательщик, а токен идёт владельцу.
На выходе: список покупок окна
(минт, SOL, подпись, время) и отметка по каждому запрошенному минту.

Только чтение цепи. Измерительный код -- без самопроверок (Правило 8).
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))


def метка(т: str) -> float:
    return calendar.timegm(time.strptime(т.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--wallet", required=True)
    р.add_argument("--okno-s", required=True)
    р.add_argument("--okno-po", required=True)
    р.add_argument("--minty", default="", help="минты через запятую -- по ним отметка")
    р.add_argument("--pary", default="",
                   help="подпись:источник через запятую -- размер покупки по цепи")
    р.add_argument("--out", required=True)
    а = р.parse_args()

    import solana_buyer200_fast_price as fp  # noqa: PLC0415
    import solana_dbot_realized_ledger as LG  # noqa: PLC0415

    с_, по_ = метка(а.okno_s), метка(а.okno_po)
    спрошено = [м.strip() for м in а.minty.split(",") if м.strip()]

    подписи = LG.get_full_history(а.wallet)
    в_окне = [s for s in подписи
              if с_ <= (s.get("blockTime") or 0) < по_ and s.get("err") is None]
    в_окне.sort(key=lambda s: s.get("blockTime") or 0)
    print(f"подписей у кошелька всего: {len(подписи)}, в окне: {len(в_окне)}")

    покупки, продажи, прочее, не_достали = [], [], 0, 0
    не_подписант, без_токенов = 0, 0
    for и, s in enumerate(в_окне, 1):
        tx = fp.get_transaction(s["signature"])
        if tx is None:
            не_достали += 1
            continue
        дельты = LG.wallet_mint_deltas(tx, а.wallet)
        sol = LG.wallet_sol_delta(tx, а.wallet) or 0.0
        # ПОДПИСАНТ -- НЕ УСЛОВИЕ, А ПОМЕТКА. У части снайперов подписывает
        # отдельный кошелёк-плательщик, а токен приходит владельцу; если
        # требовать подписи, такие покупки молча пропадают из счёта.
        подписант = LG.is_signer(tx, а.wallet)
        if not подписант:
            не_подписант += 1
        if not (дельты["increased"] or дельты["decreased"]):
            без_токенов += 1
        было = False
        for минт in дельты["increased"]:
            if минт in (LG.USDC_MINT, LG.SOL_MINT) or sol >= -0.001:
                continue
            покупки.append({"mint": минт, "sol": round(-sol, 9),
                             "подписант": подписант,
                             "signature": s["signature"],
                             "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(s.get("blockTime") or 0)),
                             "slot": tx.get("slot")})
            было = True
        for минт in дельты["decreased"]:
            if минт in (LG.USDC_MINT, LG.SOL_MINT) or sol <= 0.001:
                continue
            продажи.append({"mint": минт, "sol": round(sol, 9),
                             "подписант": подписант,
                             "signature": s["signature"],
                             "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(s.get("blockTime") or 0))})
            было = True
        if not было:
            прочее += 1
        if и % 25 == 0:
            print(f"  разобрано {и}/{len(в_окне)}")

    # РАЗМЕР ПОКУПКИ ИСТОЧНИКА ПО ЦЕПИ. Журнал решений на проходных строках
    # размер не пишет, поэтому берём его из транзакции: сколько источник отдал
    # нативного SOL и сколько стейблов. SOL-эквивалент по стейблам без курса не
    # считаем -- отдаём обе величины как есть.
    размеры = []
    for пара in [x.strip() for x in а.pary.split(",") if x.strip()]:
        подпись, _, ист = пара.partition(":")
        tx = fp.get_transaction(подпись)
        if tx is None:
            размеры.append({"signature": подпись, "источник": ист,
                             "почему": "getTransaction не отдал транзакцию"})
            continue
        sol = LG.wallet_sol_delta(tx, ист)
        дельты = LG.wallet_mint_deltas(tx, ист)
        d = дельты.get("deltas") or {}
        размеры.append({
            "signature": подпись, "источник": ист,
            "нативный_sol_отдал": (round(-sol, 9) if sol is not None else None),
            "usdc_отдал": (abs(float(d[LG.USDC_MINT]))
                            if LG.USDC_MINT in d and float(d[LG.USDC_MINT]) < 0 else None),
            "wsol_отдал": (abs(float(d[LG.SOL_MINT]))
                            if LG.SOL_MINT in d and float(d[LG.SOL_MINT]) < 0 else None),
            "токены_пришли": дельты["increased"],
            "подписант": LG.is_signer(tx, ист),
        })

    купленные = {п["mint"] for п in покупки}
    отметка = {м: ("купил" if м in купленные else "не покупал") for м in спрошено}
    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "кошелёк": а.wallet, "окно": [а.okno_s, а.okno_po],
            "подписей_в_окне": len(в_окне), "покупок": len(покупки),
            "продаж": len(продажи), "прочих_транзакций": прочее,
            "не_достали_транзакцию": не_достали,
            "где_кошелёк_не_подписант": не_подписант,
            "без_движения_токенов": без_токенов,
            "покупки": покупки, "продажи": продажи,
            "размеры_покупок_источников": размеры,
            "по_спрошенным_минтам": отметка}
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"покупок в окне: {len(покупки)}, продаж: {len(продажи)}, "
          f"прочих {прочее}, не достали {не_достали}, "
          f"не подписант {не_подписант}, без движения токенов {без_токенов}")
    for м, v in отметка.items():
        print(f"  {м[:12]}: {v}")
    for р_ in размеры:
        print(f"  размер {р_['signature'][:10]} источник {р_['источник'][:10]}: "
              f"нативный {р_.get('нативный_sol_отдал')} "
              f"wsol {р_.get('wsol_отдал')} usdc {р_.get('usdc_отдал')}"
              + (f" -- {р_['почему']}" if р_.get("почему") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
