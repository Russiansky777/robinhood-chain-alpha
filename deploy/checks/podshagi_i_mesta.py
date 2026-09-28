#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Три числа одним прогоном: подшаги «решили», место источника, симуляции продажи.

ЗАЧЕМ. После правок 28.09 вечером нужно проверить по факту, а не на слово:
  п.5 -- сколько стоит каждый подшаг решения (поле podshagi_ms в записи
         решения): гейт, подготовка веера контролей, сборка, симуляция,
         blockhash, подпись. Цель владельца -- «решили» <= 5 мс;
  п.6 -- заполняется ли место источника в блоке (source_block_index) после
         того, как у getBlock появился свой таймаут 45 с и 8 попыток;
  п.4 -- запустились ли симуляции своей продажи Pump AMM и чем кончились.

Только чтение: журнал решений и позиции читаются потоком, сети нет вовсе.
"""

from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import time

# Метка позиций полосы -- дословно как в bloom_exec_state.МЕТКА_ПОЛОСЫ.
МЕТКА_ПОЛОСЫ = "own_send"


# Имена, которые этому прогону разрешено прочитать из окружения службы. Список
# закрытый: он нужен, чтобы ответить "включена ли симуляция своей продажи" по
# факту, а не по памяти. Вторая, независимая проверка ниже отвергает всё, что
# похоже на ключ, даже если имя попало сюда по ошибке.
ФЛАГИ_КОТОРЫЕ_МОЖНО = (
    "BLOOM_SELL_OWN_SIMULATE",
    "BLOOM_SELL_VIA_JUPITER",
    "BLOOM_SELL_MIN_OUT_ZAPAS",
    "LANE_SELL_TWO_STEP",
    "BLOOM_LANE_CU_BONDING_V2",
    "BLOOM_SOURCE_BLOCK_TRIES",
    "BLOOM_NE_SELA_POSLE_S",
)
ЗАПРЕЩЁННЫЕ_ЧАСТИ = ("KEY", "TOKEN", "SECRET", "AUTH", "PASS", "PRIVATE")


def это_коммент(строка: str) -> bool:
    return строка.lstrip().startswith("#")


def флаги(env_file: str) -> dict:
    """Значения разрешённых флагов из файла окружения службы.

    Секретов тут быть не может по построению: читаются только имена из
    закрытого списка, и любое имя с KEY/TOKEN/SECRET/AUTH/PASS/PRIVATE
    отбрасывается второй проверкой, независимой от списка.
    """
    можно = tuple(и for и in ФЛАГИ_КОТОРЫЕ_МОЖНО
                   if not any(ч in и.upper() for ч in ЗАПРЕЩЁННЫЕ_ЧАСТИ))
    из_ = {"файл": env_file, "почему_нет": None,
            "значения": {и: None for и in можно}}
    try:
        with open(env_file, encoding="utf-8", errors="replace") as ф:
            for стр in ф:
                стр = стр.strip()
                if not стр or это_коммент(стр) or "=" not in стр:
                    continue
                имя, _, зн = стр.partition("=")
                имя = имя.strip().removeprefix("export ").strip()
                if имя in можно:
                    из_["значения"][имя] = зн.strip().strip('"').strip("'")
    except Exception as exc:  # noqa: BLE001
        из_["почему_нет"] = f"{type(exc).__name__}"
    return из_


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


def процентиль(ряд: list, доля: float):
    ч = sorted(x for x in ряд if isinstance(x, (int, float)))
    if not ч:
        return None
    м = min(len(ч) - 1, max(0, int(round(доля * (len(ч) - 1)))))
    return round(float(ч[м]), 3)


def время_записи(зап: dict):
    """Время записи решения: секунды эпохи из любого известного поля."""
    for поле in ("ts", "ts_intent", "ts_sent_buy"):
        зн = зап.get(поле)
        if isinstance(зн, (int, float)) and зн > 1_000_000_000:
            return float(зн)
    зн = зап.get("ts_utc")
    т = разобрать_время(зн) if isinstance(зн, str) else None
    return т


def подшаги(state_dir: str, *, с: float) -> dict:
    """p50/p90 по каждому подшагу решения из журнала решений."""
    пути = sorted(glob.glob(os.path.join(state_dir, "decisions*.jsonl*")))
    собрано: dict = {}
    записей = 0
    всего_ряд = []
    for п in пути:
        for зап in строки(п):
            ш = зап.get("podshagi_ms")
            if not isinstance(ш, dict):
                continue
            т = время_записи(зап)
            if т is not None and т < с:
                continue
            записей += 1
            for имя, зн in ш.items():
                if not isinstance(зн, (int, float)):
                    continue
                if имя == "всего":
                    всего_ряд.append(float(зн))
                else:
                    собрано.setdefault(имя, []).append(float(зн))
    из_ = {"записей": записей, "всего": {"p50": процентиль(всего_ряд, 0.5),
                                          "p90": процентиль(всего_ряд, 0.9),
                                          "max": (round(max(всего_ряд), 3)
                                                   if всего_ряд else None)}}
    из_["по_шагам"] = {
        имя: {"n": len(ряд), "p50": процентиль(ряд, 0.5),
               "p90": процентиль(ряд, 0.9), "max": round(max(ряд), 3)}
        for имя, ряд in sorted(собрано.items(), key=lambda кв: -(процентиль(кв[1], 0.5) or 0))
    }
    return из_


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


def места(state_dir: str, *, с: float) -> dict:
    """Сколько сделок с известным местом источника в блоке и почему нет."""
    из_ = {"сделок": 0, "с_местом": 0, "без_места": 0, "попыток_исчерпано": 0,
            "причины": {}, "доли": {"начало_до_0_3": 0, "хвост_от_0_7": 0,
                                     "середина": 0}}
    for п in позиции(state_dir):
        if (п.get("lane") or "") != МЕТКА_ПОЛОСЫ:
            continue
        т = п.get("ts_intent")
        if not isinstance(т, (int, float)) or float(т) < с:
            continue
        из_["сделок"] += 1
        и_, в_ = п.get("source_block_index"), п.get("source_block_total")
        if isinstance(и_, int) and isinstance(в_, int) and в_ > 0:
            из_["с_местом"] += 1
            доля = и_ / в_
            ключ = ("начало_до_0_3" if доля <= 0.3
                     else ("хвост_от_0_7" if доля >= 0.7 else "середина"))
            из_["доли"][ключ] += 1
        else:
            из_["без_места"] += 1
            почему = str(п.get("source_block_why_not") or "причина не записана")[:90]
            из_["причины"][почему] = из_["причины"].get(почему, 0) + 1
            if int(п.get("source_block_tries") or 0) >= 8:
                из_["попыток_исчерпано"] += 1
    return из_


def симуляции(state_dir: str, *, с: float) -> dict:
    """Симуляции своей продажи: счётчик, поля позиций и записи журнала сторожа.

    Где смотреть -- по коду сторожа (bloom_seller.py): счётчик лежит в
    own_sell_sim_count.json, вердикт пишется в позицию полями own_sell_sim_ok /
    own_sell_sim_why_not / own_sell_sim_units / own_sell_sim_n, а словами -- в
    seller.jsonl записью kind == "своя продажа: симуляция".
    """
    из_ = {"счётчик_файл": None, "почему_нет_счётчика": None,
            "позиции": [], "журнал": []}
    for путь in (os.path.join(state_dir, "own_sell_sim_count.json"),
                  os.path.join(state_dir, "lane_sell", "own_sell_sim_count.json")):
        try:
            with open(путь, encoding="utf-8") as ф:
                из_["счётчик_файл"] = {"путь": путь, "значение": json.load(ф)}
            break
        except Exception as exc:  # noqa: BLE001
            из_["почему_нет_счётчика"] = f"{путь}: {type(exc).__name__}"

    for п in позиции(state_dir):
        if п.get("own_sell_sim_n") is None and п.get("own_sell_sim_ok") is None:
            continue
        из_["позиции"].append({
            "cid": п.get("client_order_id") or п.get("cid"),
            "минт": п.get("mint"), "программа": п.get("pool_program"),
            "n": п.get("own_sell_sim_n"), "ok": п.get("own_sell_sim_ok"),
            "cu": п.get("own_sell_sim_units"),
            "почему": п.get("own_sell_sim_why_not")})

    for имя in ("seller.jsonl", os.path.join("lane_sell", "seller.jsonl")):
        for зап in строки(os.path.join(state_dir, имя)):
            if "симуляция" not in str(зап.get("kind") or ""):
                continue
            т = время_записи(зап)
            if т is not None and т < с:
                continue
            из_["журнал"].append({к: зап.get(к) for к in
                                   ("kind", "cid", "ok", "why_not", "units",
                                    "n", "логи_хвост") if к in зап})
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="", help="с какого времени (пусто -- за 2 часа)")
    р.add_argument("--env-file", default="/etc/bloom-executor/env",
                    help="файл окружения службы: читаются только флаги из списка")
    р.add_argument("--out", default="")
    а = р.parse_args()
    с = разобрать_время(а.s) if а.s else (time.time() - 7200)

    итог = {"с": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(с)),
             "по": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "подшаги": подшаги(а.state_dir, с=с),
             "места": места(а.state_dir, с=с),
             "симуляции": симуляции(а.state_dir, с=с),
             "флаги": флаги(а.env_file)}

    п = итог["подшаги"]
    print(f"ПОДШАГИ РЕШЕНИЯ: записей {п['записей']}, всего p50 {п['всего']['p50']} мс, "
          f"p90 {п['всего']['p90']}, max {п['всего']['max']}")
    for имя, з in п["по_шагам"].items():
        print(f"   {имя:20s} n={з['n']:3d} p50={з['p50']} p90={з['p90']} max={з['max']}")
    м = итог["места"]
    print(f"МЕСТО ИСТОЧНИКА: сделок {м['сделок']}, с местом {м['с_местом']}, "
          f"без места {м['без_места']} (попыток исчерпано {м['попыток_исчерпано']})")
    print(f"   доли: начало <= 0.3: {м['доли']['начало_до_0_3']}, "
          f"хвост >= 0.7: {м['доли']['хвост_от_0_7']}, "
          f"середина: {м['доли']['середина']}")
    for почему, н in sorted(м["причины"].items(), key=lambda кв: -кв[1])[:5]:
        print(f"   без места: {н} x {почему}")
    с_ = итог["симуляции"]
    print("СИМУЛЯЦИИ СВОЕЙ ПРОДАЖИ: счётчик "
          + json.dumps(с_["счётчик_файл"], ensure_ascii=False)
          + f" {с_.get('почему_нет_счётчика') or ''}")
    print(f"   позиций с вердиктом {len(с_['позиции'])}, "
          f"записей журнала {len(с_['журнал'])}")
    for з in с_["позиции"][:10]:
        print(f"   поз: {json.dumps(з, ensure_ascii=False)[:220]}")
    for з in с_["журнал"][:10]:
        print(f"   журнал: {json.dumps(з, ensure_ascii=False)[:300]}")
    ф_ = итог["флаги"]
    print(f"ФЛАГИ СЛУЖБЫ ({ф_['файл']}) {ф_.get('почему_нет') or ''}:")
    for имя, зн in ф_["значения"].items():
        print(f"   {имя} = {зн if зн is not None else 'в файле нет'}")
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
