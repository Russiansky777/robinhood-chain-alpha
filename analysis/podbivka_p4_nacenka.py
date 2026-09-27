#!/usr/bin/env python3
"""Подбивка п.4, лидер: корзины фактической наценки входа (к спот-цене сразу после
лидера) и корзины резерва пула в SOL-экв. -- для порога наценки. Офлайн, режим 2,
data/podbivka/rezhim2/*.jsonl (тот же отбор, что в podbivka_p4_lider.py).

Входы S+0 «сразу за ним» (S0) и «конец слота» (S0_дно), выход +150, потолок
(наша покупка остаётся в пуле). 0.5 SOL, п.п. чистыми.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from podbivka_p4_lider import ЛИДЕР, КОРЕНЬ, стат  # noqa: E402

НАЦЕНКА = (("0–10 % (вкл. <0)", -1e18, 10), ("10–25 %", 10, 25), ("25–50 %", 25, 50), ("50+ %", 50, 1e18))
РЕЗЕРВ = (("< 30", 0, 30), ("30–100", 30, 100), ("100+", 100, 1e18))
ВХОДЫ = (("S0", "сразу за ним"), ("S0_дно", "конец слота"))


def я(с):
    if not с.get("n"):
        return "0 | — | — | — | —"
    ус = f"{с['ус']:+.1f}" if с.get("ус") is not None else "—"
    return f"{с['n']} | {с['ср']:+.1f} | {ус} | {с['мед']:+.1f} | {100 * с['плюс']:.0f}%"


def main() -> int:
    по: dict = {}
    for f in sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "rezhim2" / "*.jsonl"))):
        for l in open(f, encoding="utf-8"):
            р = json.loads(l)
            п = по.get(р["signature"])
            if п is None or (п.get("why_not") and not р.get("why_not")):
                по[р["signature"]] = р
    рр = [р for р in по.values() if р.get("wallet") == ЛИДЕР and not р.get("why_not")]
    md = ["# Подбивка п.4, лидер: корзины наценки и резерва пула -- к порогу наценки", "",
          f"Лидер, режим 2 (котировка не SOL), покупок с числом: {len(рр)}. Выход +150 слотов, потолок, 0.5 SOL, "
          "п.п. чистыми. Наценка -- наша цена за токен к спот-цене пула сразу после сделки лидера (сбор пула + "
          "проскальзывание 0.5 SOL + сдвиг до нашего входа), корзина -- по наценке своего входа. Резерв -- "
          "котировочная сторона пула в SOL-экв. на момент s0. Ячейка: n | среднее | усечённое (без ceil(5 % n) "
          "лучших) | медиана | в плюс.", ""]
    md += ["## По наценке входа", "", "| корзина наценки | " + " | ".join(
        f"{и}: n | ср | ус | мед | плюс" for _, и in ВХОДЫ) + " |", "|---|" + "---|" * 5 * len(ВХОДЫ)]
    for имя, lo, hi in НАЦЕНКА + (("все", -1e18, 1e18),):
        ячейки = []
        for e, _ in ВХОДЫ:
            вк = [((р["входы"] or {}).get(e) or {}) for р in рр]
            ячейки.append(я(стат([x.get("потолок_150") for x in вк
                                   if x.get("наценка_пп") is not None and lo <= x["наценка_пп"] < hi])))
        md.append(f"| {имя} | " + " | ".join(ячейки) + " |")
    md += ["", "## По резерву пула, SOL-экв.", "", "| резерв | " + " | ".join(
        f"{и}: n | ср | ус | мед | плюс" for _, и in ВХОДЫ) + " | медиана наценки S0 / конец слота |",
           "|---|" + "---|" * (5 * len(ВХОДЫ) + 1)]
    for имя, lo, hi in РЕЗЕРВ + (("нет резерва", None, None),):
        вк = [р for р in рр if (р.get("резерв_s0_sol") is None if lo is None
                                 else р.get("резерв_s0_sol") is not None and lo <= р["резерв_s0_sol"] < hi)]
        if lo is None and not вк:
            continue
        нац = [стат([((р["входы"] or {}).get(e) or {}).get("наценка_пп") for р in вк]) for e, _ in ВХОДЫ]
        md.append(f"| {имя} | " + " | ".join(я(стат([((р["входы"] or {}).get(e) or {}).get("потолок_150") for р in вк]))
                                              for e, _ in ВХОДЫ) + " | " +
                  " / ".join(f"{н['мед']:+.1f}" if н.get("n") else "—" for н in нац) + " |")
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-27_p4_nacenka.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
