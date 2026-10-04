#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ПРЕДПУСКОВАЯ ПРОВЕРКА РАЗОВОГО ПРОГОНА. Выполняется НА ХОСТЕ, до запуска.

ЗАЧЕМ. 04--05.10 разовые прогоны упали пять раз подряд, и ни разу -- на самом
деле. Падали на оснастке:

  1. КИРИЛЛИЦА В ИМЕНИ ПЕРЕМЕННОЙ ОБОЛОЧКИ -- оболочка такие имена не
     присваивает, и переменная молча оказывалась пустой;
  2. sys.path ПЕРЕКРЫВАЛ PYTHONPATH -- каталог самого скрипта идёт в sys.path
     ПЕРВЫМ, поэтому `import bloom_seller` брал СТАРЫЙ модуль с диска службы, а
     не доставленный. Прогон шёл зелёным по чужому коду;
  3. `ssh "cat > файл" < файл` В ЦИКЛЕ доставлял НЕ ВСЕ файлы -- часть доезжала
     пустой, и падало уже внутри, на импорте;
  4. `echo "...\\x..."` НЕ РАЗВОРАЧИВАЛСЯ -- в файл уезжали литеральные `\\x`;
  5. каталог работы совпадал у двух прогонов -- второй читал файлы первого.

ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ, И КАЖДОЕ -- ОТКАЗОМ, А НЕ ПРЕДУПРЕЖДЕНИЕМ:
  * контрольная сумма КАЖДОГО доставленного файла совпала с объявленной;
  * КАЖДЫЙ названный модуль импортируется ИМЕННО ИЗ НАШЕГО каталога;
  * питон не ниже объявленного;
  * на диске есть место;
  * каталог работы -- наш и пуст от чужого.

Ничего не пишет, кроме своего вывода, и ни одного ключа не печатает.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ПИТОН_НЕ_НИЖЕ = (3, 8)
МЕСТА_НЕ_МЕНЬШЕ_МБ = 200

WHY_СУММА = "контрольная сумма не совпала"
WHY_ЧУЖОЙ_МОДУЛЬ = "модуль импортируется НЕ из нашего каталога"
WHY_ПИТОН = "питон ниже объявленного"
WHY_МЕСТО = "на диске мало места"
WHY_НЕТ_ФАЙЛА = "файла нет"


def summy_fajlov(kat: str | Path) -> dict:
    """sha256 каждого файла каталога -- именем файла, а не одним числом.

    Одно число на весь каталог не говорит, КАКОЙ файл не доехал, а именно это
    и нужно знать: 05.10 не доехал ровно один из шести.
    """
    к = Path(kat)
    из_ = {}
    for п in sorted(к.rglob("*")):
        if п.is_file():
            из_[str(п.relative_to(к))] = hashlib.sha256(п.read_bytes()).hexdigest()
    return из_


def sverit_summy(kat: str | Path, zhdjom: dict) -> dict:
    """Сверить доставленное с объявленным. Лишнее -- тоже отказ."""
    есть = summy_fajlov(kat)
    из_ = {"ok": True, "fajlov": len(есть), "ne_doehali": [], "ne_te": [],
            "lishnije": []}
    for имя, сумма in (zhdjom or {}).items():
        if имя not in есть:
            из_["ne_doehali"].append(имя)
        elif есть[имя] != сумма:
            из_["ne_te"].append(имя)
    из_["lishnije"] = [и for и in есть if и not in (zhdjom or {})]
    из_["ok"] = not (из_["ne_doehali"] or из_["ne_te"])
    return из_


def otkuda_modul(imja: str, kat: str | Path) -> dict:
    """Откуда возьмётся `import imja` -- ДО того, как его возьмут.

    Ищется по тому же sys.path, что и у настоящего импорта, но БЕЗ исполнения
    модуля: исполнять чужой код ради проверки пути -- это и есть запуск, от
    которого мы защищаемся.
    """
    import importlib.util  # noqa: PLC0415

    из_ = {"imja": imja, "fajl": None, "nash": False, "why_not": None}
    try:
        спец = importlib.util.find_spec(imja)
    except (ImportError, ValueError) as сбой:
        из_["why_not"] = f"{type(сбой).__name__}: {сбой}"
        return из_
    if спец is None or not спец.origin:
        из_["why_not"] = "модуль не найден"
        return из_
    из_["fajl"] = спец.origin
    try:
        из_["nash"] = Path(спец.origin).resolve().is_relative_to(
            Path(kat).resolve())
    except (OSError, ValueError):
        из_["nash"] = False
    if not из_["nash"]:
        из_["why_not"] = f"{WHY_ЧУЖОЙ_МОДУЛЬ}: {спец.origin}"
    return из_


def proverit(*, kat: str, moduli: list, zhdjom_summy: dict | None = None,
             mesta_ne_menshe_mb: int = МЕСТА_НЕ_МЕНЬШЕ_МБ,
             piton_ne_nizhe: tuple = ПИТОН_НЕ_НИЖЕ) -> dict:
    """ВСЁ ВМЕСТЕ: один ответ, один код выхода, все причины поимённо."""
    из_ = {"ok": True, "prichiny": [], "kat": str(kat),
            "piton": f"{sys.version_info[0]}.{sys.version_info[1]}",
            "moduli": [], "summy": None, "mesta_mb": None}
    if sys.version_info[:2] < tuple(piton_ne_nizhe):
        из_["prichiny"].append(f"{WHY_ПИТОН}: {из_['piton']}")
    к = Path(kat)
    if not к.is_dir():
        из_["prichiny"].append(f"{WHY_НЕТ_ФАЙЛА}: каталога {kat} нет")
    else:
        свободно = shutil.disk_usage(str(к)).free // (1024 * 1024)
        из_["mesta_mb"] = свободно
        if свободно < int(mesta_ne_menshe_mb):
            из_["prichiny"].append(f"{WHY_МЕСТО}: {свободно} МБ")
        if zhdjom_summy:
            с = sverit_summy(к, zhdjom_summy)
            из_["summy"] = с
            if not с["ok"]:
                из_["prichiny"].append(
                    f"{WHY_СУММА}: не доехали {с['ne_doehali']}, "
                    f"не те {с['ne_te']}")
    for имя in (moduli or []):
        м = otkuda_modul(имя, к)
        из_["moduli"].append(м)
        if not м["nash"]:
            из_["prichiny"].append(f"{имя}: {м['why_not']}")
    из_["ok"] = not из_["prichiny"]
    return из_


# ------------------------------------------------------------- самопроверка

ZHDEM_PROVEROK = 12


def self_test() -> int:  # noqa: C901, PLR0915
    import tempfile  # noqa: PLC0415

    было, плохо = 0, 0
    упавшие: list = []

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            упавшие.append(имя)
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("razovyj_predpuskovoj: самопроверка")
    врем = Path(tempfile.mkdtemp(prefix="razovyj-"))
    try:
        (врем / "a.py").write_text("ПЕРВЫЙ = 1\n", encoding="utf-8")
        (врем / "b.py").write_text("ВТОРОЙ = 2\n", encoding="utf-8")
        суммы = summy_fajlov(врем)
        chk("сумма считается ПО КАЖДОМУ файлу, а не одним числом на каталог: "
            "одно число не говорит, КАКОЙ файл не доехал",
            set(суммы) == {"a.py", "b.py"} and len(set(суммы.values())) == 2,
            суммы)
        chk("сверка сходится, когда доехало всё",
            sverit_summy(врем, суммы)["ok"], None)
        # ДОКАЗАННЫЙ КРАСНЫЙ: файл не доехал.
        (врем / "b.py").unlink()
        с = sverit_summy(врем, суммы)
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: не доехавший файл НАЗВАН -- ровно это было "
            "05.10, когда ssh cat в цикле довёз не все",
            not с["ok"] and с["ne_doehali"] == ["b.py"], с)
        # ДОКАЗАННЫЙ КРАСНЫЙ: файл доехал ПУСТЫМ (сумма другая).
        (врем / "b.py").write_text("", encoding="utf-8")
        с2 = sverit_summy(врем, суммы)
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: файл доехал ПУСТЫМ -- сумма не та, и это "
            "названо отдельно от 'не доехал'",
            not с2["ok"] and с2["ne_te"] == ["b.py"] and not с2["ne_doehali"],
            с2)
        (врем / "b.py").write_text("ВТОРОЙ = 2\n", encoding="utf-8")
        (врем / "lishnij.py").write_text("x = 1\n", encoding="utf-8")
        chk("лишний файл в рабочем каталоге виден (чужой прогон мог оставить "
            "свой), но отказом сам по себе не считается",
            sverit_summy(врем, суммы)["lishnije"] and
            sverit_summy(врем, суммы)["ok"], None)
        (врем / "lishnij.py").unlink()
        # ---------------------------- ОТКУДА МОДУЛЬ
        было_путь = list(sys.path)
        чужой = Path(tempfile.mkdtemp(prefix="chuzhoj-"))
        (чужой / "modul_opyta.py").write_text("ОТКУДА = 'chuzhoj'\n",
                                              encoding="utf-8")
        (врем / "modul_opyta.py").write_text("ОТКУДА = 'nash'\n",
                                             encoding="utf-8")
        try:
            # ТАК И БЫЛО 04.10: чужой каталог стоит В НАЧАЛЕ пути и перекрывает.
            sys.path.insert(0, str(чужой))
            sys.path.insert(1, str(врем))
            import importlib  # noqa: PLC0415
            importlib.invalidate_caches()
            м = otkuda_modul("modul_opyta", врем)
            chk("ДОКАЗАННЫЙ КРАСНЫЙ: модуль берётся ИЗ ЧУЖОГО каталога -- "
                "отказ по имени и с путём; раньше прогон шёл зелёным по "
                "чужому коду",
                not м["nash"] and WHY_ЧУЖОЙ_МОДУЛЬ in (м["why_not"] or "")
                and str(чужой) in (м["fajl"] or ""), м)
            sys.path.remove(str(чужой))
            importlib.invalidate_caches()
            м2 = otkuda_modul("modul_opyta", врем)
            chk("а когда чужого каталога в пути нет -- модуль наш, и путь "
                "назван",
                м2["nash"] and str(врем) in (м2["fajl"] or ""), м2)
            chk("проверка НЕ ИСПОЛНЯЕТ модуль: исполнить чужой код ради "
                "проверки пути -- это и есть тот запуск, от которого защита",
                "modul_opyta" not in sys.modules, None)
            # ---------------------------- ВСЁ ВМЕСТЕ
            в = proverit(kat=str(врем), moduli=["modul_opyta"],
                         zhdjom_summy=summy_fajlov(врем))
            chk("общий ответ зелёный, когда зелено всё: суммы, модуль, питон, "
                "место", в["ok"] and not в["prichiny"], в["prichiny"])
            в2 = proverit(kat=str(врем), moduli=["modul_opyta"],
                          zhdjom_summy={"net-takogo.py": "0" * 64})
            chk("ДОКАЗАННЫЙ КРАСНЫЙ: одна не сошедшаяся сумма роняет общий "
                "ответ, и причина названа",
                not в2["ok"] and any(WHY_СУММА in п for п in в2["prichiny"]),
                в2["prichiny"])
            в3 = proverit(kat=str(врем), moduli=[], zhdjom_summy=None,
                          mesta_ne_menshe_mb=10 ** 9)
            chk("ДОКАЗАННЫЙ КРАСНЫЙ: мало места -- отказ числом, а не падение "
                "на середине работы",
                not в3["ok"] and any(WHY_МЕСТО in п for п in в3["prichiny"]),
                в3["prichiny"])
            в4 = proverit(kat=str(врем / "net-takogo"), moduli=[])
            chk("каталога нет -- отказ по имени",
                not в4["ok"] and any(WHY_НЕТ_ФАЙЛА in п for п in в4["prichiny"]),
                в4["prichiny"])
        finally:
            sys.path[:] = было_путь
            shutil.rmtree(чужой, ignore_errors=True)
    finally:
        shutil.rmtree(врем, ignore_errors=True)

    if упавшие:
        print("УПАЛИ: " + "; ".join(упавшие))
    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        return 1
    return 1 if плохо else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--kat", default=None, help="рабочий каталог прогона")
    p.add_argument("--moduli", default="",
                   help="модули через запятую -- каждый обязан быть НАШИМ")
    p.add_argument("--summy", default=None,
                   help="файл с объявленными суммами (json: имя -> sha256)")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if not a.kat:
        p.print_help()
        return 0
    ждём = None
    if a.summy and Path(a.summy).exists():
        ждём = json.loads(Path(a.summy).read_text(encoding="utf-8"))
    из_ = proverit(kat=a.kat,
                   moduli=[м for м in a.moduli.split(",") if м.strip()],
                   zhdjom_summy=ждём)
    print(json.dumps(из_, ensure_ascii=False, indent=1))
    return 0 if из_["ok"] else 3


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    raise SystemExit(main())
