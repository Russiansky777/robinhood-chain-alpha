#!/usr/bin/env python3
"""Урезать ряды для Code-3: только поля, которые читает его калькулятор.

Зачем. Полный файл рядов лежит в артефакте задания (782 МБ), а хост артефактов GitHub
закрыт политикой сети и у него, и у меня (`*.blob.core.windows.net`, 403 на CONNECT).
Через git он читает всё без проблем, поэтому доставка -- урезанным файлом в ветку: по его
замеру одна запись с нужными полями 171 байт, 213 856 событий дают около 7 МБ в gz.

Что на выходе. `{"ленты_минтов": {poolId: [события]}}` -- ключ по ПУЛУ, как он просил
(в моём сборе лента лежит по минту, поэтому рядом идёт `минт_по_пулу`). Имена полей
полные, как в `podbivka_arhiv_den.КЛЮЧИ`. События не фильтруются по действию вовсе:
withdraw и deposit, если они в ленте есть, доходят как есть.

Поля: action, quoteAmount, quoteInPool, tokensInPool, poolFeeRate, block, timestamp.
Рядом идут vQuoteInBondingCurve и vTokensInBondingCurve, когда пуловых резервов в
событии нет: на кривой (до переезда) резервы лежат именно в них, и без этих двух полей
такие события пришли бы с пустыми резервами.

Только чтение входа. Выход -- один gz и краткий счёт рядом в stdout.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import sys
import time
from pathlib import Path

ПОЛЯ = ("action", "quoteAmount", "quoteInPool", "tokensInPool",
        "poolFeeRate", "block", "timestamp")
ПОЛЯ_КРИВОЙ = ("vQuoteInBondingCurve", "vTokensInBondingCurve")


def главное(а) -> int:
    вх = Path(а.vhod)
    if not вх.exists():
        print(f"нет файла {вх}", flush=True)
        return 2
    д = json.loads(gzip.open(вх, "rt", encoding="utf-8").read())
    ленты = д.get("ленты_минтов") or д.get("ряды") or {}
    по_пулу: dict = collections.defaultdict(list)
    минт_по_пулу: dict = {}
    счёт: collections.Counter = collections.Counter()
    действия: collections.Counter = collections.Counter()
    for минт, сп in ленты.items():
        for e in сп:
            счёт["событий_входа"] += 1
            pid = e.get("poolId")
            if not pid:
                счёт["без_пула"] += 1
                continue
            стр = {к: e.get(к) for к in ПОЛЯ}
            if стр.get("quoteInPool") is None or стр.get("tokensInPool") is None:
                for к in ПОЛЯ_КРИВОЙ:
                    if e.get(к) is not None:
                        стр[к] = e[к]
                счёт["с_резервами_кривой"] += 1
            по_пулу[pid].append(стр)
            минт_по_пулу.setdefault(pid, минт)
            действия[стр.get("action") or "?"] += 1
            счёт["событий_выхода"] += 1
    for сп in по_пулу.values():
        сп.sort(key=lambda x: ((x.get("block") or 0), (x.get("timestamp") or 0)))
    тело = {"что": "ряды LP для Code-3: только поля его калькулятора",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "откуда": вх.name, "часы": д.get("часы"),
            "поля": list(ПОЛЯ), "поля_кривой_когда_нет_пуловых": list(ПОЛЯ_КРИВОЙ),
            "события_не_фильтровались": True,
            "счёт": dict(счёт), "по_действиям": dict(действия.most_common()),
            "пулов": len(по_пулу), "минт_по_пулу": минт_по_пулу,
            "ленты_минтов": {k: v for k, v in по_пулу.items()}}
    вых = Path(а.vyhod)
    вых.parent.mkdir(parents=True, exist_ok=True)
    вых.write_bytes(gzip.compress(json.dumps(тело, ensure_ascii=False).encode("utf-8")))
    мб = вых.stat().st_size / 1e6
    print(f"пулов {len(по_пулу)}, событий {счёт['событий_выхода']} из "
          f"{счёт['событий_входа']}, с резервами кривой "
          f"{счёт['с_резервами_кривой']}, действия {dict(действия.most_common())}, "
          f"файл {мб:.2f} МБ", flush=True)
    if а.pushom:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        R.записано(вых)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--vhod", required=True, help="полный файл рядов (json.gz)")
    р.add_argument("--vyhod", required=True, help="куда положить урезанный gz")
    р.add_argument("--pushom", action="store_true",
                   help="записать в манифест задания, чтобы шаг коммита его взял")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
