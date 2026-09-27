#!/usr/bin/env python3
"""Выгрузка сделок полосы с ПОЛНЫМИ минтами и подписями (только чтение).

ЗАЧЕМ. В таблице полосы (data/lane_table.json) минты и подписи урезаны до
десяти-шестнадцати знаков -- по ним в цепь не сходишь. Замер толпы за источником
требует полных значений, поэтому они берутся из журнала позиций на хосте.

Журнал читается ПОТОКОМ, включая ротированные .gz. Ни ключей, ни сети.
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

МЕТКА_ПОЛОСЫ = "own_send"


def метка(текст: str) -> float:
    т = текст.replace("Z", "").replace("z", "")
    for формат in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return calendar.timegm(time.strptime(т, формат))
        except ValueError:
            continue
    raise SystemExit(f"СТОП: метку {текст!r} не разобрать")


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if с:
                    yield с
    except OSError as exc:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {путь} ({type(exc).__name__})", file=sys.stderr)


def файлы(каталог: str) -> list:
    из_ = sorted(glob.glob(os.path.join(каталог, "positions.jsonl*")))
    осн = os.path.join(каталог, "positions.jsonl")
    if осн in из_:
        из_.remove(осн)
        из_.append(осн)
    return из_


def наши_подписи(п: dict) -> list:
    """Все подписи, под которыми наша покупка могла сесть: вариант на nonce даёт
    по подписи на отправителя, а сядет ровно одна."""
    из_ = []
    for поле in ("lane_signature", "lane_signature_local"):
        зн = п.get(поле)
        if isinstance(зн, str) and зн:
            из_.append(зн)
    for зн in (п.get("lane_pool_candidates") or []):
        if isinstance(зн, str) and зн:
            из_.append(зн)
    for зн in (п.get("signatures") or []):
        if isinstance(зн, str) and зн:
            из_.append(зн)
    видели, ответ = set(), []
    for зн in из_:
        if зн not in видели:
            видели.add(зн)
            ответ.append(зн)
    return ответ


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="2026-09-25T18:00", help="с какой метки UTC")
    р.add_argument("--out", default="")
    а = р.parse_args()
    с_ = метка(а.s)
    по_cid: dict = {}
    просмотрено = 0
    for путь in файлы(а.state_dir):
        for с in строки(путь):
            просмотрено += 1
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict) or з.get("lane") != МЕТКА_ПОЛОСЫ:
                continue
            т = з.get("ts_intent") or з.get("ts_sent")
            if not isinstance(т, (int, float)) or float(т) < с_:
                continue
            cid = з.get("client_order_id")
            if not cid:
                continue
            # Позиция дописывается много раз: берём последнюю запись по cid и
            # накапливаем поля, а не заменяем -- часть полей приходит позже.
            в = по_cid.setdefault(cid, {})
            for к, зн in з.items():
                if зн is not None:
                    в[к] = зн
    ряды = []
    for cid, п in sorted(по_cid.items()):
        ряды.append({
            "cid": cid,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(float(п.get("ts_intent") or 0))),
            "group": п.get("lane_group"),
            "mint": п.get("mint"),
            "size_sol": п.get("sol_in"),
            "source": п.get("source"),
            "source_sig": п.get("source_sig"),
            "source_slot": п.get("source_slot"),
            "our_slot": п.get("own_tx_seen_slot") or п.get("our_slot"),
            "our_signatures": наши_подписи(п),
            "our_block_index": п.get("block_index"),
            "source_block_index": п.get("source_block_index"),
            "wallet": п.get("lane_wallet"),
            "chain_ok": п.get("chain_ok"),
        })
    свод = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "с": а.s, "строк_просмотрено": просмотрено, "сделок": len(ряды),
             "с_минтом_и_слотом": sum(1 for р_ in ряды
                                       if р_["mint"] and р_["source_slot"]),
             "ряды": ряды}
    текст = json.dumps(свод, ensure_ascii=False, indent=1)
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            ф.write(текст + "\n")
        print(json.dumps({к: свод[к] for к in
                           ("снято_utc", "с", "строк_просмотрено", "сделок",
                            "с_минтом_и_слотом")}, ensure_ascii=False))
    else:
        print(текст)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
