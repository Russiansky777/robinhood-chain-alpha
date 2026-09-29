#!/usr/bin/env python3
"""Подбивка: что теряем порогом 2.0 SOL -- первые входы 1.0–2.0 SOL против ≥ 2.0 у lane_s0 / batch5. Офлайн.

Архив PumpApi, прогон pyg8 (7 суток 21.09 17Z → 28.09 17Z): для 34 источников Code-1 (в них все 10 адресов
lane_s0, 12 batch5 и leader) сигнал без порога (--porog-dop 0) по правилу Code-1 -- нет покупки того же минта
в предыдущие 1800 слотов (поле «по_окну»). Котировка -- SOL (как у живых полос). Модель режима 1: входы «конец
слота» (S0_дно) и S+1, выходы +72 и +108, билет 0.3 SOL (живой у lane_s0 и batch5). Ячейка: среднее / медиана /
в плюсе, п.п. чистыми. Pump AMM в модели архива -- «не для вывода» (лишний остаток E, архив видит не все свопы):
его сигналы в ячейки не входят, их число -- отдельным столбцом. Выход: docs/podbivka_2026-09-29_porog_1_2.md.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import statistics
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
WSOL = "So11111111111111111111111111111111111111112"
ДЛЯ_ВЫВОДА = {"pump", "raydium-launchpad", "raydium-cpmm", "meteora-damm-v1"}
КОРЗИНЫ = (("1.0–2.0", 1.0, 2.0), ("≥ 2.0", 2.0, float("inf")))
КЛЕТКИ = (("S0_дно", 72), ("S0_дно", 108), ("S1", 72), ("S1", 108))


def ячейка(v: list) -> str:
    v = [x for x in v if x is not None]
    if not v:
        return "—"
    return f"{statistics.mean(v):+.1f} / {statistics.median(v):+.1f} / {100 * sum(1 for x in v if x > 0) / len(v):.0f}%"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="pyg8")
    р.add_argument("--bilet", default="0.3")
    а = р.parse_args()
    гр = json.loads((КОРЕНЬ / "data" / "podbivka" / "gruppy_code1.json").read_text(encoding="utf-8"))["groups"]
    группа = {}
    for г in ("lane_s0", "batch5"):
        for адр in (гр.get(г) or {}).get("addresses") or {}:
            группа[адр] = г
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.prefiks}_*.json.gz")))
    сиг, видел, ошибки = [], set(), []
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        ошибки += [e.split(":")[0] for e in (д.get("счёт") or {}).get("ошибки") or []]
        for с in д.get("сигналы") or []:
            if (с["signature"] in видел or с["trader"] not in группа or с.get("по_окну") is not True
                    or с.get("quoteMint") != WSOL or (с.get("sol") or 0) < 1.0):
                continue
            видел.add(с["signature"])
            сиг.append(с)
    пп = lambda с, k: ((с.get("модель") or {}).get("пп") or {}).get(k)  # noqa: E731

    def строка(имя: str, сс: list, пулы_яч: set = ДЛЯ_ВЫВОДА) -> list:
        из_ = []
        for кн, lo, hi in КОРЗИНЫ:
            к = [с for с in сс if lo <= (с.get("sol") or 0) < hi]
            м = [с for с in к if с.get("pool") in пулы_яч and not (с.get("модель") or {}).get("why_not")]
            пулы = collections.Counter(с.get("pool") for с in к)
            из_.append(f"| {имя} | {кн} | {len(к)} | {len(м)} | {пулы.get('pump-amm', 0)} | "
                       f"{', '.join(f'{p} {n}' for p, n in пулы.most_common() if p in пулы_яч) or '—'} | " +
                       " | ".join(ячейка([пп(с, f'{вх}|{а.bilet}|{h}') for с in м]) for вх, h in КЛЕТКИ) + " |")
        return из_
    шапка = ["| источник | покупка, SOL | сигналов | в ячейках | Pump AMM (не в ячейках) | пулы в ячейках | "
             "конец слота +72 | конец слота +108 | S+1 +72 | S+1 +108 |", "|---|---|---|---|---|---|---|---|---|---|"]
    md = ["# Подбивка: что теряем порогом 2.0 SOL -- первые входы 1.0–2.0 против ≥ 2.0 (lane_s0, batch5)", "",
          f"Архив PumpApi, прогон {а.prefiks}: суток {len(файлы)}; недочитанные часы: {', '.join(sorted(set(ошибки))) or 'нет'}. "
          "Сигнал -- покупка источника за SOL по правилу Code-1 (нет его покупки того же минта в предыдущие 1800 слотов), "
          f"модель режима 1, билет {а.bilet} SOL, п.п. чистыми (минус 0.002 SOL на круг). Ячейка: среднее / медиана / в плюсе. "
          "В ячейках -- кривая pump.fun, LaunchLab, CPMM, DAMM v1; Pump AMM -- только число (модель не для вывода); DLMM, "
          "DAMM v2 и прочие -- модели нет, в сигналы архива не попадают. Секунды: +72 ≈ 19 с, +108 ≈ 29 с. "
          "Ничего не рекомендуется.", "",
          "## 7JVQMwRj", ""] + шапка
    семь = next((a for a in группа if a.startswith("7JVQMwRj")), None)
    md += строка(f"`7JVQMwRj` ({группа.get(семь)})", [с for с in сиг if с["trader"] == семь])
    md += ["", "## lane_s0 и batch5 по адресам", ""] + шапка
    for г in ("lane_s0", "batch5"):
        адреса = sorted({a for a, x in группа.items() if x == г},
                        key=lambda a: -sum(1 for с in сиг if с["trader"] == a and 1.0 <= (с.get("sol") or 0) < 2.0))
        for a in адреса:
            md += строка(f"`{a[:8]}` ({г})", [с for с in сиг if с["trader"] == a])
        md += строка(f"**{г} всего**", [с for с in сиг if группа[с["trader"]] == г])
    md += строка("**обе группы**", сиг)
    md += ["", "## Pump AMM -- для справки (модель архива не для вывода: лишний остаток E, архив видит не все свопы)", ""] + шапка
    md += строка("`7JVQMwRj` (lane_s0)", [с for с in сиг if с["trader"] == семь], {"pump-amm"})
    for г in ("lane_s0", "batch5"):
        md += строка(f"**{г} всего**", [с for с in сиг if группа[с["trader"]] == г], {"pump-amm"})
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-29_porog_1_2.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
