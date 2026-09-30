#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ГРУППА cand1 в файле групп: СОСТАВИТЬ, ПРОВЕРИТЬ, и только по слову -- ПРИМЕНИТЬ.

Добавка владельца к ночному пакету 30.09->01.10, п.0, и она УСЛОВНАЯ: "если
владелец скажет «да» по cand1". Поэтому по умолчанию прогон только ПОКАЗЫВАЕТ, а
запись идёт лишь при rezhim=primenit И слове подтверждения. Ни одна цифра здесь
не берётся из головы: числа -- из добавки, адреса -- со страницы Code-2
docs/podbivka_2026-09-30_kandidaty_vne_vyborki.md (раздел «Список для cand1»).

ЧТО ЭТО МЕНЯЕТ В ДЕНЬГАХ. Появляется НОВАЯ ТОРГУЮЩАЯ группа: размер 0.1 SOL,
порог входа источника 2 SOL, удержание 108 слотов, стоп группы 0.3 SOL за сутки,
не более 3 открытых, типы пулов -- как у lane_s0. Это ровно те поля, которыми
полоса решает, покупать ли: lane_size/lane_sol, min_target_sol, hold_slots,
stop_loss_sol, lane_open_max, lane_pools, lane_trades.

ПОЧЕМУ ЧТЕНИЕ АДРЕСОВ НЕ ПЕРЕПИСАНО ЗАНОВО. Адреса групп читаются теми же
функциями, что у плана круга (deploy/checks/plan_test_krug.py): адреса лежат в
файле тремя видами, и четвёртый вид изобретать нельзя -- 30.09 02:58Z прогон уже
сказал "в группе lane_s0 не нашлось списка адресов" именно из-за своего чтения.

ГЛАВНЫЙ ГЕЙТ -- ОДИН АДРЕС В ДВУХ ГРУППАХ. Полоса считает пределы и стопы ПО
ГРУППЕ. Адрес, попавший в две торгующие группы, даёт два входа на один сигнал и
два разных удержания, а стоп каждой группы видит только свою половину. Поэтому
пересечение -- ОТКАЗ по имени, а не предупреждение.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plan_test_krug as K  # noqa: E402

ИМЯ_ГРУППЫ = "cand1"
ПОДТВЕРЖДЕНИЕ = "PRIMENIT"
ОТКУДА_ПУЛЫ = "lane_s0"

# ЧИСЛА -- СЛОВО ВЛАДЕЛЬЦА (добавка к ночному пакету 30.09->01.10, п.0).
# Менять их здесь нельзя без его слова: прогон на них же и проверяет план.
ЧИСЛА = {"lane_trades": True,
         # Размер кладётся ДВУМЯ именами одного числа: читатели денежного пути
         # спрашивают и lane_sol, и lane_size (bloom_source_groups).
         "lane_size": 0.1, "lane_sol": 0.1,
         "min_target_sol": 2.0,
         "hold_slots": 108,
         "stop_loss_sol": 0.3,
         "lane_open_max": 3,
         # Площадка Bloom по этой группе НЕ торгует: добавка про полосу.
         "bloom_trades": False,
         "subscribe": True}

# АДРЕСА -- СО СТРАНИЦЫ Code-2, раздел «Список для cand1».
АДРЕСА = ["CAPn1yH4oSywsxGU456jfgTrSSUidf9jgeAnHceNUJdw",
          "DYAn4XpAkN5mhiXkRB7dGq4Jadnx6XYgu8L5b3WGhbrt",
          "6S8GezkxYUfZy9JPtYnanbcZTMB87Wjt1qx3c6ELajKC",
          "HmBmSYwYEgEZuBUYuDs9xofyqBAkw4ywugB1d7R7sTGh",
          "5YRgrP3mjGzrzirYYN5HAQH19cTYREYwGxW6XRJQUzij",
          "2net6etAtTe3Rbq2gKECmQwnzcKVXRaLcHy2Zy1iCiWz",
          "6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy",
          "GM7Hrz2bDq33ezMtL6KGidSWZXMWgZ6qBuugkb5H8NvN",
          "Gf2wYM2k5ojfPzN5Uqi3mbP1nWEdZhhzMyDBsKrHA3kC",
          "EjtQrPTbcMevStBkpnjsH23NfUCMhGHusTYsHuGVQZp2"]

# СВЯЗАННЫЕ АДРЕСА -- С ТОЙ ЖЕ СТРАНИЦЫ (раздел «Связанные адреса»): пары, у
# которых больше половины сигналов -- тот же минт в том же слоте. Это не отказ,
# но с lane_open_max 3 такая пара даёт ДВА входа в один минт, и это должно быть
# сказано числом, а не оставлено на «потом заметим».
СВЯЗАННЫЕ = [("DYAn4XpA", "Gf2wYM2k", "общих (минт, слот) 14 из 16 / 17")]

ИСТОЧНИК_СПИСКА = "docs/podbivka_2026-09-30_kandidaty_vne_vyborki.md (Code-2)"


def адрес_по_группам(живой: dict) -> dict:
    """Адрес -> список групп, где он уже лежит. Чтением службы, не своим."""
    из_: dict = {}
    for имя, гр in ((живой or {}).get("groups") or {}).items():
        for а in K.адреса_группы(гр):
            из_.setdefault(а, []).append(имя)
    return из_


def план(живой: dict, *, откуда_пулы: str = ОТКУДА_ПУЛЫ) -> dict:
    """Каким станет файл с группой cand1. Диск не трогается."""
    из_ = {"ok": False, "why_not": None, "группа": ИМЯ_ГРУППЫ,
           "адресов": len(АДРЕСА), "источник_списка": ИСТОЧНИК_СПИСКА}
    группы = (живой or {}).get("groups")
    if not isinstance(группы, dict):
        из_["why_not"] = "в файле нет словаря groups"
        return из_
    плохие = [а for а in АДРЕСА if not K.ПОХОЖ_НА_АДРЕС.match(а)]
    if плохие:
        из_["why_not"] = f"это не адреса: {плохие}"
        return из_
    if len(set(АДРЕСА)) != len(АДРЕСА):
        из_["why_not"] = "в списке есть повторы адресов"
        return из_

    # ПУЛЫ БЕРУТСЯ У lane_s0, А НЕ ПЕРЕПИСЫВАЮТСЯ СПИСКОМ. "Пулы как у lane_s0"
    # -- это его ЖИВОЕ значение: перепиши я список сюда, и завтра он разошёлся бы
    # с lane_s0 молча.
    if откуда_пулы not in группы:
        из_["why_not"] = (f"группы {откуда_пулы}, у которой берутся пулы, в файле "
                          f"нет; есть {sorted(группы)}")
        return из_
    пулы = (группы[откуда_пулы] or {}).get("lane_pools")
    из_["пулы_откуда"] = откуда_пулы
    из_["пулы"] = list(пулы) if isinstance(пулы, list) else пулы

    # ГЛАВНЫЙ ГЕЙТ: адрес уже в другой группе.
    где = адрес_по_группам(живой)
    пересечения = {а: [г for г in где.get(а, []) if г != ИМЯ_ГРУППЫ]
                   for а in АДРЕСА if [г for г in где.get(а, []) if г != ИМЯ_ГРУППЫ]}
    из_["пересечения"] = {а: г for а, г in пересечения.items()}
    if пересечения:
        строки = ", ".join(f"{а[:8]}… уже в {'+'.join(г)}"
                           for а, г in sorted(пересечения.items()))
        из_["why_not"] = ("адрес не может торговать из двух групп: пределы, "
                          "удержание и стоп считаются ПО ГРУППЕ. "
                          f"Пересечения: {строки}. Решение -- владельца: убрать "
                          "адрес из старой группы или из списка cand1")
        return из_

    новые = copy.deepcopy(группы)
    было = ИМЯ_ГРУППЫ in новые
    из_["группа_уже_была"] = было
    из_["адреса_были"] = K.адреса_группы(новые.get(ИМЯ_ГРУППЫ) or {}) if было else []
    гр = dict(ЧИСЛА)
    гр["lane_pools"] = из_["пулы"]
    гр["addresses"] = list(АДРЕСА)
    гр["note"] = ("Кандидаты вне выборки (добавка владельца к ночному пакету "
                  "30.09->01.10, п.0). Список -- " + ИСТОЧНИК_СПИСКА
                  + ". Размер 0.1, порог 2 SOL, удержание 108, стоп группы 0.3 "
                    "за сутки, не более 3 открытых, пулы как у " + откуда_пулы)
    новые[ИМЯ_ГРУППЫ] = гр
    из_["план_группы"] = {к: v for к, v in гр.items() if к != "addresses"}
    из_["торгующих_было"] = sorted(г for г, п in группы.items()
                                   if п.get("lane_trades") is True)
    из_["торгующих_станет"] = sorted(г for г, п in новые.items()
                                     if п.get("lane_trades") is True)
    из_["чужие_группы_не_тронуты"] = all(
        новые[г] == группы[г] for г in группы if г != ИМЯ_ГРУППЫ)
    из_["связанные_в_списке"] = [
        {"пара": [а, б], "как": как}
        for а, б, как in СВЯЗАННЫЕ
        if any(х.startswith(а) for х in АДРЕСА) and any(х.startswith(б) for х in АДРЕСА)]
    из_["файл_после"] = dict(живой, groups=новые)
    из_["ok"] = True
    return из_


def проверить(п: dict) -> dict:
    """Условия плана -- кодом, а не глазами. Деньги решает этот список."""
    из_ = {"ok": False, "условия": []}

    def усл(что, ок, факт=None):
        из_["условия"].append({"условие": что, "ок": bool(ок), "факт": факт})

    гр = (п or {}).get("план_группы") or {}
    усл("план вообще собрался", bool(п.get("ok")), п.get("why_not"))
    усл("адресов ровно 10", п.get("адресов") == 10, п.get("адресов"))
    усл("ни один адрес не лежит в другой группе", not (п.get("пересечения") or {}),
        п.get("пересечения"))
    усл("группа торгует полосой", гр.get("lane_trades") is True)
    усл("размер 0.1 обоими именами",
        гр.get("lane_size") == 0.1 and гр.get("lane_sol") == 0.1,
        (гр.get("lane_size"), гр.get("lane_sol")))
    усл("порог входа источника 2 SOL", гр.get("min_target_sol") == 2.0,
        гр.get("min_target_sol"))
    усл("удержание 108 слотов", гр.get("hold_slots") == 108, гр.get("hold_slots"))
    усл("стоп группы 0.3 SOL за сутки", гр.get("stop_loss_sol") == 0.3,
        гр.get("stop_loss_sol"))
    усл("не больше 3 открытых", гр.get("lane_open_max") == 3, гр.get("lane_open_max"))
    усл("Bloom этой группой не торгует", гр.get("bloom_trades") is False)
    усл("адреса подписываются", гр.get("subscribe") is True)
    усл("пулы взяты у lane_s0 и не пусты",
        bool(п.get("пулы")) and п.get("пулы_откуда") == ОТКУДА_ПУЛЫ, п.get("пулы"))
    усл("чужие группы не тронуты", п.get("чужие_группы_не_тронуты") is True)
    # НОВОЙ ТОРГУЮЩЕЙ МОЖЕТ СТАТЬ ТОЛЬКО cand1 -- и она обязана торговать.
    # Равенство множеству {cand1} было бы неверным при повторном применении:
    # если cand1 уже торгует, новых групп нет вовсе, и условие краснело бы на
    # безобидном повторе. Гейт же нужен про ДРУГИЕ группы: ни одна из них не
    # должна включиться заодно.
    новые_торгующие = (set(п.get("торгующих_станет") or [])
                        - set(п.get("торгующих_было") or []))
    усл("кроме cand1 никто не начинает торговать",
        новые_торгующие <= {ИМЯ_ГРУППЫ},
        (sorted(новые_торгующие), п.get("торгующих_было"),
         п.get("торгующих_станет")))
    усл("и сама cand1 после правки торгует",
        ИМЯ_ГРУППЫ in (п.get("торгующих_станет") or []),
        п.get("торгующих_станет"))
    из_["ok"] = all(у["ок"] for у in из_["условия"])
    return из_


def применить(файл: str, живой: dict, п: dict, *, подтверждение: str) -> dict:
    """Запись -- только по слову. Рядом остаётся копия прежнего файла."""
    из_ = {"ok": False, "why_not": None, "файл": файл}
    if подтверждение != ПОДТВЕРЖДЕНИЕ:
        из_["why_not"] = (f"нет слова подтверждения: ожидалось {ПОДТВЕРЖДЕНИЕ}. "
                          "Файл не тронут")
        return из_
    пр = проверить(п)
    if not пр["ok"]:
        плохо = [у["условие"] for у in пр["условия"] if not у["ок"]]
        из_["why_not"] = f"план не прошёл проверку: {плохо}. Файл не тронут"
        return из_
    копия = f"{файл}.bak.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    try:
        with open(копия, "w", encoding="utf-8") as ф:
            json.dump(живой, ф, ensure_ascii=False, indent=1)
        K._записать_атомарно(файл, п["файл_после"])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return из_
    # ЧИТАЕМ НАЗАД. "Записали" без чтения -- это обещание: 30.09 отказ записи уже
    # оставлял прогон зелёным, и файл групп при этом не менялся.
    try:
        стало = json.load(open(файл, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"файл записан, но не читается назад: {type(exc).__name__}"
        return из_
    гр = (стало.get("groups") or {}).get(ИМЯ_ГРУППЫ) or {}
    адр = K.адреса_группы(гр)
    из_.update(ok=(len(адр) == len(АДРЕСА) and гр.get("lane_trades") is True),
               копия=копия, адресов_в_файле=len(адр),
               торгует=gr_torguet(стало))
    if not из_["ok"]:
        из_["why_not"] = (f"после записи в файле адресов {len(адр)}, "
                          f"lane_trades={гр.get('lane_trades')}")
    return из_


def gr_torguet(файл: dict) -> list:
    """Кто торгует полосой в этом файле -- списком, для отчёта."""
    return sorted(г for г, п in ((файл or {}).get("groups") or {}).items()
                  if (п or {}).get("lane_trades") is True)


def откатить(файл: str, живой: dict, *, подтверждение: str) -> dict:
    """Убрать группу cand1. Остальные группы не трогаются."""
    из_ = {"ok": False, "why_not": None, "файл": файл}
    if подтверждение != ПОДТВЕРЖДЕНИЕ:
        из_["why_not"] = f"нет слова подтверждения: ожидалось {ПОДТВЕРЖДЕНИЕ}"
        return из_
    группы = (живой or {}).get("groups") or {}
    if ИМЯ_ГРУППЫ not in группы:
        из_.update(ok=True, why_not=None, убрано=False,
                   заметка=f"группы {ИМЯ_ГРУППЫ} в файле нет -- убирать нечего")
        return из_
    новые = copy.deepcopy(группы)
    новые.pop(ИМЯ_ГРУППЫ)
    копия = f"{файл}.bak.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    try:
        with open(копия, "w", encoding="utf-8") as ф:
            json.dump(живой, ф, ensure_ascii=False, indent=1)
        K._записать_атомарно(файл, dict(живой, groups=новые))
        стало = json.load(open(файл, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return из_
    из_.update(ok=ИМЯ_ГРУППЫ not in (стало.get("groups") or {}),
               убрано=True, копия=копия, торгует=gr_torguet(стало))
    if not из_["ok"]:
        из_["why_not"] = "после записи группа всё ещё в файле"
    return из_


def печать(о: dict) -> None:
    п = о.get("план") or {}
    пр = о.get("проверка") or {}
    print(f"ФАЙЛ ГРУПП: {о.get('файл')}")
    print(f"ГРУППА: {ИМЯ_ГРУППЫ}, адресов {п.get('адресов')}, список -- "
          f"{п.get('источник_списка')}")
    if п.get("why_not"):
        print(f"  ПЛАН НЕ ГОДЕН: {п['why_not']}")
    for к, v in sorted((п.get("план_группы") or {}).items()):
        print(f"    {к} = {v}")
    print(f"  торгующие группы: было {п.get('торгующих_было')} -> "
          f"станет {п.get('торгующих_станет')}")
    for с in п.get("связанные_в_списке") or []:
        print(f"  ВНИМАНИЕ, связанные адреса: {с['пара'][0]} и {с['пара'][1]} -- "
              f"{с['как']}. При пределе 3 открытых это два входа в один минт")
    print("ПРОВЕРКА ПЛАНА:")
    for у in пр.get("условия") or []:
        отм = "ok  " if у["ок"] else "НЕТ "
        факт = "" if (у["ок"] or у.get("факт") is None) else f" -> {у['факт']!r}"
        print(f"   [{отм}] {у['условие']}{факт}")
    print(f"ПЛАН ГОДЕН: {pr_ok(пр)}")
    сд = о.get("сделано")
    if сд:
        print(f"СДЕЛАНО: ok={сд.get('ok')}"
              + (f", почему нет: {сд.get('why_not')}" if сд.get("why_not") else "")
              + (f", копия {сд.get('копия')}" if сд.get("копия") else "")
              + (f", торгуют {сд.get('торгует')}" if сд.get("торгует") else ""))
    else:
        print("СДЕЛАНО: ничего -- режим показа. Запись только при "
              f"rezhim=primenit и подтверждении {ПОДТВЕРЖДЕНИЕ}")


def pr_ok(пр: dict) -> bool:
    return bool((пр or {}).get("ok"))


def _разборщик() -> argparse.ArgumentParser:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--fajl", default=None,
                   help="файл групп (по умолчанию -- тот, что называет детектор)")
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--rezhim", default="pokazat",
                   choices=("pokazat", "primenit", "otkatit"))
    р.add_argument("--podtverzhdenie", default="")
    р.add_argument("--puly-otkuda", default=ОТКУДА_ПУЛЫ)
    р.add_argument("--out", default=None)
    р.add_argument("--self-test", dest="self_test", action="store_true")
    return р


def файл_детектора(state_dir: str) -> str | None:
    """Какой файл групп читает САМА служба -- её же признаком жизни."""
    путь = os.path.join(state_dir, "detector_status.json")
    try:
        с = json.load(open(путь, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return ((с.get("source_groups") or {}).get("file")) or None


def main() -> int:
    а = _разборщик().parse_args()
    if а.self_test:
        return self_test()
    файл = а.fajl or файл_детектора(а.state_dir)
    if not файл:
        print("не сделано: детектор не называет файл групп, а --fajl не задан")
        return 2
    try:
        живой = json.load(open(файл, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"не сделано: файл групп не читается: {type(exc).__name__}: {exc}")
        return 2
    о = {"что": "группа cand1", "файл": файл,
         "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "режим": а.rezhim}
    п = план(живой, откуда_пулы=а.puly_otkuda)
    о["план"] = {к: v for к, v in п.items() if к != "файл_после"}
    о["проверка"] = проверить(п)
    if а.rezhim == "primenit":
        о["сделано"] = применить(файл, живой, п, подтверждение=а.podtverzhdenie)
    elif а.rezhim == "otkatit":
        о["сделано"] = откатить(файл, живой, подтверждение=а.podtverzhdenie)
    печать(о)
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(о, ф, ensure_ascii=False, indent=1)
        print(f"записано: {а.out}")
    if а.rezhim == "pokazat":
        return 0 if о["проверка"]["ok"] else 2
    return 0 if (о.get("сделано") or {}).get("ok") else 2


def self_test() -> int:  # noqa: C901
    пройдено = провалено = 0

    def chk(что, ок, факт=None):
        nonlocal пройдено, провалено
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {что}"
              + (f" -> {факт!r}" if факт is not None and not ок else ""))
        nonlocal_счёт = None  # noqa: F841
        return ок

    def счёт(ок):
        nonlocal пройдено, провалено
        пройдено += bool(ок)
        провалено += (not ок)

    def проба(что, ок, факт=None):
        счёт(chk(что, ок, факт))

    живой = {"generated_utc": "2026-09-25T00:00:00Z",
             "groups": {"lane_s0": {"lane_trades": True, "lane_sol": 0.3,
                                    "lane_pools": ["pump_amm", "cpmm", "bonding"],
                                    "addresses": ["7JVQMwRj82STgsG57spj6vpE6XY3RqG8B64PczVc7jJr"]},
                        "batch5": {"lane_trades": True, "lane_sol": 0.3,
                                   "addresses": []},
                        "log_only": {"lane_trades": False, "addresses": []}}}
    п = план(живой)
    проба("план собрался на чистом файле", п["ok"], п.get("why_not"))
    пр = проверить(п)
    проба("все условия плана сошлись", пр["ok"],
          [у for у in пр["условия"] if not у["ок"]])
    гр = п["план_группы"]
    проба("числа -- ровно из добавки владельца",
          (гр["lane_size"], гр["lane_sol"], гр["min_target_sol"], гр["hold_slots"],
           гр["stop_loss_sol"], гр["lane_open_max"], гр["lane_trades"])
          == (0.1, 0.1, 2.0, 108, 0.3, 3, True), гр)
    проба("пулы взяты у lane_s0, а не переписаны",
          п["пулы"] == ["pump_amm", "cpmm", "bonding"], п["пулы"])
    проба("чужие группы в плане не тронуты", п["чужие_группы_не_тронуты"] is True)
    проба("торгующих было две, станет три",
          п["торгующих_было"] == ["batch5", "lane_s0"]
          and п["торгующих_станет"] == ["batch5", "cand1", "lane_s0"],
          (п["торгующих_было"], п["торгующих_станет"]))
    проба("связанная пара названа предупреждением",
          len(п["связанные_в_списке"]) == 1
          and п["связанные_в_списке"][0]["пара"] == ["DYAn4XpA", "Gf2wYM2k"],
          п["связанные_в_списке"])

    # ГЛАВНЫЙ ГЕЙТ: адрес уже в другой группе -> ОТКАЗ с именами
    занят = copy.deepcopy(живой)
    занят["groups"]["sniper_src"] = {"lane_trades": True,
                                     "addresses": [АДРЕСА[6]]}
    п2 = план(занят)
    проба("адрес из другой группы -- отказ, и группа названа",
          (not п2["ok"]) and "sniper_src" in (п2["why_not"] or "")
          and АДРЕСА[6][:8] in (п2["why_not"] or ""), п2.get("why_not"))
    проба("в отказе перечислены пересечения",
          list(п2["пересечения"]) == [АДРЕСА[6]], п2.get("пересечения"))
    проба("проверка такого плана НЕ зелёная", проверить(п2)["ok"] is False)

    # ОТСУТСТВИЕ lane_s0 -- отказ, а не пустые пулы
    без = {"groups": {"batch5": {"lane_trades": True, "addresses": []}}}
    п3 = план(без)
    проба("нет группы, у которой берём пулы -- отказ",
          (not п3["ok"]) and "lane_s0" in (п3["why_not"] or ""), п3.get("why_not"))

    # ЗАПИСЬ: без слова -- ни байта
    import tempfile
    with tempfile.TemporaryDirectory() as д:
        ф = os.path.join(д, "groups.json")
        with open(ф, "w", encoding="utf-8") as фп:
            json.dump(живой, фп, ensure_ascii=False)
        было = open(ф, encoding="utf-8").read()
        с1 = применить(ф, живой, план(живой), подтверждение="")
        проба("без слова подтверждения запись не идёт",
              (not с1["ok"]) and ПОДТВЕРЖДЕНИЕ in (с1["why_not"] or ""),
              с1.get("why_not"))
        проба("и файл на диске не изменился",
              open(ф, encoding="utf-8").read() == было)
        с2 = применить(ф, живой, план(живой), подтверждение=ПОДТВЕРЖДЕНИЕ)
        проба("со словом -- записано, и прочитано назад", с2["ok"], с2.get("why_not"))
        проба("рядом легла копия прежнего файла",
              os.path.exists(с2.get("копия") or ""), с2.get("копия"))
        стало = json.load(open(ф, encoding="utf-8"))
        проба("в файле 10 адресов группы cand1",
              len(K.адреса_группы(стало["groups"][ИМЯ_ГРУППЫ])) == 10)
        проба("торгуют теперь три группы",
              gr_torguet(стало) == ["batch5", "cand1", "lane_s0"], gr_torguet(стало))
        проба("остальные группы в файле те же",
              all(стало["groups"][г] == живой["groups"][г]
                  for г in живой["groups"]))
        # ПОВТОРНОЕ ПРИМЕНЕНИЕ не должно плодить дубли
        с3 = применить(ф, стало, план(стало), подтверждение=ПОДТВЕРЖДЕНИЕ)
        проба("повторное применение не плодит адреса", с3["ok"]
              and len(K.адреса_группы(json.load(open(ф, encoding="utf-8"))
                                      ["groups"][ИМЯ_ГРУППЫ])) == 10,
              с3.get("why_not"))
        # ОТКАТ
        живой2 = json.load(open(ф, encoding="utf-8"))
        о1 = откатить(ф, живой2, подтверждение="")
        проба("откат без слова -- ничего не делает", о1["ok"] is False)
        о2 = откатить(ф, живой2, подтверждение=ПОДТВЕРЖДЕНИЕ)
        стало2 = json.load(open(ф, encoding="utf-8"))
        проба("откат убрал cand1 и вернул двух торгующих",
              о2["ok"] and ИМЯ_ГРУППЫ not in стало2["groups"]
              and gr_torguet(стало2) == ["batch5", "lane_s0"], gr_torguet(стало2))
        о3 = откатить(ф, стало2, подтверждение=ПОДТВЕРЖДЕНИЕ)
        проба("откат, когда группы нет -- не сбой, а слова", о3["ok"] is True
              and о3.get("убрано") is False, о3)

    проба("адресов в списке ровно 10 и все похожи на адрес",
          len(АДРЕСА) == 10 and all(K.ПОХОЖ_НА_АДРЕС.match(а) for а in АДРЕСА))
    print(f"самопроверка группы cand1: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
