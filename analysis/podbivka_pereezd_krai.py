#!/usr/bin/env python3
"""Пункт и: топ-20 и худшие 20 сделок -- разбор каждой строки и ожидание без ошибочных.

Что проверяется на каждой крайней строке, не выходя в сеть:
  * тип и резерв пула -- не пылевая ли засевка (переезд в потоке размечен дважды: настоящий
    пул около 85 SOL и засевка на 1e-5 SOL, и у засевки тот же минт);
  * порядок величин токенного плеча -- не съехали ли десятичные (нормальный выпуск мемкоина
    1e8..1e9 единиц; 1e15 или 1e-3 означают, что в состоянии сырые атомы или лишний
    множитель);
  * СХОДИТСЯ ЛИ РОСТ С ДЕНЬГАМИ. Цена в пуле постоянного произведения растёт только
    притоком SOL: чтобы цена выросла в M раз, в пул должно войти x0*(sqrt(M)-1) SOL. Если
    приток по состояниям больше объёма покупок окна, строка не может быть настоящей.

Дальше ожидание пересчитывается без строк, не прошедших проверку.

Только чтение собранных файлов. Выход: data/podbivka/pereezd_krai.json и раздел «и» в
docs/podbivka_pereezd.md не пишется -- страницу собирает podbivka_pereezd.py.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ТОКЕНОВ_ГОДНО = (1e6, 1e12)      # порядок величин нормального выпуска мемкоина
ПРИТОК_ЗАПАС = 1.5               # во сколько раз приток может превысить замеченный объём


def цели(путь: Path) -> dict:
    д = json.loads(путь.read_text(encoding="utf-8"))
    нужны: dict = {}
    for имя, v in (д.get("пункт_и") or {}).items():
        for вид in ("лучшие_20", "худшие_20"):
            for e in v.get(вид) or []:
                нужны.setdefault(e["poolId"], {"строки": []})
                нужны[e["poolId"]]["строки"].append({**e, "где": f"{имя}/{вид}"})
    return нужны


def добрать(нужны: dict) -> dict:
    """Полные ряды по нужным пулам + пылевые засевки тех же минтов."""
    минты = {с["минт"] for з in нужны.values() for с in з["строки"]}
    засевки: dict = collections.defaultdict(list)
    for ф in sorted(glob.glob(str(П / "zapuski" / "dolgo_*.json.gz"))) + \
             sorted(glob.glob(str(П / "zapuski" / "zap_*.json.gz"))):
        with gzip.open(ф, "rt", encoding="utf-8") as о:
            тело = json.load(о)
        for x in тело.get("ряды") or []:
            pid, м = x.get("poolId"), x.get("минт")
            if pid in нужны and "ряд" not in нужны[pid]:
                нужны[pid]["ряд"] = x
            if м in минты:
                вх = (x.get("вход") or {}).get("створ_дно")
                засевки[м].append({"poolId": pid, "пул": x.get("пул"),
                                   "правило": x.get("правило"),
                                   "резерв": (float(вх[0]) if вх else None)})
    return засевки


def разбор(з: dict, засевки: dict) -> dict:
    x = з.get("ряд")
    стр = з["строки"][0]
    из_ = {"poolId": стр["poolId"], "минт": стр["минт"], "сутки": стр["сутки"],
           "пул": стр["пул"], "где": sorted({с["где"] for с in з["строки"]}),
           "итог_sol": стр["итог_sol"], "вид": стр["вид"], "резерв_sol": стр["резерв_sol"]}
    if not x:
        из_["проверка"] = "ряд не найден"
        return из_
    вх = (x.get("вход") or {}).get("створ_дно")
    x0, y0 = float(вх[0]), float(вх[1])
    гор = sorted(int(k) for k in (x.get("выход") or {}))
    пк = x.get("покупатели") or []
    объём = sum(к.get("sol") or 0.0 for к in пк)
    волна = ((x.get("волна") or {}).get("60с") or {}).get("sol") or 0.0
    из_.update({"x0_sol": round(x0, 3), "y0_токенов": y0,
                "цена0_sol_за_токен": x0 / y0 if y0 else None,
                "событий": x.get("событий"), "продаж": x.get("продаж"),
                "покупателей_всего": x.get("покупателей_всего"),
                "объём_верхних_sol": round(объём, 2), "волна_60с_sol": round(волна, 2),
                "создатель": x.get("создатель")})
    пыль = [з2 for з2 in засевки.get(стр["минт"]) or []
            if (з2.get("резерв") or 0) < 1.0 and з2["poolId"] != стр["poolId"]]
    из_["пылевых_пулов_того_же_минта"] = len(пыль)
    из_["десятичные_годны"] = bool(ТОКЕНОВ_ГОДНО[0] <= y0 <= ТОКЕНОВ_ГОДНО[1])
    беды = []
    if not из_["десятичные_годны"]:
        беды.append("токенное плечо вне порядка 1e6..1e12")
    if (стр["резерв_sol"] or 0) < 10.0:
        беды.append("резерв меньше 10 SOL -- похоже на засевку")
    # Рост и деньги. По постоянному произведению цена в M раз требует притока
    # x0*(sqrt(M)-1); сравниваем с замеченным объёмом покупок окна.
    худ_гор = гор[-1] if гор else None
    for h in гор:
        v = (x.get("выход") or {}).get(str(h))
        if not v:
            continue
        x1, y1 = float(v[0]), float(v[1])
        if x1 <= 0 or y1 <= 0:
            continue
        M = (x1 / y1) / (x0 / y0)
        нужно = x0 * (math.sqrt(M) - 1) if M > 1 else 0.0
        видно = max(объём, волна)
        из_[f"рост_{h}_раз"] = round(M, 3)
        из_[f"приток_нужен_{h}_sol"] = round(нужно, 2)
        if h == худ_гор:
            из_["приток_нужен_sol"] = round(нужно, 2)
            из_["приток_виден_sol"] = round(видно, 2)
            if нужно > max(1.0, видно) * ПРИТОК_ЗАПАС:
                беды.append(f"рост {M:.1f}x требует притока {нужно:.0f} SOL, "
                            f"а видно {видно:.0f}")
    из_["беды"] = беды
    из_["проверка"] = "годно" if not беды else "сомнительна"
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--celi", default=str(П / "pereezd_celi.json"))
    р.add_argument("--itog", default=str(П / "pereezd_itog.json"))
    а = р.parse_args()
    нужны = цели(Path(а.celi))
    print(f"крайних пулов {len(нужны)}", flush=True)
    засевки = добрать(нужны)
    ряды = [разбор(з, засевки) for з in нужны.values()]
    свод: collections.Counter = collections.Counter()
    for р_ in ряды:
        свод[р_["проверка"]] += 1
        for б in р_.get("беды") or []:
            свод[б.split(" требует")[0] if "требует" in б else б] += 1
    тело = {"что": "пункт и: разбор крайних сделок", "пулов": len(ряды),
            "свод": dict(свод), "ряды": sorted(ряды, key=lambda r: -(r["итог_sol"] or 0))}
    (П / "pereezd_krai.json").write_text(json.dumps(тело, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    print(json.dumps(dict(свод), ensure_ascii=False, indent=1), flush=True)
    # Сколько весят сомнительные строки: доля и вклад в среднее по тем же комбинациям.
    сомн = [r for r in ряды if r["проверка"] != "годно"]
    print(f"сомнительных {len(сомн)} из {len(ряды)}; из них в лучших "
          f"{sum(1 for r in сомн if any('лучшие' in g for g in r['где']))}, в худших "
          f"{sum(1 for r in сомн if any('худшие' in g for g in r['где']))}", flush=True)
    if сомн:
        print("итоги сомнительных, SOL: "
              + ", ".join(f"{r['итог_sol']:+.2f}" for r in сомн[:20]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
