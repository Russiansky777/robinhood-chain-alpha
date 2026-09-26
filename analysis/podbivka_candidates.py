#!/usr/bin/env python3
"""Подбивка: список для группы candidates -- офлайн по файлам кошельков прохода (а).

Правило владельца 26.09: верхние 30 по МЕДИАНЕ S+1/72 при n >= 5 (покупки с
числом симулятора), «источник продал в окне» < 30 %, котировка пула SOL >= 50 %
покупок. Порог 2 SOL-экв -- пост-фактум, как в таблицах (podbivka_tablicy).
Выход: data/podbivka/candidates_2026-09-27.csv (address,name) и .json с числами.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--katalog", default=str(КОРЕНЬ / "data" / "podbivka" / "koshelki"))
    р.add_argument("--top", type=int, default=30)
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "podbivka" / "candidates_2026-09-27.csv"))
    а = р.parse_args()
    имена = {r["address"]: r.get("name") or "" for r in csv.DictReader(open(КОРЕНЬ / "data" / "podbivka" / "wallets.csv", encoding="utf-8"))}
    ряды, кошельков = [], 0
    for ф in sorted(glob.glob(str(Path(а.katalog) / "*.json"))):
        if Path(ф).name.startswith("_"):
            continue
        к = json.loads(Path(ф).read_text(encoding="utf-8"))
        кошельков += 1
        адрес = (к.get("строка") or {}).get("address")
        пк = T.пересчёт_порога(к.get("покупки") or [], адрес)
        if not пк:
            continue
        v = [T.чп(п.get("sim"), "S1", 72) for п in пк]
        v = [x for x in v if x is not None]
        продал = [п.get("продал_в_окне") for п in пк if п.get("окно_продажи_полное", True)]
        sol = sum(1 for п in пк if п.get("котировка_пула") == "SOL")
        ряд = {"address": адрес, "name": имена.get(адрес, ""), "покупок": len(пк), "n": len(v),
               "медиана_S1_72": round(statistics.median(v), 2) if v else None,
               "среднее_S1_72": round(sum(v) / len(v), 2) if v else None,
               "продал_в_окне": round(sum(1 for x in продал if x) / len(продал), 3) if продал else None,
               "доля_SOL": round(sol / len(пк), 3)}
        ряд["проходит"] = bool(ряд["n"] >= 5 and ряд["продал_в_окне"] is not None and ряд["продал_в_окне"] < 0.30
                               and ряд["доля_SOL"] >= 0.50)
        ряды.append(ряд)
    # Слово владельца 26.09: только медиана S+1/72 >= +2 п.п.; если таких
    # меньше 10 -- добить медианой > 0 до 10 с пометкой; минусовые не пишутся.
    прошли = sorted([r for r in ряды if r["проходит"]], key=lambda r: -r["медиана_S1_72"])
    от2 = [r for r in прошли if r["медиана_S1_72"] >= 2.0]
    годные = от2[:а.top]
    if len(годные) < 10:
        добор = [dict(r, пометка="добор: медиана > 0, ниже +2") for r in прошли
                 if 0 < r["медиана_S1_72"] < 2.0][:10 - len(годные)]
        годные = годные + добор
    with open(а.out, "w", encoding="utf-8", newline="") as ф:
        w = csv.writer(ф)
        w.writerow(["address", "name"])
        for r in годные:
            имя = r["name"] + (" | " + r["пометка"] if r.get("пометка") else "")
            w.writerow([r["address"], имя])
    Path(а.out).with_suffix(".json").write_text(json.dumps(
        {"кошельков_в_файлах": кошельков, "с_покупками": len(ряды),
         "прошли_фильтр": len(прошли), "от_плюс_2": len(от2), "верхние": годные}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    мед = [r["медиана_S1_72"] for r in годные]
    print(f"candidates: кошельков {кошельков}, прошли фильтр {len(прошли)}, из них >= +2 п.п. {len(от2)}, "
          f"в списке {len(годные)} (добор {sum(1 for r in годные if r.get('пометка'))}), "
          f"медиана S+1/72 от {min(мед) if мед else None} до {max(мед) if мед else None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
