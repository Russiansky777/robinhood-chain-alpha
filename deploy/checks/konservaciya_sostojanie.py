#!/usr/bin/env python3
"""Состояние перед консервацией: позиции, зависшие, СПИСАННЫЕ -- одним снимком.

ЗАЧЕМ (пункт 5 владельца 12.10). Перед остановкой служб надо подтвердить
"позиций 0" и получить СПИСОК токенов в состоянии spisana, которые остались на
кошельке. Гейт деплоя (pered_perezapuskom_sluzhby.py) отвечает "можно/нельзя", а
здесь нужен не ответ, а ПЕРЕЧЕНЬ: минты, количества, когда списаны.

ПОЧЕМУ СПИСАННЫЕ СЧИТАЮТСЯ ОТДЕЛЬНО, А НЕ ВМЕСТЕ С ОТКРЫТЫМИ. STATES_OPEN не
содержит STATE_SPISANA (bloom_exec_state.py), то есть open_positions() списанную
НЕ ОТДАЁТ, и гейт "нуль позиций" её не видит: open=0, stuck=0. Токен при этом
может лежать на кошельке. Это известный незакрытый дефект (он записан в
docs/HOST_INVENTAR.md и по слову владельца не чинится), и именно поэтому список
списанных снимается своим проходом по журналу позиций.

ТОЛЬКО ЧТЕНИЕ. Ни одной подписи, ни одной записи, ни одного обращения в сеть:
всё считается по журналу позиций на диске. Цепь спрашивает отдельный прогон --
ему нужен ключ Helius, а этому скрипту не нужен никакой.

Запуск на хосте:
  PYTHONPATH=/home/bot/bloom_executor python3 konservaciya_sostojanie.py
  python3 konservaciya_sostojanie.py --json   # машинный вид
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _состояние():
    """ExecState хоста. Путь берётся так же, как его берут службы."""
    import bloom_exec_state as ST  # noqa: PLC0415

    база = os.environ.get("BLOOM_STATE_DIR") or "/home/bot/bloom_executor_live_data"
    return ST, ST.ExecState(base=база)


def снимок() -> dict:
    ST, состояние = _состояние()
    позиции = состояние.positions()
    открытые = состояние.open_positions()
    списана = getattr(ST, "STATE_SPISANA", "spisana")
    состояния_открытых = tuple(getattr(ST, "STATES_OPEN", ()))

    из_ = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baza": str(состояние.base),
        "pozicij_v_zhurnale": len(позиции),
        "otkrytyh": len(открытые),
        "STATES_OPEN": list(состояния_открытых),
        "STATE_SPISANA": списана,
        "spisana_v_STATES_OPEN": списана in состояния_открытых,
        "otkrytye": [],
        "spisannye": [],
        "po_sostojanijam": {},
    }
    for cid, п in sorted(позиции.items(), key=lambda кп: str(кп[0])):
        сост = str(п.get("state"))
        из_["po_sostojanijam"][сост] = из_["po_sostojanijam"].get(сост, 0) + 1

    for п in открытые:
        из_["otkrytye"].append({
            "client_order_id": п.get("client_order_id"),
            "mint": п.get("mint"),
            "state": п.get("state"),
            "lane": п.get("lane"),
            "lane_group": п.get("lane_group"),
            "sol_in": п.get("sol_in"),
            "lane_bought_raw": п.get("lane_bought_raw"),
            "ts_intent": п.get("ts_intent"),
        })

    # СПИСАННЫЕ -- СВОИМ ПРОХОДОМ: open_positions их не отдаёт.
    for cid, п in sorted(позиции.items(), key=lambda кп: str(кп[0])):
        if str(п.get("state")) != списана:
            continue
        из_["spisannye"].append({
            "client_order_id": cid,
            "mint": п.get("mint"),
            "lane": п.get("lane"),
            "lane_group": п.get("lane_group"),
            "sol_in": п.get("sol_in"),
            # СКОЛЬКО ТОКЕНА СЧИТАЛОСЬ КУПЛЕННЫМ -- по нему и искать на кошельке
            "lane_bought_raw": п.get("lane_bought_raw"),
            "spisana_since": п.get("spisana_since"),
            "unsold_since": п.get("unsold_since"),
            "unsold_reason": п.get("unsold_reason"),
            "closed_sol_net": п.get("closed_sol_net"),
            "ts_spisannaya_opros": п.get("ts_spisannaya_opros"),
            "last_sell_signatures": п.get("last_sell_signatures"),
        })
    из_["spisannyh"] = len(из_["spisannye"])
    # МИНТЫ СПИСАННЫХ -- ОТДЕЛЬНЫМ СПИСКОМ: по нему прогон сверяет цепь.
    из_["minty_spisannyh"] = sorted({
        str(с.get("mint")) for с in из_["spisannye"] if с.get("mint")})
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--json", action="store_true", help="машинный вид, одной строкой")
    а = р.parse_args()
    try:
        с = снимок()
    except Exception as сбой:  # noqa: BLE001
        print(json.dumps({"ok": False,
                           "why_not": f"{type(сбой).__name__}: {сбой}"},
                          ensure_ascii=False))
        return 1
    if а.json:
        print(json.dumps(с, ensure_ascii=False))
        return 0
    print(f"снимок {с['utc']}, журнал {с['baza']}")
    print(f"позиций в журнале {с['pozicij_v_zhurnale']}, ОТКРЫТЫХ {с['otkrytyh']}")
    print(f"по состояниям: {json.dumps(с['po_sostojanijam'], ensure_ascii=False)}")
    print(f"STATES_OPEN = {с['STATES_OPEN']}; STATE_SPISANA = {с['STATE_SPISANA']!r}; "
          f"списана входит в открытые: {с['spisana_v_STATES_OPEN']}")
    if с["otkrytye"]:
        print("--- ОТКРЫТЫЕ ---")
        for п in с["otkrytye"]:
            print(f"  {п['client_order_id']} {п['mint']} state={п['state']} "
                  f"группа={п['lane_group']} sol_in={п['sol_in']}")
    else:
        print("открытых позиций НЕТ")
    print(f"--- СПИСАННЫЕ: {с['spisannyh']} ---")
    for п in с["spisannye"]:
        print(f"  {п['client_order_id']} {п['mint']} куплено_raw={п['lane_bought_raw']} "
              f"sol_in={п['sol_in']} закрыто_net={п['closed_sol_net']} "
              f"причина={str(п['unsold_reason'])[:60]}")
    if с["minty_spisannyh"]:
        print("минты списанных (их и искать на кошельке):")
        for м in с["minty_spisannyh"]:
            print(f"  {м}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
