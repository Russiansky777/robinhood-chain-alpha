#!/usr/bin/env python3
"""Подбивка: Pump AMM -- «лишний» остаток токена в хранилище (E) против занижения
модели. Офлайн, по data/podbivka/arhiv_den/<метка>.json.gz (ряды пулов сделок
полосы) и data/podbivka/sverka_noch_vhod_raw.json.

По каждой прямой покупке пула в окне (состояние до -- предыдущее событие ряда):
y_реальный = dy·(x0 + a)/a, a = quoteAmount·(1 − poolFeeRate); E/y = 1 − y_реальный/y0.
Модель продажи нашей позиции: x·t/(Y + t)·(1 − fee) -- без E; x·t/(Y − E + t)·(1 − fee)
-- с E (E = медиана E/y × Y). Факт -- quoteAmount продажи к quoteAmount покупки.
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from podbivka_arhiv_den import ст  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--metka", default="den_2026-09-27T06")
    а = р.parse_args()
    д = json.loads(gzip.decompress((КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.metka}.json.gz").read_bytes()))
    сд = [x["polya"] for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "sverka_noch_vhod_raw.json").read_text(encoding="utf-8"))["sdelki"]]
    ц = {e["signature"]: e for e in д["цели"]}
    md = ["# Pump AMM: лишний остаток токена в хранилище (E) и занижение модели", "",
          "Ночные сделки полосы 27–28.09, ряды пулов из архива PumpApi. E/y -- медиана по прямым покупкам пула в окне.", "",
          "| покупка | f окна (1 − E/y) | E/y | факт, п.п. | модель без E | модель с E | остаток |", "|---|---|---|---|---|---|---|"]
    ост = []
    for с in сд:
        b, s = ц.get(с.get("подпись_покупки")), ц.get(с.get("подпись_продажи"))
        if not b or not s or b.get("pool") != "pump-amm":
            continue
        ряд = д["ряды_целей"].get(b["poolId"]) or []
        ib = next((i for i, e in enumerate(ряд) if e["signature"] == b["signature"]), None)
        is_ = next((i for i, e in enumerate(ряд) if e["signature"] == s["signature"]), None)
        if not ib or is_ is None:
            continue
        es = []
        for пред, e in zip(ряд, ряд[1:]):
            if e["action"] != "buy" or e["signature"] == b["signature"]:
                continue
            с0 = ст(пред)
            if not с0 or not e.get("quoteAmount") or not e.get("tokenAmount"):
                continue
            x0, y0 = с0
            a_ = float(e["quoteAmount"]) * (1 - float(e.get("poolFeeRate") or 0))
            if a_ <= 0:
                continue
            es.append((y0 - float(e["tokenAmount"]) * (x0 + a_) / a_) / y0)
        k = statistics.median(es) if es else 0.0
        A, fee, т = float(b["quoteAmount"]), float(b.get("poolFeeRate") or 0), float(b["tokenAmount"])
        X, Y = ст(ряд[is_ - 1])
        факт = (float(s["quoteAmount"]) / A - 1) * 100
        m0 = (X * т / (Y + т) * (1 - fee) / A - 1) * 100
        m1 = (X * т / (Y - k * Y + т) * (1 - fee) / A - 1) * 100
        ост.append(факт - m1)
        md.append(f"| {b['signature'][:8]} | {1 - k:.3f} | {100 * k:.1f}% | {факт:+.1f} | {m0:+.1f} | {m1:+.1f} | {факт - m1:+.1f} |")
    if ост:
        md += ["", f"Медиана |остатка| с E: {statistics.median(abs(x) for x in ост):.1f} п.п. (n {len(ост)})."]
    (КОРЕНЬ / "docs" / "podbivka_2026-09-28_pumpamm_E.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
