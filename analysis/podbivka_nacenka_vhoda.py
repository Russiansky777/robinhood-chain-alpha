#!/usr/bin/env python3
"""Наценка нашего входа к цене источника: при каком допуске минусовые отсекаются.

Зачем. Место в блоке само по себе итог не предсказывает (корзины доли блока лежат между
-0.018 и +0.013 SOL без хода). Но наценка -- насколько дороже источника мы фактически
купили -- это уже деньги, и по ней можно ставить отсечку на входе. Здесь она считается по
ФАКТУ, а не по модели.

Как считается. Наша цена входа -- `vhod_sol_po_cepi` делённое на полученные токены
(`tokenov_polucheno_raw`, 6 десятичных). Цена источника -- предельная цена пула сразу после
его сделки: состояние `вход.S0` сигнала архива, то есть x/y. Живая сделка сшивается с
сигналом по подписи источника. Наценка = наша цена / цена источника - 1.

Отсечка. Для сетки допусков считается итог, если бы мы отказывались входить при наценке
выше допуска. Отказ не бесплатен: на упавшую или отменённую транзакцию кладётся 0.001 SOL,
и это вычитается за каждый отказ. Итог на сделку = (сумма по оставшимся - 0.001 * отказов)
делённое на ВСЕ сделки -- иначе отсечка выглядела бы бесплатной.

Ночь и день врозь. Деление взято так, чтобы воспроизвести счёт владельца 55 ночь / 12 день
по группе vol_4vw: день -- часы 13 и 14 UTC, ночь -- всё остальное. Это сказано числом, а
не на глаз.

Только чтение. Выход: data/podbivka/nacenka_vhoda.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_paket_obshee as O    # noqa: E402

КОРЕНЬ = O.КОРЕНЬ
П = O.П
ВЕТКА_C1 = "origin/claude/nifty-sagan-r0polg"
ДЕСЯТИЧНЫХ = 6
ПАДЕНИЕ_SOL = 0.001          # цена отказа: упавшая или отменённая транзакция
# Наценка к цене источника оказалась огромной: медиана 30 %. Это не наше скольжение --
# это ТОЛПА в том же слоте. Пример: сделка источника оставила цену 9.112e-08, к концу его
# слота пул ушёл на 1.196e-07 (+31 %), и наш фактический налив сел на 1.185e-07 -- то есть
# на 0.9 % ЛУЧШЕ конца слота. Поэтому сетка допусков сдвинута в область, где сделки вообще
# есть, иначе отсечка просто отказывает от всего.
ДОПУСКИ = (0.12, 0.18, 0.22, 0.26, 0.30, 0.35, 0.40, 0.50, 0.70, 1e9)
ЧАСЫ_ДНЯ = (13, 14)          # воспроизводит счёт 55 ночь / 12 день по vol_4vw
ПОЛЯ_ПРИОРИТЕТА = ("tokenov_polucheno_raw", "vhod_sol_po_cepi", "итог_po_cepi_sol",
                   "our_block_index", "put_prodazhi")


def живые() -> list:
    имена = subprocess.run(["git", "ls-tree", "-r", "--name-only", ВЕТКА_C1],
                           capture_output=True, text=True, check=True).stdout.split()
    по_cid: dict = {}

    def балл(y: dict) -> int:
        return sum(1 for p in ПОЛЯ_ПРИОРИТЕТА if y.get(p) is not None)

    for ф in sorted(f for f in имена if f.startswith("data/sdelki_polosy_")):
        т = subprocess.run(["git", "show", f"{ВЕТКА_C1}:{ф}"],
                           capture_output=True, text=True).stdout
        try:
            д = json.loads(т)
        except ValueError:
            continue
        for x in д.get("ряды") or []:
            к = x.get("cid") or x.get("source_sig")
            if к and (к not in по_cid or балл(x) > балл(по_cid[к])):
                по_cid[к] = x
    return list(по_cid.values())


def отсечка(ряды: list) -> list:
    """Итог на сделку при каждом допуске, с платой за отказ."""
    всего = len(ряды)
    из_ = []
    for д in ДОПУСКИ:
        оставлены = [x for x in ряды if x["наценка"] <= д]
        отказов = всего - len(оставлены)
        сумма = sum(x["итог"] for x in оставлены) - ПАДЕНИЕ_SOL * отказов
        из_.append({"допуск": None if д > 1 else round(д, 4),
                    "вошли": len(оставлены), "отказов": отказов,
                    "итог_sol": round(сумма, 5),
                    "на_сделку_sol": round(сумма / всего, 6) if всего else None,
                    "на_вошедшую_sol": (round(sum(x["итог"] for x in оставлены)
                                              / len(оставлены), 6)
                                        if оставлены else None),
                    "в_плюсе_вошедших": sum(1 for x in оставлены if x["итог"] > 0)})
    return из_


def кванты(v: list, зн: int = 5) -> dict:
    if not v:
        return {"n": 0}
    v = sorted(v)

    def q(p):
        return round(v[min(len(v) - 1, int(p * len(v)))], зн)
    return {"n": len(v), "p10": q(0.10), "p25": q(0.25),
            "медиана": round(statistics.median(v), зн), "p75": q(0.75),
            "p90": q(0.90), "среднее": round(statistics.fmean(v), зн)}


def главное(а) -> int:  # noqa: PLR0915
    сд = живые()
    нужны = {x.get("source_sig") for x in сд
             if x.get("source_sig") and x.get("tokenov_polucheno_raw")}
    print(f"живых сделок {len(сд)}, из них с фактом входа {len(нужны)}", flush=True)
    сиг = {}
    for р in O.сигналы(фильтр=lambda r: r.get("подпись") in нужны):
        сиг[р["подпись"]] = р
    print(f"сшито с сигналами архива {len(сиг)}", flush=True)

    ряды = []
    отк: collections.Counter = collections.Counter()
    for x in сд:
        тк = x.get("tokenov_polucheno_raw")
        вх = x.get("vhod_sol_po_cepi")
        и = x.get("итог_po_cepi_sol")
        if и is None:
            и = x.get("итог_sol")
        if not (тк and вх and и is not None):
            отк["нет_факта_входа_или_итога"] += 1
            continue
        п_ = сиг.get(x.get("source_sig"))
        if п_ is None:
            отк["сигнала_в_кэше_нет"] += 1
            continue
        s0 = ((п_.get("вход") or {}).get("S0") or [None, None])
        дно = ((п_.get("вход") or {}).get("S0_дно") or [None, None])
        if not (s0[0] and s0[1]):
            отк["нет_состояния_S0"] += 1
            continue
        цена_ист = float(s0[0]) / float(s0[1])
        цена_дно = (float(дно[0]) / float(дно[1])) if (дно[0] and дно[1]) else None
        наша = вх / (тк / 10 ** ДЕСЯТИЧНЫХ)
        if цена_ист <= 0 or наша <= 0:
            отк["цена_не_положительна"] += 1
            continue
        час = int((x.get("utc") or "T00")[11:13] or 0)
        ряды.append({"utc": x.get("utc"), "группа": x.get("group"),
                     "площадка": п_.get("пул"), "итог": и,
                     "наценка": наша / цена_ист - 1,
                     "цена_наша": наша, "цена_источника": цена_ист,
                     "цена_конца_слота": цена_дно,
                     "наценка_к_концу_слота": (наша / цена_дно - 1) if цена_дно else None,
                     "когда": "день" if час in ЧАСЫ_ДНЯ else "ночь",
                     "билет": x.get("bilet_sol") or x.get("size_sol")})
    print(f"пар {len(ряды)}; отказы {dict(отк)}", flush=True)
    if not ряды:
        print("сшить нечего", flush=True)
        return 2

    срезы = {"всё": ряды,
             "ночь": [x for x in ряды if x["когда"] == "ночь"],
             "день": [x for x in ряды if x["когда"] == "день"]}
    тело = {"что": "наценка входа к цене источника и отсечка по ней",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "как_считано": {"наша_цена": "vhod_sol_po_cepi / токены (6 десятичных)",
                            "цена_источника": "x/y состояния S0 -- сразу после сделки "
                                              "источника",
                            "плата_за_отказ_sol": ПАДЕНИЕ_SOL,
                            "часы_дня_utc": list(ЧАСЫ_ДНЯ),
                            "итог_на_сделку": "сумма по оставшимся минус плата за отказы, "
                                              "делённое на ВСЕ сделки"},
            "срезы": {}}
    for имя, р in срезы.items():
        if not р:
            тело["срезы"][имя] = {"n": 0}
            continue
        по_нац = collections.defaultdict(list)
        for x in р:
            к = ("<=12 %" if x["наценка"] <= 0.12 else
                 "12-20 %" if x["наценка"] <= 0.20 else
                 "20-25 %" if x["наценка"] <= 0.25 else
                 "25-30 %" if x["наценка"] <= 0.30 else
                 "30-40 %" if x["наценка"] <= 0.40 else
                 "40-50 %" if x["наценка"] <= 0.50 else ">50 %")
            по_нац[к].append(x["итог"])
        тело["срезы"][имя] = {
            "n": len(р),
            "наценка": кванты([100 * x["наценка"] for x in р], 3),
            "наценка_к_концу_слота": кванты(
                [100 * x["наценка_к_концу_слота"] for x in р
                 if x.get("наценка_к_концу_слота") is not None], 3),
            "итог": кванты([x["итог"] for x in р]),
            "по_корзинам_наценки": {
                k: {"n": len(v), "среднее": round(statistics.fmean(v), 6),
                    "медиана": round(statistics.median(v), 6),
                    "в_плюсе_проц": round(100 * sum(1 for y in v if y > 0) / len(v), 1)}
                for k, v in sorted(по_нац.items())},
            "отсечка_по_допуску": отсечка(р)}
    ф = П / f"nacenka_vhoda{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    for имя, с in тело["срезы"].items():
        if not с.get("n"):
            continue
        print(f"\n===== {имя}: n {с['n']}; наценка % {json.dumps(с['наценка'], ensure_ascii=False)}",
              flush=True)
        print("  по корзинам наценки:", flush=True)
        for k, v in с["по_корзинам_наценки"].items():
            print(f"    {k:8} n {v['n']:>4} среднее {v['среднее']:>+10.5f} "
                  f"медиана {v['медиана']:>+10.5f} в плюсе {v['в_плюсе_проц']:>5} %",
                  flush=True)
        print(f"  отсечка (плата за отказ {ПАДЕНИЕ_SOL} SOL):", flush=True)
        for o in с["отсечка_по_допуску"]:
            д = "без отсечки" if o["допуск"] is None else f"<= {100*o['допуск']:.1f} %"
            print(f"    {д:14} вошли {o['вошли']:>4} отказов {o['отказов']:>4} "
                  f"на сделку {o['на_сделку_sol']:>+10.6f} "
                  f"на вошедшую {str(o['на_вошедшую_sol']):>12}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
