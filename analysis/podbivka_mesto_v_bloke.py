#!/usr/bin/env python3
"""Место в блоке против итога: где граница «в плюсе / в минусе».

Зачем. Сверка модели с живыми сделками показала, что главный источник завышения -- МЕСТО
ВХОДА: модель покупала по цене слота самого источника, а полоса садится позже. Здесь это
измеряется прямо по живым сделкам: итог после всех расходов против доли нашего места в
блоке и против отрыва в слотах, корзинами, с n и средним в каждой.

Данные. Живая выгрузка несёт our_block_index (наш номер транзакции в блоке),
source_block_index (номер сделки источника) и our_block_total (сколько транзакций в блоке).
По окну 27.09--08.10 индекс заполнен у 1 708 сделок из 1 711, размер блока -- у 1 645; у
группы vol_4vw -- у всех 67. Итог берётся канонический, по цепи (`итог_po_cepi_sol`), и
только у закрытых сделок.

Доли считаются две: НАША доля блока (our_index / total) и доля ОТ ИСТОЧНИКА
((our_index - source_index) / total) -- вторая и есть «сколько блока успело пройти между
источником и нами».

Отдельно печатается распределение нашей доли по посадкам: отрыв 0 слотов (S+0), 1 слот
(S+1), 2 и больше. Оно нужно, чтобы считать забор S+0 по ЧЕСТНОЙ цене: в собранных
суточных файлах есть и состояние сразу после сделки источника (S0), и состояние после
последнего события его слота (S0_дно), а значит наша цена лежит между ними, и доля
говорит, где именно.

Только чтение. Выход: data/podbivka/mesto_v_bloke.json.
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

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ВЕТКА_C1 = "origin/claude/nifty-sagan-r0polg"
ПОЛЯ_ПРИОРИТЕТА = ("our_block_index", "our_block_total", "итог_po_cepi_sol",
                   "tokenov_polucheno_raw", "put_prodazhi")
# Доля ОТ ИСТОЧНИКА бывает отрицательной: мы садимся раньше него по номеру в блоке, но
# слотом позже. Таких случаев половина, и выбрасывать их в «прочее» нельзя -- это самая
# интересная часть.
КОРЗИНЫ_ДОЛИ = (-1.01, -0.8, -0.5, -0.3, -0.15, -0.05, 0.0,
                0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.65, 0.8, 1.01)


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
            if not к:
                continue
            п = по_cid.get(к)
            if п is None or балл(x) > балл(п):
                по_cid[к] = x
    return list(по_cid.values())


def корзина(д: float) -> str:
    for i in range(len(КОРЗИНЫ_ДОЛИ) - 1):
        if КОРЗИНЫ_ДОЛИ[i] <= д < КОРЗИНЫ_ДОЛИ[i + 1]:
            а = max(-1.0, КОРЗИНЫ_ДОЛИ[i])
            б = min(1.0, КОРЗИНЫ_ДОЛИ[i + 1])
            return f"{а:+.2f}..{б:+.2f}"
    return "прочее"


def свод(v: list) -> dict:
    if not v:
        return {"n": 0}
    return {"n": len(v), "среднее": round(statistics.fmean(v), 6),
            "медиана": round(statistics.median(v), 6),
            "сумма": round(sum(v), 4),
            "в_плюсе_проц": round(100 * sum(1 for x in v if x > 0) / len(v), 1)}


def кванты(v: list) -> dict:
    if not v:
        return {"n": 0}
    v = sorted(v)

    def q(p):
        return round(v[min(len(v) - 1, int(p * len(v)))], 4)
    return {"n": len(v), "p10": q(0.10), "p25": q(0.25),
            "медиана": round(statistics.median(v), 4), "p75": q(0.75), "p90": q(0.90),
            "среднее": round(statistics.fmean(v), 4)}


def разрез(ряды: list, имя_доли: str) -> dict:
    по_корз: dict = collections.defaultdict(list)
    for x in ряды:
        д = x.get(имя_доли)
        и = x.get("итог")
        if д is None or и is None:
            continue
        по_корз[корзина(д)].append(и)
    return {k: свод(v) for k, v in sorted(по_корз.items())}


def граница(разр: dict) -> str:
    """Первая корзина, в которой среднее уходит ниже нуля и дальше не возвращается."""
    кл = sorted(разр, key=lambda k: float(k.split("..")[0]) if ".." in k else 9)
    плохо = None
    for k in кл:
        с = разр[k]
        if не_мало(с) and (с.get("среднее") or 0) <= 0:
            плохо = плохо or k
        elif не_мало(с) and (с.get("среднее") or 0) > 0:
            плохо = None
    return плохо or "ниже нуля не уходит"


def не_мало(с: dict) -> bool:
    return (с.get("n") or 0) >= 30


def главное(а) -> int:  # noqa: PLR0915
    все = живые()
    print(f"живых сделок всего {len(все)}", flush=True)
    срезы = {
        "vol_4vw": [x for x in все if x.get("group") == "vol_4vw"],
        "группы_27-08": [x for x in все if x.get("utc")
                         and "2026-09-27" <= x["utc"][:10] <= "2026-10-08"],
    }
    тело: dict = {"что": "место в блоке против итога: где граница плюса",
                  "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "корзины_доли": list(КОРЗИНЫ_ДОЛИ), "срезы": {}}
    for имя, сд in срезы.items():
        ряды = []
        for x in сд:
            и = x.get("итог_po_cepi_sol")
            if и is None:
                и = x.get("итог_sol")
            ои, си = x.get("our_block_index"), x.get("source_block_index")
            вс = x.get("our_block_total")
            лаг = ((x.get("our_slot") or 0) - (x.get("source_slot") or 0)
                   if x.get("our_slot") and x.get("source_slot") else None)
            ряды.append({
                "итог": и, "группа": x.get("group"), "utc": x.get("utc"),
                "наш_индекс": ои, "индекс_источника": си, "всего_в_блоке": вс,
                "лаг": лаг,
                "доля_наша": (ои / вс) if (ои is not None and вс) else None,
                "доля_от_источника": ((ои - си) / вс)
                if (ои is not None and си is not None and вс) else None,
                "билет": x.get("bilet_sol") or x.get("size_sol")})
        с_итогом = [x for x in ряды if x["итог"] is not None]
        по_лагу: dict = collections.defaultdict(list)
        for x in с_итогом:
            if x["лаг"] is not None:
                по_лагу[min(x["лаг"], 5)].append(x["итог"])
        доли_по_лагу = {}
        for л in sorted({x["лаг"] for x in ряды if x["лаг"] is not None}):
            д = [x["доля_наша"] for x in ряды
                 if x["лаг"] == л and x["доля_наша"] is not None]
            if д:
                доли_по_лагу[f"лаг_{л}"] = кванты(д)
        тело["срезы"][имя] = {
            "сделок": len(сд), "с_итогом": len(с_итогом),
            "с_индексом": sum(1 for x in ряды if x["наш_индекс"] is not None),
            "с_размером_блока": sum(1 for x in ряды if x["всего_в_блоке"]),
            "всё": свод([x["итог"] for x in с_итогом]),
            "по_доле_нашей": разрез(с_итогом, "доля_наша"),
            "по_доле_от_источника": разрез(с_итогом, "доля_от_источника"),
            "по_лагу_слотов": {f"лаг_{k}": свод(v) for k, v in sorted(по_лагу.items())},
            "с_лагом": sum(1 for x in ряды if x["лаг"] is not None),
            "без_лага_почему": ("в выгрузке этого окна нет our_slot или source_slot"
                                if not по_лагу else None),
            "граница_по_доле_нашей": граница(разрез(с_итогом, "доля_наша")),
            "граница_по_доле_от_источника":
                граница(разрез(с_итогом, "доля_от_источника")),
            "граница_по_лагу": граница({f"{k}-{k}": v for k, v in
                                        ((str(float(kk.split('_')[1])), vv)
                                         for kk, vv in
                                         {f"лаг_{k}": свод(v)
                                          for k, v in sorted(по_лагу.items())}.items())}),
            "доля_наша_по_посадкам": доли_по_лагу,
            "наш_индекс": кванты([x["наш_индекс"] for x in ряды
                                  if x["наш_индекс"] is not None]),
            "всего_в_блоке": кванты([x["всего_в_блоке"] for x in ряды
                                     if x["всего_в_блоке"]])}
    ф = П / f"mesto_v_bloke{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    for имя, с in тело["срезы"].items():
        print(f"\n===== {имя}: сделок {с['сделок']}, с итогом {с['с_итогом']}, "
              f"с индексом {с['с_индексом']}, с размером блока {с['с_размером_блока']}",
              flush=True)
        print(f"  всё: {json.dumps(с['всё'], ensure_ascii=False)}", flush=True)
        for кл in ("по_доле_нашей", "по_доле_от_источника"):
            print(f"  {кл}:", flush=True)
            for k, v in с[кл].items():
                if not v.get("n"):
                    continue
                print(f"    {k:12} n {v['n']:>5} среднее {v['среднее']:>+10.5f} "
                      f"медиана {v['медиана']:>+10.5f} в плюсе {v['в_плюсе_проц']:>5} %",
                      flush=True)
            к_гр = "граница_" + кл
            print(f"    граница: {с.get(к_гр, '—')}", flush=True)
        print("  по лагу слотов:", flush=True)
        for k, v in с["по_лагу_слотов"].items():
            print(f"    {k:8} n {v['n']:>5} среднее {v['среднее']:>+10.5f} "
                  f"в плюсе {v['в_плюсе_проц']:>5} %", flush=True)
        print("  доля блока по посадкам (для забора S+0):", flush=True)
        for k, v in с["доля_наша_по_посадкам"].items():
            print(f"    {k:8} n {v['n']:>5} p10 {v['p10']} медиана {v['медиана']} "
                  f"p90 {v['p90']}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
