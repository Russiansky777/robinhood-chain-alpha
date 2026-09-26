#!/usr/bin/env python3
"""Ночная конфигурация групп источников: РОВНО те поля, что назвал владелец.

СЛОВО ВЛАДЕЛЬЦА 26.09 (пункт 3): "Ночная конфигурация с 19:00Z: полоса 0.01 на
всех группах; Bloom -- только speed_only 0.01, на bloom_lane и lane_only
выключен. Стопы по настоящему счёту: полоса -0.3, Bloom -0.2."

ЧТО ДЕЛАЕТ. Правит файл групп: размер полосы, признак торговли Bloom и стоп
полосы по каждой группе. Ничего другого не трогает: список адресов, пороги
входа, потолки оборота и веер остаются как есть. Рядом остаётся копия прежнего
файла, после записи всё перечитывается и печатается.

Стоп Bloom (-0.2) живёт не здесь, а в окружении службы (BLOOM_DAILY_LOSS_SOL):
это общий предел площадки, а не свойство группы. Его пишет прогон.

Только этот файл. Ни сделок, ни ключей.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# ЧТО СТАВИМ. Размер полосы, торгует ли Bloom, стоп полосы -- по группам.
НОЧЬ = {
    "bloom_lane": {"lane_sol": 0.01, "bloom_trades": False, "stop_loss_sol": 0.3},
    "lane_only": {"lane_sol": 0.01, "bloom_trades": False, "stop_loss_sol": 0.3},
    "speed_only": {"lane_sol": 0.01, "bloom_trades": True, "stop_loss_sol": 0.3},
    "candidates": {"lane_sol": 0.01, "bloom_trades": False, "stop_loss_sol": 0.2},
}
# У кандидатов стоп свой (-0.2) по слову владельца 26.09 при заведении группы.


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--file", required=True)
    р.add_argument("--apply", action="store_true",
                   help="без него ничего не пишется -- только показ")
    а = р.parse_args()
    путь = Path(а.file)
    д = json.loads(путь.read_text(encoding="utf-8"))
    группы = д.get("groups")
    if not isinstance(группы, dict):
        print("СТОП: в файле нет словаря groups", file=sys.stderr)
        return 2
    было, станет = {}, {}
    for имя, поля in НОЧЬ.items():
        if имя not in группы:
            print(f"СТОП: группы {имя} в файле нет; есть {sorted(группы)}", file=sys.stderr)
            return 3
        было[имя] = {к: группы[имя].get(к) for к in поля}
        станет[имя] = dict(поля)
    print(json.dumps({"файл": str(путь), "было": было, "станет": станет},
                     ensure_ascii=False, indent=1))
    if not а.apply:
        print("режим показа: файл не изменён")
        return 0
    копия = путь.with_suffix(путь.suffix + f".bak-noch-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    shutil.copy2(путь, копия)
    for имя, поля in НОЧЬ.items():
        группы[имя].update(поля)
    путь.write_text(json.dumps(д, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    свежее = json.loads(путь.read_text(encoding="utf-8")).get("groups") or {}
    после = {имя: {к: свежее.get(имя, {}).get(к) for к in поля}
             for имя, поля in НОЧЬ.items()}
    print(json.dumps({"копия": str(копия), "после": после}, ensure_ascii=False, indent=1))
    плохо = [имя for имя, поля in НОЧЬ.items()
             if any(свежее.get(имя, {}).get(к) != v for к, v in поля.items())]
    if плохо:
        print(f"СТОП: после записи не совпало у групп {плохо}", file=sys.stderr)
        return 5
    print("ОК: все поля легли как задумано")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
