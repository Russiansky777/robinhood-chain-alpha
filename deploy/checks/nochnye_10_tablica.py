#!/usr/bin/env python3
"""Таблица по ночным минтам: кого видели, в какой группе, сколько, почему нет.

ЗАЧЕМ (пункт 0 владельца, утро 27.09). По каждому минту, который DBot купил
ночью, нужна одна строка: адрес источника, чью покупку видел детектор, его
группа, размер покупки в SOL-эквиваленте, котировка пула и точная причина, по
которой полоса не купила. Плюс отдельный разбор по лидеру: сколько его строк в
журнале и есть ли строка по каждому минту.

ОТКУДА ЧИСЛА. Журнал решений на хосте, потоком (журналы в память не читаем).
Группа -- тем же модулем, которым её читает служба: bloom_source_groups из
/home/bot/bloom_executor. Размер -- из полей записи, а если их нет, из текста
причины ("вход источника X SOL-эквивалента"), и тогда это помечено.

Только чтение. Измерительный код -- без самопроверок (Правило 8).
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import json
import os
import re
import sys
import time

ПОЛЯ = ("stage", "mint", "signature", "source", "action", "kind", "why_not",
        "why", "reason", "code", "pool", "program", "pool_program", "quote_mint",
        "target_sol", "target_usd", "sol_equiv", "rate_usd_sol", "amount_sol",
        "sol_in", "min_target_sol", "group", "taxed", "tax_bps", "t_recv_utc",
        "source_slot", "ts_utc", "lane_allowed", "lane_reason")

РАЗМЕР_ИЗ_ТЕКСТА = re.compile(r"вход источника\s+([0-9]+(?:\.[0-9]+)?)\s*SOL")
ПРОДАЖА = ("продажа источника", "sellSettings")


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
    for ф in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return calendar.timegm(time.strptime(т, ф))
        except ValueError:
            continue
    return 0.0


def группы_модуль():
    for кат in ("/home/bot/bloom_executor",
                "/home/bot/robinhood-chain-alpha/analysis",
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))), "analysis")):
        if not os.path.exists(os.path.join(кат, "bloom_source_groups.py")):
            continue
        if кат not in sys.path:
            sys.path.insert(0, кат)
        try:
            import importlib  # noqa: PLC0415

            м = importlib.import_module("bloom_source_groups")
            print(f"группы из {м.__file__}")
            return м
        except Exception as exc:  # noqa: BLE001
            print(f"группы из {кат} не загружены: {type(exc).__name__}",
                  file=sys.stderr)
    return None


def размер(з: dict):
    for к in ("sol_equiv", "target_sol", "amount_sol", "sol_in"):
        v = з.get(к)
        if isinstance(v, (int, float)) and v > 0:
            return round(float(v), 6), к
    м = РАЗМЕР_ИЗ_ТЕКСТА.search(str(з.get("reason") or ""))
    if м:
        return float(м.group(1)), "из текста причины"
    return None, None


def это_продажа(з: dict) -> bool:
    т = f"{з.get('reason') or ''} {з.get('why_not') or ''}"
    return any(п in т for п in ПРОДАЖА) or з.get("kind") == "sell"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--minty", required=True, help="минты через запятую")
    р.add_argument("--lider", default="", help="адрес лидера")
    р.add_argument("--okno-s", default="", help="начало окна UTC")
    р.add_argument("--okno-po", default="", help="конец окна UTC")
    р.add_argument("--out", default="/tmp/nochnye_10.json")
    а = р.parse_args()

    минты = [м.strip() for м in а.minty.split(",") if м.strip()]
    набор = set(минты)
    с_ = метка(а.okno_s) if а.okno_s else 0.0
    по_ = метка(а.okno_po) if а.okno_po else 2 ** 40

    по_минту = {м: [] for м in минты}
    лидер_всего, лидер_в_окне, лидер_по_минту = 0, 0, {}
    строк = 0
    for путь in пути(а.state_dir, "decisions.jsonl"):
        for з in строки(путь):
            строк += 1
            ист = з.get("source")
            м = з.get("mint")
            в_окне = с_ <= метка(з.get("t_recv_utc") or з.get("ts_utc") or "") < по_
            if а.lider and ист == а.lider:
                лидер_всего += 1
                if в_окне:
                    лидер_в_окне += 1
                if м:
                    лидер_по_минту.setdefault(м, 0)
                    лидер_по_минту[м] += 1
            if м in набор:
                ряд = {к: з.get(к) for к in ПОЛЯ if з.get(к) is not None}
                if ряд:
                    ряд["_в_окне"] = в_окне
                    по_минту[м].append(ряд)

    SG = группы_модуль()

    def группа(адрес):
        if not (SG and адрес):
            return None
        try:
            return SG.группа(адрес)
        except Exception:  # noqa: BLE001
            return None

    таблица = []
    for м in минты:
        все = по_минту[м]
        покупки = [з for з in все if з.get("_в_окне") and not это_продажа(з)]
        if not покупки:
            покупки = [з for з in все if not это_продажа(з)]
        основная = покупки[0] if покупки else None
        ист = основная.get("source") if основная else None
        # Причина: сначала отказ полосы/сборки (why_not), потом общий reason.
        причины, котировки, программы = [], [], []
        for з in все:
            if not з.get("_в_окне") or это_продажа(з):
                continue
            if з.get("source") != ист:
                continue
            for к in ("why_not", "lane_reason", "reason", "why"):
                v = з.get(к)
                if v and str(v) not in причины:
                    причины.append(str(v))
            if з.get("quote_mint") and з["quote_mint"] not in котировки:
                котировки.append(з["quote_mint"])
            if з.get("pool_program") and з["pool_program"] not in программы:
                программы.append(з["pool_program"])
        раз, откуда = размер(основная or {})
        таблица.append({
            "mint": м,
            "источник": ист,
            "группа": группа(ист),
            "размер_sol_экв": раз,
            "размер_откуда": откуда,
            "котировка": котировки,
            "программа_пула": программы,
            "причины": причины,
            "строк_всего": len(все),
            "строк_в_окне": sum(1 for з in все if з.get("_в_окне")),
            "источников_в_окне": sorted({з.get("source") for з in все
                                         if з.get("_в_окне") and з.get("source")}),
        })

    лидер = {"адрес": а.lider or None,
             "группа": группа(а.lider) if а.lider else None,
             "строк_всего": лидер_всего,
             "строк_в_окне": лидер_в_окне,
             "по_нашим_минтам": {м: лидер_по_минту.get(м, 0) for м in минты}}

    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "строк_прочитано": строк,
            "окно": [а.okno_s, а.okno_po],
            "таблица": таблица,
            "лидер": лидер,
            "по_минту_сырьё": по_минту}
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)

    print(f"строк прочитано: {строк}")
    print("минт | источник | группа | размер SOL-экв | котировка | причина")
    for р_ in таблица:
        print(f"{р_['mint'][:10]} | {(р_['источник'] or '-')[:12]} | "
              f"{р_['группа'] or '-'} | {р_['размер_sol_экв']} "
              f"({р_['размер_откуда'] or '-'}) | "
              f"{','.join(k[:6] for k in р_['котировка']) or '-'} | "
              f"{(р_['причины'][0] if р_['причины'] else '-')[:90]}")
    print(f"лидер {лидер['адрес']}: группа {лидер['группа']}, "
          f"строк всего {лидер['строк_всего']}, в окне {лидер['строк_в_окне']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
