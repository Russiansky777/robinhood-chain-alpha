#!/usr/bin/env python3
"""Показать или изменить ОДНО числовое поле политики группы источников.

ЗАЧЕМ. Владелец 26.09 (пункт 9): "Bloom на bloom_lane 0.2 -> 0.05 при снятии
KILL". Размер покупки площадки для группы живёт в файле групп источников
(groups.<группа>.bloom_sol); отсутствующее значение означает "брать общий
buy_sol", а он 0.2. Менять общий buy_sol нельзя -- он для всех групп сразу.

ИМЯ КЛЮЧА. В ФАЙЛЕ группы лежат под ключом "groups" (см.
analysis/bloom_source_groups.py: загрузить() читает д["groups"]); "policies" --
это имя уже РАЗОБРАННОЙ политики в признаке жизни детектора, в файле такого
ключа нет. Первый прогон 26.09 упал именно на этой путанице.

ЧТО ДЕЛАЕТ. Печатает текущее значение; с --set меняет РОВНО одно поле у РОВНО
одной группы, не трогая ничего другого, и кладёт рядом копию прежнего файла.
Пишет в том же виде (indent=2), в каком файл собирает
analysis/sources_groups_build.py. Никаких других правок, никаких сделок, ключей не читает.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def без_адресов(г: dict) -> dict:
    """Политика без списков адресов: в докладе они только шум (38 строк)."""
    return {к: v for к, v in (г or {}).items()
            if not isinstance(v, (list, dict))}


def _целое(текст: str) -> bool:
    """Записано ли значение целым числом ("3", "-1"), а не дробным ("0.05")."""
    т = (текст or "").strip()
    return bool(т) and (т[1:] if т[0] in "+-" else т).isdigit()


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--file", required=True)
    р.add_argument("--group", required=True)
    р.add_argument("--field", required=True)
    р.add_argument("--set", default="", help="новое значение (пусто -- только показать)")
    а = р.parse_args()

    путь = Path(а.file)
    д = json.loads(путь.read_text(encoding="utf-8"))
    политики = д.get("groups")
    if not isinstance(политики, dict):
        print("СТОП: в файле нет словаря groups", file=sys.stderr)
        return 2
    if а.group not in политики:
        print(f"СТОП: группы {а.group} нет; есть {sorted(политики)}", file=sys.stderr)
        return 3
    было = политики[а.group].get(а.field, None)
    print(json.dumps({"файл": str(путь), "группа": а.group, "поле": а.field,
                      "было": было,
                      "вся_политика_группы": без_адресов(политики[а.group])},
                     ensure_ascii=False, indent=1))
    if not а.set:
        print("режим показа: файл не изменён")
        return 0
    # ЦЕЛОЕ ПИШЕТСЯ ЦЕЛЫМ. Поле lane_open_max -- счёт позиций, и "3.0" в файле
    # счётом не выглядит; читателю (bloom_own_send.предел_открытых) всё равно,
    # но человек, открывший файл, обязан видеть 3, а не 3.0. Дробные поля
    # (bloom_sol 0.05) при этом остаются дробными.
    # ДА/НЕТ -- ТОЖЕ ЗНАЧЕНИЕ. Поля lane_trades, bloom_trades, subscribe, fanout
    # логические, и до 28.09 этот прогон их поставить НЕ МОГ вовсе: любое
    # нечисло падало кодом 4 "поле числовое". Из-за этого попытка выключить
    # торговлю группе sniper_src в 23:46:49Z прошла впустую -- прогон прочитал
    # политику, упал и файл не тронул, а по отчёту это читалось как правка.
    ЛОЖЬ = ("false", "нет", "no", "0", "off")
    ИСТИНА = ("true", "да", "yes", "1", "on")
    слово = а.set.strip().lower()
    логическое = слово in ЛОЖЬ or слово in ИСТИНА
    # "1" и "0" -- это числа, и числовым полям они нужны числами. Логическим
    # значение пишется логическим: решает ТИП ТОГО, ЧТО В ФАЙЛЕ СЕЙЧАС.
    if логическое and isinstance(было, bool):
        новое = слово in ИСТИНА
    elif слово == "null" or слово == "none":
        новое = None
    else:
        try:
            новое = int(а.set) if _целое(а.set) else float(а.set)
        except ValueError:
            if логическое:
                print(f"СТОП: поле {а.field} в файле не логическое (сейчас "
                       f"{было!r}), а {а.set!r} -- да/нет", file=sys.stderr)
            else:
                print(f"СТОП: {а.set!r} не число и не да/нет", file=sys.stderr)
            return 4
    копия = путь.with_suffix(путь.suffix + f".bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    shutil.copy2(путь, копия)
    политики[а.group][а.field] = новое
    # indent=2 и перевод строки в конце -- ровно так файл пишет
    # analysis/sources_groups_build.py: иначе правка одного числа переформатирует
    # весь файл и разойдётся с копией в репозитории.
    путь.write_text(json.dumps(д, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    свежее = json.loads(путь.read_text(encoding="utf-8"))
    пол2 = свежее.get("groups") or {}
    print(json.dumps({"копия": str(копия), "стало": пол2.get(а.group, {}).get(а.field),
                      "вся_политика_группы_после": без_адресов(пол2.get(а.group))},
                     ensure_ascii=False, indent=1))
    return 0 if полит_ок(пол2, а.group, а.field, новое) else 5


def полит_ок(политики, группа, поле, ожидали) -> bool:
    есть = (политики.get(группа) or {}).get(поле)
    if есть != ожидали:
        print(f"СТОП: после записи в файле {есть!r}, а ожидали {ожидали!r}", file=sys.stderr)
        return False
    return True


def self_test() -> int:
    """Тип значения не выдумывается: логическое пишется логическим, число числом."""
    import tempfile  # noqa: PLC0415

    пройдено = провалено = 0

    def chk(что, ок, факт=None):
        nonlocal пройдено, провалено
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {что}"
              + (f" -> {факт!r}" if факт is not None and not ок else ""))
        пройдено += bool(ок)
        провалено += (not ок)

    образец = {"groups": {"g": {"lane_trades": True, "lane_open_max": 3,
                                 "bloom_sol": 0.05, "note": "строка"}}}
    with tempfile.TemporaryDirectory() as вр:
        ф = Path(вр) / "groups.json"

        def прогон(поле, значение):
            ф.write_text(json.dumps(образец, ensure_ascii=False), encoding="utf-8")
            сохр = sys.argv
            sys.argv = ["x", "--file", str(ф), "--group", "g",
                         "--field", поле, "--set", значение]
            try:
                код = main()
            finally:
                sys.argv = сохр
            свежее = json.loads(ф.read_text(encoding="utf-8"))
            return код, свежее["groups"]["g"].get(поле)

        код, стало = прогон("lane_trades", "false")
        chk("lane_trades=false пишется ЛОЖЬЮ, а не нулём",
            код == 0 and стало is False, (код, стало))
        код, стало = прогон("lane_trades", "true")
        chk("lane_trades=true пишется ИСТИНОЙ", код == 0 and стало is True, (код, стало))
        код, стало = прогон("lane_open_max", "2")
        chk("число остаётся числом, а не логическим",
            код == 0 and стало == 2 and not isinstance(стало, bool), (код, стало))
        код, стало = прогон("bloom_sol", "0.07")
        chk("дробное остаётся дробным", код == 0 and стало == 0.07, (код, стало))
        код, стало = прогон("lane_open_max", "да")
        chk("«да» числовому полю -- отказ, файл не тронут",
            код == 4 and стало == 3, (код, стало))
        код, стало = прогон("note", "мусор")
        chk("строковому полю отказ (тип не выдумывается)",
            код == 4 and стало == "строка", (код, стало))

    print(f"самопроверка политики группы: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    raise SystemExit(main())
