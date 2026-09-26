#!/usr/bin/env python3
"""Наши сделки из журнала позиций -- потоком, для подбивки (задание 2).

Журнал на хосте большой, в память его не читаем: идём построчно и держим
только последнее состояние по client_order_id, а из него -- ровно те поля,
которые нужны симулятору: минт, подпись сделки источника, наш слот и размер,
подпись НАШЕЙ севшей транзакции, чья позиция и что известно об итоге.

Только чтение. Ничего не меняет.
"""
import argparse
import gzip
import json
import os
import sys
import time

ПОЛЯ = ("client_order_id", "mint", "source", "source_sig", "source_slot", "sol_in",
        "lane", "lane_group", "mode", "state", "ts_intent", "ts_intent_utc",
        "signature", "signatures", "lane_signature", "lane_landed_signature",
        "lane_landed_slot", "lane_buy_native_sol", "lane_buy_fee_sol", "qty",
        "closed_sol_net", "closed_sol_delta", "closed_signature", "chain_ok",
        "result_uncountable", "slot", "our_slot")


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default=os.environ.get("BLOOM_STATE_DIR")
                   or "/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", default="2026-09-24T00:00:00Z")
    р.add_argument("--out", default="/tmp/nashi_sdelki.json")
    а = р.parse_args()
    порог = time.mktime(time.strptime(а.since_utc, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone

    пути = [os.path.join(а.state_dir, "positions.jsonl")]
    пути += sorted(p for p in (os.path.join(а.state_dir, f)
                               for f in os.listdir(а.state_dir))
                   if os.path.basename(p).startswith("positions.jsonl.") and p.endswith(".gz"))
    по_cid: dict = {}
    прочитано = 0
    for путь in пути:
        if not os.path.exists(путь):
            continue
        for з in строки(путь):
            прочитано += 1
            cid = з.get("client_order_id")
            if not cid:
                continue
            по_cid.setdefault(cid, {}).update({к: v for к, v in з.items()
                                               if к in ПОЛЯ and v is not None})
    сделки = []
    for cid, п in по_cid.items():
        т = п.get("ts_intent")
        if т and float(т) < порог:
            continue
        сделки.append(п)
    сделки.sort(key=lambda п: float(п.get("ts_intent") or 0))
    итог = {"state_dir": а.state_dir, "since_utc": а.since_utc,
            "строк_прочитано": прочитано, "позиций_всего": len(по_cid),
            "сделок_в_окне": len(сделки), "сделки": сделки}
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False)
    print(json.dumps({к: v for к, v in итог.items() if к != "сделки"}, ensure_ascii=False))
    полоса = sum(1 for с in сделки if с.get("lane"))
    print(f"из них полосы {полоса}, площадки {len(сделки) - полоса}")
    есть_источник = sum(1 for с in сделки if с.get("source_sig"))
    есть_наша = sum(1 for с in сделки if с.get("lane_landed_signature") or с.get("signature")
                    or с.get("signatures"))
    print(f"с подписью источника {есть_источник}, с нашей подписью {есть_наша}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
