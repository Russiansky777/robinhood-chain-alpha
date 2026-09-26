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


def tx_по_подписи(подпись: str):
    от = SL.rpc_call("getTransaction", [подпись, {"encoding": "jsonParsed",
                                                  "maxSupportedTransactionVersion": 0,
                                                  "commitment": "confirmed"}])
    return (от or {}).get("result") if isinstance(от, dict) else None


def счета_минта(кошелёк: str, минт: str) -> list:
    """Наши токен-счета этого минта -- включая уже ЗАКРЫТЫЕ.

    Закрытый счёт узел в getTokenAccountsByOwner уже не отдаёт, поэтому адрес
    выводится: ATA от кошелька и минта для обеих программ токена. История
    закрытого счёта в цепи остаётся и отвечает на вопрос, чем он закрылся.
    """
    из_ = []
    try:
        import c2_swap_build as B  # noqa: PLC0415
        for программа in (B.TOKEN_PROGRAM, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
            из_.append(B.ata(кошелёк, минт, программа))
    except Exception:  # noqa: BLE001
        pass
    return из_


def дельта_по_счёту(tx: dict, счёт: str):
    """Дельта остатка КОНКРЕТНОГО токен-счёта. Пропавшая строка -- ноль."""
    мета = (tx or {}).get("meta") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in ((((tx or {}).get("transaction") or {}).get("message") or {})
                       .get("accountKeys") or [])]
    try:
        и = ключи.index(счёт)
    except ValueError:
        return None
    def взять(сторона):
        for b in мета.get(сторона) or []:
            if b.get("accountIndex") == и:
                try:
                    return int((b.get("uiTokenAmount") or {}).get("amount"))
                except (TypeError, ValueError):
                    return None
        return 0
    до = взять("preTokenBalances")
    после = взять("postTokenBalances")
    if до is None:
        return None
    return после - до


def натив_кошелька(tx: dict, кошелёк: str):
    """Нативная дельта кошелька в этой транзакции, SOL (с платой за подпись)."""
    мета = (tx or {}).get("meta") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in ((((tx or {}).get("transaction") or {}).get("message") or {})
                       .get("accountKeys") or [])]
    try:
        и = ключи.index(кошелёк)
        до = (мета.get("preBalances") or [])[и]
        после = (мета.get("postBalances") or [])[и]
    except (ValueError, IndexError):
        return None
    return round((после - до) / 1_000_000_000, 9)


def закрывающая_по_счёту(кошелёк: str, минт: str, *, предел: int = 25) -> dict:
    """Транзакция, в которой остаток НАШЕГО минта уменьшился: ищем по истории
    самого токен-счёта. Возвращает то же, что найти_закрывающую сторожа."""
    из_ = {"signature": None, "outcome": None, "looked": 0, "why_not": None,
           "accounts": []}
    for счёт in счета_минта(кошелёк, минт):
        из_["accounts"].append(счёт)
        от = SL.rpc_call("getSignaturesForAddress", [счёт, {"limit": предел}])
        строки = (от or {}).get("result") or [] if isinstance(от, dict) else []
        for зап in строки:                      # от новых к старым
            подпись = (зап or {}).get("signature")
            if not подпись:
                continue
            tx = tx_по_подписи(подпись)
            из_["looked"] += 1
            if not tx:
                continue
            исход = SL.итог_продажи(tx, кошелёк, минт)
            дельта = (исход or {}).get("tokens_delta")
            if дельта is None or дельта >= 0:
                # ПРОПАВШАЯ СТРОКА -- ЭТО НОЛЬ, А НЕ "НЕТ ДАННЫХ". Продажа часто
                # идёт вместе с закрытием токен-счёта одной транзакцией, и
                # закрытый счёт из postTokenBalances исчезает совсем: сторож
                # видит "остаток не уменьшался", хотя он ушёл в ноль. Считаем
                # дельту прямо по строкам этого счёта.
                дельта = дельта_по_счёту(tx, счёт)
            if дельта is not None and дельта < 0:
                из_.update(signature=подпись,
                           outcome=(исход if (исход or {}).get("tokens_delta") is not None
                                    else {**(исход or {}), "tokens_delta": дельта,
                                          "sol_delta": натив_кошелька(tx, кошелёк),
                                          "sol_delta_net": натив_кошелька(tx, кошелёк),
                                          "why_not": "остаток счёта ушёл в ноль вместе с закрытием "
                                                     "счёта -- дельта посчитана по строкам счёта"}))
                return из_
    из_["why_not"] = (f"в истории токен-счетов ({', '.join(из_['accounts'])}) "
                      f"нет транзакции, уменьшившей остаток минта; осмотрено {из_['looked']}")
    return из_


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
        # ИСКАТЬ НАДО ПО ИСТОРИИ ТОКЕН-СЧЁТА, А НЕ КОШЕЛЬКА. Продажа была 13
        # часов назад, и последние подписи кошелька -- это уже совсем другие
        # транзакции (26.09 там 22 закрытия пустых счетов). У ATA история
        # короткая: создание, покупка, продажа, закрытие -- и в ней закрывающая
        # находится точно.
        найдено = закрывающая_по_счёту(кошелёк, минт)
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
