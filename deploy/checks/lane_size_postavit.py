#!/usr/bin/env python3
"""Размер сделки полосы у названных групп: поставить и ПРОВЕРИТЬ, что он применился.

ЗАЧЕМ ОТДЕЛЬНЫМ ИНСТРУМЕНТОМ. Размер группы -- одно число под ДВУМЯ именами:
lane_size (имя финального плана) и lane_sol (прежнее). Если оба заданы и разные,
загрузчик по слову владельца 29.09 СТАВИТ ГРУППУ В НЕТОРГУЮЩИЕ: неясность на
деньгах = запрет. Ровно на этом 29.09 сгорела правка: в файле лежал lane_size 0.3,
инструмент поставил lane_sol 0.1, приоритет остался у lane_size -- размер не
изменился, а по признаку жизни этого не видно, потому что наружу уходит уже
разрешённое число.

Поэтому здесь: читаем СЫРОЙ файл, ставим новое число ВО ВСЕ присутствующие из двух
имён (а если нет ни одного -- в lane_size), пишем атомарно с копией, перечитываем
политику заново и спрашиваем у самой полосы (bloom_own_send.размер_sol) -- что
она теперь считает размером. Не сошлось -- прогон падает.

ЧЕГО НЕ ДЕЛАЕТ: не трогает пороги стопов (stop_loss_sol, min_target_sol,
lane_open_max) и lane_pools -- ни одного другого поля, кроме размера.
"""
import argparse
import json
import os
import sys

КОД = os.environ.get("BLOOM_CODE_DIR") or "/home/bot/bloom_executor"
if КОД not in sys.path:
    sys.path.insert(0, КОД)
ПРОВЕРКИ = os.path.dirname(os.path.abspath(__file__))
if ПРОВЕРКИ not in sys.path:
    sys.path.insert(0, ПРОВЕРКИ)

from lane_pools_vkljuchit import записать_атомарно, хэш  # noqa: E402

ИМЕНА_РАЗМЕРА = ("lane_size", "lane_sol")
# Потолок -- тот же, что у полосы (ПОТОЛОК_РАЗМЕРА_SOL). Здесь он ещё и запрет:
# поставить больше потолка значит поставить размер, который полоса срежет молча.
ПОТОЛОК_ПО_УМОЛЧАНИЮ = 0.5


def какие_имена(сырое_группы: dict) -> list:
    """Какие из двух имён размера в файле ЕСТЬ. Нет ни одного -- ставим lane_size."""
    есть = [и for и in ИМЕНА_РАЗМЕРА if и in (сырое_группы or {})]
    return есть or ["lane_size"]


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--gruppy", default="", help="группы через запятую")
    р.add_argument("--razmer", default="", help="новый размер в SOL")
    р.add_argument("--podtverzhdenie", default="",
                   help="ровно DA -- иначе только показать")
    р.add_argument("--out", default=None)
    р.add_argument("--self-test", dest="self_test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    группы = [г.strip() for г in а.gruppy.split(",") if г.strip()]
    if not группы or not а.razmer:
        print("СБОЙ: нужны --gruppy и --razmer")
        return 2
    try:
        новый = float(а.razmer)
    except ValueError:
        print(f"СБОЙ: размер '{а.razmer}' -- не число")
        return 2
    if not (0 < новый <= ПОТОЛОК_ПО_УМОЛЧАНИЮ):
        print(f"СБОЙ: размер {новый} вне (0, {ПОТОЛОК_ПО_УМОЛЧАНИЮ}]")
        return 2
    писать = (а.podtverzhdenie or "").strip() == "DA"

    import bloom_own_send as OS  # noqa: PLC0415
    import bloom_source_groups as SG  # noqa: PLC0415

    потолок = float(getattr(OS, "ПОТОЛОК_РАЗМЕРА_SOL", ПОТОЛОК_ПО_УМОЛЧАНИЮ))
    if новый > потолок:
        print(f"СБОЙ: размер {новый} выше потолка полосы {потолок}")
        return 2
    д = SG.загрузить()
    файл = д.get("file")
    if not файл or not os.path.exists(файл):
        print(f"СБОЙ: файла групп нет ({файл}); {д.get('why_not')}")
        return 2
    сырое = json.loads(open(файл, encoding="utf-8").read())
    разделы = сырое.get("groups") or {}
    print(f"файл групп: {файл} (hash {хэш(файл)})")
    print(f"ставим размер {новый} SOL группам: {', '.join(группы)}; "
           f"потолок полосы {потолок}; писать: {'ДА' if писать else 'нет'}")

    план, ошибки = [], []
    for г in группы:
        if г not in разделы:
            ошибки.append(f"группы {г} в файле нет")
            continue
        п = SG.политика(г)
        if not п.get("lane_trades"):
            ошибки.append(f"группа {г} не торгует полосой -- размер ей ни к чему")
            continue
        имена = какие_имена(разделы[г])
        было = OS.размер_sol(г)
        план.append({"группа": г, "имена": имена, "было": было, "станет": новый,
                      "в_файле": {и: разделы[г].get(и) for и in ИМЕНА_РАЗМЕРА},
                      "стопы_не_тронуты": {
                          к: разделы[г].get(к) for к in
                          ("stop_loss_sol", "min_target_sol", "lane_open_max")}})
        print(f"  {г}: было {было} -> станет {новый}; правим поля {имена}; "
               f"стопы остаются {план[-1]['стопы_не_тронуты']}")
    if ошибки:
        for о in ошибки:
            print(f"  СБОЙ: {о}")
        return 1

    итог = {"файл_групп": файл, "hash_до": хэш(файл), "размер": новый,
             "план": план, "записано": False, "после": None}
    if писать:
        for ш in план:
            for и in ш["имена"]:
                разделы[ш["группа"]][и] = новый
            # ОБА ИМЕНИ ПРИВОДИМ К ОДНОМУ ЧИСЛУ, если оба в файле были: иначе
            # загрузчик увидит спор и остановит торговлю группе.
            if all(и in разделы[ш["группа"]] for и in ИМЕНА_РАЗМЕРА):
                for и in ИМЕНА_РАЗМЕРА:
                    разделы[ш["группа"]][и] = новый
        сырое["groups"] = разделы
        копия = записать_атомарно(файл, сырое)
        итог.update(записано=True, копия=копия, hash_после=хэш(файл))
        print(f"записано; копия прежнего: {копия}; hash после {хэш(файл)}")
        SG.загрузить(заново=True)
        после = {}
        for ш in план:
            г = ш["группа"]
            п = SG.политика(г)
            после[г] = {"размер_полосы": OS.размер_sol(г),
                         "lane_size": п.get("lane_size"),
                         "lane_sol": п.get("lane_sol"),
                         "торгует": п.get("lane_trades"),
                         "спор": п.get("lane_size_spor"),
                         "stop_loss_sol": п.get("stop_loss_sol"),
                         "min_target_sol": п.get("min_target_sol")}
            print(f"  ПОСЛЕ {г}: полоса считает размером "
                   f"{после[г]['размер_полосы']}, торгует={после[г]['торгует']}, "
                   f"стопы {после[г]['stop_loss_sol']}/{после[г]['min_target_sol']}")
        итог["после"] = после
        плохо = [г for г, з in после.items()
                  if abs(float(з["размер_полосы"] or 0) - новый) > 1e-12
                  or not з["торгует"] or з["спор"]]
        if плохо:
            print(f"СБОЙ: у групп {плохо} размер не применился или спор имён")
            if а.out:
                json.dump(итог, open(а.out, "w", encoding="utf-8"),
                           ensure_ascii=False, indent=1)
            return 1
    if а.out:
        json.dump(итог, open(а.out, "w", encoding="utf-8"),
                   ensure_ascii=False, indent=1)
        print(f"отчёт записан: {а.out}")
    return 0


def self_test() -> int:
    """Только своя логика имён и границ. Ни сети, ни файла групп, ни полосы."""
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    chk("в файле оба имени -- правим оба",
        какие_имена({"lane_size": 0.1, "lane_sol": 0.1}) == ["lane_size", "lane_sol"])
    chk("в файле только lane_sol -- правим его",
        какие_имена({"lane_sol": 0.1}) == ["lane_sol"])
    chk("в файле только lane_size -- правим его",
        какие_имена({"lane_size": 0.1}) == ["lane_size"])
    # НЕТ НИ ОДНОГО -- СТАВИМ lane_size: имя финального плана, и приоритет у него.
    chk("нет ни одного имени -- ставим lane_size",
        какие_имена({}) == ["lane_size"] and какие_имена(None) == ["lane_size"])
    chk("потолок по умолчанию -- 0.5, как у полосы",
        ПОТОЛОК_ПО_УМОЛЧАНИЮ == 0.5)
    print(f"самопроверка размера полосы: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    return 0 if всего[1] == всего[0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
