#!/usr/bin/env python3
"""Образцы НАСТОЯЩИХ покупок по типу пула -- для сборщика следующего строителя.

ЗАЧЕМ. Раскладка счетов у каждого типа пула устанавливается ТОЛЬКО по живым
сделкам (так сделаны все уже работающие строители: кривая -- по 5 сделкам,
DAMM v2 -- по 17, продажа CPMM -- по 34). Документации мы не верим: в ней нет
разновидностей инструкции и порядка счетов у конкретной версии программы.

КАК. Журнал решений на хосте уже содержит программу пула по каждому сигналу
(строка тени). Отсюда берутся подписи сделок ИСТОЧНИКОВ по нужной программе,
каждая читается getTransaction (версия 1 тоже), и по ней определяется хранилище
пула и котировка теми же функциями, которыми это делает полоса
(c2_common.identify_pool, c2_pool_programs.pool_program). Образец пишется в том
же виде, что уже лежащие в data/c2_pool_samples.

Только чтение. Ни одной отправки в цепь.
"""
from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

КОД = os.environ.get("BLOOM_CODE_DIR", "/home/bot/bloom_executor")
if КОД and Path(КОД).exists():
    sys.path.insert(0, КОД)
КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

ВЕРСИЯ_TX = 1


def узел() -> str:
    к = (os.environ.get("HELIUS_API") or os.environ.get("HELIUS_API_KEY") or "").strip()
    if not к:
        raise SystemExit("СБОЙ: ключ Helius не задан в окружении")
    return f"https://mainnet.helius-rpc.com/?api-key={к}"


def rpc(метод: str, параметры: list, *, повторов: int = 3):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    пауза, последняя = 0.3, None
    for _ in range(повторов):
        req = urllib.request.Request(узел(), data=тело,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
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


def в_секунды(с: str) -> float:
    с = str(с).replace("Z", "").replace("T", " ")
    return float(calendar.timegm(time.strptime(с[:19], "%Y-%m-%d %H:%M:%S")))


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


def главное() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--program", required=True, help="адрес программы пула")
    р.add_argument("--skolko", type=int, default=12)
    р.add_argument("--since-utc", default="")
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--out", default="/tmp/obrazcy_pula.json")
    а = р.parse_args()
    порог = в_секунды(а.since_utc) if а.since_utc else 0.0

    подписи: dict = {}
    строк = 0
    for путь in sorted(glob.glob(str(Path(а.state_dir) / "decisions.jsonl*"))):
        for з in строки(путь):
            строк += 1
            if з.get("pool_program") != а.program:
                continue
            t = з.get("ts")
            if t is None and з.get("ts_utc"):
                try:
                    t = в_секунды(з["ts_utc"])
                except ValueError:
                    t = None
            if t is not None and порог and float(t) < порог:
                continue
            п = з.get("signature") or з.get("source_sig")
            минт, ист = з.get("mint"), з.get("source")
            if not п or not минт or not ист or п in подписи:
                continue
            подписи[п] = {"source": ист, "mint": минт, "ts_utc": з.get("ts_utc")}

    итог = {"program": а.program, "strok_zhurnala": строк,
             "podpisey_naydeno": len(подписи), "skolko_prosili": а.skolko,
             "snyato_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "obrazcov": 0, "otkazy": {}, "obrazcy": []}
    try:
        import c2_common as C  # noqa: PLC0415
        import c2_pool_programs as PP  # noqa: PLC0415
        import c2_swap_build as SB  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        итог["pochemu_net"] = f"модули сборки не загрузились: {type(exc).__name__}"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1

    for п, св in list(подписи.items()):
        if итог["obrazcov"] >= а.skolko:
            break
        tx = rpc("getTransaction", [п, {"encoding": "jsonParsed",
                                         "maxSupportedTransactionVersion": ВЕРСИЯ_TX}])
        if not isinstance(tx, dict) or not tx.get("meta"):
            итог["otkazy"]["транзакцию узел не отдал"] = (
                итог["otkazy"].get("транзакцию узел не отдал", 0) + 1)
            continue
        пул = C.identify_pool(tx, св["source"], св["mint"])
        if not пул.get("ok"):
            ключ = str(пул.get("why_not") or "пул не определён")[:80]
            итог["otkazy"][ключ] = итог["otkazy"].get(ключ, 0) + 1
            continue
        прог = PP.pool_program(tx, пул.get("pool_vault"), SB._labels())
        if прог.get("pool_program") != а.program:
            ключ = f"программа по хранилищу другая: {прог.get('pool_program')}"
            итог["otkazy"][ключ] = итог["otkazy"].get(ключ, 0) + 1
            continue
        итог["obrazcy"].append({"source": св["source"], "mint": св["mint"],
                                 "pool_vault": пул.get("pool_vault"),
                                 "quote_mint": пул.get("quote_mint"),
                                 "signature": п, "ts_utc": св.get("ts_utc"),
                                 "tx": tx})
        итог["obrazcov"] += 1
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False), encoding="utf-8")
    краткий = {к: v for к, v in итог.items() if к != "obrazcy"}
    краткий["minty"] = [о["mint"] for о in итог["obrazcy"]]
    print(json.dumps(краткий, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(главное())
