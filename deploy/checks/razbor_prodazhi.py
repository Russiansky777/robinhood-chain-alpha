#!/usr/bin/env python3
"""Разбор одной продажи по журналу позиций: факты, а не гипотезы.

ЗАЧЕМ. Владелец спрашивает по конкретной продаже: почему отказала подпись
Jupiter, какое удержание применилось, где ждали между попытками. Ответ обязан
быть фактом из записи позиции, поэтому прогон печатает ВСЕ поля позиции и
считает по ним времена: от намерения до отправки, до срока продажи, до попытки,
до посадки.

Только чтение: журнал позиций (потоком) и файл групп. Ни подписи, ни отправки.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

# Поля, по которым считаются времена. Имена -- те, что пишет служба.
ВРЕМЕНА = ("ts_intent", "ts_sent", "ts_accepted", "ts_last_sell_attempt",
            "sell_landed_ts", "ts_closed")


def строки(путь: str):
    """Строки журнала потоком: файл идёт на сотни тысяч строк."""
    try:
        with open(путь, encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if not с.startswith("{"):
                    continue
                try:
                    yield json.loads(с)
                except ValueError:
                    continue
    except OSError:
        return


def позиции(state_dir: str) -> dict:
    из_: dict = {}
    for путь in sorted(glob.glob(str(Path(state_dir) / "positions.jsonl*"))):
        for r in строки(путь):
            cid = r.get("client_order_id")
            if cid:
                из_.setdefault(cid, {}).update(r)
    return из_


def подходит(поз: dict, что: str) -> bool:
    """Позиция про этот минт, подпись или cid (хватит и начала строки)."""
    ч = что.strip()
    if not ч:
        return False
    for поле in ("mint", "client_order_id", "source_sig", "lane_signature",
                 "lane_landed_signature", "buy_signature"):
        з = поз.get(поле)
        if isinstance(з, str) and (з.startswith(ч) or ч.startswith(з[:8])
                                   and len(з) >= 8):
            return True
    подписи = поз.get("last_sell_signatures") or []
    return any(isinstance(з, str) and з.startswith(ч) for з in подписи)


def времена(поз: dict) -> dict:
    """Отметки времени позиции и разницы между ними -- в секундах."""
    из_ = {}
    for поле in ВРЕМЕНА:
        з = поз.get(поле)
        if isinstance(з, (int, float)) and з:
            из_[поле] = {"ts": round(float(з), 3),
                          "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(float(з)))}
    основа = поз.get("ts_sent") or поз.get("ts_intent")
    срок = поз.get("sell_after_s")
    if isinstance(основа, (int, float)) and isinstance(срок, (int, float)):
        пора = float(основа) + float(срок)
        из_["пора_продавать"] = {
            "ts": round(пора, 3),
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(пора))}
        for имя, поле in (("от_поры_до_попытки_с", "ts_last_sell_attempt"),
                           ("от_поры_до_посадки_с", "sell_landed_ts")):
            з = поз.get(поле)
            if isinstance(з, (int, float)) and з:
                из_[имя] = round(float(з) - пора, 3)
    for имя, a, b in (("удержание_до_попытки_с", "ts_sent", "ts_last_sell_attempt"),
                       ("удержание_до_посадки_с", "ts_sent", "sell_landed_ts")):
        п1, п2 = поз.get(a), поз.get(b)
        if isinstance(п1, (int, float)) and isinstance(п2, (int, float)) and п2:
            из_[имя] = round(float(п2) - float(п1), 3)
    return из_


def политика_группы(файл: str, группа: str | None) -> dict:
    """Политика группы из файла групп -- КАК ОНА ЛЕЖИТ, без нормализации."""
    if not группа:
        return {"why_not": "у позиции нет группы"}
    try:
        д = json.loads(Path(файл).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}
    г = ((д.get("groups") or {}).get(группа)) or {}
    return {к: v for к, v in г.items()
            if к not in ("addresses", "by_signal", "snipers",
                          "dropped_by_credits")}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--gruppy", default="/home/bot/data/sources_2026-09-25.json")
    р.add_argument("--chto", default="", help="минты, cid или подписи через запятую")
    р.add_argument("--out", default="")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    искомое = [x.strip() for x in (а.chto or "").split(",") if x.strip()]
    if not искомое:
        print("нужен --chto")
        return 2
    все = позиции(а.state_dir)
    итог = {"искали": искомое, "нашли": [], "только_чтение": True}
    for cid, поз in все.items():
        if not any(подходит(поз, ч) for ч in искомое):
            continue
        запись = {"client_order_id": cid, "поля": поз, "времена": времена(поз),
                   "политика_группы": политика_группы(а.gruppy,
                                                       поз.get("lane_group"))}
        итог["нашли"].append(запись)
    print(f"позиций в журнале {len(все)}, подошло {len(итог['нашли'])}")
    for з in итог["нашли"]:
        print(json.dumps(з, ensure_ascii=False, indent=1)[:6000])
    if а.out:
        Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 0


def самопроверка() -> int:
    сбоев = всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    поз = {"mint": "Ai66LHZG9MabcdefghijklmnopqrstuvwxyzABCDEF",
            "client_order_id": "lane-1", "ts_sent": 1000.0,
            "sell_after_s": 28.8, "ts_last_sell_attempt": 1075.0,
            "sell_landed_ts": 1077.0, "lane_group": "batch5"}
    chk("позиция находится по началу минта", подходит(поз, "Ai66LHZG9M"))
    chk("чужой минт не находится", not подходит(поз, "ZZZZZZZZ"))
    в = времена(поз)
    chk(f"пора продавать посчитана ({в.get('пора_продавать', {}).get('ts')})",
        в["пора_продавать"]["ts"] == 1028.8)
    chk(f"от поры до попытки {в.get('от_поры_до_попытки_с')} с",
        abs(в["от_поры_до_попытки_с"] - 46.2) < 1e-6)
    chk(f"удержание до посадки {в.get('удержание_до_посадки_с')} с",
        в["удержание_до_посадки_с"] == 77.0)
    chk("без срока продажи поры нет",
        "пора_продавать" not in времена({"ts_sent": 1.0}))
    print(f"самопроверка разбора продажи: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


if __name__ == "__main__":
    sys.exit(main())
