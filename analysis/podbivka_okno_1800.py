#!/usr/bin/env python3
"""Окно 1800 слотов или «без окна»: сигналы 34 источников Code-1 по правилу Code-1 в pyg10, разложенные на «первая
покупка токена за 7 суток» и «повторная». Офлайн.

pyg10 -- архив PumpApi с правилом сигнала Code-1 для всех наших адресов (analysis/podbivka_arhiv_den.py --okno-vsem
--okno-dokupki 1800, порог 2 SOL-экв.), окно pyg7 21.09 17Z → 28.09 17Z (разгонный час 16Z каждых суток отброшен).
Первая / повторная -- по ВСЕМ покупкам источника в архиве (наши_события, любого размера, все 7 суток подряд): была ли
у него покупка того же минта раньше в окне. Покупки до 21.09 17Z архив не видит -- такие «повторные» считаются первыми.
Ячейки -- котировка WSOL, кривая pump.fun / LaunchLab / CPMM / DAMM v1; Pump AMM -- только число. Вход «конец слота»,
билет 0.3, выходы +72 / +108, п.п. чистыми.
Выход: docs/podbivka_2026-09-30_okno_1800.md.
"""
from __future__ import annotations

import glob
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_kandidaty_vne as K  # noqa: E402

КОРЕНЬ = K.КОРЕНЬ


def main() -> int:
    сп = json.loads((K.П / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    ист = set(сп["группы_code1"]) | set(сп["кандидаты"]) | set(сп.get("снайперы") or [])
    файлы = sorted(glob.glob(str(K.П / "arhiv_den" / "pyg10_*.json.gz")))
    первые: dict = {}                      # (трейдер, минт) -> наименьший слот покупки в окне
    сс, видел = [], set()
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        for e in д.get("наши_события") or []:
            if e.get("trader") in ист and e.get("action") == "buy" and e.get("block") and K.окно_pyg10(e, f):
                кл = (e["trader"], e.get("mint"))
                первые[кл] = min(первые.get(кл, e["block"]), e["block"])
        for с in д.get("сигналы") or []:
            if с["trader"] in ист and с["signature"] not in видел and K.окно_pyg10(с, f):
                видел.add(с["signature"])
                сс.append(с)
    ряды = {"первая": [], "повторная": []}
    for с in сс:
        if с.get("по_окну") is not True:
            continue
        вид = "первая" if с["block"] <= первые.get((с["trader"], с["mint"]), с["block"]) else "повторная"
        ряды[вид].append(с)
    ф = lambda s_: "—" if not s_.get("n") else f"{s_['n']} | {s_['медиана']:+.1f} | {s_['среднее']:+.1f} | {100 * s_['в_плюсе']:.0f}%"  # noqa: E731
    md = ["# Окно 1800 слотов: первая покупка токена за 7 суток против повторной (34 источника Code-1, pyg10)", "",
          "Архив PumpApi pyg10 (окно pyg7: 21.09 17Z → 28.09 17Z) с правилом сигнала Code-1 (нет покупки того же минта в "
          "предыдущие 1800 слотов) для всех адресов, порог 2 SOL-экв. «Первая» -- у источника не было покупки этого минта "
          "раньше в окне (по всем его покупкам в архиве, любого размера); «повторная» -- была (раньше чем за 1800 слотов, "
          "иначе сигнала нет). Покупки до 21.09 17Z архив не видит: у токенов, купленных раньше, повторная считается первой. "
          "Ячейки -- котировка WSOL, кривая pump.fun / LaunchLab / CPMM / DAMM v1, вход «конец слота», билет 0.3; "
          "Pump AMM -- только число. Ячейка: n | медиана | среднее | в плюсе, п.п. чистыми. Ничего не рекомендуется.", "",
          "| сигнал | всего | Pump AMM | +72: n / медиана / среднее / в плюсе | +108: n / медиана / среднее / в плюсе |",
          "|---|---|---|---|---|"]
    for вид in ("первая", "повторная"):
        L = ряды[вид]
        яч = [с for с in L if K.в_ячейке(с)]
        md.append(f"| {вид} покупка токена | {len(L)} | {sum(1 for с in L if с.get('pool') == 'pump-amm')} | "
                  f"{ф(K.стат([K.пп(с, '72') for с in яч]))} | {ф(K.стат([K.пп(с, '108') for с in яч]))} |")
    md += ["", f"Файлов pyg10: {len(файлы)}; сигналов 34 источников в окне: {len(сс)}, из них по правилу Code-1: "
           f"{sum(len(v) for v in ряды.values())}."]
    (КОРЕНЬ / "docs" / "podbivka_2026-09-30_okno_1800.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
