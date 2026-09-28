#!/usr/bin/env python3
"""Подбивка: режим 1, ранние выходы (живёт ли режим 1 при выходе через ~3 с). Офлайн.

Из готовых данных: вход S+0 сразу за источником, 0.5 SOL, потолок; выходы +12 и +25
(ближайшая к +24 посчитанная точка) -- основной симулятор, 543 + 133; +6 (и +2/+4/+8)
-- только пост-обработка m4 (проход (а) 404 кошельков). +36, вход «конец слота» при
ранних выходах и билет 0.3 SOL в готовых данных не посчитаны -- строки «—».
По типу пула (кривая pump.fun / Pump AMM / LaunchLab) и по источникам 54uaRuJE.
Ячейка: n | среднее | усеч. | медиана | p95 | макс | доля итога от 5 % лучших.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tablicy as T  # noqa: E402
from podbivka_hvosty import КОРЕНЬ, шапка, стат, я  # noqa: E402

ТИП = {"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "кривая pump.fun",
       "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
       "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "LaunchLab"}
ИСТ_54 = ("6qudAN2k", "387FRwow", "BCrTEXmW", "DAEdBmTP", "JDFDma1T", "4S9Vbao1", "AFmiexHw", "9emXYGUF", "2CQgjcdN")
ВЫХОДЫ = ("6", "12", "25", "72")


def main() -> int:
    м4 = {}
    for f in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "postobr" / "m4_*.jsonl")):
        for l in open(f, encoding="utf-8"):
            r = json.loads(l)
            if not r.get("why_not"):
                м4[r["signature"]] = (r.get("чистый_пп") or {}).get("S0") or {}
    ряды, видел = [], set()
    for кат in ("koshelki", "koshelki_dos", "nashi"):
        for к in T.читать(T.каталог_задачи(кат)):
            а = (к.get("строка") or {}).get("address")
            if not а:
                continue
            for п in T.пересчёт_порога(к.get("покупки") or [], а):
                с = п.get("sim") or {}
                if п["signature"] in видел or T.чп(с, "S0", 12) is None:
                    continue
                видел.add(п["signature"])
                x = {"wallet": а, "тип": ТИП.get(с.get("program"), "прочие"), "м4": п["signature"] in м4}
                for H in ("12", "25", "72"):
                    x[H] = T.чп(с, "S0", int(H))
                x["6"] = (м4.get(п["signature"]) or {}).get("6")
                x["36"] = None
                ряды.append(x)
    md = ["# Подбивка: режим 1, ранние выходы", "",
          f"Покупок с числом S+0: {len(ряды)} (543 + 133, дубли сняты); +6 -- только у {sum(r['м4'] for r in ряды)} "
          "покупок прохода (а) (m4). Вход S+0 сразу за источником, 0.5 SOL, потолок (наша покупка остаётся в пуле), "
          "п.п. чистыми (минус 0.002 SOL). +25 -- ближайшая к +24 посчитанная точка. Не посчитаны в готовых данных: "
          "+36, вход «конец слота» при ранних выходах, билет 0.3 SOL.", ""]
    md += ["## По типу пула", ""] + шапка(["тип пула", "выход", "x"])
    for тип in ("кривая pump.fun", "Pump AMM", "LaunchLab", "прочие", "все"):
        рр = [р for р in ряды if тип == "все" or р["тип"] == тип]
        for H in ВЫХОДЫ:
            md.append(f"| {тип} | +{H} | {я(стат([р[H] for р in рр]))} |")
    md += ["", "## Те же покупки прохода (а) -- одна выборка на всех выходах (m4)", ""] + шапка(["тип пула", "выход", "x"])
    for тип in ("кривая pump.fun", "Pump AMM", "LaunchLab", "все"):
        рр = [р for р in ряды if р["м4"] and р["6"] is not None and (тип == "все" or р["тип"] == тип)]
        for H in ("6", "12", "25", "72"):
            md.append(f"| {тип} | +{H} | {я(стат([р[H] for р in рр]))} |")
    md += ["", "## Источники 54uaRuJE (7 дней)", ""] + шапка(["источник", "выход", "x"])
    for пр in ИСТ_54:
        рр = [р for р in ряды if р["wallet"].startswith(пр)]
        if not рр:
            md.append(f"| {пр} | — | нет в готовых данных (не в 543 / 133 или без числа) | | | | | | |")
            continue
        for H in ВЫХОДЫ:
            md.append(f"| {пр} | +{H} | {я(стат([р[H] for р in рр]))} |")
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-28_rannie.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
