#!/usr/bin/env python3
"""Суточный список копировщиков из записей соседей по слоту (только чтение).

ОПРЕДЕЛЕНИЕ ВЛАДЕЛЬЦА (26.09, ночь): копировщик -- кошелёк, который не менее
трёх раз садился позади наших источников в том же слоте; список обновляется раз
в сутки. Считает это analysis/bloom_copiers.собрать, а здесь только сбор записей.

ОТКУДА ЗАПИСИ. Поля позиции slot_peers_slot и slot_peers_wallets -- их пишет
фоновая догонялка детектора (догнать_соседей_по_слоту) по полному блоку слота
источника. Ни одного сетевого вызова здесь нет: это чтение журналов.

Журналы читаются ПОТОКОМ, включая ротированные .gz. Сам список пишется только
с --out; без него -- показ.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys

# Модуль со счётом берётся из живого кода службы, а не переписывается здесь:
# иначе порог "три встречи" жил бы в двух местах и разошёлся бы.
КАТАЛОГИ_КОДА = ("/home/bot/bloom_executor", "/home/bot/robinhood-chain-alpha/analysis",
                 os.path.join(os.path.dirname(os.path.dirname(
                     os.path.dirname(os.path.abspath(__file__)))), "analysis"))


def модуль_счёта():
    for кат in КАТАЛОГИ_КОДА:
        путь = os.path.join(кат, "bloom_copiers.py")
        if not os.path.exists(путь):
            continue
        if кат not in sys.path:
            sys.path.insert(0, кат)
        import importlib  # noqa: PLC0415

        м = importlib.import_module("bloom_copiers")
        print(f"модуль счёта: {м.__file__}")
        return м
    raise SystemExit("СТОП: bloom_copiers.py не найден -- считать нечем")


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for стр in ф:
                стр = стр.strip()
                if стр:
                    yield стр
    except OSError as exc:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {путь} не прочитан ({type(exc).__name__})",
              file=sys.stderr)


def файлы(каталог: str, имя: str) -> list:
    из_ = sorted(glob.glob(os.path.join(каталог, имя + "*")))
    основной = os.path.join(каталог, имя)
    if основной in из_:
        из_.remove(основной)
        из_.append(основной)
    return из_


def записи(каталог: str) -> tuple:
    """[(слот, [кошельки])] по всем журналам позиций; дубли по cid+слоту -- раз."""
    видели = set()
    из_ = []
    просмотрено = 0
    for путь in файлы(каталог, "positions.jsonl"):
        for стр in строки(путь):
            просмотрено += 1
            try:
                з = json.loads(стр)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            слот = з.get("slot_peers_slot")
            кошельки = з.get("slot_peers_wallets")
            if not isinstance(слот, int) or not isinstance(кошельки, list):
                continue
            ключ = (з.get("client_order_id"), слот)
            if ключ in видели:
                continue
            видели.add(ключ)
            из_.append((слот, [к for к in кошельки if isinstance(к, str)]))
    return из_, просмотрено


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--out", default="", help="куда писать список (пусто -- показ)")
    р.add_argument("--porog", type=int, default=0,
                   help="порог встреч (0 -- порог модуля, слово владельца 3)")
    а = р.parse_args()
    CP = модуль_счёта()
    ряды, просмотрено = записи(а.state_dir)
    порог = а.porog or CP.ПОРОГ_ВСТРЕЧ
    д = CP.собрать(ряды, порог=порог)
    д["записей_соседей"] = len(ряды)
    д["строк_просмотрено"] = просмотрено
    print(json.dumps(д, ensure_ascii=False, indent=1))
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            ф.write(json.dumps(д, ensure_ascii=False, indent=1) + "\n")
        print(f"записан: {а.out}")
    if not ряды:
        print("ЗАПИСЕЙ СОСЕДЕЙ НЕТ: догонялка детектора ещё ничего не собрала "
              "-- список пуст, и в строке будет черта, а не ноль", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
