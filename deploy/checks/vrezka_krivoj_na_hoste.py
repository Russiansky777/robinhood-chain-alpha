#!/usr/bin/env python3
"""ЖИВА ЛИ ВРЕЗКА КРИВОЙ v2/v3 НА ХОСТЕ. Только чтение, ни одного вызова сети.

ЗАЧЕМ. Деплой умеет доставить модуль и НЕ доставить то, что модуль читает
файлом. Сборщик c3_pump_sborka берёт раскладки из data/idl по пути
<родитель каталога кода>/data/idl, то есть /home/bot/data/idl; без этих двух
файлов врезка отказывает словами "сборщик по IDL не доступен" на КАЖДОМ
сигнале кривой, а снаружи это выглядит как молчание полосы -- ровно так она и
стояла 08.10.

Проверка идёт ОТ ПОЛЬЗОВАТЕЛЯ СЛУЖБЫ и ЕГО питоном: доставленный root'ом файл
с чужими правами службе не читается, и узнать это надо здесь, а не на сигнале.

Скрипт подаётся на хост через stdin (python3 -), поэтому кавычек в ssh нет.
"""
from __future__ import annotations

import json
import sys


def из_() -> dict:
    о = {"ok": False, "why_not": None}
    try:
        import c2_swap_build as B
        import c3_prodavec_sborka as SP
        import c3_pump_sborka as PS
    except Exception as сбой:  # noqa: BLE001
        о["why_not"] = f"модуль не импортируется: {type(сбой).__name__}: {сбой}"
        return о
    try:
        идл = PS.zagruzit_idl()
    except Exception as сбой:  # noqa: BLE001
        о["why_not"] = f"IDL не читается: {type(сбой).__name__}: {сбой}"
        return о
    ix = идл["pump"]["ix"]
    for имя in ("buy_exact_quote_in_v3", "sell_v3", "buy_exact_quote_in_v2",
                "buy_v3", "buy_v2", "sell_v2"):
        if имя not in ix:
            о["why_not"] = f"в IDL нет {имя}"
            return о
    о["instrukcij_pump"] = len(ix)
    о["instrukcij_pump_amm"] = len(идл["pump_amm"]["ix"])
    о["raskhozhdenij_diskriminatorov"] = (идл["pump"]["rashozhdenija"]
                                          + идл["pump_amm"]["rashozhdenija"])
    о["buy_exact_quote_in_v3"] = {
        "disc": ix["buy_exact_quote_in_v3"]["disc"].hex(),
        "schetov": len(ix["buy_exact_quote_in_v3"]["accounts"])}
    о["sell_v3"] = {"disc": ix["sell_v3"]["disc"].hex(),
                    "schetov": len(ix["sell_v3"]["accounts"])}
    # ВРЕЗКА, А НЕ ТОЛЬКО СБОРЩИК: обе двери денежного пути должны УЗНАВАТЬ
    # разновидность по дискриминатору сделки источника.
    о["c2_vidit_v3"] = B.вариант_кривой_по_idl(
        ix["buy_exact_quote_in_v3"]["disc"].hex())
    о["c2_vidit_v2"] = B.вариант_кривой_по_idl(
        ix["buy_exact_quote_in_v2"]["disc"].hex())
    о["c2_vidit_sell_v3"] = B.вариант_кривой_по_idl(
        ix["sell_v3"]["disc"].hex(), продажа=True)
    о["prodavec_znaet_sell_po_pokupke"] = dict(SP.IDL_ПРОДАЖА_ПО_ПОКУПКЕ)
    о["prog_tokena_2022"] = PS.ПРОГ_ТОКЕНА_2022
    о["ok"] = (о["c2_vidit_v3"] == "buy_exact_quote_in_v3"
               and о["c2_vidit_sell_v3"] == "sell_v3"
               and not о["raskhozhdenij_diskriminatorov"])
    if not о["ok"] and not о["why_not"]:
        о["why_not"] = "врезка не узнаёт разновидность или IDL разошёлся"
    return о


if __name__ == "__main__":
    о = из_()
    print(json.dumps(о, ensure_ascii=False, indent=1))
    sys.exit(0 if о["ok"] else 1)
