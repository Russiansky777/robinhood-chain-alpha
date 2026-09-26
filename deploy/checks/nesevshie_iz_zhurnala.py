#!/usr/bin/env python3
"""Отправленные, но не севшие сделки полосы + время ответа Bloom -- потоком.

ЗАЧЕМ (пункт 2 владельца 26.09): из 17 отправленных с 11:33Z село 10. По каждой
не севшей надо знать: не дошла до цепи вовсе или села с ошибкой (и с какой), и
списан ли расход у севших с ошибкой. Плюс распределение времени ответа Bloom.

Здесь -- ТОЛЬКО выжимка из журналов хоста: подписи-кандидаты по каждой позиции и
числа bloom_ms. Цепь спрашивает вторая часть (на облачном бегунке), потому что
ключа узла на хосте для этого разбора не нужно.

Только чтение, потоком, в память журналы не грузим.
"""
import argparse
import calendar
import gzip
import json
import os
import time

ПОЛЯ = ("client_order_id", "mint", "lane", "lane_group", "sol_in", "state",
        "ts_intent", "ts_intent_utc", "chain_ok", "lane_signature",
        "lane_signature_accepted_first", "lane_pool_candidates",
        "lane_signature_local", "lane_landed_signature", "lane_bought_raw",
        "lane_tips_total_sol", "lane_priority_lamports", "pnl_counted",
        "pnl_counted_sol", "pnl_counted_spend_sol", "closed_reason",
        "closed_sol_net", "lane_buy_native_sol", "lane_buy_fee_sol",
        "lane_send_ambiguous", "why_not")


def строки(путь):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def файлы(каталог, имя):
    из_ = [os.path.join(каталог, имя)]
    из_ += sorted(os.path.join(каталог, ф) for ф in os.listdir(каталог)
                  if ф.startswith(имя + ".") and ф.endswith(".gz"))
    return [п for п in из_ if os.path.exists(п)]


def в_секунды(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default=os.environ.get("BLOOM_STATE_DIR")
                   or "/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", required=True)
    р.add_argument("--out", default="/tmp/nesevshie.json")
    а = р.parse_args()
    порог = в_секунды(а.since_utc)

    по_cid = {}
    for путь in файлы(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if not cid:
                continue
            по_cid.setdefault(cid, {}).update({к: v for к, v in з.items()
                                               if к in ПОЛЯ and v is not None})
    сделки = []
    for cid, п in по_cid.items():
        if not п.get("lane"):
            continue
        if float(п.get("ts_intent") or 0) < порог:
            continue
        варианты = [п.get("lane_landed_signature"), п.get("lane_signature"),
                    п.get("lane_signature_accepted_first"),
                    п.get("lane_signature_local")]
        варианты += list(п.get("lane_pool_candidates") or [])
        варианты = [в for в in dict.fromkeys(варианты) if в and len(str(в)) >= 80]
        сделки.append({**{к: п.get(к) for к in ПОЛЯ if к in п},
                       "кандидаты": варианты})
    сделки.sort(key=lambda с: float(с.get("ts_intent") or 0))

    # ВРЕМЯ ОТВЕТА BLOOM. bloom_ms живёт в журнале вызовов API (stage=response).
    мс, всего_строк = [], 0
    пути_api = файлы(а.state_dir, "api_calls.jsonl")
    for путь in пути_api:
        for з in строки(путь):
            всего_строк += 1
            у = з.get("ts_utc")
            try:
                т = в_секунды(у) if isinstance(у, str) and у.endswith("Z") else None
            except ValueError:
                т = None
            if т is None or т < порог:
                continue
            зн = з.get("bloom_ms")
            if isinstance(зн, (int, float)):
                мс.append(float(зн))
    итог = {"с": а.since_utc, "сделок_полосы": len(сделки),
            "файлы_api": [os.path.basename(п) for п in пути_api],
            "строк_api_всего": всего_строк,
            "bloom_ms": sorted(мс), "сделки": сделки}
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False)
    print(json.dumps({к: v for к, v in итог.items()
                      if к not in ("сделки", "bloom_ms")}, ensure_ascii=False))
    print(f"ответов Bloom с числом: {len(мс)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
