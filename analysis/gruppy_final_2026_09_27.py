#!/usr/bin/env python3
"""Сборка файла групп по финальному плану живого теста (владелец, 27.09).

ЗАЧЕМ ОТДЕЛЬНЫЙ СБОРЩИК, А НЕ ПРАВКА ФАЙЛА РУКАМИ. Файл групп -- это деньги:
он решает, чей сигнал покупается, каким размером и с какими пределами. Руками
такой файл не собирают: каждый адрес должен иметь проверяемое происхождение.
Здесь оно записано кодом -- полный адрес назван владельцем или разрешён по
префиксу в прежнем файле, и совпадение должно быть РОВНО ОДНО.

ЧТО СТРОИТСЯ. Четыре группы плана вместо прежних четырёх:
  leader   -- лидер, 0.2 SOL, порог источника 15 SOL-экв.;
  batch5   -- восемь названных кошельков BATCH-5, 0.05;
  lane_s0  -- десять кошельков с усечённым S+0, 0.05, только кривая pump.fun;
  off      -- девять адресов, которые владелец велел выключить.
Плюс log_only: все прочие адреса, на которые служба подписана СЕЙЧАС. Они не
торгуются (lane_trades=false, bloom_trades=false), но подписка и полный лог
сигналов остаются -- слово владельца: "все остальные известные адреса:
lane_trades=false, только лог сигналов". Убрать их из файла было бы НЕ то же
самое: адреса, которого в файле нет, политика по умолчанию тоже не торгует, но
и подписки на него не будет, а значит не будет и лога.

Bloom не торгует НИ ПО ОДНОЙ группе (bloom_trades=false везде) и веер выключен
(fanout=false): слово владельца в том же плане.

Запись -- атомарная, через bloom_source_groups.записать (tmp -> rename).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_source_groups as SG  # noqa: E402

НАШИ_КОШЕЛЬКИ = ("4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
                  "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w",
                  "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x")

# ---- адреса, названные владельцем ПОЛНОСТЬЮ (финальный план 27.09, п.2) ----
ЛИДЕР = {"Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit": "лидер"}
BATCH5 = {
    "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv": "Omakase",
    "H3en1XWQHfbNWjEDRnRFi6HKVkZG1P7twdAHTvnCnYE3": "MaxHuh",
    "EC2f5DnHzuNRit1ExqghSifDbp1wgrzktsRRZCtU92MJ": "Theo",
    "Ak6gsstZwaRDYKnzdyNg2HvCXDFvv21afjVix9VGRMQv": "RugDalio",
    "HYh78tNpGBUcxoHgPU9v7VeP2wqSbkapuTzrfYHjfyLo": "CardinalSaint",
    "5opd5KBmodmoNuAThQ5cmXKbRxHbQDfomWGKAs3uEUP9": "soby",
    "9CNyLECt2j8tnDhqxtjYk5HUhZ2b8Nwnyb7sfYN7vND2": "rasmr",
    "4KFjw2xfH4cXJJKjG1jDZRNphctZPMoFz3K6r3bAVtmD": "dreamloader",
    # Четыре адреса, названные владельцем 27.09 вечером (п.6). Два из них
    # (Nach, Pasterniq) уже были подписаны и лежали в log_only -- слово
    # владельца переводит их в batch5; Avast и AviFelman в файле новые, и с
    # ними подписка вырастает на два адреса (555 в логе вместо 557).
    "8xL8S7P4QLdTGRquHas8NP5EVjp2qUGbmSgrkh97mvmq": "Avast",
    "BA3nKHc4DoSANRrx4FcCupExzs6cWzw1wkPpqjnqJaCN": "AviFelman",
    "GAsnqm4XkNkPVgrAofNQ65jWf8f3tKCLHhE9ZqSy2AP1": "Nach",
    "9zZCjLr9xfXfp3qdqvPh6YeaEaagepz21khLhca69B18": "Pasterniq",
}
LANE_S0_ПОЛНЫЕ = {
    "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ": "frank",
    "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB": "Brez",
    "Xk9onqHkpULDEYYN9ZPyM7Q9AfTNYUrsCkzywyqdMeb": "Xk9onq",
    "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8": "jg",
    "4vER1GJQs73HtN9oYRswHZV4PSe2dvWQ8NLFoDhXeZjm": "Bitman",
    "8RCEq8RrBJ1G6eji9vZqjtjQgkDjUHMysPtynZENWo7S": "Avocado",
    "5pHeNsWMVEi1cbMzLhgqABnhEUwRTSzy5vBfeGWyJfxS": "5pHeNs",
}
LANE_S0_ПРЕФИКСЫ = ("B8m6fDRc", "7JVQMwRj", "3Um4qsYQ")
OFF_ПОЛНЫЕ = {
    "DAejzMs5cUeCCENNvapy9KWFwzwegh7LvcgNkZ6hnf1y": "fomopumpguy",
    "DYbZngFdHcaEEo4iLpjtAXKgXCECftbAhZeampomtCQc": "earn",
    "Hn5gVKAApv69t5HX7Q77uX7o5ayhEwArgYx7kukMLVGn": "gginvestments",
    "6F91X5t8af1yRHALkwf8EStEn6mKYvsW8zpLiVpfYr38": "Pyro",
}
OFF_ПРЕФИКСЫ = ("FjUJgFT3", "D1wfmvGq", "DijCeWfM", "J2QQwDNY", "4vgKuikt")

# ---- политики групп: числа владельца, слово в слово из плана ----
ПОЛИТИКИ = {
    "leader": {
        "lane_size": 0.5, "lane_trades": True, "bloom_trades": False,
        "bloom_sol": None, "fanout": False,
        "min_target_sol": 3, "max_slots_from_source": None,
        "skip_flippers": False, "allow_taxed_route": True,
        # НАЦЕНКА ЛИДЕРА -- ЧИСЛА ВЛАДЕЛЬЦА 27.09 вечером. 0.2 значит min-out
        # 80 % ожидания, то есть допустимая наценка входа 25 % (по данным
        # Code-2); прежние 0.25 давали наценку 33 %. На ТОНКОМ пуле (резерв
        # котировочной стороны ниже 30 SOL-экв. по meta сделки источника)
        # наценка до 100 %, то есть min-out 50 % ожидания.
        "slippage": 0.2, "min_pool_sol_reserve": None,
        "slippage_thin_pool": 0.5, "thin_pool_below_sol": 30,
        # ТАЙМЕР ВЫХОДА -- 150 СЛОТОВ ПРИ ЛЮБОМ РАЗМЕРЕ. Правило "36 слотов на
        # покупке источника меньше 15 SOL-экв." владелец отменил тем же вечером
        # 27.09; полей hold_slots_small здесь нет, и правило не действует.
        "lane_open_max": 3, "stop_loss_sol": 0.75, "hold_slots": 150,
        "lane_pools": ["pump_amm", "cpmm", "bonding", "two_step",
                        "damm2", "dbc"],
        "note": ("Лидер. Размер 0.5 SOL, порог источника 3 SOL-экв., налоговый "
                  "маршрут берём и пишем налог в строку решения, наценка входа до "
                  "0.2 (min-out 80 %, наценка 25 %), а на пуле с резервом ниже "
                  "30 SOL-экв. -- 0.5 (наценка до 100 %); три открытых, стоп "
                  "-0.75 за сутки, держим 36 слотов от s0 при покупке "
                  "150 слотов от s0 при любом размере покупки источника. Пулы: Pump AMM, Raydium CPMM, кривая pump.fun и "
                  "двухшаговый маршрут через котировочный токен (п.3). LaunchLab, "
                  "CLMM, DAMM v2 и DBC -- следующим шагом по одному (п.4), "
                  "поэтому их в списке ПОКА НЕТ."),
    },
    "batch5": {
        "lane_size": 0.3, "lane_trades": True, "bloom_trades": False,
        "bloom_sol": None, "fanout": False,
        "min_target_sol": 2, "max_slots_from_source": None,
        "skip_flippers": False, "allow_taxed_route": True,
        # РЕЗЕРВ ПУЛА У batch5 -- НЕ ПРЕДЕЛ (слово владельца 27.09, дополнение
        # п.2): "min_pool_sol_reserve -- нет; резерв только пишется в строку
        # позиции". None и значит "не проверять": число всё равно идёт в строку.
        "slippage": 0.40, "min_pool_sol_reserve": None,
        "lane_open_max": 3, "stop_loss_sol": 0.45, "hold_slots": 72,
        "lane_pools": ["pump_amm", "cpmm", "bonding", "two_step",
                        "damm2", "dbc"],
        "note": ("Восемь названных кошельков BATCH-5. 0.3 SOL, порог 2 SOL-экв., "
                  "наценка входа до 0.40, предела резерва пула НЕТ (резерв только "
                  "пишется в строку позиции), три открытых, стоп -0.45, держим 72 "
                  "слота от s0."),
    },
    "lane_s0": {
        "lane_size": 0.3, "lane_trades": True, "bloom_trades": False,
        "bloom_sol": None, "fanout": False,
        # ПРАВИЛА КАК У batch5 (слово владельца 27.09, вечер, п.5): все типы
        # пулов вместе с двухшаговым маршрутом, налоговый маршрут берём, предела
        # резерва пула нет (резерв только пишется в строку позиции), наценка
        # входа до 0.40. Предел "не больше 3 слотов от s0" остаётся -- он и есть
        # смысл этой группы.
        "min_target_sol": 2, "max_slots_from_source": 3,
        "skip_flippers": True, "allow_taxed_route": True,
        "slippage": 0.40, "min_pool_sol_reserve": None,
        "lane_open_max": 3, "stop_loss_sol": 0.45, "hold_slots": 72,
        "lane_pools": ["pump_amm", "cpmm", "bonding", "two_step",
                        "damm2", "dbc"],
        "note": ("Десять кошельков с усечённым S+0. Правила как у batch5 "
                  "(слово владельца 27.09, вечер): 0.3 SOL, порог 2 SOL-экв., "
                  "наценка входа до 0.40, налоговый маршрут берём, предела "
                  "резерва пула нет (резерв только пишется в строку позиции), "
                  "все типы пулов вместе с двухшаговым маршрутом. Своё у группы "
                  "одно: не покупаем, если от слота источника прошло больше 3 "
                  "слотов, и помеченных перекупщиками пропускаем (сейчас не "
                  "помечен ни один)."),
    },
    "off": {
        "lane_size": None, "lane_trades": False, "bloom_trades": False,
        "bloom_sol": None, "fanout": False,
        "note": ("Выключены прямым словом владельца 27.09. Подписка и лог "
                  "остаются, денег не тратим."),
    },
    "log_only": {
        "lane_size": None, "lane_trades": False, "bloom_trades": False,
        "bloom_sol": None, "fanout": False,
        "note": ("Все прочие адреса, на которые служба подписана. Слово "
                  "владельца: lane_trades=false, только лог сигналов со всеми "
                  "полями. Из файла их убирать нельзя -- пропала бы подписка, а "
                  "с ней и лог."),
    },
}


def разрешить_префиксы(префиксы: tuple, все: set) -> dict:
    """Префикс -> полный адрес. Совпадений не ровно одно -- отказ со словами."""
    из_, беда = {}, []
    for п in префиксы:
        нашлись = sorted(а for а in все if а.startswith(п))
        if len(нашлись) == 1:
            из_[п] = нашлись[0]
        else:
            беда.append((п, нашлись))
    return {"адреса": из_, "спорные": беда}


def адреса_прежнего(д: dict) -> list:
    """Адреса, на которые служба подписана СЕЙЧАС: из групп прежнего файла."""
    из_ = []
    for имя, г in (д.get("groups") or {}).items():
        ад = г.get("addresses")
        if isinstance(ад, dict):
            из_ += [(а, имя) for а in ад]
        elif isinstance(ад, list):
            из_ += [((з.get("address") if isinstance(з, dict) else з), имя)
                    for з in ад]
        for поле in ("by_signal", "snipers"):
            из_ += [((з.get("address") if isinstance(з, dict) else з),
                      f"{имя}/{поле}") for з in (г.get(поле) or [])]
    return [(а, г) for а, г in из_ if а]


ФАЙЛ_543 = "data/podbivka/wallets.csv"


def адреса_543(путь: str | None = None) -> list:
    """Адреса списка 543 из файла второй сессии. Нет файла -- пустой список.

    Молча резать список нельзя (слово владельца), но и выдумывать адреса,
    которых в файле нет, тоже: если файла нет, сборка добавит ноль адресов, и
    это будет видно по числу в отчёте.
    """
    п = _путь(путь or ФАЙЛ_543)
    if not п.exists():
        return []
    из_ = []
    with open(п, encoding="utf-8") as ф:
        for стр in csv.DictReader(ф):
            а = (стр.get("address") or "").strip()
            if len(а) >= 32:
                из_.append((а, f"список 543: {(стр.get('group') or '')[:40]}"))
    return из_


def _путь(путь: str) -> Path:
    """Путь как дан или рядом с корнем репозитория.

    Сборку запускают и из корня, и из analysis (так делает прогон замены), и
    относительный путь во втором случае не находится. Падать на этом -- терять
    самопроверку там, где она нужнее всего.
    """
    п = Path(путь)
    if п.exists():
        return п
    рядом = Path(__file__).resolve().parent.parent / путь
    return рядом if рядом.exists() else п


def собрать(путь_прежнего: str) -> dict:
    файл_прежнего = _путь(путь_прежнего)
    прежний = json.loads(файл_прежнего.read_text(encoding="utf-8"))
    все_в_файле = set(re.findall(r'"([1-9A-HJ-NP-Za-km-z]{32,44})"',
                                  файл_прежнего.read_text(encoding="utf-8")))
    s0 = разрешить_префиксы(LANE_S0_ПРЕФИКСЫ, все_в_файле)
    off = разрешить_префиксы(OFF_ПРЕФИКСЫ, все_в_файле)
    спорные = s0["спорные"] + off["спорные"]

    группы = {}
    for имя, пол in ПОЛИТИКИ.items():
        группы[имя] = dict(пол)
        группы[имя]["addresses"] = {}
    for а, имя in ЛИДЕР.items():
        группы["leader"]["addresses"][а] = {"name": имя, "from": "названо владельцем"}
    for а, имя in BATCH5.items():
        группы["batch5"]["addresses"][а] = {"name": имя, "from": "названо владельцем"}
    for а, имя in LANE_S0_ПОЛНЫЕ.items():
        группы["lane_s0"]["addresses"][а] = {"name": имя, "from": "названо владельцем",
                                              "flipper": False}
    for п, а in s0["адреса"].items():
        группы["lane_s0"]["addresses"][а] = {"name": п, "from": f"префикс {п}",
                                             "flipper": False}
    for а, имя in OFF_ПОЛНЫЕ.items():
        группы["off"]["addresses"][а] = {"name": имя, "from": "названо владельцем"}
    for п, а in off["адреса"].items():
        группы["off"]["addresses"][а] = {"name": п, "from": f"префикс {п}"}

    названные = set()
    for имя in ("leader", "batch5", "lane_s0", "off"):
        названные |= set(группы[имя]["addresses"])
    for а, откуда in адреса_прежнего(прежний):
        if а in названные or а in НАШИ_КОШЕЛЬКИ:
            continue
        группы["log_only"]["addresses"][а] = {"from": f"прежний файл: {откуда}"}
    # СПИСОК 543 (дополнение владельца 27.09): все адреса подбивки Code-2 --
    # в лог-группу, lane_trades=false, только лог сигналов. Список читается из
    # файла второй сессии, а не переписывается сюда руками: переписанный он
    # разошёлся бы с её таблицей молча.
    for а, откуда in адреса_543():
        if а in названные or а in НАШИ_КОШЕЛЬКИ:
            continue
        if а in группы["log_only"]["addresses"]:
            группы["log_only"]["addresses"][а]["also"] = откуда
            continue
        группы["log_only"]["addresses"][а] = {"from": откуда}

    новый = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "decision": ("Финальный план живого теста, владелец 27.09. Четыре группы: "
                      "leader, batch5, lane_s0 и off; Bloom не торгует ни по одной "
                      "(bloom_trades=false), веер выключен (fanout=false), Jupiter "
                      "в покупке не участвует ни на одной группе. Прежние группы "
                      "speed_only, bloom_lane, lane_only и candidates упразднены."),
        "groups": группы,
        "prefixes_resolved": {**s0["адреса"], **off["адреса"]},
        "disputed_prefixes": спорные,
        "archive_2026_09_25": {k: прежний.get(k) for k in
                                ("generated_utc", "decision", "sources_s0_data",
                                 "not_added", "our_task_wallets_excluded",
                                 "not_signers_excluded") if k in прежний},
    }
    return {"файл": новый, "спорные": спорные,
            "торгующих": sum(len(группы[и]["addresses"])
                             for и in ("leader", "batch5", "lane_s0")),
            "в_логе": len(группы["log_only"]["addresses"]),
            "выключенных": len(группы["off"]["addresses"])}


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    с = собрать(SG.ФАЙЛ_ПО_УМОЛЧАНИЮ)
    ф = с["файл"]
    chk("спорных префиксов нет", not с["спорные"], с["спорные"])
    # 23 = 1 лидер + 12 batch5 (восемь прежних и четыре названных владельцем
    # 27.09 вечером) + 10 lane_s0.
    chk("торгующих адресов ровно 23 (слово владельца)", с["торгующих"] == 23,
        с["торгующих"])
    chk("в leader один адрес", len(ф["groups"]["leader"]["addresses"]) == 1,
        len(ф["groups"]["leader"]["addresses"]))
    chk("в batch5 двенадцать", len(ф["groups"]["batch5"]["addresses"]) == 12,
        len(ф["groups"]["batch5"]["addresses"]))
    for _адр, _имя in (("8xL8S7P4QLdTGRquHas8NP5EVjp2qUGbmSgrkh97mvmq", "Avast"),
                        ("BA3nKHc4DoSANRrx4FcCupExzs6cWzw1wkPpqjnqJaCN", "AviFelman"),
                        ("GAsnqm4XkNkPVgrAofNQ65jWf8f3tKCLHhE9ZqSy2AP1", "Nach"),
                        ("9zZCjLr9xfXfp3qdqvPh6YeaEaagepz21khLhca69B18", "Pasterniq")):
        chk(f"{_имя} в batch5, и ни в одной другой группе",
            _адр in ф["groups"]["batch5"]["addresses"]
            and not any(_адр in (ф["groups"][г].get("addresses") or {})
                        for г in ("leader", "lane_s0", "off", "log_only")),
            _адр[:10])
    chk("в lane_s0 десять", len(ф["groups"]["lane_s0"]["addresses"]) == 10,
        len(ф["groups"]["lane_s0"]["addresses"]))
    chk("в off девять", len(ф["groups"]["off"]["addresses"]) == 9,
        len(ф["groups"]["off"]["addresses"]))
    все = []
    for имя, г in ф["groups"].items():
        все += list(г["addresses"])
    chk("ни один адрес не попал в две группы", len(все) == len(set(все)),
        [а for а in set(все) if все.count(а) > 1])
    chk("наших кошельков в источниках нет",
        not (set(НАШИ_КОШЕЛЬКИ) & set(все)), set(НАШИ_КОШЕЛЬКИ) & set(все))
    # ДЕНЬГИ: размеры и стопы -- те, что назвал владелец.
    chk("лидер: 0.5, стоп -0.75, порог 3, наценка 0.2 и 0.5 на тонком пуле",
        ф["groups"]["leader"]["lane_size"] == 0.5
        and ф["groups"]["leader"]["stop_loss_sol"] == 0.75
        and ф["groups"]["leader"]["min_target_sol"] == 3
        and ф["groups"]["leader"]["slippage"] == 0.2
        and ф["groups"]["leader"]["slippage_thin_pool"] == 0.5
        and ф["groups"]["leader"]["thin_pool_below_sol"] == 30,
        ф["groups"]["leader"])
    chk("batch5 0.3, стоп -0.45, предела резерва нет, наценка 0.40",
        ф["groups"]["batch5"]["lane_size"] == 0.3
        and ф["groups"]["batch5"]["stop_loss_sol"] == 0.45
        and ф["groups"]["batch5"]["min_pool_sol_reserve"] is None
        and ф["groups"]["batch5"]["slippage"] == 0.40, ф["groups"]["batch5"])
    # ПРАВИЛА lane_s0 ПРИРАВНЕНЫ К batch5 (слово владельца 27.09, вечер, п.5).
    chk("lane_s0 0.3, стоп -0.45, три слота от s0, правила batch5",
        ф["groups"]["lane_s0"]["lane_size"] == 0.3
        and ф["groups"]["lane_s0"]["stop_loss_sol"] == 0.45
        and ф["groups"]["lane_s0"]["max_slots_from_source"] == 3
        and ф["groups"]["lane_s0"]["slippage"] == 0.40
        and ф["groups"]["lane_s0"]["min_pool_sol_reserve"] is None
        and set(ф["groups"]["lane_s0"]["lane_pools"])
        >= {"pump_amm", "cpmm", "bonding", "two_step"}
        and ф["groups"]["lane_s0"]["allow_taxed_route"] is True,
        ф["groups"]["lane_s0"])
    chk("Bloom не торгует ни по одной группе",
        not any(г.get("bloom_trades") for г in ф["groups"].values()),
        [и for и, г in ф["groups"].items() if г.get("bloom_trades")])
    chk("веер выключен везде",
        not any(г.get("fanout") for г in ф["groups"].values()),
        [и for и, г in ф["groups"].items() if г.get("fanout")])
    chk("список 543 прочитан из файла второй сессии, а не переписан руками",
        len(адреса_543()) >= 500, len(адреса_543()))
    торг = set(list(ф["groups"]["leader"]["addresses"])
               + list(ф["groups"]["batch5"]["addresses"])
               + list(ф["groups"]["lane_s0"]["addresses"]))
    # Часть списка 543 -- это и есть кошельки, которых владелец назвал для
    # торговли (их оттуда и выбирали). Правило не "их там нет", а "список 543
    # не перебивает названную группу и не двоит адрес".
    общие = set(а for а, _ in адреса_543()) & торг
    chk("адреса из 543, названные для торговли, остались в торгующей группе и "
        "в log_only не задвоены",
        общие and not (общие & set(ф["groups"]["log_only"]["addresses"])),
        (len(общие), sorted(общие & set(ф["groups"]["log_only"]["addresses"]))[:5]))
    chk("все адреса списка 543 есть в файле (молча не резали)",
        not (set(а for а, _ in адреса_543())
             - set(ф["groups"]["log_only"]["addresses"]) - торг
             - set(ф["groups"]["off"]["addresses"])),
        len(set(а for а, _ in адреса_543())
            - set(ф["groups"]["log_only"]["addresses"]) - торг
            - set(ф["groups"]["off"]["addresses"])))
    chk("off и log_only не торгуют полосой",
        not ф["groups"]["off"]["lane_trades"]
        and not ф["groups"]["log_only"]["lane_trades"], "")
    chk("Jupiter в покупке не включён ни на одной группе",
        not any(г.get("lane_route") for г in ф["groups"].values()),
        [и for и, г in ф["groups"].items() if г.get("lane_route")])

    плохо = [(и, ф_) for и, ок, ф_ in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}"
              + ("" if ок else f" -- факт: {факт}"))
    print(f"самопроверка сборки групп: {len(проверки) - len(плохо)}/"
          f"{len(проверки)} пройдено")
    return 1 if плохо else 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--iz", default=SG.ФАЙЛ_ПО_УМОЛЧАНИЮ,
                   help="прежний файл групп (источник префиксов и лог-группы)")
    р.add_argument("--zapisat", default="",
                   help="куда записать новый файл (пусто -- только показать)")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    с = собрать(а.iz)
    print(f"торгующих {с['торгующих']}, в логе {с['в_логе']}, "
          f"выключенных {с['выключенных']}, спорных префиксов {len(с['спорные'])}")
    if с["спорные"]:
        print("СПОРНЫЕ ПРЕФИКСЫ (в файл не добавлены):")
        for п, нашлись in с["спорные"]:
            print(f"  {п}: {нашлись or 'совпадений 0'}")
    if а.zapisat:
        итог = SG.записать(с["файл"], а.zapisat)
        print(json.dumps(итог, ensure_ascii=False))
        return 0 if итог.get("ok") else 1
    print(json.dumps(с["файл"], ensure_ascii=False, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
