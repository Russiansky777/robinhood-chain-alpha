#!/usr/bin/env python3
"""Две строки к таблице 22 (задание владельца 26.09, ночь).

1) "порог источника" x4: сколько источник купил В SOL-ЭКВИВАЛЕНТЕ по НАШЕМУ
   счёту и по журналу DBot, и в чём разница -- валюта сделки, курс, порог задачи.
2) "сборка: тип пула" x3 и "котировка" x3: программа и адрес пула по каждой,
   чтобы утром выбрать сборщик.

Числа только измеренные: наши -- из журнала решений на хосте (там лежит и
сумма цели, и курс, которым мы считали), DBot -- из его журнала follow_trades
(валюта и количество, которые он сам записал).

Только чтение.
"""
import argparse
import json
import os
import re
import sys
import time

МАРКЕРЫ_ПОРОГА = ("порог", "цел", "min_target", "мелк")


def строки(путь: str):
    import gzip
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


ПОЛЯ = ("stage", "mint", "signature", "source", "action", "why_not", "why",
        "reason", "pool", "program", "pool_vault", "quote_mint", "target_sol",
        "target_usd", "sol_equiv", "rate_usd_sol", "rate_source", "amount_sol",
        "sol_in", "min_target_sol", "group", "taxed", "tax_bps",
        "token_program", "route", "t_recv_utc", "source_slot", "pool_program")


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--minty", required=True,
                   help="минты через запятую")
    р.add_argument("--out", default="/tmp/dve_stroki.json")
    а = р.parse_args()
    минты = {м.strip() for м in а.minty.split(",") if м.strip()}
    собрано = {м: [] for м in минты}
    строк = 0
    for путь in пути(а.state_dir, "decisions.jsonl"):
        for з in строки(путь):
            строк += 1
            м = з.get("mint")
            if м in собрано:
                ряд = {к: з.get(к) for к in ПОЛЯ if з.get(к) is not None}
                if ряд:
                    собрано[м].append(ряд)
    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "строк_прочитано": строк,
            "по_минту": {м: собрано[м] for м in минты}}
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    for м in минты:
        ряды = собрано[м]
        print(f"=== {м[:14]} ({len(ряды)} строк) ===")
        for ряд in ряды[:8]:
            print("   " + json.dumps(ряд, ensure_ascii=False)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
