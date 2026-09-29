#!/usr/bin/env python3
"""Подбивка: сигналы lane_s0 и batch5 по часам суток UTC и итог на +108 (вход «конец слота» и S+1). Офлайн.

Архив PumpApi, прогон pyg7 (7 суток 21.09 17Z → 28.09 17Z, порог 2.0 SOL): сигнал -- покупка источника группы за SOL
по правилу Code-1 (нет его покупки того же минта в предыдущие 1800 слотов, поле «по_окну»). Час -- по timestamp
события в архиве, UTC. Модель режима 1: входы «конец слота» (S0_дно) и S+1, выход +108, билет 0.3 SOL (живой у
lane_s0 и batch5). Ячейка: среднее / медиана / в плюсе, п.п. чистыми. В ячейках -- кривая pump.fun, LaunchLab,
CPMM, DAMM v1; Pump AMM в модели архива -- «не для вывода» (в ячейки не входит, в «сигналов» входит).
Выход: docs/podbivka_2026-09-30_chas_sutok.md.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import statistics
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
WSOL = "So11111111111111111111111111111111111111112"
ДЛЯ_ВЫВОДА = {"pump", "raydium-launchpad", "raydium-cpmm", "meteora-damm-v1"}
ГРУППЫ = ("lane_s0", "batch5")


def ячейка(v: list) -> str:
    v = [x for x in v if x is not None]
    if not v:
        return "—"
    return f"{statistics.mean(v):+.1f} / {statistics.median(v):+.1f} / {100 * sum(1 for x in v if x > 0) / len(v):.0f}%"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="pyg7")
    р.add_argument("--bilet", default="0.3")
    а = р.parse_args()
    гр = json.loads((КОРЕНЬ / "data" / "podbivka" / "gruppy_code1.json").read_text(encoding="utf-8"))["groups"]
    группа = {адр: г for г in ГРУППЫ for адр in ((гр.get(г) or {}).get("addresses") or {})}
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.prefiks}_*.json.gz")))
    сиг, видел, ошибки, порог = [], set(), [], set()
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        порог.add(д.get("порог_sol"))
        ошибки += [e.split(":")[0] for e in (д.get("счёт") or {}).get("ошибки") or []]
        for с in д.get("сигналы") or []:
            if (с["signature"] in видел or с["trader"] not in группа or с.get("по_окну") is not True
                    or с.get("quoteMint") != WSOL):
                continue
            видел.add(с["signature"])
            с["час"] = time.gmtime(с["timestamp"] / 1000).tm_hour
            сиг.append(с)
    пп = lambda с, k: ((с.get("модель") or {}).get("пп") or {}).get(k)  # noqa: E731
    в_яч = lambda с: с.get("pool") in ДЛЯ_ВЫВОДА and not (с.get("модель") or {}).get("why_not")  # noqa: E731

    def клетки(сс: list) -> list:
        м = [с for с in сс if в_яч(с)]
        return [str(len(сс)), str(len(м)), ячейка([пп(с, f"S0_дно|{а.bilet}|108") for с in м]),
                ячейка([пп(с, f"S1|{а.bilet}|108") for с in м])]
    шапка = "| час UTC | " + " | ".join(f"{г}: сигналов | {г}: в ячейках | {г}: конец слота +108 | {г}: S+1 +108"
                                        for г in ГРУППЫ) + " |"
    md = ["# Подбивка: час суток и исход -- lane_s0 и batch5 по часам UTC, итог на +108", "",
          f"Архив PumpApi, прогон {а.prefiks}: суток {len(файлы)} (21.09 17Z → 28.09 17Z), порог архива "
          f"{', '.join(str(x) for x in sorted(порог))} SOL; недочитанные часы: {', '.join(sorted(set(ошибки))) or 'нет'}. "
          "Сигнал -- покупка источника группы за SOL по правилу Code-1 (нет его покупки того же минта в предыдущие "
          f"1800 слотов). Час -- UTC по времени события. Модель режима 1, билет {а.bilet} SOL (живой у обеих групп), "
          "выход +108 (≈ 29 с), п.п. чистыми (минус 0.002 SOL на круг). Ячейка: среднее / медиана / в плюсе. "
          "«В ячейках» -- кривая pump.fun, LaunchLab, CPMM, DAMM v1; Pump AMM (модель архива не для вывода) и пулы без "
          "модели считаются только в «сигналов». Мало сигналов в часе -- шум, не закономерность. Ничего не рекомендуется.",
          "", шапка, "|" + "---|" * (1 + 4 * len(ГРУППЫ))]
    for ч in range(24):
        md.append(f"| {ч:02d} | " + " | ".join(" | ".join(клетки([с for с in сиг if с["час"] == ч and группа[с["trader"]] == г]))
                                               for г in ГРУППЫ) + " |")
    md.append("| **все** | " + " | ".join(" | ".join(клетки([с for с in сиг if группа[с["trader"]] == г])) for г in ГРУППЫ) + " |")
    import collections  # noqa: PLC0415
    for г in ГРУППЫ:
        пулы = collections.Counter(с.get("pool") for с in сиг if группа[с["trader"]] == г)
        md += ["", f"{г}, сигналы по пулам: " + ", ".join(f"{p} {n}" for p, n in пулы.most_common()) + "."]
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-30_chas_sutok.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
