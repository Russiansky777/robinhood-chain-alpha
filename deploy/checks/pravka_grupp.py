#!/usr/bin/env python3
"""Правка файла групп источников: поля политики, создание группы, переносы.

ЗАЧЕМ. politika_gruppy.py умеет ровно одно: одно ЧИСЛОВОЕ поле у существующей
группы. Пункт 3 владельца 27.09 требует большего и всё это -- деньги:
включить Bloom по группе (признак, не число), дать группе свой порог входа
источника, поднять предел открытых, поставить стоп группы, перенести адрес из
одной группы в другую, убрать адрес из торгующих групп вовсе и перечислить типы
пулов полосы (список, а не число).

ПОЧЕМУ ФАЙЛОМ, А НЕ ОКРУЖЕНИЕМ. Файл групп детектор перечитывает на ходу, а
окружение -- только при перезапуске. Слово владельца 27.09: включение по одному,
без перезапусков.

САМОПРОВЕРКА ДЕНЕЖНОГО ПУТИ. После записи файл перечитывается ТЕМ ЖЕ модулем,
которым его читает служба (bloom_source_groups), и проверяется: каждый
затронутый адрес отдаёт ожидаемую группу, а группа -- ожидаемые значения полей.
Не сошлось -- файл возвращается из копии, код выхода не ноль.

Ключей не читает, сделок не делает, служб не касается.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

КОНТЕЙНЕРЫ = ("addresses", "by_signal", "snipers", "dropped_by_credits")
ПРИЗНАКИ = ("bloom_trades", "lane_trades", "fanout")
СПИСКИ = ("lane_pools",)


def модуль_групп(файл: str):
    """bloom_source_groups -- тот же, что у службы; порядок каталогов важен."""
    for кат in ("/home/bot/bloom_executor",
                "/home/bot/robinhood-chain-alpha/analysis",
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))), "analysis")):
        if not os.path.exists(os.path.join(кат, "bloom_source_groups.py")):
            continue
        if кат not in sys.path:
            sys.path.insert(0, кат)
        try:
            import importlib  # noqa: PLC0415

            м = importlib.import_module("bloom_source_groups")
            os.environ["BLOOM_SOURCE_GROUPS"] = файл
            if hasattr(м, "_КЭШ"):
                м._КЭШ = None
            return м
        except Exception as exc:  # noqa: BLE001
            print(f"группы из {кат} не загружены: {type(exc).__name__}", file=sys.stderr)
    return None


def разобрать(значение: str, поле: str):
    """Значение поля из строки: признак, список, число или строка -- по полю."""
    з = (значение or "").strip()
    if поле in ПРИЗНАКИ:
        if з.lower() in ("1", "true", "да", "yes", "on"):
            return True
        if з.lower() in ("0", "false", "нет", "no", "off"):
            return False
        raise ValueError(f"{поле} -- признак, а {з!r} на признак не похоже")
    if поле in СПИСКИ:
        if not з or з == "[]":
            return []
        return [x.strip() for x in з.strip("[]").replace('"', "").split(",") if x.strip()]
    if з == "" or з.lower() in ("none", "null", "нет"):
        return None
    т = з[1:] if з[:1] in "+-" else з
    if т.isdigit():
        return int(з)
    try:
        return float(з)
    except ValueError:
        return з


def адреса_группы(тело: dict) -> list:
    из_ = []
    for к in КОНТЕЙНЕРЫ:
        v = тело.get(к)
        if isinstance(v, dict):
            из_ += list(v.keys())
        elif isinstance(v, list):
            из_ += [(x.get("address") if isinstance(x, dict) else x) for x in v]
    return [а for а in из_ if а]


def убрать_адрес(тело: dict, адрес: str) -> int:
    убрано = 0
    for к in КОНТЕЙНЕРЫ:
        v = тело.get(к)
        if isinstance(v, dict) and адрес in v:
            v.pop(адрес)
            убрано += 1
        elif isinstance(v, list):
            было = len(v)
            тело[к] = [x for x in v
                       if (x.get("address") if isinstance(x, dict) else x) != адрес]
            убрано += было - len(тело[к])
    return убрано


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--file", required=True)
    р.add_argument("--pokazat", action="store_true", help="только показать")
    р.add_argument("--gruppa", default="")
    р.add_argument("--pole", default="")
    р.add_argument("--znachenie", default="")
    р.add_argument("--sozdat", default="", help="имя новой группы")
    р.add_argument("--politika", default="", help="JSON политики новой группы")
    р.add_argument("--perenesti", default="", help="адрес")
    р.add_argument("--v", default="", help="в какую группу перенести")
    р.add_argument("--pometka", default="", help="пометка адреса при переносе")
    р.add_argument("--otklyuchit", default="",
                   help="адрес -- перевести в НЕТОРГУЮЩУЮ группу (имя в --v)")
    а = р.parse_args()

    путь = Path(а.file)
    д = json.loads(путь.read_text(encoding="utf-8"))
    гр = д.get("groups")
    if not isinstance(гр, dict):
        print("СТОП: в файле нет словаря groups", file=sys.stderr)
        return 2

    if а.pokazat or not (а.gruppa or а.sozdat or а.perenesti or а.otklyuchit):
        свод = {}
        for имя, тело in гр.items():
            свод[имя] = {"адресов": len(адреса_группы(тело)),
                          **{к: v for к, v in тело.items()
                             if not isinstance(v, (list, dict))}}
        print(json.dumps(свод, ensure_ascii=False, indent=1))
        return 0

    копия = путь.with_suffix(путь.suffix + f".bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    shutil.copy2(путь, копия)
    ждём_группу, ждём_поля = {}, {}

    if а.sozdat:
        if а.sozdat in гр:
            print(f"СТОП: группа {а.sozdat} уже есть", file=sys.stderr)
            return 3
        try:
            политика = json.loads(а.politika) if а.politika else {}
        except ValueError as exc:
            print(f"СТОП: политика не JSON ({exc})", file=sys.stderr)
            return 4
        политика.setdefault("addresses", {})
        гр[а.sozdat] = политика
        ждём_поля[а.sozdat] = {к: v for к, v in политика.items()
                                if not isinstance(v, (list, dict))}
        print(f"создана группа {а.sozdat}: "
              f"{json.dumps(ждём_поля[а.sozdat], ensure_ascii=False)}")

    if а.gruppa and а.pole:
        if а.gruppa not in гр:
            print(f"СТОП: группы {а.gruppa} нет; есть {sorted(гр)}", file=sys.stderr)
            return 5
        было = гр[а.gruppa].get(а.pole)
        стало = разобрать(а.znachenie, а.pole)
        гр[а.gruppa][а.pole] = стало
        ждём_поля.setdefault(а.gruppa, {})[а.pole] = стало
        print(f"{а.gruppa}.{а.pole}: было {было!r}, стало {стало!r}")

    if а.perenesti:
        if not а.v or а.v not in гр:
            print(f"СТОП: куда переносить -- группы {а.v!r} нет", file=sys.stderr)
            return 6
        откуда = [имя for имя, тело in гр.items() if а.perenesti in адреса_группы(тело)]
        for имя in откуда:
            n = убрать_адрес(гр[имя], а.perenesti)
            print(f"убран из {имя} ({n} мест)")
        цель = гр[а.v]
        if not isinstance(цель.get("addresses"), dict):
            цель["addresses"] = {}
        цель["addresses"][а.perenesti] = а.pometka or "перенос по слову владельца"
        ждём_группу[а.perenesti] = а.v
        print(f"{а.perenesti[:8]}: {откуда or ['(нигде)']} -> {а.v}")

    if а.otklyuchit:
        # ПРОСТО УБРАТЬ ИЗ ФАЙЛА НЕЛЬЗЯ. Адрес, которого в файле нет, модуль
        # относит к ГРУППЕ ПО УМОЛЧАНИЮ (bloom_lane), а она торгует и полосой,
        # и площадкой -- то есть удаление не выключает источник, а даёт ему
        # САМЫЕ БОЛЬШИЕ деньги. Поэтому выключение -- это перевод в группу, у
        # которой оба признака торговли сняты, и это проверяется здесь же.
        if not а.v or а.v not in гр:
            print(f"СТОП: куда выключать -- группы {а.v!r} нет", file=sys.stderr)
            return 8
        цел = гр[а.v]
        if цел.get("lane_trades") is not False or цел.get("bloom_trades") is not False:
            print(f"СТОП: группа {а.v} торгует "
                  f"(lane_trades={цел.get('lane_trades')!r}, "
                  f"bloom_trades={цел.get('bloom_trades')!r}) -- "
                  "выключать в неё нельзя", file=sys.stderr)
            return 9
        откуда = [имя for имя, тело in гр.items()
                  if а.otklyuchit in адреса_группы(тело)]
        for имя in откуда:
            n = убрать_адрес(гр[имя], а.otklyuchit)
            print(f"убран из {имя} ({n} мест)")
        if not isinstance(цел.get("addresses"), dict):
            цел["addresses"] = {}
        цел["addresses"][а.otklyuchit] = а.pometka or "выключен по слову владельца"
        ждём_группу[а.otklyuchit] = а.v
        ждём_поля.setdefault(а.v, {}).update(
            {"lane_trades": False, "bloom_trades": False})
        print(f"{а.otklyuchit[:8]}: {откуда or ['(нигде)']} -> {а.v} (не торгует)")

    путь.write_text(json.dumps(д, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # --- САМОПРОВЕРКА ДЕНЕЖНОГО ПУТИ ---
    SG = модуль_групп(str(путь))
    плохо = []
    if SG is None:
        плохо.append("модуль групп не загружен -- проверить нечем")
    else:
        for адрес, ожидали in ждём_группу.items():
            есть = SG.группа(адрес)
            если_по_умолчанию = getattr(SG, "ГРУППА_ПО_УМОЛЧАНИЮ", None)
            надо = ожидали if ожидали is not None else если_по_умолчанию
            if есть != надо:
                плохо.append(f"адрес {адрес[:8]}: группа {есть!r}, ожидали {надо!r}")
        # ПРОВЕРЯЕМ ТОЛЬКО ТО, ЧТО МОДУЛЬ ЧИТАЕТ. У политики есть свой набор
        # полей (ПОЛИТИКА_ПО_УМОЛЧАНИЮ); пояснения вроде note в него не входят
        # и на деньги не влияют -- требовать их обратно значило бы падать на
        # ровном месте. Поля вне набора называются словами и не сверяются.
        известные = set(getattr(SG, "ПОЛИТИКА_ПО_УМОЛЧАНИЮ", {}) or {})
        for имя, поля in ждём_поля.items():
            пол = SG.политика(имя)
            for к, v in поля.items():
                if к not in известные:
                    print(f"поле {имя}.{к} политика не читает -- не сверяю")
                    continue
                if пол.get(к) != v:
                    плохо.append(f"{имя}.{к}: читается {пол.get(к)!r}, ожидали {v!r}")
    if плохо:
        shutil.copy2(копия, путь)
        print("СТОП: самопроверка не прошла, файл возвращён из копии:", file=sys.stderr)
        for с in плохо:
            print("  " + с, file=sys.stderr)
        return 7
    print(f"самопроверка денежного пути пройдена; копия {копия}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
