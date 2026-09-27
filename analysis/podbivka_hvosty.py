#!/usr/bin/env python3
"""Подбивка: решения по среднему с хвостами (слово владельца 27.09). Офлайн, без сканов.

Ячейка везде: n | среднее | усечённое (без ceil(5 % n) лучших) | медиана | p95 |
максимум | доля итога от 5 % лучших (сумма ceil(5 % n) лучших / сумма всех; при
сумме всех <= 0 -- «итог <= 0» и сумма лучших в п.п.). 0.5 SOL, п.п. чистыми.

п.1  режим 1, 543 (проход (а) + «досчитать») + 133 наших, по типу пула:
     кривая pump.fun / Pump AMM / Raydium CPMM / прочие. Вход S+0 сразу за источником (числа симулятора);
     «конец слота» -- только где он посчитан (m4 = проход (а)), n указан.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = T.КОРЕНЬ
КРИВАЯ = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
ШАПКА = "n | среднее | усеч. | медиана | p95 | макс | доля 5 % лучших"


def стат(xs) -> dict:
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return {"n": 0}
    k = math.ceil(0.05 * n)
    ус = xs[:n - k]
    сумма, лучшие = sum(xs), sum(xs[n - k:])
    return {"n": n, "ср": сумма / n, "ус": (sum(ус) / len(ус)) if ус else None, "мед": statistics.median(xs),
            "p95": xs[min(n - 1, int(0.95 * n))], "макс": xs[-1], "сумма": сумма, "лучшие": лучшие,
            "доля": (лучшие / сумма) if сумма > 0 else None}


def я(с: dict) -> str:
    if not с.get("n"):
        return "0 | — | — | — | — | — | —"
    ус = f"{с['ус']:+.1f}" if с.get("ус") is not None else "—"
    доля = f"{100 * с['доля']:.0f}%" if с.get("доля") is not None else f"итог ≤ 0 (лучшие {с['лучшие']:+.0f})"
    return f"{с['n']} | {с['ср']:+.1f} | {ус} | {с['мед']:+.1f} | {с['p95']:+.1f} | {с['макс']:+.1f} | {доля}"


def шапка(колонки: list) -> list:
    return ["| " + " | ".join(колонки[:-1]) + " | " + ШАПКА + " |",
            "|" + "---|" * (len(колонки) - 1 + 7)]


def тип_пула(прог) -> str:
    return {КРИВАЯ: "кривая pump.fun", PUMP_AMM: "Pump AMM", CPMM: "CPMM (Raydium)"}.get(прог, "прочие")


def п1() -> list:
    дно = {}
    for f in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "postobr" / "m4_*.jsonl")):
        for l in open(f, encoding="utf-8"):
            r = json.loads(l)
            кс = (r.get("доп") or {}).get("S0_конец_слота") or {}
            if not r.get("why_not") and кс:
                дно[r["signature"]] = кс
    ряды, видел = [], set()
    for кат, группа in (("koshelki", "543"), ("koshelki_dos", "543"), ("nashi", "133 наших")):
        for к in T.читать(T.каталог_задачи(кат)):
            а = (к.get("строка") or {}).get("address")
            if not а or (кат != "nashi" and (к.get("why_not") or (к.get("скан") or {}).get("why_not"))):
                continue
            for п in T.пересчёт_порога(к.get("покупки") or [], а):
                с = п.get("sim") or {}
                if п["signature"] in видел or T.чп(с, "S0", 72) is None:
                    continue
                видел.add(п["signature"])
                кс = дно.get(п["signature"]) or {}
                ряды.append({"группа": группа, "тип": тип_пула(с.get("program")),
                             ("S0", 72): T.чп(с, "S0", 72), ("S0", 150): T.чп(с, "S0", 150),
                             ("дно", 72): кс.get("72"), ("дно", 150): кс.get("150")})
    md = ["## п.1 Режим 1 (543 + 133) по типу пула", "",
          f"Покупок с числом S+0: {len(ряды)} (543: {sum(р['группа'] == '543' for р in ряды)}, 133 наших: "
          f"{sum(р['группа'] != '543' for р in ряды)}; дубли по подписи сняты). Вход «сразу за ним» -- числа "
          "основного симулятора. «Конец слота» посчитан только для прохода (а) 404 кошельков (m4) -- у "
          "«досчитать» и наших его нет, n в строке. Потолок (наша покупка остаётся в пуле).", ""]
    md += шапка(["группа", "тип пула", "вход", "выход", "x"])
    for гр in ("все", "543", "133 наших"):
        for тип in ("кривая pump.fun", "Pump AMM", "CPMM (Raydium)", "прочие", "все", "без кривой"):
            рр = [р for р in ряды if (гр == "все" or р["группа"] == гр)
                  and (тип == "все" or р["тип"] == тип or (тип == "без кривой" and р["тип"] != "кривая pump.fun"))]
            for e, имя in (("S0", "сразу за ним"), ("дно", "конец слота")):
                for H in (72, 150):
                    md.append(f"| {гр} | {тип} | {имя} | +{H} | {я(стат([р[(e, H)] for р in рр]))} |")
    return md + [""]


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--p", nargs="+", type=int, default=[1])
    а = р.parse_args()
    md = ["# Подбивка: решения по среднему с хвостами", "",
          "Ячейка: " + ШАПКА + ". Доля 5 % лучших -- сумма ceil(5 % n) лучших сделок к сумме всех (итог "
          "стратегии в п.п. на круг × n). 0.5 SOL, п.п. чистыми. Без новых сканов.", ""]
    for н in а.p:
        md += globals()[f"п{н}"]()
    out = КОРЕНЬ / "docs" / "podbivka_2026-09-27_hvosty.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
