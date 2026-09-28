#!/usr/bin/env python3
"""Подбивка: одна сводная таблица по 23 торгующим источникам полосы (конфигурация
Code-1 data/sources_2026-09-25.json с ветки claude/nifty-sagan-r0polg: leader,
batch5, lane_s0 -- только чтение) и кандидатам. Офлайн.

По кошельку, выход +150, потолок, 0.5 SOL, п.п. чистыми:
  режим 1 -- SOL-пулы, основной симулятор (data/podbivka/nashi, первые покупки от
             2 SOL-экв.); «конец слота» -- только где посчитан (m4, проход (а));
  режим 2 -- не-SOL, data/podbivka/rezhim2/*.jsonl; «оценка f» -- сколько покупок
             посчитано с долей траты по программе пула (не по калибровке).
Ячейка: n | среднее / медиана / p95 / в плюс.
Утренний список на добавление/снятие; деньги по нему без живых сделок не меняются.
"""
from __future__ import annotations

import glob
import json
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = T.КОРЕНЬ
SOL = ("So11111111111111111111111111111111111111112", "native_sol")
КАНДИДАТЫ = {"BMgsHTvc": "DipWheeler", "CzU8MaRc": "Rowdy", "2L17Sw85": "User2L17", "5VRgqb2q": "figaro",
             "GZi5tmvZ": "GZi5tmvZ", "7txcAXw9": "7txcAXw9"}


def стат(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return {"n": 0}
    return {"n": n, "ср": sum(xs) / n, "мед": statistics.median(xs), "p95": xs[min(n - 1, int(0.95 * n))],
            "плюс": sum(1 for x in xs if x > 0) / n}


def я(с):
    if not с.get("n"):
        return "0 | —"
    return f"{с['n']} | {с['ср']:+.1f} / {с['мед']:+.1f} / {с['p95']:+.1f} / {100 * с['плюс']:.0f}%"


def main() -> int:
    конф = json.loads(subprocess.run(["git", "show", "origin/claude/nifty-sagan-r0polg:data/sources_2026-09-25.json"],
                                     cwd=КОРЕНЬ, capture_output=True, text=True, check=True).stdout)
    список = []
    for гр in ("leader", "batch5", "lane_s0"):
        for а, x in конф["groups"][гр]["addresses"].items():
            список.append((гр, а, x.get("name") or а[:8]))
    адреса_133 = {x["address"] for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki.json")
                                                   .read_text(encoding="utf-8"))["источники"]}
    for пр, имя in КАНДИДАТЫ.items():
        а = next((a for a in адреса_133 if a.startswith(пр)), None)
        if а and а not in {x[1] for x in список}:
            список.append(("кандидат", а, имя))
    # режим 1 и перечень не-SOL покупок по кошельку
    дно = {}
    for f in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "postobr" / "m4_*.jsonl")):
        for l in open(f, encoding="utf-8"):
            r = json.loads(l)
            кс = (r.get("доп") or {}).get("S0_конец_слота") or {}
            if not r.get("why_not") and кс:
                дно[r["signature"]] = кс.get("150")
    р1: dict = {}
    не_sol: dict = {}
    for к in T.читать(T.каталог_задачи("nashi")):
        а = (к.get("строка") or {}).get("address")
        for п in T.пересчёт_порога(к.get("покупки") or [], а):
            с = п.get("sim") or {}
            if "котировка пула не SOL" in (с.get("why_not") or ""):
                не_sol[п["signature"]] = а
            elif с.get("quote_mint") in SOL and T.чп(с, "S0", 150) is not None:
                р1.setdefault(а, []).append((T.чп(с, "S0", 150), дно.get(п["signature"])))
    по: dict = {}
    for f in sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "rezhim2" / "*.jsonl"))):
        for l in open(f, encoding="utf-8"):
            р = json.loads(l)
            if р["signature"] not in не_sol:
                continue
            п = по.get(р["signature"])
            if п is None or (п.get("why_not") and not р.get("why_not")):
                по[р["signature"]] = р
    р2: dict = {}
    отказ2: dict = {}
    for s, р in по.items():
        а = не_sol[s]
        if р.get("why_not"):
            отказ2[а] = отказ2.get(а, 0) + 1
            continue
        вх = р.get("входы") or {}
        р2.setdefault(а, []).append(((вх.get("S0") or {}).get("потолок_150"), (вх.get("S0_дно") or {}).get("потолок_150"),
                                     bool(р.get("f_оценка"))))
    всего_нс: dict = {}
    for а in не_sol.values():
        всего_нс[а] = всего_нс.get(а, 0) + 1
    md = ["# Подбивка: 23 торгующих + кандидаты -- режим 1 и режим 2 рядом", "",
          "Список -- конфигурация Code-1 (data/sources_2026-09-25.json, группы leader / batch5 / lane_s0; только "
          "чтение) и кандидаты. Выход +150 слотов, потолок, 0.5 SOL, п.п. чистыми. Ячейка: n | среднее / медиана / "
          "p95 / в плюс. Режим 1 -- SOL-пулы, основной симулятор; «конец слота» режима 1 посчитан только в m4 "
          "(проход (а) 543), у наших источников его нет. Режим 2 -- не-SOL, модель A3; «оценка f» -- покупки с долей "
          "траты по программе пула. Утренний список на добавление/снятие: деньги по нему без живых сделок не меняются.", "",
          "| группа | кошелёк | р1 сразу за ним | р1 конец слота | р2 сразу за ним | р2 конец слота | р2: не-SOL всего / без числа / оценка f |",
          "|---|---|---|---|---|---|---|"]
    for гр, а, имя in список:
        x1 = р1.get(а) or []
        x2 = р2.get(а) or []
        md.append(f"| {гр} | {имя} {а[:8]} | {я(стат([v for v, _ in x1]))} | {я(стат([d for _, d in x1]))} | "
                  f"{я(стат([v for v, _, _ in x2]))} | {я(стат([d for _, d, _ in x2]))} | "
                  f"{всего_нс.get(а, 0)} / {всего_нс.get(а, 0) - len(x2)} / "
                  f"{sum(1 for *_, e in x2 if e)} |")
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-28_svod_torguyuschie.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
