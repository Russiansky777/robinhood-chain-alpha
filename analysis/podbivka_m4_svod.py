#!/usr/bin/env python3
"""Подбивка: сводка пост-обработки m4 (data/podbivka/postobr/m4_*.jsonl), офлайн.

п.12  S+0 (сразу после источника) против нижней границы «S+0 после всех свопов
      слота s0»: выход +72 / +150.
п.14  путь цены: конец s0, s0+1, s0+2 в п.п. к цене сразу после источника
      (спот-цена пула по состоянию), медиана и p90 по группам.
п.19  билет 0.5 / 1 / 2 / 3 SOL при S+0 (группа «толпа > 10» и другие).
      Модель симулятора: наша покупка ВСТАВЛЕНА в состояние выхода, поэтому
      проскальзывание круга платится один раз. Отдельной колонкой -- оценка
      проскальзывания продажи, если наш след в пуле рассосался:
      b / (резерв котировки на +72 + b), для кривой pump.fun резерв + 30 SOL
      виртуальных.
Группы: не продал / продал (по покупке), толпа > 10 (кошельки), 14 кандидатов.
"""
from __future__ import annotations

import glob
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_tablicy as T  # noqa: E402

КОРЕНЬ = T.КОРЕНЬ
ПЫЛЬ_ПП = 10_000
ВИРТ_КРИВОЙ = 30 * 10**9


def стат(xs: list) -> dict:
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return {"n": 0}
    отбр = math.ceil(0.05 * n)
    ус = xs[:n - отбр]
    return {"n": n, "ср": round(sum(xs) / n, 2), "ус": round(sum(ус) / len(ус), 2) if ус else None,
            "мед": round(statistics.median(xs), 2), "p90": round(xs[min(n - 1, int(0.9 * n))], 2),
            "плюс": round(sum(1 for x in xs if x > 0) / n, 3)}


def ф(v, зн="+.2f"):
    return "—" if v is None else format(v, зн)


def main() -> int:
    строки = {}
    for f in sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "postobr" / "m4_*.jsonl"))):
        for l in open(f, encoding="utf-8"):
            r = json.loads(l)
            строки[r["signature"]] = r
    покупки = {}
    толпа: dict = {}
    # Проба -- подмножество koshelki (те же 10 кошельков, ранний код): не читается.
    for к in T.читать(T.каталог_задачи("koshelki")):
        адрес = (к.get("строка") or {}).get("address")
        for п in к.get("покупки") or []:
            покупки[п["signature"]] = п
            с = п.get("sim") or {}
            if с.get("толпа_s0_2") is not None and not с.get("why_not"):
                толпа.setdefault(адрес, []).append(с["толпа_s0_2"])
    толпа_кош = {a for a, v in толпа.items() if len(v) >= 5 and statistics.median(v) > 10}
    канд = {x["address"] for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "candidates_2026-09-27.json")
                                             .read_text(encoding="utf-8"))["верхние"]}
    годные = []
    for sig, r in строки.items():
        if r.get("why_not") or not r.get("доп"):
            continue
        s0 = ((r.get("чистый_пп") or {}).get("S0") or {}).get("72")
        if r.get("режим") == "price" and s0 is not None and s0 > ПЫЛЬ_ПП:
            continue
        годные.append((sig, r))

    def группа(имя: str) -> list:
        из_ = []
        for sig, r in годные:
            п = покупки.get(sig) or {}
            if имя == "не продал" and п.get("продал_в_окне") is False \
                    or имя == "продал" and п.get("продал_в_окне") is True \
                    or имя == "толпа > 10" and r["wallet"] in толпа_кош \
                    or имя == "14 кандидатов" and r["wallet"] in канд \
                    or имя == "все":
                из_.append(r)
        return из_

    ГР = ("все", "не продал", "продал", "толпа > 10", "14 кандидатов")
    md = ["# Подбивка: пост-обработка m4 -- п.12, п.14, п.19", "",
          f"Строк m4: {len(строки)} из 1768; с числом: {len(годные)}; "
          f"без числа: {sum(1 for r in строки.values() if r.get('why_not'))}. 0.5 SOL, п.п. чистыми, "
          "усечённое -- без ceil(5 % n) лучших.", ""]

    # п.12
    md += ["## п.12 S+0 сразу после источника против S+0 после всех свопов слота s0", "",
           "| группа | выход | n | S+0 ср | S+0 ус. | S+0 мед | конец слота ср | ус. | мед | разница ср |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for г in ГР:
        rr = группа(г)
        for H in ("72", "150"):
            пары = [(r["чистый_пп"]["S0"].get(H), (r["доп"]["S0_конец_слота"] or {}).get(H)) for r in rr]
            пары = [(a, b) for a, b in пары if a is not None and b is not None]
            a, b = стат([x for x, _ in пары]), стат([y for _, y in пары])
            md.append(f"| {г} | +{H} | {a.get('n', 0)} | {ф(a.get('ср'))} | {ф(a.get('ус'))} | {ф(a.get('мед'))} | "
                      f"{ф(b.get('ср'))} | {ф(b.get('ус'))} | {ф(b.get('мед'))} | "
                      f"{ф((b['ср'] - a['ср']) if a.get('n') else None)} |")
    сл = стат([r["доп"]["S0_конец_слота"].get("свопов_после_источника_в_слоте") for r in группа("все")])
    md += ["", f"Свопов пула в слоте s0 после источника: медиана {сл.get('мед')}, p90 {сл.get('p90')}, среднее {сл.get('ср')}.", ""]

    # п.14
    md += ["## п.14 Путь цены: п.п. к цене сразу после источника (спот пула)", "",
           "| группа | n | конец s0 мед | p90 | конец s0+1 мед | p90 | конец s0+2 мед | p90 | свопов s0 / s0+1 / s0+2 (мед) |",
           "|---|---|---|---|---|---|---|---|---|"]
    итог14 = {}
    for г in ГР:
        rr = [r for r in группа(г) if (r["доп"].get("путь_цены") or {}).get("конец_s0") is not None]
        с = {k: стат([r["доп"]["путь_цены"].get(k) for r in rr]) for k in ("конец_s0", "конец_s1", "конец_s2")}
        св = {k: стат([r["доп"]["путь_цены"]["свопов"].get(k) for r in rr]) for k in ("s0", "s1", "s2")}
        итог14[г] = с
        md.append(f"| {г} | {len(rr)} | " + " | ".join(f"{ф(с[k].get('мед'))} | {ф(с[k].get('p90'))}"
                                                      for k in ("конец_s0", "конец_s1", "конец_s2"))
                  + f" | {св['s0'].get('мед')} / {св['s1'].get('мед')} / {св['s2'].get('мед')} |")
    md.append("")

    # п.19
    md += ["## п.19 Размер билета при S+0", "",
           "Модель: наша покупка вставлена в состояние выхода (круг платит проскальзывание один раз). "
           "«След рассосался» -- оценка доп. потери на продаже: медиана b / (резерв на +72 + b), п.п.", "",
           "| группа | билет, SOL | выход | n | среднее | усечённое | медиана | в плюс | след рассосался: доп. потеря, мед |",
           "|---|---|---|---|---|---|---|---|---|"]
    for г in ("толпа > 10", "не продал", "14 кандидатов"):
        rr = группа(г)
        for H in ("72", "150"):
            for b in ("0.5", "1", "2", "3"):
                if b == "0.5":
                    xs = [r["чистый_пп"]["S0"].get(H) for r in rr]
                else:
                    xs = [((r["доп"].get("билеты_S0") or {}).get(b) or {}).get(H) for r in rr]
                с = стат(xs)
                потери = []
                for r in rr:
                    рез = r["доп"].get("резерв_72")
                    if not рез:
                        continue
                    рез = рез + (ВИРТ_КРИВОЙ if r.get("режим") == "curve" else 0)
                    лам = float(b) * 1e9
                    потери.append(100 * лам / (рез + лам))
                пт = стат(потери)
                md.append(f"| {г} | {b} | +{H} | {с.get('n', 0)} | {ф(с.get('ср'))} | {ф(с.get('ус'))} | "
                          f"{ф(с.get('мед'))} | {('%.0f%%' % (100 * с['плюс'])) if с.get('n') else '—'} | "
                          f"{ф(-пт['мед'] if пт.get('n') else None)} |")
    md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-27_m4.md").write_text("\n".join(md), encoding="utf-8")
    print(f"m4: строк {len(строки)}, с числом {len(годные)}, толпа>10 кошельков {len(толпа_кош)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
