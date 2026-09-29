#!/usr/bin/env python3
"""Почему круг не идёт: причины отказов по ЗАДАЧЕ источника, числами.

ЗАЧЕМ. 29.09 с 12:08Z торгует одна группа test_krug (сначала 10 адресов
lane_s0, с 13:19Z плюс 12 адресов batch5 -- слово владельца "если к 13:15Z
сигнала нет"). За 106 минут по этой задаче прошло 76 решений и НИ ОДНО не
дошло до гейта полосы: `own_send.by_stage.gate` так и остался 1, и тот вызов
был раньше круга. Признак жизни говорит "решений 76", но не говорит, ЧТО
именно их остановило: разбивка по кодам в нём общая, по всем задачам сразу.
Этот прогон даёт разбивку ПО ЗАДАЧЕ и ПО КОДУ и отдельно показывает все
покупки источников -- с тратой источника против порога входа.

Владельцу это нужно, чтобы решать по факту, а не по ощущению: ждать дальше
или условия круга стали узким местом. Ни одного порога прогон не меняет.

ТОЛЬКО ЧТЕНИЕ. Журнал читается ПОТОКОМ (слово владельца: "журналы больше не
читать в память"), ни один файл не пишется, кроме отчёта по --out.
ИЗМЕРИТЕЛЬНЫЙ КОД: по правилу 8 самопроверок денежного пути здесь нет -- он
не решает ни одной отправки.
"""
import argparse
import calendar
import gzip
import json
import sys
import time


def строки(путь: str):
    """Строки журнала потоком; .gz тоже."""
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for ln in ф:
                ln = ln.strip()
                if ln.startswith("{"):
                    yield ln
    except OSError as exc:
        print(f"ЖУРНАЛ НЕ ПРОЧИТАН {путь}: {type(exc).__name__}", file=sys.stderr)


def в_секундах(т: str | None) -> float | None:
    """ISO-время журнала в секунды UTC. Только calendar.timegm: mktime здесь
    давала сдвиг на час местного времени бегунка."""
    if not т:
        return None
    try:
        return calendar.timegm(time.strptime(т[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--zhurnal",
                   default="/home/bot/bloom_executor_live_data/decisions.jsonl")
    р.add_argument("--zadacha", default="test_krug",
                   help="задача источника; 'vse' -- все задачи")
    р.add_argument("--s", default="", help="с какого времени UTC (ISO), пусто -- с начала журнала")
    р.add_argument("--out", default="/tmp/krug_prichiny.json")
    а = р.parse_args()

    с_ts = в_секундах(а.s) if а.s else None
    только = None if а.zadacha.strip().lower() in ("vse", "все", "") else а.zadacha.strip()

    по_коду: dict[str, int] = {}
    по_виду: dict[str, int] = {}
    покупки: list[dict] = []
    всего = 0
    первое = последнее = None
    источники: dict[str, int] = {}

    for ln in строки(а.zhurnal):
        try:
            з = json.loads(ln)
        except ValueError:
            continue
        if только is not None and з.get("source_task") != только:
            continue
        т = з.get("ts_utc")
        ts = в_секундах(т)
        if с_ts is not None and (ts is None or ts < с_ts):
            continue
        всего += 1
        if первое is None:
            первое = т
        последнее = т
        код = з.get("code") or "БЕЗ_КОДА"
        по_коду[код] = по_коду.get(код, 0) + 1
        вид = з.get("kind") or "без_вида"
        по_виду[вид] = по_виду.get(вид, 0) + 1
        ист = з.get("source")
        if ист:
            источники[ист] = источники.get(ист, 0) + 1
        # ПОКУПКИ ИСТОЧНИКА -- единственное, что вообще может стать нашей
        # сделкой. Их немного, и каждую показываем целиком: трата источника,
        # первый ли это вход, докупка ли, и что сказал детектор.
        if вид == "buy":
            покупки.append({
                "ts_utc": т,
                "источник": ист,
                "минт": з.get("mint"),
                "подпись": з.get("signature"),
                "трата_sol_экв": з.get("spend_sol_eq"),
                "первый_вход": з.get("first_entry"),
                "докупка": з.get("dokupka"),
                "пулы": з.get("dex_programs"),
                "наш_пул": з.get("source_pool"),
                "почему_не_пул": з.get("pool_why_not"),
                "код": код,
                "причина": з.get("reason"),
                "действие": з.get("action"),
                "лаг_слотов": з.get("slot_lag"),
            })

    из_ = {
        "журнал": а.zhurnal,
        "задача": только or "все",
        "с": а.s or None,
        "решений": всего,
        "первое": первое,
        "последнее": последнее,
        "по_коду": dict(sorted(по_коду.items(), key=lambda п: -п[1])),
        "по_виду": dict(sorted(по_виду.items(), key=lambda п: -п[1])),
        "покупок_источников": len(покупки),
        "покупки": покупки,
        "адресов_со_сигналами": len(источники),
        "по_источнику": dict(sorted(источники.items(), key=lambda п: -п[1])),
    }
    try:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(из_, ф, ensure_ascii=False, indent=1)
    except OSError as exc:
        print(f"ОТЧЁТ НЕ ЗАПИСАН: {type(exc).__name__}", file=sys.stderr)

    print(f"=== причины по задаче {из_['задача']}: решений {всего}, "
          f"{первое} .. {последнее} ===")
    print(f"адресов, давших хотя бы один сигнал: {из_['адресов_со_сигналами']}")
    print("-- по коду --")
    for к, н in из_["по_коду"].items():
        print(f"  {н:6d}  {к}")
    print("-- по виду транзакции источника --")
    for к, н in из_["по_виду"].items():
        print(f"  {н:6d}  {к}")
    print(f"-- покупки источников: {len(покупки)} --")
    for п in покупки:
        print(f"  {п['ts_utc']}  трата {п['трата_sol_экв']}  первый_вход "
              f"{п['первый_вход']}  докупка {п['докупка']}  {п['код']}")
        print(f"      минт {п['минт']}  пулы {п['пулы']}")
        print(f"      {п['причина']}")
    if not покупки:
        print("  ни одной покупки источника в окне -- отказывать было нечему")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
