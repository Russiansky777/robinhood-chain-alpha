#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ПЛАН утреннего круга 0.01: группа test_krug -- СОСТАВИТЬ И ПРОВЕРИТЬ, НЕ ПРИМЕНЯТЬ.

Слово владельца 29.09 ночью (п.2): "подготовить, не запускать: группа test_krug
в файле групп (источники lane_s0, lane_sol 0.01, hold_slots 12, max_slots 1,
lane_open_max 1, все остальные группы lane_trades=false на время круга) и одна
команда для владельца ... Проверка плана самопроверкой, сам файл до утра не
менять".

ЧТО ДЕЛАЕТ. Читает ЖИВОЙ файл групп, строит В ПАМЯТИ, каким он должен стать на
время круга, проверяет план списком условий и печатает: план, план возврата и
команду владельца. Файл на диске НЕ ТРОГАЕТ ВООБЩЕ -- ни записи, ни бэкапа.

ПОЧЕМУ С САМОПРОВЕРКОЙ. План решает, чем торгует полоса: одна лишняя группа с
lane_trades=true -- и вместо одного круга на 0.01 пойдёт обычная торговля.
Поэтому условия проверяются кодом, а не глазами.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys

ИМЯ_КРУГА = "test_krug"
# Каким должен быть круг. Числа -- слово владельца, менять их здесь нельзя без
# его слова: прогон на них же и проверяет план.
КРУГ = {"lane_trades": True, "lane_sol": 0.01, "hold_slots": 12,
         "max_slots_from_source": 1, "lane_open_max": 1,
         "bloom_trades": False, "subscribe": True,
         "note": ("Утренний круг 0.01 (слово владельца 29.09): один вход, "
                   "удержание 12 слотов, продажа по таймеру, остаток на "
                   "processed. После круга группу убрать, остальные вернуть.")}
ПОХОЖ_НА_АДРЕС = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def поле_адресов(группа: dict) -> str | None:
    """Ключ группы, в котором лежит список адресов источников."""
    for ключ, зн in (группа or {}).items():
        if isinstance(зн, list) and зн and all(
                isinstance(x, str) and ПОХОЖ_НА_АДРЕС.match(x) for x in зн):
            return ключ
    return None


def план(живой: dict, *, откуда: str = "lane_s0") -> dict:
    """Каким станет файл на время круга. Возвращает и план, и план возврата."""
    из_ = {"ok": False, "why_not": None}
    группы = (живой or {}).get("groups")
    if not isinstance(группы, dict):
        из_["why_not"] = "в файле нет словаря groups"
        return из_
    if откуда not in группы:
        из_["why_not"] = f"группы {откуда} в файле нет; есть {sorted(группы)}"
        return из_
    поле = поле_адресов(группы[откуда])
    if not поле:
        из_["why_not"] = f"в группе {откуда} не нашлось списка адресов"
        return из_

    новые = copy.deepcopy(группы)
    # 1. Все торгующие группы -- выключить, и запомнить, кто торговал.
    было_торгующих = sorted(г for г, п in группы.items() if п.get("lane_trades") is True)
    for г in новые:
        if новые[г].get("lane_trades") is True:
            новые[г]["lane_trades"] = False
    # 2. Группа круга: адреса lane_s0, числа владельца.
    круг = dict(КРУГ)
    круг[поле] = list(группы[откуда][поле])
    новые[ИМЯ_КРУГА] = круг

    из_.update(ok=True, поле_адресов=поле,
                адресов_в_круге=len(круг[поле]),
                было_торгующих=было_торгующих,
                план_групп={г: {"lane_trades": новые[г].get("lane_trades"),
                                 "lane_sol": новые[г].get("lane_sol")}
                             for г in sorted(новые)},
                возврат=({"убрать_группу": ИМЯ_КРУГА,
                           "вернуть_lane_trades_true": было_торгующих}),
                групп_после=len(новые))
    из_["новые_группы"] = новые
    return из_


def проверить(п: dict) -> dict:
    """Список условий плана. Любое невыполненное -- план не годен."""
    из_ = {"ok": False, "условия": []}

    def усл(что, ок, факт=None):
        из_["условия"].append({"условие": что, "ок": bool(ок), "факт": факт})

    if not п.get("ok"):
        усл("план собрался", False, п.get("why_not"))
        return из_
    г = п["новые_группы"]
    торгуют = [и for и, з in г.items() if з.get("lane_trades") is True]
    усл("торгует РОВНО одна группа", len(торгуют) == 1, торгуют)
    усл("и это группа круга", торгуют == [ИМЯ_КРУГА], торгуют)
    к = г.get(ИМЯ_КРУГА) or {}
    усл("размер сделки 0.01 SOL", к.get("lane_sol") == 0.01, к.get("lane_sol"))
    усл("удержание 12 слотов", к.get("hold_slots") == 12, к.get("hold_slots"))
    усл("не дальше S+1", к.get("max_slots_from_source") == 1,
        к.get("max_slots_from_source"))
    усл("открытых не больше одной", к.get("lane_open_max") == 1,
        к.get("lane_open_max"))
    усл("Bloom этой группой не торгует", к.get("bloom_trades") is False,
        к.get("bloom_trades"))
    усл("адреса в круге есть", int(п.get("адресов_в_круге") or 0) > 0,
        п.get("адресов_в_круге"))
    усл("sniper_src остаётся выключенным",
        (г.get("sniper_src") or {}).get("lane_trades") is not True,
        (г.get("sniper_src") or {}).get("lane_trades"))
    # Один адрес не должен торговать из двух групп сразу.
    поле = п.get("поле_адресов")
    занято: dict = {}
    двойных = []
    for и, з in г.items():
        if з.get("lane_trades") is not True:
            continue
        for а in (з.get(поле) or []):
            if а in занято:
                двойных.append((а, занято[а], и))
            занято[а] = и
    усл("ни один адрес не торгует из двух групп", not двойных, двойных[:3])
    из_["ok"] = all(у["ок"] for у in из_["условия"])
    return из_


КОМАНДА = """КОМАНДА ВЛАДЕЛЬЦА НА УТРЕННИЙ КРУГ 0.01 (по шагам, каждый -- отдельный прогон)

 1. Круг включить (файл групп, без перезапуска -- читается по mtime):
      run_vps_group_policy_nl.yml  group=<каждая торгующая>  field=lane_trades  set=false
      затем группа круга добавляется прогоном подготовки круга (см. ниже)
 2. Снять рубильник полосы -- ТОЛЬКО ваше действие:
      run_vps_bloom_kill_nl.yml  switch=lane  action=off  telegram=yes
 3. Ждать одну покупку группы test_krug (0.01 SOL, S+0 или S+1).
    Продажа уйдёт сама по таймеру через 12 слотов (~3.1 с).
 4. Проверить круг по цепи:
      run_pravilo11_po_cepi_nl.yml  s=<время покупки>  telegram=no
    Ждём в строке: ушло ~0.01, вернулось > 0, статус "по цепи",
    удержание_слотов около 12, продажа найдена.
 5. Остаток и счёт:
      run_bloom_token_accounts.yml  wallet=4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x
    Ждём: счетов с остатком 0. Если счёт остался пустым -- закрыть:
      run_close_empty_accounts.yml  wallet=lane live=yes first_one=yes while_trading=yes
 6. Рубильник обратно (ваше действие) и вернуть группы:
      run_vps_bloom_kill_nl.yml  switch=lane  action=on  telegram=yes
      run_vps_group_policy_nl.yml  group=<каждая из «было торгующих»>  field=lane_trades  set=true
      группу test_krug убрать (прогон подготовки круга с --otkatit)
"""


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--file", default="", help="файл групп (пусто -- из признака жизни)")
    р.add_argument("--otkuda", default="lane_s0", help="чьи источники брать")
    р.add_argument("--out", default="")
    а = р.parse_args()

    путь = а.file
    if not путь:
        try:
            с = json.load(open("/home/bot/bloom_executor_live_data/detector_status.json",
                                encoding="utf-8"))
            путь = ((с.get("source_groups") or {}).get("file") or "")
        except Exception as exc:  # noqa: BLE001
            print(f"СБОЙ: признак жизни не прочитан ({type(exc).__name__})")
            return 2
    if not путь or not os.path.exists(путь):
        print(f"СБОЙ: файла групп нет: {путь!r}")
        return 2
    живой = json.load(open(путь, encoding="utf-8"))
    п = план(живой, откуда=а.otkuda)
    пр = проверить(п)

    print(f"ФАЙЛ ГРУПП: {путь}")
    print(f"ПЛАН: группа {ИМЯ_КРУГА}, адресов {п.get('адресов_в_круге')}, "
          f"поле адресов {п.get('поле_адресов')!r}")
    print(f"было торгующих: {п.get('было_торгующих')}")
    for г, з in (п.get("план_групп") or {}).items():
        print(f"   {г:11s} lane_trades={str(з.get('lane_trades')):5s} lane_sol={з.get('lane_sol')}")
    print("ПРОВЕРКА ПЛАНА:")
    for у in пр["условия"]:
        print(f"   [{'ok  ' if у['ок'] else 'ПРОВАЛ'}] {у['условие']}"
              + ("" if у["ок"] else f" -> {у['факт']!r}"))
    print(f"ПЛАН ГОДЕН: {пр['ok']}")
    print()
    print(КОМАНДА)
    print("ФАЙЛ НА ДИСКЕ НЕ ИЗМЕНЁН: этот прогон только читает.")
    if а.out:
        итог = {"файл": путь, "план": {к: v for к, v in п.items()
                                        if к != "новые_группы"},
                 "проверка": пр, "команда": КОМАНДА}
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print("записано:", а.out)
    return 0 if пр["ok"] else 1


def self_test() -> int:
    пройдено = провалено = 0

    def chk(что, ок, факт=None):
        nonlocal пройдено, провалено
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {что}"
              + (f" -> {факт!r}" if факт is not None and not ок else ""))
        пройдено += bool(ок)
        провалено += (not ок)

    А1 = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
    А2 = "3Um4qsYQKYULYSJwRChReZtgsXu3kiGm6HNTvZpy9dYy"
    живой = {"groups": {
        "lane_s0": {"lane_trades": True, "lane_sol": 0.3, "addresses": [А1, А2]},
        "batch5": {"lane_trades": True, "lane_sol": 0.3, "addresses": [А1]},
        "sniper_src": {"lane_trades": False, "lane_sol": 0.3, "addresses": [А2]},
        "log_only": {"lane_trades": False, "lane_sol": None, "addresses": [А1]},
    }}
    п = план(живой)
    пр = проверить(п)
    chk("план собрался и годен", п["ok"] and пр["ok"], пр["условия"])
    chk("торгует только круг",
        [и for и, з in п["новые_группы"].items() if з.get("lane_trades") is True]
        == [ИМЯ_КРУГА])
    chk("числа круга из слова владельца",
        п["новые_группы"][ИМЯ_КРУГА]["lane_sol"] == 0.01
        and п["новые_группы"][ИМЯ_КРУГА]["hold_slots"] == 12
        and п["новые_группы"][ИМЯ_КРУГА]["lane_open_max"] == 1)
    chk("адреса взяты у lane_s0",
        п["новые_группы"][ИМЯ_КРУГА]["addresses"] == [А1, А2])
    chk("возврат называет, кого включать обратно",
        п["возврат"]["вернуть_lane_trades_true"] == ["batch5", "lane_s0"],
        п["возврат"])
    chk("живой словарь не изменён планом",
        живой["groups"]["lane_s0"]["lane_trades"] is True
        and ИМЯ_КРУГА not in живой["groups"])

    # ПЛАН ОБЯЗАН ПАДАТЬ, если в нём осталась вторая торгующая группа.
    плохой = copy.deepcopy(п["новые_группы"])
    плохой["batch5"]["lane_trades"] = True
    chk("вторая торгующая группа -- план не годен",
        проверить({"ok": True, "новые_группы": плохой, "поле_адресов": "addresses",
                    "адресов_в_круге": 2})["ok"] is False)
    # И если размер не 0.01.
    плохой2 = copy.deepcopy(п["новые_группы"])
    плохой2[ИМЯ_КРУГА]["lane_sol"] = 0.3
    chk("размер не 0.01 -- план не годен",
        проверить({"ok": True, "новые_группы": плохой2, "поле_адресов": "addresses",
                    "адресов_в_круге": 2})["ok"] is False)
    # Группы, из которой берём адреса, нет вовсе.
    chk("нет группы-источника -- честный отказ",
        план({"groups": {}}, откуда="lane_s0")["ok"] is False)

    print(f"самопроверка плана круга: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    raise SystemExit(main())
