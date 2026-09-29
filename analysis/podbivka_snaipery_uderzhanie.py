#!/usr/bin/env python3
"""Подбивка: удержание после покупки снайперских источников -- архив PumpApi, 7 суток (pyg5_*). Офлайн.

По покупкам источника за WSOL (покупки_ист прогона podbivka_arhiv_den.py --istochniki):
  * первая чужая продажа от 0.5 SOL-экв. -- через сколько слотов (кошелёк по трейдеру);
  * пик цены пула в s0..s0+150 -- слот и величина к цене после его покупки.
Пять снайперских источников (sniper_src Code-1) -- отдельно; группы Code-1 и кандидаты -- для сравнения.
Pump AMM -- архив видит не все свопы пула (продажи через чужие программы), отдельной строкой.
Выход: docs/podbivka_2026-09-29_snaipery_uderzhanie.md.
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
СНАЙПЕРЫ = ["6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy", "BCrTEXmWutwPz8qv6w1S5gDbaLnSLpXKM5kSGVWyyfxu",
            "DAEdBmTPEKM6xkwfzC3d411QUe6coKpkND6UURa4CvHC", "JDFDma1TMb1tWNFY1pruwCsHBybMdzwxveythZB2dcaG",
            "AFmiexHwMBFjKY7N9spbYjeKCr2nmj6k7zmAaamcjkVy"]
ОКНО = 150
ТЕКУЩЕЕ = 12      # удержание sniper_src сейчас, слотов


def мед(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def ф(x, fmt=".0f"):
    return "—" if x is None else format(x, fmt)


def строка(имя: str, пп: list) -> str:
    уд = [п["удержание"] for п in пп if not (п.get("удержание") or {}).get("why_not")]
    if not уд:
        return f"| {имя} | {len(пп)} | 0 | — | — | — | — | — | — |"
    пр = [u["первая_продажа_05_слотов"] for u in уд]
    без = sum(1 for x in пр if x is None)
    до12 = sum(1 for x in пр if x is not None and x <= ТЕКУЩЕЕ)
    пик = [u["пик_слотов"] for u in уд]
    пик_пп = [u["пик_пп"] for u in уд]
    q = sorted(x for x in пр if x is not None)
    кв = f"{q[len(q) // 4]} / {q[(3 * len(q)) // 4]}" if len(q) >= 4 else "—"
    return (f"| {имя} | {len(пп)} | {len(уд)} | {ф(мед(пр))} ({кв}) | {100 * без / len(уд):.0f}% | "
            f"{100 * до12 / len(уд):.0f}% | {ф(мед(пик))} | {100 * sum(1 for x in пик if x <= ТЕКУЩЕЕ) / len(уд):.0f}% | "
            f"{ф(мед(пик_пп), '+.1f')} |")


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="pyg6")
    а = р.parse_args()
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.prefiks}_*.json.gz")))
    пок, видел, ошибки = [], set(), []
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        ошибки += [e.split(":")[0] for e in (д.get("счёт") or {}).get("ошибки") or []]
        for п in д.get("покупки_ист") or []:
            к = (п["signature"], п["trader"], п.get("poolId"))
            if к in видел or "удержание" not in п:
                continue
            видел.add(к)
            пок.append(п)
    кр = lambda п: п.get("pool") == "pump"  # noqa: E731
    шапка = ["| кто | покупок | с рядом | первая чужая продажа ≥ 0.5 SOL, слотов: медиана (четверти) | не было за 150 | "
             f"≤ {ТЕКУЩЕЕ} слотов | пик, слот (медиана) | пик ≤ {ТЕКУЩЕЕ} | пик, п.п. (медиана) |",
             "|---|---|---|---|---|---|---|---|---|"]
    md = ["# Подбивка: удержание после покупки снайперских источников -- архив 7 суток", "",
          f"Прогон {а.prefiks}, файлов суток {len(файлы)}; недочитанные часы: {', '.join(sorted(set(ошибки))) or 'нет'}. "
          f"Покупки источника за WSOL, все размеры, первая и не первая. Окно {ОКНО} слотов. Сейчас удержание sniper_src -- "
          f"{ТЕКУЩЕЕ} слотов. Пик -- максимум цены пула после его покупки (к цене сразу после неё); если цена только падала, "
          "пик = 0 слотов. «С рядом» -- покупка найдена в ряду пула.", "",
          "## 1. Пять снайперских источников, кривая pump.fun", ""] + шапка
    for w in СНАЙПЕРЫ:
        md.append(строка(f"`{w}`", [п for п in пок if п["trader"] == w and кр(п)]))
    md.append(строка("**все пять**", [п for п in пок if п["trader"] in СНАЙПЕРЫ and кр(п)]))
    md += ["", "## 2. Пять снайперских источников по типу пула", ""] + шапка
    по_типу = collections.defaultdict(list)
    for п in пок:
        if п["trader"] in СНАЙПЕРЫ:
            по_типу[п.get("pool")].append(п)
    for т, пп in sorted(по_типу.items(), key=lambda kv: -len(kv[1])):
        md.append(строка(f"{т}" + (" (архив видит не все свопы)" if т == "pump-amm" else ""), пп))
    md += ["", "## 3. Для сравнения: прочие источники (группы Code-1 и кандидаты), кривая pump.fun", ""] + шапка
    прочие = collections.defaultdict(list)
    for п in пок:
        if п["trader"] not in СНАЙПЕРЫ and кр(п):
            прочие[п["trader"]].append(п)
    for w, пп in sorted(прочие.items(), key=lambda kv: -len(kv[1])):
        if len(пп) >= 5:
            md.append(строка(f"`{w[:8]}`", пп))
    md.append(строка("**все прочие**", [п for пп in прочие.values() for п in пп]))
    (КОРЕНЬ / "docs" / "podbivka_2026-09-29_snaipery_uderzhanie.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
