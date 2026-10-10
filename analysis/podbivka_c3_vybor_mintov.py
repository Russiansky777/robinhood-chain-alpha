#!/usr/bin/env python3
"""Выборка минтов для второго прохода по заказу Code-3 (ряды LP на 24 ч).

Правило выборки -- его: половина случайно из всех переездов блока, половина --
верх по числу событий первых десяти минут. У первого прохода окно 160 слотов
(~35 с), поэтому `событий_пула_в_окне` берётся как суррогат десяти минут --
годится для отбора, а точный порог считает уже его калькулятор по ряду.

Вход: файл первого прохода data/podbivka/arhiv_den/<метка>.json.gz.
Выход: текстовый файл со списком минтов через запятую (для --minty) и json с
разбором выборки. Только чтение входа.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ПЕРЕЕЗД = "pump-amm"      # переезд кривой: сигнал в пуле pump-amm
СЕМЯ = 20261010


def главное(а) -> int:
    д = json.loads(gzip.open(а.vhod, "rt", encoding="utf-8").read())
    лучшее: dict = {}
    счёт: collections.Counter = collections.Counter()
    for с in д.get("сигналы") or []:
        счёт["сигналов"] += 1
        if (с.get("pool") or с.get("пул")) != а.pul:
            continue
        м = с.get("mint")
        if not м:
            continue
        счёт["переездов"] += 1
        е = с.get("событий_пула_в_окне") or 0
        если_лучше = лучшее.get(м)
        if если_лучше is None or е > если_лучше["событий"]:
            лучшее[м] = {"минт": м, "poolId": с.get("poolId"),
                         "событий": е, "блок": с.get("block"),
                         "ts": с.get("timestamp")}
    все = sorted(лучшее.values(), key=lambda x: -x["событий"])
    счёт["минтов_всего"] = len(все)
    верх_n = а.mintov // 2
    верх = все[:верх_n]
    остальные = все[верх_n:]
    рнд = random.Random(СЕМЯ)
    случ = рнд.sample(остальные, min(а.mintov - len(верх), len(остальные)))
    выбор = верх + случ
    минты = [x["минт"] for x in выбор]
    Path(а.vyhod).write_text(",".join(минты), encoding="utf-8")
    разбор = {"что": "выборка минтов для второго прохода (ряды LP на 24 ч)",
              "вход": str(а.vhod), "пул": а.pul, "просили": а.mintov,
              "счёт": dict(счёт),
              "взято": {"верх_по_событиям": len(верх), "случайно": len(случ)},
              "порог_событий_у_верха": верх[-1]["событий"] if верх else None,
              "минты": выбор}
    Path(а.vyhod_json).write_text(json.dumps(разбор, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    print(f"переездов {счёт['переездов']}, минтов {счёт['минтов_всего']}, "
          f"взято {len(минты)} (верх {len(верх)}, случайно {len(случ)}); "
          f"порог событий у верха "
          f"{верх[-1]['событий'] if верх else None}", flush=True)
    return 0 if минты else 2


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--vhod", required=True, help="json.gz первого прохода")
    р.add_argument("--vyhod", required=True, help="файл со списком минтов через запятую")
    р.add_argument("--vyhod-json", required=True, help="json с разбором выборки")
    р.add_argument("--mintov", type=int, default=250)
    р.add_argument("--pul", default=ПЕРЕЕЗД)
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
