#!/usr/bin/env python3
"""Дожать застрявшие записи позиций: чем они закрылись по ЦЕПИ.

ЗАЧЕМ. 26.09 гейт деплоя сторожа отказался перезапускать службу: открытых
позиций 3, и все три висят с 25.09 21:59Z с qty=null -- это корень I.4
(количество спрашивали по принятой подписи, а садится другая). Токены по цепи
приходили, но к утру счета этих минтов уже пустые: позиции ПРОДАНЫ, а записи об
этом не обновились. Пока они "открыты", сторож не перезапустить, а значит и
починку не поставить.

ЧТО ДЕЛАЕТ. Ровно то, что делает сам сторож в докладе о прошлой попытке: ищет
по цепи транзакцию, в которой остаток НАШЕГО минта уменьшился до нуля
(bloom_seller.найти_закрывающую), считает итог продажи (bloom_seller.итог_продажи)
и пишет его в запись позиции. Своей арифметики здесь нет.

ЧЕГО НЕ ДЕЛАЕТ. Не продаёт и не покупает: ключи кошельков не читаются вовсе.
Если остаток минта НЕ нулевой -- запись не трогается, потому что тогда позиция
действительно открыта и закрывать её должен сторож продажей, а не отчёт.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.environ.get("PYTHONPATH", "") or ".")

import bloom_exec_state as ST  # noqa: E402
import bloom_seller as SL  # noqa: E402


def остаток(кошелёк: str, минт: str) -> dict:
    return SL.token_balance_raw(кошелёк, минт)


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--apply", action="store_true",
                   help="без него ничего не пишется: печатается только разбор")
    р.add_argument("--cids", default="", help="список cid через запятую (пусто -- все открытые)")
    а = р.parse_args()

    с = ST.ExecState()
    поз = с.open_positions()
    если = {x.strip() for x in а.cids.split(",") if x.strip()}
    if если:
        поз = [p for p in поз if p.get("client_order_id") in если]
    итог = {"открытых": len(поз), "записано": 0, "оставлено": 0, "позиции": []}
    for p in поз:
        cid = p.get("client_order_id")
        минт = p.get("mint")
        кошелёк = SL.кошелёк_позиции(p)
        строка = {"cid": cid, "mint": минт, "wallet": кошелёк, "state": p.get("state"),
                  "lane": p.get("lane"), "действие": None, "why_not": None}
        ост = остаток(кошелёк, минт)
        строка["остаток_raw"] = ост.get("raw")
        строка["остаток_ok"] = ост.get("ok")
        if not ост.get("ok"):
            строка["действие"] = "оставлено"
            строка["why_not"] = f"остаток не прочитан: {ост.get('why_not')}"
            итог["оставлено"] += 1
            итог["позиции"].append(строка)
            continue
        if int(ост.get("raw") or 0) > 0:
            строка["действие"] = "оставлено"
            строка["why_not"] = ("остаток НЕ нулевой -- позиция действительно открыта, "
                                 "закрывать её должен сторож продажей")
            итог["оставлено"] += 1
            итог["позиции"].append(строка)
            continue
        найдено = SL.найти_закрывающую(кошелёк, минт, предел=20,
                                       читатель_tx=lambda s: (SL.rpc_call(
                                           "getTransaction",
                                           [s, {"encoding": "jsonParsed",
                                                "maxSupportedTransactionVersion": 0,
                                                "commitment": "confirmed"}]) or {}).get("result"))
        строка["закрывающая"] = найдено.get("signature")
        строка["осмотрено_подписей"] = найдено.get("looked")
        исход = найдено.get("outcome") or {}
        строка["sol_delta"] = исход.get("sol_delta")
        строка["sol_delta_net"] = исход.get("sol_delta_net")
        if not найдено.get("signature"):
            строка["действие"] = "оставлено"
            строка["why_not"] = ("остаток нулевой, но закрывающей транзакции в последних "
                                 f"подписях не нашлось ({найдено.get('why_not')})")
            итог["оставлено"] += 1
            итог["позиции"].append(строка)
            continue
        if а.apply:
            с.update_position(
                cid, state=ST.STATE_CLOSED, closed_via="chain_backfill",
                closed_signature=найдено["signature"],
                closed_sol_delta=исход.get("sol_delta"),
                closed_sol_net=исход.get("sol_delta_net"),
                closed_confirmed=True,
                closed_why_not=("дожато по цепи 26.09: остаток минта ноль, закрывающая "
                                "транзакция найдена; запись висела открытой из-за qty=null"))
            строка["действие"] = "записано"
            итог["записано"] += 1
        else:
            строка["действие"] = "записалось бы (нет --apply)"
        итог["позиции"].append(строка)
    print(json.dumps(итог, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
