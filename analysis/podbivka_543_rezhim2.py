#!/usr/bin/env python3
"""Подбивка: не-SOL покупки 543 кошельков моделью режима 2 -- по кошелькам. Офлайн,
по data/podbivka/rezhim2/k543_*.jsonl.

По кошельку: n; S+0 «сразу за ним» (сразу после сделки источника), S+0 «конец
слота» (после всех свопов его слота), S+1 -- выход +72 и +150 (потолок): среднее,
усечённое (без ceil(5 % n) лучших), медиана, доля в плюс; программы пулов и
котировочные токены. Кандидаты в лог-only: n >= 15 и медиана +72 > 0 и «сразу за
ним», и «в конце слота». Проверка на подгонку: покупки до 24.09 против 24–26.09
(покупки до 24.09 есть только у кошельков второго прохода).
"""
from __future__ import annotations

import collections
import glob
import json
import math
import statistics
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
ГРАНЬ = 1790208000
ТОЧКИ = (("S0", "сразу за ним"), ("S0_дно", "конец слота"), ("S1", "S+1"))
СИМВОЛЫ = {"HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ": "GP",
           "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
           "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CP",
           "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
           "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "LaunchLab",
           "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "AMM v4"}


def стат(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return {"n": 0}
    ус = xs[:n - math.ceil(0.05 * n)]
    return {"n": n, "ср": sum(xs) / n, "ус": (sum(ус) / len(ус)) if ус else None,
            "мед": statistics.median(xs), "плюс": sum(1 for x in xs if x > 0) / n}


def я(с):
    if not с.get("n"):
        return "—"
    ус = f"{с['ус']:+.1f}" if с.get("ус") is not None else "—"
    return f"{с['ср']:+.1f} / {ус} / {с['мед']:+.1f} / {100 * с['плюс']:.0f}%"


def знач(р, e, H):
    return ((р.get("входы") or {}).get(e) or {}).get(f"потолок_{H}")


def main() -> int:
    по: dict = {}
    for f in sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "rezhim2" / "k543_*.jsonl"))):
        for l in open(f, encoding="utf-8"):
            р = json.loads(l)
            п = по.get(р["signature"])
            if п is None or (п.get("why_not") and not р.get("why_not")):
                по[р["signature"]] = р
    спис = {x["signature"]: x for x in json.loads(
        (КОРЕНЬ / "data" / "podbivka" / "rezhim2_spisok_543.json").read_text(encoding="utf-8"))["покупки"]}
    ок = [р for р in по.values() if not р.get("why_not")]
    отказ = collections.Counter((р.get("why_not") or "")[:60] for р in по.values() if р.get("why_not"))
    for р in ок:
        р["blockTime"] = (спис.get(р["signature"]) or {}).get("blockTime")
    по_кош: dict = {}
    for р in ок:
        по_кош.setdefault(р["wallet"], []).append(р)
    md = ["# Подбивка: не-SOL покупки 543 кошельков -- модель режима 2 по кошелькам", "",
          f"Покупок в списке: {len(спис)}; обработано: {len(по)}; с числом: {len(ок)}. 0.5 SOL, п.п. чистыми, потолок "
          "(наша покупка остаётся в пуле). Ячейка: среднее / усечённое / медиана / в плюс. Флаги модели: курс "
          "котировочного на выходе = на входе, проскальзывание нашей ноги SOL↔котировочный не моделируется.", "",
          "Без числа: " + "; ".join(f"{k} -- {v}" for k, v in отказ.most_common()), ""]
    # сводно
    md += ["## Сводно", "", "| вход | +72 | +150 |", "|---|---|---|"]
    for e, имя in ТОЧКИ:
        md.append(f"| {имя} | {я(стат([знач(р, e, 72) for р in ок]))} | {я(стат([знач(р, e, 150) for р in ок]))} |")
    # по кошелькам
    md += ["", "## По кошелькам (n >= 5, сортировка по медиане «сразу за ним» +72)", "",
           "| кошелёк | n | сразу за ним +72 | конец слота +72 | S+1 +72 | сразу за ним +150 | конец слота +150 | S+1 +150 | "
           "программы | котировочные | кандидат |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    кандидаты = []
    строки = []
    for а, рр in по_кош.items():
        if len(рр) < 5:
            continue
        с = {(e, H): стат([знач(р, e, H) for р in рр]) for e, _ in ТОЧКИ for H in (72, 150)}
        прог = collections.Counter(СИМВОЛЫ.get(р.get("program"), (р.get("program") or "?")[:6]) for р in рр)
        кот = collections.Counter(СИМВОЛЫ.get(р.get("quote_mint"), (р.get("quote_mint") or "?")[:6]) for р in рр)
        канд = len(рр) >= 15 and (с[("S0", 72)].get("мед") or -1) > 0 and (с[("S0_дно", 72)].get("мед") or -1) > 0
        if канд:
            кандидаты.append(а)
        строки.append(((с[("S0", 72)].get("мед") or -1e9), f"| {а} | {len(рр)} | " + " | ".join(
            я(с[(e, H)]) for H in (72, 150) for e, _ in ТОЧКИ) + " | " +
            ", ".join(f"{k} {v}" for k, v in прог.most_common(3)) + " | " +
            ", ".join(f"{k} {v}" for k, v in кот.most_common(3)) + f" | {'ДА' if канд else ''} |"))
    md += [s for _, s in sorted(строки, key=lambda t: -t[0])]
    md += ["", f"Кандидаты в лог-only (n >= 15, медиана +72 > 0 и «сразу за ним», и «конец слота»): {len(кандидаты)}", ""]
    # подгонка
    md += ["## Проверка на подгонку: до 24.09 против 24–26.09 (те же кошельки; +72)", "",
           "| срез | часть | n | сразу за ним | конец слота | S+1 |", "|---|---|---|---|---|---|"]
    с_историей = {р["wallet"] for р in ок if (р.get("blockTime") or 0) < ГРАНЬ}
    for имя, кош in (("все кошельки с покупками до 24.09", с_историей),
                     ("кандидаты лог-only", set(кандидаты) & с_историей)):
        for часть, усл in (("до 24.09", lambda р: (р.get("blockTime") or 0) < ГРАНЬ),
                           ("24–26.09", lambda р: (р.get("blockTime") or 0) >= ГРАНЬ)):
            рр = [р for р in ок if р["wallet"] in кош and усл(р)]
            md.append(f"| {имя} ({len(кош)}) | {часть} | {len(рр)} | " +
                      " | ".join(я(стат([знач(р, e, 72) for р in рр])) for e, _ in ТОЧКИ) + " |")
    md += ["", "### Кандидаты по одному: до 24.09 против 24–26.09 (+72, «сразу за ним» / «конец слота»)", "",
           "| кошелёк | до 24.09: n | сразу за ним | конец слота | 24–26.09: n | сразу за ним | конец слота |",
           "|---|---|---|---|---|---|---|"]
    for а in кандидаты:
        до = [р for р in по_кош[а] if (р.get("blockTime") or 0) < ГРАНЬ]
        по_ = [р for р in по_кош[а] if (р.get("blockTime") or 0) >= ГРАНЬ]
        md.append(f"| {а} | {len(до)} | {я(стат([знач(р, 'S0', 72) for р in до]))} | {я(стат([знач(р, 'S0_дно', 72) for р in до]))} | "
                  f"{len(по_)} | {я(стат([знач(р, 'S0', 72) for р in по_]))} | {я(стат([знач(р, 'S0_дно', 72) for р in по_]))} |")
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-27_543_rezhim2.md").write_text("\n".join(md), encoding="utf-8")
    (КОРЕНЬ / "data" / "podbivka" / "kandidaty_log_only.json").write_text(
        json.dumps({"кандидаты": кандидаты, "правило": "n >= 15, медиана +72 > 0 при S+0 сразу за источником и в конце слота"},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(md[:14]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
