#!/usr/bin/env python3
"""Сколько источник купил по журналу DBot -- по каждому минту.

Вторая половина первой строки к таблице 22: у DBot в записи лежит, ЧТО и
СКОЛЬКО отдал источник (follow.send: валюта и количество) и что получил сам
DBot. Из этого видно, в какой валюте была сделка источника -- а именно валюта
и курс объясняют, почему наш порог в SOL-эквиваленте её не пропустил.

Только чтение файла журнала, который уже лежит в репозитории.
"""
import argparse
import json
import re
import sys

ДЕСЯТИЧНЫЕ = {"So11111111111111111111111111111111111111112": 9}


def записи(путь: str):
    глуб, буф = 0, []
    with open(путь, encoding="utf-8") as ф:
        ф.readline()
        строка = ф.readline()
        while строка:
            if глуб == 0:
                if re.match(r'\s*"[^"]+":\s*\{', строка):
                    глуб, буф = 1, ["{"]
                строка = ф.readline()
                continue
            буф.append(строка)
            глуб += строка.count("{") - строка.count("}")
            if глуб <= 0:
                try:
                    з = json.loads("".join(буф).rstrip().rstrip(","))
                except ValueError:
                    з = None
                if з:
                    yield з.get("record") or {}
                глуб, буф = 0, []
            строка = ф.readline()


def количество(узел: dict):
    """(число, символ, знаков). Количество в журнале -- в сырых единицах."""
    инфо = (узел or {}).get("info") or {}
    сумма = (узел or {}).get("amount")
    знаков = инфо.get("decimals")
    if знаков is None:
        знаков = ДЕСЯТИЧНЫЕ.get(инфо.get("contract"))
    if сумма is None or знаков is None:
        return None, инфо.get("symbol"), знаков
    try:
        return int(сумма) / (10 ** int(знаков)), инфо.get("symbol"), знаков
    except (TypeError, ValueError):
        return None, инфо.get("symbol"), знаков


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--follow-file", default="data/dbot_follow_trades_raw.json")
    р.add_argument("--minty", required=True)
    р.add_argument("--out", default="data/dbot_summy_po_mintam.json")
    а = р.parse_args()
    минты = {м.strip() for м in а.minty.split(",") if м.strip()}
    найдено = {м: [] for м in минты}
    for р_ in записи(а.follow_file):
        получено = ((р_.get("receive") or {}).get("info") or {}).get("contract")
        отдано = ((р_.get("send") or {}).get("info") or {}).get("contract")
        минт = получено if получено in минты else (отдано if отдано in минты else None)
        if not минт:
            continue
        ф_ = р_.get("follow") or {}
        и_кол, и_сим, и_зн = количество(ф_.get("send"))
        п_кол, п_сим, _ = количество(ф_.get("receive"))
        н_кол, н_сим, _ = количество(р_.get("send"))
        найдено[минт].append({
            "config": р_.get("configName"), "type": р_.get("type"),
            "state": р_.get("state"), "skip": р_.get("skipReason") or "",
            "utc_ms": р_.get("createAt"),
            "источник": ф_.get("wallet"),
            "источник_отдал": и_кол, "источник_валюта": и_сим,
            "источник_получил": п_кол, "источник_получил_символ": п_сим,
            "dbot_отдал": н_кол, "dbot_валюта": н_сим,
            "dbot_fee_rate": р_.get("dbotFeeRate"),
        })
    print(json.dumps({"по_минту": найдено}, ensure_ascii=False, indent=1)[:6000])
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump({"по_минту": найдено}, ф, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
