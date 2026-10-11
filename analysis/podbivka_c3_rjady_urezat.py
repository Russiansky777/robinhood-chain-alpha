#!/usr/bin/env python3
"""Урезать ряды для Code-3: только поля его калькулятора, частями по ветке.

Зачем. Полный файл рядов лежит в артефакте задания (754 МБ), а хост артефактов GitHub
(`*.blob.core.windows.net`) закрыт политикой сети и у Code-3, и у меня: 403 на CONNECT,
качается 0 байт. Через git он читает всё, поэтому доставка -- урезанным файлом в ветку.

Размер. Его замер 171 байт на событие верен, но событий в ленте не 213 856, а 4 574 311
(из них 4 353 354 с poolId): 213 856 -- это счётчик «наших событий» в журнале прохода,
который я тогда принял за число событий ленты. Урезанный выход -- около 123 МБ, то есть
одним файлом в ветку не лезет (предел GitHub -- 100 МБ), поэтому он режется на части по
`--sobytij-na-chast` событий. Пулы между частями делятся целиком: события одного пула
никогда не разрываются. Рядом пишется `<имя>.manifest.json`: список частей, число пулов,
событий и счётчики по типам действий.

Действия в ленте называются `remove`/`add` (а не `withdraw`/`deposit`), плюс `createPool`,
`migrate`, `create`. По действию ничего не фильтруется -- всё доходит как есть.

Формат части: `{"ленты_минтов": {poolId: [события]}}` -- ключ по ПУЛУ, как он просил; в
моём сборе лента лежит по минту, поэтому рядом идёт `минт_по_пулу`. Имена полей полные,
как в `podbivka_arhiv_den.КЛЮЧИ`. Поля: action, quoteAmount, quoteInPool, tokensInPool,
poolFeeRate, block, timestamp; когда пуловых резервов в событии нет (события на кривой,
до переезда), рядом кладутся vQuoteInBondingCurve и vTokensInBondingCurve -- иначе
резервы пришли бы пустыми.

Только чтение входа. Ничего из podbivka_run не тянется: на бегунке доставки нет solders.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import time
from pathlib import Path

ПОЛЯ = ("action", "quoteAmount", "quoteInPool", "tokensInPool",
        "poolFeeRate", "block", "timestamp")
ПОЛЯ_КРИВОЙ = ("vQuoteInBondingCurve", "vTokensInBondingCurve")


def основа(вых: Path) -> str:
    """`...urezan.json.gz` -> `...urezan`: к нему клеятся .partN.json.gz и .manifest.json."""
    имя = вых.name
    for хвост in (".json.gz", ".jsonl.gz", ".json", ".gz"):
        if имя.endswith(хвост):
            return имя[: -len(хвост)]
    return имя


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
    # части: пулы раскладываются целиком, самый людный -- первым
    части: list = [[]]
    в_части = 0
    for pid, сп in sorted(по_пулу.items(), key=lambda kv: -len(kv[1])):
        if в_части and в_части + len(сп) > а.sobytij_na_chast:
            части.append([])
            в_части = 0
        части[-1].append(pid)
        в_части += len(сп)
    вых = Path(а.vyhod)
    вых.parent.mkdir(parents=True, exist_ok=True)
    корень = основа(вых)
    общее = {"что": "ряды LP для Code-3: только поля его калькулятора",
             "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "откуда": вх.name, "часы": д.get("часы"),
             "поля": list(ПОЛЯ), "поля_кривой_когда_нет_пуловых": list(ПОЛЯ_КРИВОЙ),
             "события_не_фильтровались": True,
             "действия_в_ленте": "remove/add вместо withdraw/deposit"}
    файлы: list = []
    for н, пулы in enumerate(части, 1):
        ф = вых.parent / f"{корень}.part{н}.json.gz"
        тело = {**общее, "часть": н, "частей": len(части), "пулов": len(пулы),
                "событий": sum(len(по_пулу[p]) for p in пулы),
                "минт_по_пулу": {p: минт_по_пулу[p] for p in пулы},
                "ленты_минтов": {p: по_пулу[p] for p in пулы}}
        ф.write_bytes(gzip.compress(
            json.dumps(тело, ensure_ascii=False).encode("utf-8")))
        файлы.append({"файл": ф.name, "часть": н, "пулов": len(пулы),
                      "событий": тело["событий"],
                      "мб": round(ф.stat().st_size / 1e6, 2)})
        print(f"  часть {н}/{len(части)}: пулов {len(пулы)}, событий "
              f"{тело['событий']}, {файлы[-1]['мб']} МБ ({ф.name})", flush=True)
    манифест = вых.parent / f"{корень}.manifest.json"
    манифест.write_text(json.dumps(
        {**общее, "частей": len(части), "файлы": файлы,
         "пулов": len(по_пулу), "событий": счёт["событий_выхода"],
         "событий_во_входе": счёт["событий_входа"],
         "без_пула": счёт["без_пула"],
         "с_резервами_кривой": счёт["с_резервами_кривой"],
         "по_действиям": dict(действия.most_common()),
         "пулы_по_частям": {f"{корень}.part{н}.json.gz": пулы
                            for н, пулы in enumerate(части, 1)}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"пулов {len(по_пулу)}, событий {счёт['событий_выхода']} из "
          f"{счёт['событий_входа']}, частей {len(части)}, действия "
          f"{dict(действия.most_common())}, манифест {манифест.name}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--vhod", required=True, help="полный файл рядов (json.gz)")
    р.add_argument("--vyhod", required=True,
                   help="имя-основа выхода: к нему клеятся .partN.json.gz и .manifest.json")
    р.add_argument("--sobytij-na-chast", type=int, default=1_200_000,
                   help="сколько событий на часть (предел файла в ветке -- 100 МБ)")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
