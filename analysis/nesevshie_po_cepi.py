#!/usr/bin/env python3
"""Разбор не севших отправок полосы ПО ЦЕПИ + распределение ответа Bloom.

ЗАЧЕМ (пункт 2 владельца 26.09). Из 17 отправленных сделок полосы с 11:33Z село
10. По каждой не севшей надо знать одно из двух: она не дошла до цепи вовсе или
села с ошибкой -- и тогда с каким кодом; отдельно -- списан ли расход у тех, что
сели с ошибкой (у отклонённой транзакции чаевые и вход откатываются, а приоритет
и тариф платятся).

Работает на облачном бегунке: выжимка из журналов приходит файлом, цепь
спрашивается по подписям-кандидатам (их до шести на одну покупку -- нонс, пул).
Только чтение. Ключей кошельков здесь нет.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

ОПЦИИ_TX = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed"}
ЛАМПОРТОВ = 1_000_000_000
РАЗМЕР_ПАКЕТА = 25


def чисто(текст) -> str:
    т = str(текст)
    т = re.sub(r"https?://[^\s\"']+", "<узел вычищен>", т)
    т = re.sub(r"api[-_]?key=[A-Za-z0-9-]+", "<ключ вычищен>", т)
    return т


def узел() -> str:
    ключ = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        raise RuntimeError("ключа узла нет в окружении (HELIUS_API_KEY)")
    return f"https://mainnet.helius-rpc.com/?api-key={ключ}"


class Узел:
    def __init__(self) -> None:
        self.обращений = 0
        self.запросов = 0
        self.ошибок = 0
        self.кэш: dict = {}

    def пакет(self, подписи: list) -> dict:
        import requests  # noqa: PLC0415
        из_ = {}
        нужно = [п for п in подписи if п not in self.кэш]
        for п in подписи:
            if п in self.кэш:
                из_[п] = self.кэш[п]
        for и in range(0, len(нужно), РАЗМЕР_ПАКЕТА):
            кусок = нужно[и:и + РАЗМЕР_ПАКЕТА]
            тело = [{"jsonrpc": "2.0", "id": j, "method": "getTransaction",
                     "params": [п, ОПЦИИ_TX]} for j, п in enumerate(кусок)]
            for попытка in range(5):
                self.обращений += 1
                от = requests.post(узел(), json=тело, timeout=60)
                if от.status_code == 429:
                    time.sleep(1.5 * (попытка + 1))
                    continue
                от.raise_for_status()
                ответ = от.json()
                break
            else:
                raise RuntimeError("узел отвечает 429 пять попыток подряд")
            self.запросов += len(кусок)
            по_id = {о.get("id"): о for о in (ответ if isinstance(ответ, list) else [ответ])
                     if isinstance(о, dict)}
            for j, п in enumerate(кусок):
                о = по_id.get(j) or {}
                tx = None if "error" in о else о.get("result")
                if "error" in о:
                    self.ошибок += 1
                self.кэш[п] = tx
                из_[п] = tx
            time.sleep(0.15)
        return из_


def код_ошибки(err) -> tuple:
    """(короткий код, вид словами). Вид -- по слову владельца: проскальзывание,
    blockhash, nonce, другое."""
    if err is None:
        return "", "села без ошибки"
    текст = json.dumps(err, ensure_ascii=False)
    if "InstructionError" in текст:
        try:
            подробно = err["InstructionError"][1]
        except Exception:  # noqa: BLE001
            подробно = текст
        код = (json.dumps(подробно, ensure_ascii=False)
               if not isinstance(подробно, str) else подробно)
        # Пулы отвечают своими номерами; 6040 -- отказ пула по минимуму
        # (живой пример 26.09 00:04:23Z, покупка 0.05 отклонена).
        вид = "проскальзывание или минимум пула" if "6040" in код or "6001" in код \
            else "ошибка инструкции"
        return код[:60], вид
    if "BlockhashNotFound" in текст:
        return "BlockhashNotFound", "blockhash"
    if "Nonce" in текст:
        return текст[:60], "nonce"
    if "AlreadyProcessed" in текст:
        return "AlreadyProcessed", "дубль подписи"
    return текст[:60], "другое"


def натив(tx: dict, кошелёк: str):
    мета = (tx or {}).get("meta") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in ((((tx or {}).get("transaction") or {}).get("message") or {})
                       .get("accountKeys") or [])]
    if кошелёк not in ключи:
        return None
    и = ключи.index(кошелёк)
    до = мета.get("preBalances") or []
    после = мета.get("postBalances") or []
    if и >= len(до) or и >= len(после):
        return None
    return round((int(после[и]) - int(до[и])) / ЛАМПОРТОВ, 9)


def плательщик(tx: dict) -> str:
    сырые = ((((tx or {}).get("transaction") or {}).get("message") or {})
             .get("accountKeys") or [])
    if not сырые:
        return ""
    п = сырые[0]
    return (п.get("pubkey") if isinstance(п, dict) else п) or ""


def квантиль(значения, доля):
    if not значения:
        return None
    з = sorted(значения)
    и = min(len(з) - 1, int(round(доля * (len(з) - 1))))
    return round(з[и], 1)


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--vyzhimka", required=True)
    р.add_argument("--out", required=True)
    а = р.parse_args()
    д = json.loads(Path(а.vyzhimka).read_text(encoding="utf-8"))
    уз = Узел()

    строки, свод = [], {"отправлено": 0, "село": 0, "не_село": 0,
                         "не_дошло_до_цепи": 0, "села_с_ошибкой": 0,
                         "без_кандидатов": 0}
    все_подписи = []
    for с in д.get("сделки") or []:
        все_подписи += с.get("кандидаты") or []
    txs = {}
    if все_подписи:
        txs = уз.пакет(list(dict.fromkeys(все_подписи)))

    for с in д.get("сделки") or []:
        канд = с.get("кандидаты") or []
        нашлись = [(п, txs.get(п)) for п in канд if txs.get(п)]
        строка = {"cid": с.get("client_order_id"), "минт": (с.get("mint") or "")[:10],
                  "группа": с.get("lane_group"), "вход_sol": с.get("sol_in"),
                  "utc": с.get("ts_intent_utc"), "состояние": с.get("state"),
                  "chain_ok": с.get("chain_ok"), "кандидатов": len(канд),
                  "нашлось_на_цепи": len(нашлись)}
        if not канд:
            строка["вид"] = "подписи в записи нет -- отправки не было"
            свод["без_кандидатов"] += 1
            строки.append(строка)
            continue
        свод["отправлено"] += 1
        if not нашлись:
            строка["вид"] = "не дошла до цепи вовсе (ни одна из подписей не найдена)"
            свод["не_село"] += 1
            свод["не_дошло_до_цепи"] += 1
            строки.append(строка)
            continue
        # Если среди найденных есть без ошибки -- сделка села.
        удачные = [(п, tx) for п, tx in нашлись if not ((tx.get("meta") or {}).get("err"))]
        цель = (удачные or нашлись)[0]
        подпись, tx = цель
        err = (tx.get("meta") or {}).get("err")
        код, вид = код_ошибки(err)
        кошелёк = плательщик(tx)
        д_натив = натив(tx, кошелёк)
        строка.update(подпись=подпись, слот=tx.get("slot"), код_ошибки=код, вид=вид,
                      кошелёк_плательщик=кошелёк[:8],
                      дельта_кошелька_sol=д_натив,
                      комиссия_sol=round((tx.get("meta") or {}).get("fee", 0) / ЛАМПОРТОВ, 9))
        if удачные:
            свод["село"] += 1
        else:
            свод["не_село"] += 1
            свод["села_с_ошибкой"] += 1
            строка["расход_списан"] = (д_натив is not None and д_натив < 0)
        строки.append(строка)

    # ВРЕМЯ ОТВЕТА BLOOM -- из записей позиций (там его пишет исполнитель), а
    # журнал вызовов API этого поля не содержит вовсе: 544 строки, ноль с
    # bloom_ms. Если выжимка старая и поля нет -- берём прежний источник, чтобы
    # отчёт не врал молчанием.
    мс = д.get("bloom_ms_iz_pozicij") or д.get("bloom_ms") or []
    ответ_bloom = {
        "источник": ("записи позиций (bloom_ms)" if д.get("bloom_ms_iz_pozicij")
                     else "журнал вызовов API"),
        "ответов": len(мс),
        "медиана_мс": round(statistics.median(мс), 1) if мс else None,
        "p90_мс": квантиль(мс, 0.9),
        "макс_мс": round(max(мс), 1) if мс else None,
        "больше_1s": sum(1 for х in мс if х > 1000.0),
    }
    итог = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "с": д.get("с"), "свод": свод, "ответ_bloom": ответ_bloom,
            "обращений_к_узлу": уз.обращений, "запросов": уз.запросов,
            "ошибок_узла": уз.ошибок, "сделки": строки}
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"свод": свод, "ответ_bloom": ответ_bloom}, ensure_ascii=False, indent=1))
    for с in строки:
        if с.get("вид") and с.get("вид") != "села без ошибки":
            print(f"{с.get('utc')} {с.get('минт')} {с.get('группа')} -- {с.get('вид')}"
                  + (f" код={с.get('код_ошибки')}" if с.get("код_ошибки") else "")
                  + (f" расход_списан={с.get('расход_списан')}"
                     if "расход_списан" in с else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
