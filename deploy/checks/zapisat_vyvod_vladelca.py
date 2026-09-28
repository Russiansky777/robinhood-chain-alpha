#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Записать ВЫВОД ВЛАДЕЛЬЦА с кошелька полосы -- по подписи, проверенной цепью.

ЗАЧЕМ (слово владельца 28.09 ночью, п.4). Владелец снял свои деньги своим
ключом. Для порога "просадка от 00:00" это выглядит потерей размером со вывод,
и рубильник встал бы на ровном месте. Вывод записывается отдельным списком и
вычитается из просадки -- рубильник считает ТОРГОВЛЮ.

ПОЧЕМУ СУММА БЕРЁТСЯ С ЦЕПИ, А НЕ ИЗ ВХОДА. Число, напечатанное человеком,
проверить нечем, а ошибка в нём прямо ослабляет рубильник. Поэтому на вход
идёт только ПОДПИСЬ: сумма считается как нативная дельта кошелька полосы в
этой транзакции, и записывается лишь если она отрицательна (деньги ушли) и
транзакция без ошибки.

Ничего не подписывает и не отправляет: одно чтение транзакции и одна запись в
файл состояния.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def дельта_кошелька(tx: dict, кошелёк: str) -> dict:
    """Нативная дельта кошелька в транзакции: (до, после, дельта) в лампортах."""
    из_ = {"ok": False, "why_not": None}
    мета = (tx or {}).get("meta") or {}
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = [(к.get("pubkey") if isinstance(к, dict) else к)
              for к in (сообщение.get("accountKeys") or [])]
    if кошелёк not in ключи:
        из_["why_not"] = "кошелька полосы в транзакции нет вовсе"
        return из_
    и = ключи.index(кошелёк)
    до = (мета.get("preBalances") or [])
    после = (мета.get("postBalances") or [])
    if и >= len(до) or и >= len(после):
        из_["why_not"] = "балансов в мете нет"
        return из_
    из_.update(ok=True, до=int(до[и]), после=int(после[и]),
                дельта=int(после[и]) - int(до[и]), err=мета.get("err"),
                fee=мета.get("fee"))
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--podpis", required=True, help="подпись транзакции вывода")
    р.add_argument("--koshelek", default="", help="кошелёк полосы (пусто -- из окружения)")
    р.add_argument("--primechanie", default="вывод владельца")
    р.add_argument("--zapisat", action="store_true",
                    help="без него только показывает, что записал бы")
    а = р.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bloom_exec_state as ST  # noqa: PLC0415
    import bloom_own_send as OSW  # noqa: PLC0415
    import helius_client as helius  # noqa: PLC0415

    кошелёк = (а.koshelek or os.environ.get("OWN_SEND_WALLET")
                or os.environ.get("BLOOM_LANE_WALLET") or "")
    if not кошелёк:
        print("СБОЙ: кошелёк полосы не задан")
        return 2
    tx = helius.call("getTransaction",
                      [а.podpis, {"encoding": "jsonParsed",
                                   "maxSupportedTransactionVersion": 1,
                                   "commitment": "finalized"}], таймаут=30.0)
    tx = (tx or {}).get("result") if isinstance(tx, dict) and "result" in tx else tx
    if not tx:
        print("СБОЙ: транзакция не прочиталась")
        return 3
    д = дельта_кошелька(tx, кошелёк)
    if not д["ok"]:
        print(f"СБОЙ: {д['why_not']}")
        return 4
    if д.get("err") is not None:
        print(f"СБОЙ: транзакция села с ошибкой {д['err']} -- вывода не было")
        return 5
    if д["дельта"] >= 0:
        print(f"СБОЙ: дельта кошелька {д['дельта']} лампортов -- это не вывод")
        return 6
    sol = -д["дельта"] / 1_000_000_000
    когда = tx.get("blockTime")
    print(f"вывод по цепи: {sol:.9f} SOL, слот {tx.get('slot')}, "
          f"blockTime {когда}, комиссия {д.get('fee')}")
    if not а.zapisat:
        print("сухой прогон: с --zapisat запишу в список выводов")
        return 0
    состояние = ST.ExecState()
    р_ = OSW.записать_вывод(состояние, подпись=а.podpis, sol=sol,
                             когда=(float(когда) if когда else None),
                             примечание=а.primechanie)
    print("запись:", json.dumps(р_, ensure_ascii=False))
    всего = OSW.выводы_владельца(состояние)
    print("выводов в списке:", json.dumps(всего, ensure_ascii=False))
    return 0 if р_.get("ok") else 7


if __name__ == "__main__":
    raise SystemExit(main())
