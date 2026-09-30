#!/usr/bin/env python3
"""Включить тип пула в lane_pools названных групп -- полным списком, с проверкой.

ЗАЧЕМ. Владелец 30.09: "у lane_s0 и batch5 lane_pools полным списком (всё, что
торгует сейчас, + clmm + whirlpool)". Список ПОЛНЫЙ И ЗАПРЕЩАЮЩИЙ: чего в нём
нет -- то выключено, и одна забытая строка тихо останавливает торговлю по типу.

ЧТО ДЕЛАЕТ. Нынешний набор берёт не из файла, а ВЫЗОВОМ bloom_own_send.
типы_полосы(группа) и двухшаговый_включён(группа) -- тем же кодом, по которому
решает полоса, -- и к нему добавляет названные типы. Пишет атомарно (временный
файл рядом, os.replace), с копией прежнего файла. После записи ПЕРЕЧИТЫВАЕТ
политику заново и печатает, что теперь берёт каждая группа: без этого "записал"
и "действует" -- разные утверждения.

ОТКАЗЫВАЕТСЯ, а не пишет наугад: если группы нет в файле; если она не торгует
полосой; если из полного списка исчез бы хоть один нынешний тип; если в наборе
есть программа, которую мы не умеем назвать словом lane_pools.

ПЕРЕЗАПУСК НЕ НУЖЕН: загрузить() снимает кеш по метке файла (путь, mtime_ns,
размер), bloom_source_groups.py:261. Проверять применённость -- по строке
признака жизни (там mtime и hash файла групп), а не по факту записи.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time

КОД = os.environ.get("BLOOM_CODE_DIR") or "/home/bot/bloom_executor"
if КОД not in sys.path:
    sys.path.insert(0, КОД)
ПРОВЕРКИ = os.path.dirname(os.path.abspath(__file__))
if ПРОВЕРКИ not in sys.path:
    sys.path.insert(0, ПРОВЕРКИ)

from lane_pools_polnyj_spisok import предложение, состояние_группы  # noqa: E402


def хэш(путь: str) -> str:
    h = hashlib.sha256()
    with open(путь, "rb") as ф:
        for кусок in iter(lambda: ф.read(65536), b""):
            h.update(кусок)
    return h.hexdigest()[:12]


def записать_атомарно(путь: str, данные: dict) -> str:
    """Копия прежнего рядом, запись во временный, os.replace. Без частичных файлов."""
    метка = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    копия = f"{путь}.do_{метка}"
    shutil.copy2(путь, копия)
    врем = f"{путь}.nov_{метка}"
    with open(врем, "w", encoding="utf-8") as ф:
        json.dump(данные, ф, ensure_ascii=False, indent=1)
        ф.write("\n")
        ф.flush()
        os.fsync(ф.fileno())
    os.replace(врем, путь)
    return копия


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    # НЕ required: самопроверка обязана идти без единого аргумента (её зовёт
    # прогон до всякого хоста). Отсутствие проверяется ниже, после --self-test.
    р.add_argument("--gruppy", default="", help="группы через запятую")
    р.add_argument("--dobavit", default="", help="типы через запятую")
    р.add_argument("--podtverzhdenie", default="",
                   help="ровно DA -- иначе только показать, не писать")
    р.add_argument("--out", default=None)
    р.add_argument("--self-test", dest="self_test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    группы = [г.strip() for г in а.gruppy.split(",") if г.strip()]
    добавить = [т.strip().lower() for т in а.dobavit.split(",") if т.strip()]
    if not группы or not добавить:
        print("СБОЙ: нужны --gruppy и --dobavit")
        return 2
    писать = (а.podtverzhdenie or "").strip() == "DA"

    import bloom_own_send as OS  # noqa: PLC0415
    import bloom_source_groups as SG  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415

    д = SG.загрузить()
    файл = д.get("file")
    if not файл or not os.path.exists(файл):
        print(f"СБОЙ: файла групп нет ({файл}); {д.get('why_not')}")
        return 2
    сырое = json.loads(open(файл, encoding="utf-8").read())
    разделы = сырое.get("groups") or {}
    print(f"файл групп: {файл} (hash {хэш(файл)}, групп {len(разделы)})")
    print(f"добавляем: {', '.join(добавить)}; группы: {', '.join(группы)}")
    print(f"писать: {'ДА' if писать else 'нет -- только показ'}")

    план, ошибки = [], []
    for группа in группы:
        if группа not in разделы:
            ошибки.append(f"группы {группа} в файле нет")
            continue
        п = SG.политика(группа)
        if not (bool(п.get("lane_trades")) and п.get("lane_sol") is not None):
            ошибки.append(f"группа {группа} не торгует полосой "
                           f"(lane_trades={п.get('lane_trades')}, "
                           f"lane_sol={п.get('lane_sol')})")
            continue
        с = состояние_группы(OS, B, группа)
        if с["неизвестных_программ"]:
            ошибки.append(f"группа {группа}: не умеем назвать "
                           f"{с['неизвестных_программ']}")
            continue
        новый = предложение(с["сейчас"], добавить)
        пропало = sorted(set(с["сейчас"]) - set(новый))
        if пропало:
            ошибки.append(f"группа {группа}: из списка пропали {пропало}")
            continue
        план.append({"группа": группа, "было": с["сейчас"], "станет": новый,
                      "в_файле_было": (разделы[группа] or {}).get("lane_pools"),
                      "добавится": sorted(set(новый) - set(с["сейчас"]))})
        print(f"  {группа}: было {с['сейчас']}")
        print(f"      станет {новый} (добавится {план[-1]['добавится']})")

    if ошибки:
        for о in ошибки:
            print(f"  СБОЙ: {о}")
        return 1
    итог = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "файл_групп": файл, "hash_до": хэш(файл), "план": план,
             "записано": False, "копия": None, "после": None}
    if писать:
        for шаг in план:
            разделы[шаг["группа"]]["lane_pools"] = шаг["станет"]
        сырое["groups"] = разделы
        копия = записать_атомарно(файл, сырое)
        итог.update(записано=True, копия=копия, hash_после=хэш(файл))
        print(f"записано; копия прежнего: {копия}; hash после {хэш(файл)}")
        # ПЕРЕЧИТЫВАЕМ ЗАНОВО и спрашиваем полосу: что теперь берёт каждая группа.
        SG.загрузить(заново=True)
        после = {}
        for шаг in план:
            после[шаг["группа"]] = состояние_группы(OS, B, шаг["группа"])["сейчас"]
            print(f"  ПОСЛЕ перечитывания {шаг['группа']}: {после[шаг['группа']]}")
            не_вошло = sorted(set(шаг["станет"]) - set(после[шаг["группа"]]))
            if не_вошло:
                print(f"      ТРЕВОГА: в гейт не попало {не_вошло}")
        итог["после"] = после
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print(f"отчёт записан: {а.out}")
    if писать:
        плохо = [г for г, набор in (итог["после"] or {}).items()
                  if not set(добавить) <= set(набор)]
        if плохо:
            print(f"СБОЙ: у групп {плохо} новый тип в гейт не попал")
            return 1
    return 0


def self_test() -> int:
    """Только своя арифметика и атомарная запись. Ни сети, ни модулей полосы."""
    import tempfile
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    with tempfile.TemporaryDirectory() as кат:
        путь = os.path.join(кат, "sources.json")
        начало = {"groups": {"batch5": {"lane_pools": ["pump_amm"]}}}
        open(путь, "w", encoding="utf-8").write(json.dumps(начало))
        х1 = хэш(путь)
        копия = записать_атомарно(
            путь, {"groups": {"batch5": {"lane_pools": ["pump_amm", "clmm"]}}})
        chk("копия прежнего файла осталась рядом и читается",
            json.loads(open(копия, encoding="utf-8").read()) == начало)
        chk("новый файл записан целиком и разбирается",
            json.loads(open(путь, encoding="utf-8").read())["groups"]["batch5"]
            ["lane_pools"] == ["pump_amm", "clmm"])
        chk("хэш изменился -- метка файла другая, политика перечитается",
            хэш(путь) != х1)
        chk("временных файлов не осталось",
            not [и for и in os.listdir(кат) if ".nov_" in и], os.listdir(кат))
    print(f"самопроверка включения lane_pools: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    return 0 if всего[1] == всего[0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
