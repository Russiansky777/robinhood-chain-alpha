#!/usr/bin/env python3
"""Подбивка: не-SOL покупки наших 133 источников моделью режима 2 -- по кошелькам.
Офлайн, по data/podbivka/rezhim2/*.jsonl (прогон n133_ и уже посчитанные раньше
покупки лидера и BATCH-5 из тех же первых покупок).

По кошельку: n; входы S+0 сразу за ним (S0), конец слота (S0_дно), S+1; выход
+72 и +150 (потолок): среднее / усечённое (без ceil(5 % n) лучших) / медиана /
p95 / доля в плюс; программы пулов и котировочные. 0.5 SOL, п.п. чистыми.
Флаги модели: курс котировочного на выходе = на входе; проскальзывание нашей ноги
SOL<->котировочный не моделируется.
"""
from __future__ import annotations

import collections
import glob
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = T.КОРЕНЬ
BATCH3 = ("4vER1GJQ", "4hwPamSo", "Fvkc2thk", "GAsnqm4X", "8xL8S7P4", "BA3nKHc4", "9zZCjLr9")
ВХОДЫ = (("S0", "сразу за ним"), ("S0_дно", "конец слота"), ("S1", "S+1"))
ИМЕНА_ПРОГ = {"HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ": "GP",
              "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
              "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CP",
              "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
              "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "LaunchLab",
              "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "AMM v4",
              "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
              "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
              "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
              "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
              "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC"}


def стат(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return {"n": 0}
    ус = xs[:n - math.ceil(0.05 * n)]
    return {"n": n, "ср": sum(xs) / n, "ус": (sum(ус) / len(ус)) if ус else None, "мед": statistics.median(xs),
            "p95": xs[min(n - 1, int(0.95 * n))], "плюс": sum(1 for x in xs if x > 0) / n}


def я(с):
    if not с.get("n"):
        return "—"
    ус = f"{с['ус']:+.1f}" if с.get("ус") is not None else "—"
    return f"{с['ср']:+.1f} / {ус} / {с['мед']:+.1f} / {с['p95']:+.1f} / {100 * с['плюс']:.0f}%"


def знач(р, e, H):
    return ((р.get("входы") or {}).get(e) or {}).get(f"потолок_{H}")


def имя_прог(x):
    return ИМЕНА_ПРОГ.get(x, (x or "?")[:6])


def main() -> int:
    имена = json.loads((КОРЕНЬ / "data" / "podbivka" / "imena_istochnikov.json").read_text(encoding="utf-8"))["имена"]
    нужны: dict = {}
    for к in T.читать(T.каталог_задачи("nashi")):
        а = (к.get("строка") or {}).get("address")
        for п in T.пересчёт_порога(к.get("покупки") or [], а):
            if "котировка пула не SOL" in ((п.get("sim") or {}).get("why_not") or ""):
                нужны[п["signature"]] = а
    по: dict = {}
    for f in sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "rezhim2" / "*.jsonl"))):
        for l in open(f, encoding="utf-8"):
            р = json.loads(l)
            if р["signature"] not in нужны:
                continue
            п = по.get(р["signature"])
            if п is None or (п.get("why_not") and not р.get("why_not")):
                по[р["signature"]] = р
    по_кош: dict = {}
    for s, р in по.items():
        по_кош.setdefault(нужны[s], []).append(р)
    всего_кош = collections.Counter(нужны.values())
    ок_всего = sum(1 for р in по.values() if not р.get("why_not"))
    md = ["# Подбивка: не-SOL покупки наших 133 источников -- режим 2 по кошелькам", "",
          f"Не-SOL первых покупок от 2 SOL-экв.: {len(нужны)} у {len(всего_кош)} источников; обработано {len(по)}, "
          f"с числом {ок_всего}. 0.5 SOL, п.п. чистыми, потолок (наша покупка остаётся в пуле). Ячейка: среднее / "
          "усечённое / медиана / p95 / доля в плюс. Флаги: курс котировочного на выходе = на входе; проскальзывание "
          "ноги SOL↔котировочный не моделируется. Отказы -- CLMM / DLMM / Whirlpool / DAMM v2 / DBC (хранилища цену "
          "не дают), пул не опознан, история пула > 150 страниц.", ""]

    def блок(заг, адреса):
        т = [f"## {заг}", "", "| кошелёк | обработано / всего | n с числом | вход | +72 | +150 | программы (с числом) | "
             "котировочные | отказы |", "|---|---|---|---|---|---|---|---|---|"]
        строки = []
        for а in адреса:
            рр = по_кош.get(а) or []
            ок = [р for р in рр if not р.get("why_not")]
            прог = collections.Counter(имя_прог(р.get("program")) for р in ок)
            кот = collections.Counter(имя_прог(р.get("quote_mint")) for р in ок)
            отк = collections.Counter((р.get("why_not") or "")[:40].replace("|", "/") for р in рр if р.get("why_not"))
            имя = f"{имена.get(а, '')} {а[:8]}".strip()
            ключ = стат([знач(р, "S0", 72) for р in ок]).get("мед", -1e9) if ок else -1e9
            for н, (e, вх) in enumerate(ВХОДЫ):
                строки.append(((-ключ, а, н), f"| {имя if н == 0 else ''} | {f'{len(рр)} / {всего_кош[а]}' if н == 0 else ''} | "
                               f"{len(ок) if н == 0 else ''} | {вх} | {я(стат([знач(р, e, 72) for р in ок]))} | "
                               f"{я(стат([знач(р, e, 150) for р in ок]))} | "
                               + (", ".join(f"{k} {v}" for k, v in прог.most_common(3)) if н == 0 else "") + " | "
                               + (", ".join(f"{k} {v}" for k, v in кот.most_common(3)) if н == 0 else "") + " | "
                               + ("; ".join(f"{k} {v}" for k, v in отк.most_common(2)) if н == 0 else "") + " |"))
        return т + [s for _, s in sorted(строки)] + [""]

    b3 = [а for а in всего_кош if а.startswith(BATCH3)]
    нет_b3 = [п for п in BATCH3 if not any(а.startswith(п) for а in всего_кош)]
    md += блок("BATCH-3", b3)
    if нет_b3:
        md += [f"Без не-SOL первых покупок от 2 SOL: {', '.join(нет_b3)}.", ""]
    ост = [а for а in всего_кош if а not in b3 and по_кош.get(а)]
    md += блок(f"Остальные наши (с обработанными покупками: {len(ост)} из {len(всего_кош) - len(b3)})", ост)
    ок = [р for р in по.values() if not р.get("why_not")]
    md += ["## Сводно по всем", "", "| вход | +72 | +150 |", "|---|---|---|"]
    for e, вх in ВХОДЫ:
        md.append(f"| {вх} (n {стат([знач(р, e, 72) for р in ок]).get('n', 0)}) | {я(стат([знач(р, e, 72) for р in ок]))} | "
                  f"{я(стат([знач(р, e, 150) for р in ок]))} |")
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-27_nashi_rezhim2.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
