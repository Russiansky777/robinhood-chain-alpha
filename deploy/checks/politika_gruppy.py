#!/usr/bin/env python3
"""Показать или изменить ОДНО числовое поле политики группы источников.

ЗАЧЕМ. Владелец 26.09 (пункт 9): "Bloom на bloom_lane 0.2 -> 0.05 при снятии
KILL". Размер покупки площадки для группы живёт в файле групп источников
(policies.<группа>.bloom_sol); пустое значение означает "брать общий buy_sol",
а он 0.2. Менять общий buy_sol нельзя -- он относится ко всем группам.

ЧТО ДЕЛАЕТ. Печатает текущее значение; с --set меняет РОВНО одно поле у РОВНО
одной группы, оставив файл в остальном байт в байт, и кладёт рядом копию
прежнего файла. Никаких других правок, никаких сделок, ключей не читает.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--file", required=True)
    р.add_argument("--group", required=True)
    р.add_argument("--field", required=True)
    р.add_argument("--set", default="", help="новое значение (пусто -- только показать)")
    а = р.parse_args()

    путь = Path(а.file)
    д = json.loads(путь.read_text(encoding="utf-8"))
    политики = д.get("policies")
    if not isinstance(политики, dict):
        # У файла может быть другая обёртка: ищем словарь policies на верхнем
        # уровне или внутри одного ключа, но НЕ угадываем глубже.
        for к, v in д.items():
            if isinstance(v, dict) and isinstance(v.get("policies"), dict):
                политики = v["policies"]
                break
    if not isinstance(политики, dict):
        print("СТОП: в файле нет словаря policies", file=sys.stderr)
        return 2
    if а.group not in политики:
        print(f"СТОП: группы {а.group} нет; есть {sorted(политики)}", file=sys.stderr)
        return 3
    было = политики[а.group].get(а.field, None)
    print(json.dumps({"файл": str(путь), "группа": а.group, "поле": а.field,
                      "было": было, "вся_политика_группы": политики[а.group]},
                     ensure_ascii=False, indent=1))
    if not а.set:
        print("режим показа: файл не изменён")
        return 0
    try:
        новое = float(а.set)
    except ValueError:
        print(f"СТОП: {а.set!r} не число -- поле числовое", file=sys.stderr)
        return 4
    копия = путь.with_suffix(путь.suffix + f".bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    shutil.copy2(путь, копия)
    политики[а.group][а.field] = новое
    путь.write_text(json.dumps(д, ensure_ascii=False, indent=1), encoding="utf-8")
    свежее = json.loads(путь.read_text(encoding="utf-8"))
    пол2 = свежее.get("policies") or next((v["policies"] for v in свежее.values()
                                           if isinstance(v, dict) and isinstance(v.get("policies"), dict)), {})
    print(json.dumps({"копия": str(копия), "стало": пол2.get(а.group, {}).get(а.field),
                      "вся_политика_группы_после": пол2.get(а.group)},
                     ensure_ascii=False, indent=1))
    return 0 if полит_ок(пол2, а.group, а.field, новое) else 5


def полит_ок(политики, группа, поле, ожидали) -> bool:
    есть = (политики.get(группа) or {}).get(поле)
    if есть != ожидали:
        print(f"СТОП: после записи в файле {есть!r}, а ожидали {ожидали!r}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
