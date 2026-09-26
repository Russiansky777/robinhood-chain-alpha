#!/usr/bin/env python3
"""Подбивка: план прогона из маркера -- матрица пакетов для облачного бегунка.

Маркер (data/podbivka/zapusk/zadacha_*.json), пример:
  {"skript": "podbivka_run.py", "zadacha": "koshelki", "spisok": "wallets_csv",
   "s": 10, "po": 0, "paket": 50, "do_utc": "2026-09-26T15:00:00Z",
   "predel_na_porog": 20, "predel_podpisey": 20000, "sverka": 0}
  {"skript": "podbivka_1b2.py"}
Выход: matrix={"include": [{"skript": ..., "args": ...}, ...]} в GITHUB_OUTPUT.
Только ASCII в ключах (правило репозитория).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent


def длина_списка(имя: str) -> int:
    """Без тяжёлых импортов: план идёт системным питоном, без solders."""
    if имя == "wallets_csv":
        with open(КОРЕНЬ / "data" / "podbivka" / "wallets.csv", encoding="utf-8") as ф:
            return sum(1 for r in csv.DictReader(ф) if r.get("address"))
    if имя == "nashi":
        return len(json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki.json")
                              .read_text(encoding="utf-8"))["источники"])
    raise SystemExit(f"неизвестный список {имя}")


def пакеты(м: dict) -> list:
    if м.get("skript", "podbivka_run.py") != "podbivka_run.py":
        return [{"skript": м["skript"], "args": " ".join(м.get("args") or []), "name": м["skript"],
                 "helius_rps": "10", "_rps": м.get("helius_rps_na_paket")}]
    всего = длина_списка(м["spisok"])
    с, по = int(м.get("s") or 0), int(м.get("po") or 0) or всего
    шаг = int(м.get("paket") or 50)
    из_ = []
    for н in range(с, по, шаг):
        до = min(по, н + шаг)
        арг = ["--zadacha", м["zadacha"], "--spisok", м["spisok"], "--s", str(н), "--po", str(до),
               "--do-utc", м["do_utc"], "--dney", str(м.get("dney", 7)),
               "--predel-na-porog", str(м.get("predel_na_porog", 0)),
               "--predel-podpisey", str(м.get("predel_podpisey", 0)),
               "--sverka", str(м.get("sverka", 0) if н == с else 0), "--push"]
        if м.get("fakt"):
            арг.append("--fakt")
        if м.get("tolko_shyft"):
            арг.append("--tolko-shyft")
        из_.append({"skript": "podbivka_run.py", "args": " ".join(арг), "name": f"{м['zadacha']}_{н}_{до}"})
    # Предел Helius 10 запросов/с -- на все одновременные пакеты (max-parallel 12).
    доля = round(10.0 / min(12, max(1, len(из_))), 3)
    for x in из_:
        x["helius_rps"] = str(доля)
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("markery", nargs="*")
    а = р.parse_args()
    вкл = []
    for путь in а.markery:
        if not путь.endswith(".json") or not Path(путь).exists():
            continue
        м = json.loads(Path(путь).read_text(encoding="utf-8"))
        пк = пакеты(м)
        for x in пк:
            x.setdefault("_rps", м.get("helius_rps_na_paket"))
        вкл.extend(пк)
    # Предел Helius 10 запросов/с -- на ВСЕ одновременные задания прогона.
    for x in вкл:
        общий = round(10.0 / min(12, max(1, len(вкл))), 3)
        x["helius_rps"] = str(min(общий, float(x.pop("_rps") or общий)))
    строка = "matrix=" + json.dumps({"include": вкл}, ensure_ascii=True)
    print(строка)
    print(f"count={len(вкл)}")
    вых = os.environ.get("GITHUB_OUTPUT")
    if вых:
        with open(вых, "a", encoding="utf-8") as ф:
            ф.write(строка + "\n")
            ф.write(f"count={len(вкл)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
