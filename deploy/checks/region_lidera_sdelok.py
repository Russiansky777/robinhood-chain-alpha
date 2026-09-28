#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регион лидера слота ИСТОЧНИКА рядом с «увидели» по каждой сделке.

ЗАЧЕМ (слово владельца 28.09): "по каждой сделке с 18:00Z -- регион лидера
слота источника (EU / US / Asia) рядом с «увидели». Если 180-250 только у
не-EU лидеров -- это география, зонд ни при чём; если и у EU -- хост".

Как считается, без догадок:
  * «увидели» -- поле seen_lag_ms позиции (оценка по слотам), как в докладе;
  * слот источника -- поле source_slot той же позиции;
  * лидер слота -- расписание лидеров эпохи (getLeaderSchedule): в ответе
    индексы слотов от начала эпохи, начало берётся из getEpochInfo;
  * регион лидера -- готовая карта "личность -> регион" (data/leader_regions.json,
    собирается прогоном karta_regionov_liderov: gossip-адрес узла и гео по нему).

Слоты чужих эпох честно помечаются "расписания эпохи нет" -- выдумывать лидера
нельзя. Только чтение: журнал позиций потоком, две RPC-команды.
"""

from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import time

HELIUS = "https://mainnet.helius-rpc.com"
СЛОТОВ_В_ЭПОХЕ_ПО_УМОЛЧАНИЮ = 432_000


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}" if к else "https://api.mainnet-beta.solana.com"


def зов(метод: str, параметры, *, таймаут: float = 120.0) -> dict:
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    try:
        зпр = urllib.request.Request(урл(), data=тело,
                                      headers={"content-type": "application/json"})
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            д = json.loads(отв.read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if "error" in д:
        return {"ok": False, "why_not": str(д["error"])[:200]}
    return {"ok": True, "result": д.get("result"), "why_not": None}


def разобрать_время(строка: str):
    if not строка:
        return None
    т = строка.strip().replace("Z", "")
    for вид in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(time.strptime(т, вид)))
        except ValueError:
            continue
    return None


def строки(путь: str):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        ф = открыть(путь, "rt", encoding="utf-8")
    except Exception:  # noqa: BLE001
        return
    with ф:
        for стр in ф:
            стр = стр.strip()
            if not стр:
                continue
            try:
                yield json.loads(стр)
            except ValueError:
                continue


def позиции(state_dir: str) -> list:
    видели: dict = {}
    for п in sorted(glob.glob(os.path.join(state_dir, "positions*.jsonl*"))):
        for зап in строки(п):
            cid = зап.get("client_order_id") or зап.get("cid")
            if not cid:
                continue
            было = видели.get(cid) or {}
            было.update({к: v for к, v in зап.items() if v is not None})
            видели[cid] = было
    return list(видели.values())


def процентиль(ряд: list, доля: float):
    ч = sorted(x for x in ряд if isinstance(x, (int, float)))
    if not ч:
        return None
    место = min(len(ч) - 1, max(0, int(round(доля * (len(ч) - 1)))))
    return round(float(ч[место]), 1)


def карта_регионов(путь: str) -> dict:
    try:
        with open(путь, encoding="utf-8") as ф:
            д = json.load(ф)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}", "по_лидеру": {},
                 "собрано_utc": None}
    return {"ok": True, "why_not": None, "по_лидеру": д.get("по_лидеру") or {},
             "собрано_utc": д.get("собрано_utc"),
             "по_региону": д.get("по_региону")}


def расписание_эпохи() -> dict:
    """{слот: личность} для ТЕКУЩЕЙ эпохи плюс её границы."""
    о = зов("getEpochInfo", [])
    if not о["ok"]:
        return {"ok": False, "why_not": о["why_not"], "по_слоту": {}}
    и = о["result"] or {}
    абс, индекс = и.get("absoluteSlot"), и.get("slotIndex")
    if not isinstance(абс, int) or not isinstance(индекс, int):
        return {"ok": False, "why_not": "getEpochInfo без слотов", "по_слоту": {}}
    начало = абс - индекс
    всего = и.get("slotsInEpoch") or СЛОТОВ_В_ЭПОХЕ_ПО_УМОЛЧАНИЮ
    о2 = зов("getLeaderSchedule", [начало, {"commitment": "confirmed"}])
    if not о2["ok"]:
        return {"ok": False, "why_not": о2["why_not"], "по_слоту": {},
                 "начало": начало, "конец": начало + всего}
    по_слоту: dict = {}
    for личность, индексы in (о2["result"] or {}).items():
        for и_ in индексы or []:
            по_слоту[начало + int(и_)] = личность
    return {"ok": True, "why_not": None, "по_слоту": по_слоту,
             "начало": начало, "конец": начало + всего, "эпоха": и.get("epoch")}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="2026-09-28T18:00:00Z")
    р.add_argument("--po", default="")
    р.add_argument("--regiony", default="leader_regions.json")
    р.add_argument("--out", default="")
    а = р.parse_args()

    начало = разобрать_время(а.s)
    конец = разобрать_время(а.po) if а.po else time.time()
    if начало is None:
        print("СБОЙ: начало окна не разобрать")
        return 1
    карта = карта_регионов(а.regiony)
    расп = расписание_эпохи()
    поз = позиции(а.state_dir)

    ряды = []
    for п in поз:
        тс = п.get("ts_intent")
        if not isinstance(тс, (int, float)) or not (начало <= float(тс) < конец):
            continue
        слот = п.get("source_slot")
        личность = None
        почему = None
        if not isinstance(слот, int):
            почему = "в записи нет слота источника"
        elif not расп.get("ok"):
            почему = f"расписания эпохи нет: {расп.get('why_not')}"
        elif not (расп["начало"] <= слот < расп["конец"]):
            почему = "слот чужой эпохи -- расписание не спрашивали"
        else:
            личность = расп["по_слоту"].get(слот)
            if not личность:
                почему = "лидер этого слота в расписании не найден"
        регион = (карта["по_лидеру"].get(личность) if личность else None)
        if личность and not регион:
            почему = почему or "личность есть, региона в карте нет"
        ряды.append({
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(тс))),
            "минт": п.get("mint"), "источник": п.get("source"),
            "группа": п.get("lane_group"), "слот_источника": слот,
            "лидер": личность, "регион": регион, "why_not": почему,
            "увидели_мс": п.get("seen_lag_ms"),
            "решили_мс": (round((float(п["ts_intent"]) - float(п["signal_recv_ts"]))
                                 * 1000.0, 1)
                           if isinstance(п.get("signal_recv_ts"), (int, float))
                           and п.get("signal_recv_ts") else None),
        })
    ряды.sort(key=lambda x: x["utc"])

    по_региону: dict = {}
    for р_ in ряды:
        имя = р_["регион"] or "региона нет"
        с_ = по_региону.setdefault(имя, {"n": 0, "увидели": [], "решили": []})
        с_["n"] += 1
        if isinstance(р_["увидели_мс"], (int, float)):
            с_["увидели"].append(float(р_["увидели_мс"]))
        if isinstance(р_["решили_мс"], (int, float)):
            с_["решили"].append(float(р_["решили_мс"]))
    свод = {}
    for имя, с_ in по_региону.items():
        свод[имя] = {
            "сделок": с_["n"],
            "увидели": {"n": len(с_["увидели"]), "p50": процентиль(с_["увидели"], 0.5),
                         "p90": процентиль(с_["увидели"], 0.9),
                         "min": (round(min(с_["увидели"]), 1) if с_["увидели"] else None),
                         "max": (round(max(с_["увидели"]), 1) if с_["увидели"] else None)},
            "решили": {"n": len(с_["решили"]), "p50": процентиль(с_["решили"], 0.5),
                        "p90": процентиль(с_["решили"], 0.9)},
            # СКОЛЬКО СДЕЛОК ПОПАЛИ В ОКНО 180-250 мс -- ровно то, о чём спросил
            # владелец: если такие только у не-EU, дело в географии.
            "увидели_180_250": sum(1 for x in с_["увидели"] if 180.0 <= x <= 250.0),
            "увидели_выше_250": sum(1 for x in с_["увидели"] if x > 250.0),
        }

    итог = {"с": а.s, "по": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(конец)),
             "карта_регионов": {"ok": карта["ok"], "собрано_utc": карта.get("собрано_utc"),
                                 "why_not": карта.get("why_not"),
                                 "по_региону_в_карте": карта.get("по_региону")},
             "расписание": {"ok": расп.get("ok"), "эпоха": расп.get("эпоха"),
                             "начало": расп.get("начало"), "конец": расп.get("конец"),
                             "why_not": расп.get("why_not")},
             "сделок": len(ряды), "по_региону": свод, "сделки": ряды}

    print(json.dumps({к: итог[к] for к in ("с", "по", "сделок", "карта_регионов",
                                            "расписание")}, ensure_ascii=False))
    print()
    print("| время | минт | группа | слот источника | лидер | регион | увидели, мс | решили, мс |")
    for р_ in ряды:
        print("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            р_["utc"][11:19], (р_["минт"] or "")[:8], р_["группа"],
            р_["слот_источника"], (р_["лидер"] or (р_["why_not"] or "--"))[:20],
            р_["регион"] or "--", р_["увидели_мс"], р_["решили_мс"]))
    print()
    for имя, с_ in sorted(свод.items(), key=lambda т: -т[1]["сделок"]):
        у = с_["увидели"]
        print(f"{имя:16s} сделок {с_['сделок']:3d} | увидели p50 {у['p50']} "
              f"p90 {у['p90']} (min {у['min']}, max {у['max']}) | "
              f"в 180-250 мс: {с_['увидели_180_250']}, выше 250: {с_['увидели_выше_250']} | "
              f"решили p50 {с_['решили']['p50']}")
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
