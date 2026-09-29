#!/usr/bin/env python3
"""Подбивка: lane_s0 -- S+0 против S+1…S+3 по цепи за 28.09 и «сразу за ним» против «конца слота» по архиву.

1. По цепи: сделки 28.09 из файла Code-1 (Правило 11, итог по нативному балансу) и
   лаг нашей покупки от покупки источника (podbivka_lane_s0_cepi.py, lag_28.json).
2. По архиву PumpApi, 7 суток (pyg4_*): сигналы 10 кошельков lane_s0 (порог 2 SOL-экв.),
   модель режима 1, билет 0.3: S0 -- сразу за ним, S0_дно -- конец слота, S1 -- слот
   S+1. Pump AMM -- не для вывода (лишний остаток E, архив видит не все свопы).
Выход: docs/podbivka_2026-09-29_lane_s0.md. Офлайн.
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import statistics
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
LANE_S0 = {
    "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ": "frank",
    "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB": "Brez",
    "Xk9onqHkpULDEYYN9ZPyM7Q9AfTNYUrsCkzywyqdMeb": "Xk9onq",
    "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8": "jg",
    "4vER1GJQs73HtN9oYRswHZV4PSe2dvWQ8NLFoDhXeZjm": "Bitman",
    "8RCEq8RrBJ1G6eji9vZqjtjQgkDjUHMysPtynZENWo7S": "Avocado",
    "5pHeNsWMVEi1cbMzLhgqABnhEUwRTSzy5vBfeGWyJfxS": "5pHeNs",
    "B8m6fDRcw9VnkqWBXo5MEh9uY8HSPCkFuH4NNv5pCiPk": "B8m6fDRc",
    "7JVQMwRj82STgsG57spj6vpE6XY3RqG8B64PczVc7jJr": "7JVQMwRj",
    "3Um4qsYQKYULYSJwRChReZtgsXu3kiGm6HNTvZpy9dYy": "3Um4qsYQ",
}
ТИПЫ = {"pump": "кривая pump.fun", "raydium-launchpad": "LaunchLab", "raydium-cpmm": "CPMM",
        "meteora-damm-v1": "DAMM v1", "meteora-damm-v2": "DAMM v2", "meteora-dbc": "DBC",
        "pump-amm": "Pump AMM (не для вывода)"}
WSOL = "So11111111111111111111111111111111111111112"
ГОР = (36, 72, 108, 150)   # 108 -- удержание живой полосы lane_s0 (hold 108 слотов)
ПОРОГ = 2.0     # min_target_sol группы lane_s0; в прогоне для источников порог сигнала 0 (--porog-dop 0)
ВХОДЫ = (("S0", "сразу за ним"), ("S0_дно", "конец слота"), ("S1", "S+1"))


def ячейка(v: list) -> str:
    v = sorted(x for x in v if x is not None)
    if not v:
        return "—"
    return (f"{len(v)} | {statistics.mean(v):+.1f} | {statistics.median(v):+.1f} | "
            f"{100 * sum(1 for x in v if x > 0) / len(v):.0f}%")


def по_цепи() -> list:
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "lane_s0" / "pravilo11_po_cepi_20260928.json").read_text(encoding="utf-8"))
    п_лаг = КОРЕНЬ / "data" / "podbivka" / "lane_s0" / "lag_28.json"
    лаг = {}
    if п_лаг.exists():
        лаг = {x["подпись_покупки"]: x for x in json.loads(п_лаг.read_text(encoding="utf-8"))["сделки"] if x.get("подпись_покупки")}
    md = ["## 1. По цепи, 28.09 (файл Code-1 «Правило 11 только по цепи»)", "",
          f"Период файла: с {д['с']}; сделок {len(д['сделки'])}. Итог -- по нативному балансу наших счетов, всё включено "
          "(комиссии, чаевые, рента). Лаг = слот нашей покупки − слот последней покупки источника того же минта до неё "
          "(по цепи, подписи минта).", ""]
    if not лаг:
        return md + ["Лаг ещё считается в облаке (analysis/podbivka_lane_s0_cepi.py).", ""]
    корз = collections.defaultdict(list)
    строки = []
    for сд in д["сделки"]:
        л = лаг.get(сд.get("подпись_покупки")) or {}
        k = л.get("лаг")
        if сд.get("итог_sol") is None:
            continue
        ошибка = bool(сд.get("покупка_с_ошибкой"))
        корз[(сд["группа"], "ошибка" if ошибка else ("?" if k is None else ("S+0" if k == 0 else ("S+1…S+3" if k <= 3 else "S+4 и дальше"))))].append(сд["итог_sol"])
        строки.append((сд["utc"][11:19], сд["минт"][:8], сд["источник"][:8], сд["группа"], k,
                       (f"{л.get('индекс_источника')} → {л.get('индекс_наш')} / {л.get('в_блоке')}" if k == 0 else ""),
                       сд["итог_sol"], сд.get("удержание_слотов"), л.get("why_not") or ("покупка с ошибкой" if ошибка else "")))
    md += ["| группа | лаг | n | сумма, SOL | в плюсе |", "|---|---|---|---|---|"]
    for (г, к), v in sorted(корз.items()):
        md.append(f"| {г} | {к} | {len(v)} | {sum(v):+.6f} | {sum(1 for x in v if x > 0)} из {len(v)} |")
    md += ["", "| время | минт | источник | группа | лаг, слотов | индекс ист. → наш / в блоке | итог, SOL | удержание | примечание |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in строки:
        md.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {'—' if r[4] is None else r[4]} | {r[5]} | {r[6]:+.6f} | {r[7] or '—'} | {r[8]} |")
    сверх = [r for r in строки if r[3] == "lane_s0" and r[4] is not None and r[4] > 3]
    if сверх:
        md += ["", "Лаг больше предела группы (max_slots_from_source 3): " + "; ".join(f"{r[0]} {r[1]} {r[2]} -- {r[4]} слотов" for r in сверх)
               + ". Через Helius лаг тот же; запись Code-1 (data/sdelki_polosy_2026-09-28.json: source_slot 451163726, entry_slot 451163731) даёт те же 5. Предел в Code-1 проверяется при отправке (bloom_own_send.py: слот сети − слот источника ≤ 3), а не по слоту, где сделка села; слота отправки в записи нет -- «отправили в пределе, село позже» не проверено."]
    md += ["", "Билеты разные (0.3 и 0.012 у двух сделок), суммы -- как есть по цепи."]
    return md + [""]


def по_архиву(префикс: str) -> list:
    сиг, видел, ошибки = [], set(), []
    не_sol = collections.Counter()
    перв = collections.defaultdict(collections.Counter)      # первые покупки ≥ 2 SOL за WSOL по типу пула
    видел_п = set()
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{префикс}_*.json.gz")))
    for f in файлы:
        a = json.loads(gzip.decompress(Path(f).read_bytes()))
        ошибки += [e.split(":")[0] for e in (a.get("счёт") or {}).get("ошибки") or []]
        for п in a.get("покупки_ист") or []:
            if п["trader"] in LANE_S0 and п.get("первая") and (п.get("sol") or 0) >= 2 and п["signature"] not in видел_п:
                видел_п.add(п["signature"])
                перв[п["trader"]][п.get("pool")] += 1
        for с in a.get("сигналы") or []:
            if (с["signature"] in видел or с.get("trader") not in LANE_S0 or (с.get("модель") or {}).get("why_not")
                    or (с.get("sol") or 0) < ПОРОГ):
                continue
            if с.get("quoteMint") != WSOL:
                не_sol[с["trader"]] += 1
                continue
            видел.add(с["signature"])
            сиг.append(с)
    пп = lambda с, k: (с["модель"].get("пп") or {}).get(k)  # noqa: E731  (нет ключа +108 в pyg4 -- ячейка «—»)
    md = [f"## 2. По архиву PumpApi, 7 суток ({префикс}), 10 кошельков lane_s0", "",
          f"Файлов суток {len(файлы)}; недочитанные часы: {', '.join(ошибки) if ошибки else 'нет'}. Сигналов (первая покупка "
          f"≥ 2 SOL-экв., котировка SOL): {len(сиг)}; с котировкой не SOL -- {sum(не_sol.values())}, в таблицы не входят "
          f"(" + ", ".join(f"{LANE_S0[w]} {n}" for w, n in не_sol.most_common()) + "). Модель режима 1, билет 0.3 SOL, п.п. чистыми. Ячейка: n | среднее | медиана | в плюс. "
          "Живая полоса держит 108 слотов -- столбец +108 (есть с прогона pyg6); +36, +72, +150 для сравнения. Сигналы -- только в пулах с моделью (кривая, LaunchLab, "
          "CPMM, DAMM v1, Pump AMM); DAMM v2, DLMM, DBC не моделируются -- их первые покупки в последнем столбце таблицы по кошелькам.", "",
          "| пул | вход | " + " | ".join(f"+{h}" for h in ГОР) + " |", "|---|---|" + "---|" * len(ГОР)]
    for p, имя in ТИПЫ.items():
        сс = [с for с in сиг if с["pool"] == p]
        if not сс:
            continue
        for вход, ви in ВХОДЫ:
            md.append(f"| {имя} ({len(сс)}) | {ви} | " + " | ".join(ячейка([пп(с, f'{вход}|0.3|{h}') for с in сс]) for h in ГОР) + " |")
    сек = {h: [((с.get("удержание") or {}).get("секунд_до") or {}).get(str(h)) for с in сиг] for h in (72, 108, 150)}
    if any(x is not None for v in сек.values() for x in v):
        md += ["", "Секунды от сигнала до слота s0+h по меткам времени архива (медиана / четверти, n): " + "; ".join(
            f"+{h} -- {statistics.median(v):.1f} с ({sorted(v)[len(v) // 4]:.1f}…{sorted(v)[3 * len(v) // 4]:.1f}), n {len(v)}"
            for h, v in ((h, [x for x in vv if x is not None]) for h, vv in сек.items()) if v) + "."]
    прочие = collections.Counter(с["pool"] for с in сиг if с["pool"] not in ТИПЫ)
    if прочие:
        md += ["", "Прочие пулы (не в таблице): " + ", ".join(f"{k} {n}" for k, n in прочие.most_common())]
    md += ["", "### По кошелькам (без Pump AMM, +108)", "",
           "| кошелёк | имя | сигналов | сразу за ним | конец слота | S+1 | первых покупок ≥ 2 SOL за WSOL, все пулы |",
           "|---|---|---|---|---|---|---|"]
    for w, имя in LANE_S0.items():
        сс = [с for с in сиг if с["trader"] == w and с["pool"] != "pump-amm"]
        md.append(f"| `{w}` | {имя} | {len(сс)} | " + " | ".join(ячейка([пп(с, f'{вход}|0.3|108') for с in сс]) for вход, _ in ВХОДЫ) + " | " +
                  (", ".join(f"{k} {n}" for k, n in перв[w].most_common()) or "0") + " |")
    return md + [""]


def main() -> int:
    import argparse  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="pyg4")
    а = р.parse_args()
    md = ["# Подбивка: lane_s0 -- S+0 против S+1…S+3 (цепь 28.09) и «сразу за ним» против «конца слота» (архив 7 суток)", "",
          "Для решения владельца по max_slots_from_source. Ничего не рекомендуется.", ""]
    md += по_цепи() + по_архиву(а.prefiks)
    (КОРЕНЬ / "docs" / "podbivka_2026-09-29_lane_s0.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
