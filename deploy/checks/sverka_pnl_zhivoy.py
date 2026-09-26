#!/usr/bin/env python3
"""Сверка записи учёта с фактом по цепи: итог = возврат - вход - расход.

ЗАЧЕМ (пункт 3 дневного плана владельца 26.09). После деплоя I.1+I.4+I.5
суточный счёт пишется один раз на позицию (pnl_counted) и итог берётся по
НАТИВНОЙ дельте кошелька, а не из полей. Верить этому можно только если на
живой сделке запись совпадает с цепью. Этот прогон и есть проверка: по каждой
закрытой сделке полосы берутся ДВЕ транзакции -- покупка и продажа -- и
складываются нативные дельты нашего кошелька. Сумма двух дельт и есть
"возврат - вход - расход" целиком, без всякого учёта.

ПОЧЕМУ КОШЕЛЁК НЕ ИЗ ОКРУЖЕНИЯ. Плательщик комиссии нашей транзакции -- это и
есть наш кошелёк полосы (accountKeys[0]). Брать его из транзакции честнее и не
требует ни ключа, ни адреса в прогоне.

Только чтение: ни подписи, ни отправки, ключей кошельков здесь нет.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

import requests

ПОРОГ_SOL = 0.0002  # расхождение записи с цепью, выше которого это уже разбор


def узел() -> str:
    ключ = os.environ.get("HELIUS_API_KEY") or ""
    if not ключ:
        print("СТОП: HELIUS_API_KEY не задан -- цепь читать нечем", file=sys.stderr)
        raise SystemExit(2)
    return f"https://mainnet.helius-rpc.com/?api-key={ключ}"


class Цепь:
    def __init__(self) -> None:
        self.url = узел()
        self.вызовов = 0
        self.кэш: dict = {}

    def транзакция(self, подпись: str) -> dict | None:
        if подпись in self.кэш:
            return self.кэш[подпись]
        тело = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                "params": [подпись, {"encoding": "jsonParsed",
                                      "maxSupportedTransactionVersion": 0,
                                      "commitment": "confirmed"}]}
        for попытка in range(4):
            self.вызовов += 1
            о = requests.post(self.url, json=тело, timeout=30)
            if о.status_code == 429:
                time.sleep(2 * (попытка + 1))
                continue
            о.raise_for_status()
            tx = (о.json() or {}).get("result")
            self.кэш[подпись] = tx
            return tx
        self.кэш[подпись] = None
        return None


def дельта_натив(tx: dict, кошелёк: str) -> tuple:
    """(дельта SOL, причина отказа). Дельта СЫРАЯ -- вместе с комиссией."""
    мета = (tx or {}).get("meta") or {}
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    сырые = сообщение.get("accountKeys") or []
    ключи = [к.get("pubkey") if isinstance(к, dict) else к for к in сырые]
    if кошелёк not in ключи:
        return None, "кошелька нет среди счетов транзакции"
    и = ключи.index(кошелёк)
    до = мета.get("preBalances") or []
    после = мета.get("postBalances") or []
    if и >= len(до) or и >= len(после):
        return None, "в meta нет баланса по этому счёту"
    return (int(после[и]) - int(до[и])) / 1_000_000_000.0, ""


def плательщик(tx: dict) -> str:
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    сырые = сообщение.get("accountKeys") or []
    if not сырые:
        return ""
    первый = сырые[0]
    return (первый.get("pubkey") if isinstance(первый, dict) else первый) or ""


def подпись_покупки(п: dict) -> str:
    return (п.get("lane_landed_signature") or п.get("lane_signature")
            or п.get("signature") or "")


def подпись_продажи(п: dict) -> str:
    """Подпись НАШЕЙ продажи -- из трёх мест, потому что их три.

    У позиции полосы сторож пишет закрытие полями state/ts_closed/
    closed_reason/closed_sol_net (bloom_seller.py:983), а подпись продажи
    остаётся в last_sell_reported (доложенная) или в last_sell_signatures.
    Поле closed_signature есть у пути площадки. Первый прогон сверки искал
    только его и написал "сверено 0" на трёх живых закрытых сделках.
    """
    подписи = п.get("last_sell_signatures") or []
    последняя = подписи[-1] if isinstance(подписи, list) and подписи else ""
    подпись = (п.get("closed_signature") or п.get("last_sell_reported")
               or последняя or "")
    # Служебные метки самопроверок ("ПРОДАЖА_ТАЙМЕРА" и подобные) подписями не
    # являются: подпись Solana -- base58 длиной 87-88 знаков.
    return подпись if len(подпись) >= 80 else ""


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--sdelki", required=True, help="выборка из журнала позиций")
    р.add_argument("--pnl", default="", help="файл суточного счёта (json)")
    р.add_argument("--since-utc", default="")
    р.add_argument("--limit", type=int, default=0)
    р.add_argument("--out", required=True)
    а = р.parse_args()

    д = json.load(open(а.sdelki, encoding="utf-8"))
    порог_т = 0.0
    if а.since_utc:
        порог_т = time.mktime(time.strptime(а.since_utc, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    сделки = [с for с in д.get("сделки") or []
              if с.get("lane") and (float(с.get("ts_intent") or 0) >= порог_т)]
    # СОСТОЯНИЯ ВСЕХ СДЕЛОК ОКНА -- отдельной строкой. Без неё "сверено 0"
    # читается как "полоса не торговала", а она могла купить и ещё не продать.
    состояния: dict = {}
    for с in сделки:
        ключ = f"{с.get('state') or 'нет состояния'}"
        if с.get("state") == "closed" and not подпись_продажи(с):
            ключ = "closed без подписи продажи"
        состояния[ключ] = состояния.get(ключ, 0) + 1
    закрытые = [с for с in сделки if с.get("state") == "closed" and подпись_продажи(с)]
    закрытые.sort(key=lambda с: float(с.get("ts_closed") or с.get("ts_intent") or 0))
    if а.limit:
        закрытые = закрытые[-а.limit:]

    ц = Цепь()
    строки, отказы = [], []
    for с in закрытые:
        куп = подпись_покупки(с)
        прод = подпись_продажи(с)
        if not куп:
            отказы.append({"cid": с.get("client_order_id"),
                            "почему": "в записи нет подписи нашей покупки"})
            continue
        tx_куп = ц.транзакция(куп)
        if not tx_куп:
            отказы.append({"cid": с.get("client_order_id"),
                            "почему": f"узел не отдал покупку {куп[:12]}"})
            continue
        кошелёк = плательщик(tx_куп)
        d_куп, почему1 = дельта_натив(tx_куп, кошелёк)
        tx_прод = ц.транзакция(прод)
        if not tx_прод:
            отказы.append({"cid": с.get("client_order_id"),
                            "почему": f"узел не отдал продажу {прод[:12]}"})
            continue
        d_прод, почему2 = дельта_натив(tx_прод, кошелёк)
        if d_куп is None or d_прод is None:
            отказы.append({"cid": с.get("client_order_id"),
                            "почему": (почему1 or почему2) + " (кошелёк "
                                      + кошелёк[:8] + ")"})
            continue
        по_цепи = round(d_куп + d_прод, 9)
        запись = с.get("pnl_counted_sol")
        строка = {
            "cid": с.get("client_order_id"),
            "минт": с.get("mint"),
            "группа": с.get("lane_group"),
            "источник": с.get("source"),
            "закрыта_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(float(с.get("ts_closed") or 0)))
                            if с.get("ts_closed") else None,
            "вход_sol": с.get("sol_in"),
            "возврат_sol": с.get("closed_sol_net"),
            "расход_записи_sol": с.get("pnl_counted_spend_sol"),
            "натив_покупки_sol": с.get("lane_buy_native_sol"),
            "комиссия_покупки_sol": с.get("lane_buy_fee_sol"),
            "pnl_counted": с.get("pnl_counted"),
            "итог_записи_sol": запись,
            "дельта_покупки_по_цепи_sol": round(d_куп, 9),
            "дельта_продажи_по_цепи_sol": round(d_прод, 9),
            "итог_по_цепи_sol": по_цепи,
            "расхождение_sol": (round(float(запись) - по_цепи, 9)
                                 if запись is not None else None),
            "подпись_покупки": куп,
            "подпись_продажи": прод,
            "кошелёк": кошелёк,
        }
        строки.append(строка)

    расхождения = [abs(с["расхождение_sol"]) for с in строки
                   if с["расхождение_sol"] is not None]
    свод = {
        "сделок_полосы_в_окне": len(сделки),
        "по_состояниям": состояния,
        "сверено_сделок": len(строки),
        "без_записи_итога": sum(1 for с in строки if с["итог_записи_sol"] is None),
        "сумма_по_записи_sol": round(sum(float(с["итог_записи_sol"] or 0) for с in строки), 9),
        "сумма_по_цепи_sol": round(sum(float(с["итог_по_цепи_sol"] or 0) for с in строки), 9),
        "медиана_расхождения_sol": (round(statistics.median(расхождения), 9)
                                     if расхождения else None),
        "худшее_расхождение_sol": (round(max(расхождения), 9) if расхождения else None),
        "порог_sol": ПОРОГ_SOL,
        "вызовов_узла": ц.вызовов,
        "отказов": len(отказы),
    }
    счёт = {}
    if а.pnl and os.path.exists(а.pnl):
        try:
            счёт = json.load(open(а.pnl, encoding="utf-8"))
        except ValueError:
            счёт = {"corrupt": True}

    # ЗАКРЫТЫЕ БЕЗ ПРОДАЖИ -- отдельным списком с причиной. Упавшая по цепи
    # покупка и дыра в учёте выглядят в сводке одинаково, а различать их надо.
    без_продажи = [{
        "cid": с.get("client_order_id"),
        "минт": с.get("mint"),
        "группа": с.get("lane_group"),
        "вход_sol": с.get("sol_in"),
        "куплено_по_цепи": с.get("chain_ok"),
        "pnl_counted": с.get("pnl_counted"),
        "итог_записи_sol": с.get("pnl_counted_sol"),
        "расход_записи_sol": с.get("pnl_counted_spend_sol"),
        "натив_покупки_sol": с.get("lane_buy_native_sol"),
        "причина": (с.get("closed_reason") or с.get("close_reason")
                    or с.get("why_not") or "")[:300],
        "подпись_покупки": подпись_покупки(с),
    } for с in сделки if с.get("state") == "closed" and not подпись_продажи(с)]
    итог = {"собрано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "окно_с": а.since_utc, "свод": свод, "суточный_счёт": счёт,
            "сделки": строки, "закрытые_без_продажи": без_продажи,
            "отказы": отказы}
    os.makedirs(os.path.dirname(а.out) or ".", exist_ok=True)
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    итог_кратко = {"свод": свод, "суточный_счёт": счёт}
    print(json.dumps(итог_кратко, ensure_ascii=False, indent=1))
    for с in сделки[-12:]:
        print(f"{с.get('ts_intent_utc') or с.get('ts_intent')} состояние={с.get('state')} "
              f"группа={с.get('lane_group')} вход={с.get('sol_in')} "
              f"источник={(с.get('source_task') or с.get('source') or '')[:14]} "
              f"минт={(с.get('mint') or '')[:8]} "
              f"наша_подпись={'есть' if подпись_покупки(с) else 'нет'} "
              f"продажа={'есть' if подпись_продажи(с) else 'нет'}")
    for с in строки[-10:]:
        print(f"{с['закрыта_utc']} {(с['минт'] or '')[:8]} группа={с['группа']} "
              f"запись={с['итог_записи_sol']} цепь={с['итог_по_цепи_sol']} "
              f"расхождение={с['расхождение_sol']}")
    for б in без_продажи[-10:]:
        print(f"БЕЗ ПРОДАЖИ {(б['минт'] or '')[:8]} группа={б['группа']} "
              f"вход={б['вход_sol']} цепь={б['куплено_по_цепи']} "
              f"итог={б['итог_записи_sol']} причина={б['причина'][:120]}")
    for о in отказы[:10]:
        print(f"ОТКАЗ {о['cid']}: {о['почему']}")
    if свод["худшее_расхождение_sol"] is not None and свод["худшее_расхождение_sol"] > ПОРОГ_SOL:
        print(f"ВНИМАНИЕ: худшее расхождение {свод['худшее_расхождение_sol']} SOL "
              f"больше порога {ПОРОГ_SOL} -- учёт и цепь разошлись", file=sys.stderr)
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
