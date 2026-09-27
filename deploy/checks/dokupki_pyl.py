#!/usr/bin/env python3
"""Докупки источников: сколько из них -- пыль (остаток меньше 1 % покупки).

ЗАЧЕМ (вопрос владельца 27.09, вечер, п.2). Определение докупки в детекторе --
"у источника есть ЛЮБОЙ остаток токена": признак first_entry считается из meta
самой сделки источника как `not in_pre or pre_raw == 0`. Никакого окна "покупал
в последние N часов" в коде нет. Отсюда вопрос: сколько из сегодняшних докупок
были с остатком меньше 1 % от размера покупки, то есть по сути пылью.

КАК СЧИТАЕТСЯ. Журнал решений даёт подписи с кодом SKIP_TARGET_INCREASE_POSITION.
По каждой подписи берётся сама транзакция источника (getTransaction) и из её
pre/postTokenBalances -- остаток ДО покупки и прирост за покупку по паре
"владелец = источник, минт = покупаемый". Доля = остаток_до / прирост. Никаких
догадок: подпись, у которой транзакция не пришла или балансов по паре нет,
идёт в отдельный счётчик, а не в числитель или знаменатель.

Измерительный код (правило 8): самопроверок нет, ни одной отправки в цепь.
Только чтение: журнал потоком, транзакции -- getTransaction.
"""
from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

КОД_ДОКУПКА = "SKIP_TARGET_INCREASE_POSITION"


def в_секунды(с: str) -> float:
    с = str(с).replace("Z", "").replace("T", " ")
    return float(calendar.timegm(time.strptime(с[:19], "%Y-%m-%d %H:%M:%S")))


def узел() -> str:
    к = (os.environ.get("HELIUS_API") or os.environ.get("HELIUS_API_KEY") or "").strip()
    if not к:
        raise SystemExit("СБОЙ: ключ Helius не задан в окружении")
    return f"https://mainnet.helius-rpc.com/?api-key={к}"


def rpc(метод: str, параметры: list, *, повторов: int = 3):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    пауза, последняя = 0.35, None
    for _ in range(повторов):
        req = urllib.request.Request(узел(), data=тело,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                о = json.loads(r.read().decode())
            if "error" in о:
                последняя = str(о["error"])[:160]
            else:
                return о.get("result")
        except Exception as exc:  # noqa: BLE001
            последняя = type(exc).__name__
        time.sleep(пауза)
        пауза *= 2
    return {"__почему_нет": последняя}


def файлы(каталог: str, имя: str) -> list:
    п = sorted(glob.glob(str(Path(каталог) / f"{имя}*")))
    return [x for x in п if not x.endswith(".tmp")]


def строки(путь: str):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        with открыть(путь, "rt", encoding="utf-8", errors="replace") as f:
            for с in f:
                с = с.strip()
                if not с:
                    continue
                try:
                    yield json.loads(с)
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return


def остаток_и_прирост(tx: dict, источник: str, минт: str):
    """(остаток_до_raw, прирост_raw) по паре владелец+минт. None -- балансов нет."""
    meta = (tx or {}).get("meta") or {}
    до = {}
    for б in meta.get("preTokenBalances") or []:
        if б.get("owner") == источник and б.get("mint") == минт:
            до[б.get("accountIndex")] = int(
                ((б.get("uiTokenAmount") or {}).get("amount")) or 0)
    после = {}
    for б in meta.get("postTokenBalances") or []:
        if б.get("owner") == источник and б.get("mint") == минт:
            после[б.get("accountIndex")] = int(
                ((б.get("uiTokenAmount") or {}).get("amount")) or 0)
    if not после:
        return None
    было = sum(до.values())
    стало = sum(после.values())
    return было, стало - было


def главное() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", default=None,
                    help="с какого времени (по умолчанию -- полночь UTC сегодня)")
    р.add_argument("--predel", type=int, default=400,
                    help="сколько подписей смотреть (защита от лишних кредитов)")
    р.add_argument("--out", default="/tmp/dokupki_pyl.json")
    а = р.parse_args()
    порог = (в_секунды(а.since_utc) if а.since_utc
              else в_секунды(time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())))

    подписи: dict = {}
    строк = 0
    for путь in файлы(а.state_dir, "decisions.jsonl"):
        for з in строки(путь):
            строк += 1
            if з.get("code") != КОД_ДОКУПКА:
                continue
            t = з.get("ts")
            if t is None and з.get("ts_utc"):
                try:
                    t = в_секунды(з["ts_utc"])
                except ValueError:
                    t = None
            if t is not None and float(t) < порог:
                continue
            п = з.get("signature")
            if not п or п in подписи:
                continue
            подписи[п] = {"ts_utc": з.get("ts_utc"), "source": з.get("source"),
                           "mint": з.get("mint"),
                           "received_ui": з.get("received_ui")}

    итог = {"since_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(порог)),
             "snyato_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "strok_zhurnala": строк,
             "opredelenie_dokupki": ("first_entry = not in_pre or pre_raw == 0 -- "
                                      "ЛЮБОЙ остаток токена у источника; окна "
                                      "'покупал в последние N часов' в коде нет"),
             "dokupok_naydeno": len(подписи), "predel": а.predel}

    доли, пыль, без_данных, без_прироста, примеры = [], 0, 0, 0, []
    for п, з in list(подписи.items())[: а.predel]:
        tx = rpc("getTransaction", [п, {"encoding": "jsonParsed",
                                         "maxSupportedTransactionVersion": 0}])
        if not isinstance(tx, dict) or tx.get("__почему_нет") or not tx.get("meta"):
            без_данных += 1
            continue
        пара = остаток_и_прирост(tx, з.get("source") or "", з.get("mint") or "")
        if пара is None:
            без_данных += 1
            continue
        было, прирост = пара
        if прирост <= 0:
            без_прироста += 1
            continue
        доля = было / прирост
        доли.append(доля)
        if доля < 0.01:
            пыль += 1
            if len(примеры) < 12:
                примеры.append({"ts_utc": з.get("ts_utc"),
                                 "source": з.get("source"), "mint": з.get("mint"),
                                 "ostatok_raw": было, "prirost_raw": прирост,
                                 "dolya": round(доля, 9)})

    итог["poschitano"] = len(доли)
    итог["pyl_menshe_1pct"] = пыль
    итог["bez_dannyh"] = без_данных
    итог["bez_prirosta"] = без_прироста
    if доли:
        доли_с = sorted(доли)
        итог["dolya_mediana"] = round(statistics.median(доли_с), 9)
        итог["dolya_min"] = round(доли_с[0], 9)
        итог["dolya_max"] = round(доли_с[-1], 9)
        итог["dolya_kvartili"] = [round(доли_с[int(len(доли_с) * q)], 9)
                                   for q in (0.25, 0.5, 0.75)]
    итог["primery_pyli"] = примеры
    текст = json.dumps(итог, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(главное())
