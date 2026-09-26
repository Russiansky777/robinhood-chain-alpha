#!/usr/bin/env python3
"""Ночная конфигурация групп источников: РОВНО те поля, что назвал владелец.

СЛОВО ВЛАДЕЛЬЦА 26.09, ВЕЧЕР (заменяет прежнюю ночную): "Bloom выключен на всех
группах; speed_only выключен; полоса на bloom_lane и lane_only по 0.01, стоп
полосы -0.15". Прежняя ночная (полоса 0.01 везде, Bloom на speed_only, стоп
-0.3) отменена после дневного убытка -0.572 SOL и остановки в 17:15Z.

ВЫКЛЮЧЕНИЕ ГРУППЫ -- ПРИЗНАКОМ lane_trades, А НЕ НУЛЯМИ. Ноль размера и ноль
потолка группу не выключают: в bloom_own_send.размер_sol стоит "if если", а в
потолок_группы -- "or потолок", и ноль там ложен и уходит в значение по
умолчанию. Признак читает полоса_торгует() на денежном пути.

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

# ЧТО СТАВИМ. Размер полосы, торгует ли полоса, торгует ли Bloom, стоп полосы.
НОЧЬ = {
    "bloom_lane": {"lane_sol": 0.01, "lane_trades": True,
                   "bloom_trades": False, "stop_loss_sol": 0.15},
    "lane_only": {"lane_sol": 0.01, "lane_trades": True,
                  "bloom_trades": False, "stop_loss_sol": 0.15},
    # speed_only ВЫКЛЮЧЕНА ЦЕЛИКОМ: за сутки она дала -0.226 по учёту при 141
    # сделке и 102 несчитаемых, и именно она жгла расход (0.455 SOL чаевых).
    "speed_only": {"lane_sol": 0.01, "lane_trades": False,
                   "bloom_trades": False, "stop_loss_sol": 0.15},
    # candidates ночью не торгует: её включение -- утро 27.09 по списку второй
    # сессии, и адресов в ней пока нет вовсе.
    "candidates": {"lane_sol": 0.01, "lane_trades": False,
                   "bloom_trades": False, "stop_loss_sol": 0.15},
}


def самопроверка() -> int:
    """Денежный путь: размеры, признаки торговли и стопы ложатся как задумано."""
    import tempfile
    пройдено = провалено = 0

    def ок(условие, что):
        nonlocal пройдено, провалено
        if условие:
            пройдено += 1
        else:
            провалено += 1
            print(f"ПРОВАЛ: {что}")

    с_адресами = {"addresses": {"АДРЕС1": 1}, "min_target_sol": 2.0,
                  "day_cap_sol": 3.0, "fanout": False}
    с_днём = {имя: dict(с_адресами, lane_sol=0.05, bloom_trades=True,
                        stop_loss_sol=0.5) for имя in НОЧЬ}
    with tempfile.TemporaryDirectory() as кат:
        путь = Path(кат) / "groups.json"
        путь.write_text(json.dumps({"groups": с_днём}, ensure_ascii=False),
                        encoding="utf-8")
        ок(применить(путь, писать=False) == 0, "показ не пишет и не падает")
        было = json.loads(путь.read_text(encoding="utf-8"))["groups"]
        ок(все_равны(было, "lane_sol", 0.05), "в режиме показа файл не изменён")
        ок(применить(путь, писать=True) == 0, "запись прошла")
        стало = json.loads(путь.read_text(encoding="utf-8"))["groups"]
        ок(все_равны(стало, "bloom_trades", False), "Bloom выключен на всех группах")
        ок(все_равны(стало, "stop_loss_sol", 0.15), "стоп полосы -0.15 везде")
        ок(стало["speed_only"]["lane_trades"] is False
           and стало["candidates"]["lane_trades"] is False,
           "speed_only и candidates для полосы выключены")
        ок(стало["bloom_lane"]["lane_trades"] is True
           and стало["lane_only"]["lane_trades"] is True,
           "bloom_lane и lane_only торгуют полосой")
        ок(все_равны(стало, "lane_sol", 0.01), "размер полосы 0.01")
        # ЧУЖИЕ ПОЛЯ НЕ ТРОГАЕМ: адреса, порог входа, потолок, веер.
        ок(стало["lane_only"]["addresses"] == {"АДРЕС1": 1}
           and стало["lane_only"]["min_target_sol"] == 2.0
           and стало["lane_only"]["day_cap_sol"] == 3.0
           and стало["lane_only"]["fanout"] is False,
           "адреса, порог, потолок и веер остались как были")
        ок(len(list(Path(кат).glob("groups.json.bak-noch-*"))) == 1,
           "копия прежнего файла рядом")
        # Группы нет в файле -- останов, а не тихая правка остальных.
        путь2 = Path(кат) / "gr2.json"
        путь2.write_text(json.dumps({"groups": {"bloom_lane": {}}},
                                     ensure_ascii=False), encoding="utf-8")
        ок(применить(путь2, писать=True) == 3, "нет группы -- останов")
    print(f"самопроверка ночной конфигурации: {пройдено}/{пройдено + провалено} пройдено")
    return 1 if провалено else 0


def все_равны(группы: dict, поле: str, значение) -> bool:
    return all(группы[имя].get(поле) == значение for имя in НОЧЬ)


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--file", required=False)
    р.add_argument("--apply", action="store_true",
                   help="без него ничего не пишется -- только показ")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    if not а.file:
        print("СТОП: нужен --file", file=sys.stderr)
        return 2
    return применить(Path(а.file), писать=bool(а.apply))


def применить(путь: Path, *, писать: bool) -> int:
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
    if not писать:
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
