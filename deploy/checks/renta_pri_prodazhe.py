#!/usr/bin/env python3
"""Вернулась ли рента при продаже: записи close_on_sell из журнала позиций.

ЗАЧЕМ (пункт 2 владельца 27.09). Путь закрытия токен-счёта при продаже
(BLOOM_CLOSE_ON_SELL) пишет свой итог В ПОЗИЦИЮ полем close_on_sell: сколько
счетов закрыто, сколько лампортов вернулось, подпись и причина отказа. Здесь это
поле достаётся по журналу потоком и печатается по сделкам.

ВАЖНО ПРО ЖУРНАЛ. Строки позиции дописываются кусками: в первой есть lane и
время замысла, в последующих -- только client_order_id и изменившиеся поля.
Поэтому два прохода: сначала собрать cid, потом склеить все поля по cid.

Только чтение. Измерительный код -- без самопроверок (Правило 8).
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import json
import os
import time


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


def метка(т: str) -> float:
    т = (т or "").replace("Z", "")
    try:
        return calendar.timegm(time.strptime(т, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="", help="только позиции с этого UTC")
    р.add_argument("--out", default="/tmp/renta_pri_prodazhe.json")
    а = р.parse_args()
    с_ = метка(а.s) if а.s else 0.0

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

    ряды = []
    for cid, поз in по_cid.items():
        т = поз.get("ts_intent") or поз.get("ts_utc") or ""
        if с_ and метка(т) < с_:
            continue
        зак = поз.get("close_on_sell")
        ряды.append({
            "cid": cid, "utc": т, "lane": поз.get("lane"),
            "mint": поз.get("mint"), "state": поз.get("state"),
            "закрытие_было": зак is not None,
            "ok": (зак or {}).get("ok"),
            "счетов_закрыто": (зак or {}).get("accounts_closed"),
            "рента_sol": (зак or {}).get("rent_returned_sol"),
            "рента_ожидалась_sol": (зак or {}).get("rent_expected_sol"),
            "подпись_закрытия": (зак or {}).get("signature"),
            "почему_нет": (зак or {}).get("why_not"),
        })
    ряды.sort(key=lambda x: x["utc"] or "")

    с_закрытием = [р_ for р_ in ряды if р_["закрытие_было"]]
    вернулось = sum(float(р_["рента_sol"] or 0) for р_ in с_закрытием)
    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "строк_прочитано": строк, "позиций_в_окне": len(ряды),
            "с_записью_закрытия": len(с_закрытием),
            "закрытий_ок": sum(1 for р_ in с_закрытием if р_["ok"]),
            "рента_вернулась_sol": round(вернулось, 9),
            "ряды": ряды[-40:]}
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)

    print(f"строк {строк}, позиций в окне {len(ряды)}, "
          f"с записью закрытия {len(с_закрытием)}, ок {итог['закрытий_ок']}, "
          f"рента вернулась {итог['рента_вернулась_sol']} SOL")
    for р_ in ряды[-12:]:
        print(f"  {р_['utc']} {(р_['mint'] or '-')[:10]} {р_['state']} "
              f"закрытие={р_['закрытие_было']} ok={р_['ok']} "
              f"счетов={р_['счетов_закрыто']} рента={р_['рента_sol']} "
              f"{(р_['почему_нет'] or '')[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
