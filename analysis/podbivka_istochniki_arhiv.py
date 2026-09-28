#!/usr/bin/env python3
"""Подбивка: источники по архиву PumpApi -- таблица «вершина / за ним никто / продажи в
его слоте» (офлайн). Вход -- data/podbivka/arhiv_den/<префикс>*.json.gz (покупки_ист
из podbivka_arhiv_den.py --istochniki). Кошелёк -- по трейдеру (breakdown), не по
плательщику комиссии.

По каждому кошельку (группы Code-1 + кандидаты, data/podbivka/istochniki_code1_kandidaty.json),
покупки в SOL-пулах:
  * покупок (все / от 2 SOL);
  * «вершина» -- доля покупок после роста цены пула ≥ 20 % за 750 слотов (≈ 5 мин) до
    покупки (цена перед покупкой к наименьшей за окно);
  * «за ним никто» -- доля покупок, после которых за 15 с не было ни одной покупки от
    0.5 SOL другого кошелька в том же пуле;
  * «продажи в его слоте» -- доля покупок, после которых в том же слоте (порядок файла,
    timestamp мс -- оценка) были продажи других кошельков, и медиана SOL этих продаж.
Архив не видит часть свопов Pump AMM через сторонние программы.
Выход: docs/podbivka_<метка>_istochniki_arhiv.md.
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


def доля(сс: list, усл) -> str:
    сс = [x for x in сс if усл(x) is not None]
    return f"{100 * sum(1 for x in сс if усл(x)) / len(сс):.0f} %" if сс else "—"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="pyg4_")
    р.add_argument("--metka", default="2026-09-28")
    а = р.parse_args()
    сп = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    канд = set(сп["кандидаты"])
    кошельки = sorted(set(сп["группы_code1"]) | канд)
    адр = json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.prefiks}*.json.gz")))
    пок, видел, ошибки, t = [], set(), [], []
    for f in файлы:
        a = json.loads(gzip.decompress(Path(f).read_bytes()))
        ошибки += [e.split(":")[0] for e in (a.get("счёт") or {}).get("ошибки") or []]
        for x in a.get("покупки_ист") or []:
            if x["signature"] + x["trader"] not in видел:
                видел.add(x["signature"] + x["trader"])
                пок.append(x)
                t.append(x.get("timestamp") or 0)
    md = [f"# Источники по архиву PumpApi: вершина, за ним никто, продажи в его слоте ({а.metka})", "",
          f"Архив: {len(файлы)} файлов суток ({а.prefiks}*), " +
          (f"{time.strftime('%d.%m %H:%M', time.gmtime(min(t) / 1000))} – {time.strftime('%d.%m %H:%M', time.gmtime(max(t) / 1000))} UTC; "
           if t else "") + "недочитанные часы (обрыв связи): " + (", ".join(ошибки) if ошибки else "нет") + ". "
          "Покупки в SOL-пулах (pump, pump-amm, raydium-cpmm, raydium-launchpad, meteora-damm-v1). Кошелёк -- по трейдеру. "
          "«Вершина» -- рост цены пула ≥ 20 % за 750 слотов (≈ 5 мин) до покупки; «никто» -- за 15 с ни одной покупки "
          "≥ 0.5 SOL другого кошелька в том же пуле; «продажи в слоте» -- продажи других кошельков в его слоте после его "
          "покупки (порядок внутри слота -- по timestamp, оценка). Архив видит не все свопы Pump AMM.", "",
          "| кошелёк | группа | покупок (от 2 SOL) | вершина | вершина, от 2 SOL | никто ≥ 0.5 за 15 с | "
          "продажи в его слоте: доля | медиана SOL продаж в слоте |", "|---|---|---|---|---|---|---|---|"]
    по_к: dict = {}
    for x in пок:
        по_к.setdefault(x["trader"], []).append(x)
    строки = []
    for w in кошельки:
        сс = по_к.get(w, [])
        с2 = [x for x in сс if (x.get("sol") or 0) >= 2]
        вер = lambda x: (x["рост_до_750"] >= 20) if x.get("рост_до_750") is not None else None  # noqa: E731
        ник = lambda x: (x["следом_05_15с"] == 0) if x.get("следом_05_15с") is not None else None  # noqa: E731
        пр = lambda x: (x["продаж_в_слоте"] > 0) if x.get("продаж_в_слоте") is not None else None  # noqa: E731
        сумм = [x["продажи_в_слоте_sol"] for x in сс if (x.get("продаж_в_слоте") or 0) > 0]
        г = "кандидат" if w in канд else ", ".join(g for g in ((адр.get(w) or {}).get("группы") or []) if g in ("leader", "batch5", "lane_s0"))
        имя = (адр.get(w) or {}).get("имя")
        строки.append(len(сс))
        md.append(f"| `{w[:8]}`{(' ' + имя) if имя else ''} | {г or '—'} | {len(сс)} ({len(с2)}) | {доля(сс, вер)} | {доля(с2, вер)} | "
                  f"{доля(сс, ник)} | {доля(сс, пр)} | {statistics.median(сумм):.2f} |" if сумм else
                  f"| `{w[:8]}`{(' ' + имя) if имя else ''} | {г or '—'} | {len(сс)} ({len(с2)}) | {доля(сс, вер)} | {доля(с2, вер)} | "
                  f"{доля(сс, ник)} | {доля(сс, пр)} | — |")
    все = [x for w in кошельки for x in по_к.get(w, [])]
    md += ["", f"Всего покупок этих кошельков: {len(все)}; кошельков без покупок в SOL-пулах за период: "
               f"{sum(1 for n in строки if n == 0)}.", ""]
    out = КОРЕНЬ / "docs" / f"podbivka_{а.metka}_istochniki_arhiv.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
