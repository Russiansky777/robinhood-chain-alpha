#!/usr/bin/env python3
"""Широкая вселенная архива под порог владельца (п.2 задачи 08.10): есть ли кого догонять.

Зачем. Суточные проходы СО СОСТОЯНИЯМИ пула идут по реестру (765 адресов) -- только по ним
считается калиброванная модель с билетами 0.5/1/2 SOL. Проходы по всей вселенной
(`cand2_*`, `konv_*`, 27 тыс. адресов) состояний не пишут, у них есть лишь готовые п.п.
прохода при билете 0.1 и входе «конец слота» -- а это ровно режим «только S+0».

Поэтому здесь вселенная просеивается порогом владельца БЕЗ условия конвейера «среднее
>= +2 п.п.» (оно отсекает и тех, у кого медиана в плюсе, а среднее тянет вниз хвост):
n >= --n-min, медиана > 0, медиана > 0 в обеих половинах окна, >= 60 % суток в плюсе,
последние сутки вне выборки в плюсе. Кто выжил -- список для прицельного прохода со
состояниями; если не выжил никто, догонять в вселенной некого и большой билет там не
проверить нечем.

Поправка на калибровку: п.п. прохода оптимистичнее калиброванных на множитель наценки
выхода к_g -- итог меняется примерно на (1 - к_g)*100 п.п., это вычитается из каждого
значения (при к_g = 0.9908 -- 0.92 п.п.).

Копилки по адресам и формула ячейки взяты из `podbivka_konveyer` -- своих копий нет.
Выход: data/podbivka/vselennaya_porog.json. Только чтение.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_konveyer as KV  # noqa: E402

КОРЕНЬ = KV.КОРЕНЬ
П = KV.П


def по_суткам(я: list) -> dict:
    из_: dict = {}
    for ts, v in я:
        д = time.strftime("%Y-%m-%d", time.gmtime(ts))
        из_.setdefault(д, []).append(v)
    return из_


def порог(к: dict, поправка: float, n_min: int) -> dict:
    """Порог владельца по п.п. билета 0.1, без условия «среднее >= +2»."""
    я = [(ts, v - поправка) for ts, v in к["я01"]]
    зн = [v for _, v in я]
    вне = [v - поправка for v in к["вне01"]]
    если = {"n": len(зн)}
    if not зн:
        return если | {"прошёл": False, "почему_нет": ["нет ячеек"]}
    я = sorted(я)
    пол = len(я) // 2
    м1 = statistics.median([v for _, v in я[:пол]]) if пол else None
    м2 = statistics.median([v for _, v in я[пол:]]) if len(я) - пол else None
    пс = по_суткам(я)
    в_плюсе_суток = sum(1 for v in пс.values() if sum(v) > 0)
    верх = sorted(зн, reverse=True)
    если |= {"медиана": round(statistics.median(зн), 3),
             "среднее": round(statistics.mean(зн), 3),
             "в_плюсе": round(100 * sum(1 for v in зн if v > 0) / len(зн), 1),
             "медиана_1": round(м1, 3) if м1 is not None else None,
             "медиана_2": round(м2, 3) if м2 is not None else None,
             "суток": len(пс), "суток_в_плюсе": в_плюсе_суток,
             "доля_суток": round(в_плюсе_суток / len(пс), 3) if пс else None,
             "сумма_пп": round(sum(зн), 1),
             "сумма_без_5_лучших_пп": round(sum(верх[5:]), 1) if len(верх) > 5 else None,
             "вне_n": len(вне),
             "вне_медиана": round(statistics.median(вне), 3) if вне else None}
    нет = []
    if если["n"] < n_min:
        нет.append(f"n {если['n']} < {n_min}")
    if если["медиана"] <= 0:
        нет.append("медиана не больше нуля")
    if (если["сумма_без_5_лучших_пп"] or -1) <= 0:
        нет.append("без 5 лучших не в плюсе")
    if (м1 or -99) <= 0 or (м2 or -99) <= 0:
        нет.append("половина окна не в плюсе")
    if (если["доля_суток"] or 0) < 0.6:
        нет.append("суток в плюсе меньше 60 %")
    if не_вне := (если["вне_n"] < KV.N_ВНЕ_МИН or (если["вне_медиана"] or -99) <= 0):
        нет.append("последние сутки вне выборки не в плюсе")
    _ = не_вне
    return если | {"прошёл": not нет, "почему_нет": нет}


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--sutok", type=int, default=11)
    р.add_argument("--n-min", type=int, default=100)
    р.add_argument("--k-g", type=float, default=0.0,
                   help="множитель наценки выхода из калибровки; 0 -- взять из kalibrovka.json")
    р.add_argument("--metka", default=time.strftime("%Y-%m-%d", time.gmtime()))
    а = р.parse_args()

    к_g = а.k_g
    if not к_g:
        ф = П / "kalibrovka.json"
        к_g = float((json.loads(ф.read_text(encoding="utf-8")).get("калибровка") or {})
                    .get("к_g") or 1.0) if ф.exists() else 1.0
    поправка = round(100 * (1 - к_g), 3)

    файлы = []
    for ш in KV.ШАБЛОНЫ:
        файлы += glob.glob(str(П / "arhiv_den" / ш))
    файлы = sorted(set(файлы), key=KV.начало_файла)
    свои = [f for f in файлы if Path(f).name.startswith("konv_")]
    if len(свои) >= а.sutok + 1:
        файлы = свои
    вне = {файлы[-1]}
    окно = файлы[-(а.sutok + 1):-1] if len(файлы) > 1 else файлы
    по, счёт = KV.собрать(окно + sorted(вне), вне)

    адр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    годные = {a: к for a, к in по.items() if к["первых"] >= KV.N_МИН}
    ряды = {a: порог(к, поправка, а.n_min) for a, к in годные.items()}
    прошли = sorted([a for a, v in ряды.items() if v["прошёл"]],
                    key=lambda a: -(ряды[a]["медиана"] or -99))
    # кто бы прошёл при меньшем n -- чтобы видеть, не режет ли порог по объёму
    почти = sorted([a for a, v in ряды.items()
                    if not v["прошёл"] and v["почему_нет"] == [f"n {v['n']} < {а.n_min}"]],
                   key=lambda a: -(ряды[a]["медиана"] or -99))

    итог = {"что": "широкая вселенная архива под порог владельца (билет 0.1, вход конец слота)",
            "источник_чисел": {"файлы_окна": [Path(f).name for f in окно],
                               "файл_вне_выборки": [Path(f).name for f in вне],
                               "поправка_на_калибровку_пп": поправка, "к_g": к_g},
            "счёт": {k: v for k, v in счёт.items() if k != "ошибки"},
            "адресов_всего": len(по), "адресов_с_n_мин_первых": len(годные),
            "порог": {"n_min": а.n_min, "условия": ["медиана > 0", "без 5 лучших > 0",
                                                    "обе половины окна > 0", ">= 60 % суток в плюсе",
                                                    "последние сутки вне выборки > 0"]},
            "прошли": len(прошли),
            "прошли_адреса": [{"адрес": a, "группы": (адр.get(a) or {}).get("группы") or [],
                               **ряды[a]} for a in прошли[:50]],
            "почти_только_n": len(почти),
            "почти_адреса": [{"адрес": a, "группы": (адр.get(a) or {}).get("группы") or [],
                              **ряды[a]} for a in почти[:50]],
            "почему_не_прошли": {}}
    из_причин: dict = {}
    for v in ряды.values():
        for п in v["почему_нет"]:
            ключ = "n меньше предела" if п.startswith("n ") else п
            из_причин[ключ] = из_причин.get(ключ, 0) + 1
    итог["почему_не_прошли"] = dict(sorted(из_причин.items(), key=lambda kv: -kv[1]))

    out = П / "vselennaya_porog.json"
    out.write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    print(f"вселенная: адресов {len(по)}, с n>={KV.N_МИН} первых {len(годные)}, "
          f"прошли порог {len(прошли)}, не хватает только n -- {len(почти)}; "
          f"поправка на калибровку {поправка} п.п.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
