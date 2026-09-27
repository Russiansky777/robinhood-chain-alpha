#!/usr/bin/env python3
"""Наши живые покупки на Pump AMM: подписи для разбора по лампортам.

ЗАЧЕМ (пункт 7 владельца 27.09). Нужна доля траты, реально дошедшей до пула,
ПО НАШИМ покупкам -- против ожидания 0.988 и против списков второй сессии.
Здесь только собираются подписи: кошелёк, минт, размер, тип пула и метки
чаевых с приоритетом из позиции. Сам разбор по лампортам -- на бегунке.

Строки позиции дописываются кусками, поэтому два прохода по cid.
Только чтение. Измерительный код -- без самопроверок (Правило 8).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import time

ИМЕНА_PUMP = ("PUMP", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def пути(каталог: str, имя: str) -> list:
    основной = os.path.join(каталог, имя)
    обороты = []
    try:
        for ф in sorted(os.listdir(каталог)):
            if ф.startswith(имя + ".") and ф.endswith(".gz"):
                обороты.append(os.path.join(каталог, ф))
    except OSError:
        pass
    return обороты + ([основной] if os.path.exists(основной) else [])


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--out", default="/tmp/nashi_pump_amm.json")
    р.add_argument("--predel", type=int, default=200)
    а = р.parse_args()

    по_cid: dict = {}
    строк = 0
    for путь in пути(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            строк += 1
            cid = з.get("client_order_id")
            if not cid:
                continue
            по_cid.setdefault(cid, {}).update(
                {к: v for к, v in з.items() if v is not None})

    ряды, по_программам = [], {}
    for cid, поз in по_cid.items():
        прог = поз.get("program") or поз.get("pool_program")
        по_программам[str(прог)] = по_программам.get(str(прог), 0) + 1
        if str(прог) not in ИМЕНА_PUMP:
            continue
        подпись = поз.get("lane_signature") or поз.get("lane_signature_local")
        кошелёк = поз.get("lane_wallet") or поз.get("wallet")
        if not (подпись and кошелёк):
            continue
        ряды.append({
            "cid": cid, "signature": подпись, "wallet": кошелёк,
            "mint": поз.get("mint"), "program": прог,
            "sol_in": поз.get("sol_in") or поз.get("lane_sol"),
            "utc": поз.get("ts_intent") or поз.get("ts_utc"),
            "чаевые_из_позиции_sol": поз.get("lane_tips_total_sol"),
            "приоритет_из_позиции_lamports": поз.get("lane_priority_lamports"),
            "state": поз.get("state"),
        })
    ряды.sort(key=lambda x: str(x.get("utc") or ""))
    ряды = ряды[-а.predel:]

    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "строк_прочитано": строк, "позиций_всего": len(по_cid),
            "по_программам": dict(sorted(по_программам.items(),
                                          key=lambda x: -x[1])[:12]),
            "наших_pump_amm": len(ряды), "ряды": ряды}
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    print(f"строк {строк}, позиций {len(по_cid)}, наших Pump AMM {len(ряды)}")
    print("по программам:", json.dumps(итог["по_программам"], ensure_ascii=False))
    for р_ in ряды[-8:]:
        print(f"  {р_['utc']} {(р_['mint'] or '-')[:10]} {р_['sol_in']} "
              f"{р_['signature'][:12]} {р_['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
