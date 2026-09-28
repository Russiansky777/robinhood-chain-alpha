#!/usr/bin/env python3
"""Подбивка: 543 не-SOL на архиве PumpApi (7 суток, pyg4_*) -- офлайн.

Сигнал -- первая покупка от 2 SOL-экв. в пуле с котировкой не SOL (podbivka_arhiv_den.py
--ne-sol): курс котировки к SOL -- последнее событие SOL-пула этого минта в архиве,
доллары -- курс детектора. Модель режима 1 в единицах котировки: билет 0.3 / 0.5 SOL
по курсу сигнала; курс на выходе = на входе (флаг); нога SOL↔котировка не
моделируется. Pump AMM -- не для вывода (лишний остаток E, архив видит не все свопы).
Выход: docs/podbivka_2026-09-28_543_nesol_arhiv.md.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import statistics
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
WSOL = "So11111111111111111111111111111111111111112"
USD = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
ТИПЫ = {"pump": "кривая pump.fun", "raydium-launchpad": "LaunchLab", "raydium-cpmm": "CPMM", "meteora-damm-v1": "DAMM v1",
        "pump-amm": "Pump AMM (не для вывода)"}


def ячейка(v: list) -> str:
    v = sorted(x for x in v if x is not None)
    if not v:
        return "—"
    k = -(-len(v) * 5 // 100)
    ус = v[:-k] if len(v) > k else v
    return (f"{len(v)} | {statistics.mean(v):+.1f} | {statistics.mean(ус):+.1f} | {statistics.median(v):+.1f} | "
            f"{100 * sum(1 for x in v if x > 0) / len(v):.0f}%")


def main() -> int:
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / "pyg4_*.json.gz")))
    сиг, видел, ошибки = [], set(), []
    for f in файлы:
        a = json.loads(gzip.decompress(Path(f).read_bytes()))
        ошибки += [e.split(":")[0] for e in (a.get("счёт") or {}).get("ошибки") or []]
        for с in a.get("сигналы") or []:
            if с["signature"] in видел or (с.get("модель") or {}).get("why_not") or с.get("quoteMint") in (None, WSOL):
                continue
            видел.add(с["signature"])
            сиг.append(с)
    адр = json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    гр = lambda с: set((адр.get(с["trader"]) or {}).get("группы") or [])  # noqa: E731
    пп = lambda с, k: (с["модель"].get("пп") or {}).get(k)  # noqa: E731
    с543 = [с for с in сиг if "543" in гр(с)]
    котир = collections.Counter(USD.get(с["quoteMint"], с["quoteMint"][:8]) for с in с543)
    md = ["# Подбивка: 543 не-SOL на архиве PumpApi -- 7 суток", "",
          f"Файлов суток: {len(файлы)}; недочитанные часы: {', '.join(ошибки) if ошибки else 'нет'}. Сигналов не-SOL всех наших "
          f"адресов: {len(сиг)}, из них 543: {len(с543)}. Котировки у 543: " +
          ", ".join(f"{k} {n}" for k, n in котир.most_common(10)) + ". Флаги: курс котировки на выходе = на входе; нога "
          "SOL↔котировка не моделируется; курс котировки -- по SOL-пулам архива (доллары -- курс детектора). Ячейка: n | "
          "среднее | усечённое | медиана | в плюс; 0.5 SOL, п.п. чистыми.", "",
          "## 1. По типу пула и входу (543)", "", "| пул | вход | +6 | +36 | +72 | +150 |", "|---|---|---|---|---|---|"]
    for p, имя in ТИПЫ.items():
        сс = [с for с in с543 if с["pool"] == p]
        if not сс:
            continue
        for вход, ви in (("S0", "сразу за ним"), ("S0_дно", "конец слота"), ("S1", "S+1")):
            md.append(f"| {имя} | {ви} | " + " | ".join(ячейка([пп(с, f'{вход}|0.5|{h}') for с in сс]) for h in (6, 36, 72, 150)) + " |")
    md += ["", "## 2. По котировке (543, без Pump AMM, сразу за ним)", "", "| котировка | +6 | +36 | +150 |", "|---|---|---|---|"]
    for q, n in котир.most_common(12):
        сс = [с for с in с543 if USD.get(с["quoteMint"], с["quoteMint"][:8]) == q and с["pool"] != "pump-amm"]
        if сс:
            md.append(f"| {q} | " + " | ".join(ячейка([пп(с, f'S0|0.5|{h}') for с in сс]) for h in (6, 36, 150)) + " |")
    md += ["", "## 3. По кошелькам 543 (без Pump AMM, ≥ 5 сигналов, сразу за ним, +36)", "",
           "| кошелёк | сигналов | +36 | конец слота, +36 |", "|---|---|---|---|"]
    по_к = collections.defaultdict(list)
    for с in с543:
        if с["pool"] != "pump-amm":
            по_к[с["trader"]].append(с)
    for w, сс in sorted(по_к.items(), key=lambda kv: -len(kv[1])):
        if len(сс) < 5:
            continue
        md.append(f"| `{w}` | {len(сс)} | {ячейка([пп(с, 'S0|0.5|36') for с in сс])} | {ячейка([пп(с, 'S0_дно|0.5|36') for с in сс])} |")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-28_543_nesol_arhiv.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:30]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
