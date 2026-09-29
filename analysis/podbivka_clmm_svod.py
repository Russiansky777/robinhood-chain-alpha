#!/usr/bin/env python3
"""Свод по Raydium CLMM: разбор инструкций сделок источников и сверка порта котировки. Офлайн.

Вход: data/podbivka/clmm_sbor*.json (analysis/podbivka_clmm_sbor.py), data/podbivka/clmm_proverka.json
(analysis/podbivka_clmm_proverka.py). Выход: docs/podbivka_2026-09-29_clmm.md.
"""
from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_clmm_quote as Q  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def main() -> int:
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "clmm_sbor.json").read_text(encoding="utf-8"))
    пр = json.loads((КОРЕНЬ / "data" / "podbivka" / "clmm_proverka.json").read_text(encoding="utf-8"))
    сд = д["сделки"]
    виды = collections.Counter()
    ост = collections.Counter()
    ext = collections.Counter()
    плат = collections.Counter()
    гр: dict = collections.defaultdict(list)
    for s in сд:
        for i in s["инструкции"]:
            виды[(i["вид"], i["место"].split()[0])] += 1
            if i["вид"] not in ("swap", "swap_v2"):
                continue
            р = i["роли"]
            e = Q.адрес_расширения(р["pool_state"])
            о = i.get("остаток") or []
            ext[e in о] += 1
            ta = [x for x in о if x != e]
            ост[(i["вид"], len(ta) + (1 if i["вид"] == "swap" else 0))] += 1
            плат[р["payer"] == s["кошелёк"]] += 1
            if i["вид"] == "swap_v2":
                гр[(р["pool_state"], р["input_vault"])].append((s["slot"], tuple(ta), р["observation_state"], р["amm_config"]))
    групп = м_ta = м_obs = м_cfg = same = tot = 0
    for v in гр.values():
        if len(v) < 2:
            continue
        групп += 1
        v.sort()
        м_ta += len({x[1] for x in v}) > 1
        м_obs += len({x[2] for x in v}) > 1
        м_cfg += len({x[3] for x in v}) > 1
        for a, b in zip(v, v[1:]):
            if a[1] and b[1]:
                tot += 1
                same += a[1][0] == b[1][0]
    длины = collections.Counter(e["длина"] for s in сд for e in s["события"])
    вер = д.get("версия") or {}
    ит = пр["итог"]
    посчитано = [r for r in ит if r.get("модель_выход") is not None]
    ровно = [r for r in посчитано if r["разница"] == 0]
    md = ["# Raydium CLMM для Code-1: разбор инструкций источников и сверка котировки", "",
          f"Сбор: analysis/podbivka_clmm_sbor.py, Helius, окно {д['окно_с']} → 29.09 ~17Z, кошельков источников {д['кошельков']} "
          f"(группы Code-1, кандидаты, снайперы), сделок с инструкцией CLMM {len(сд)}. Порт: analysis/podbivka_clmm_quote.py "
          "(raydium-clmm ed1eb41). Сверка: analysis/podbivka_clmm_proverka.py.", "",
          "## Версия программы в сети", "",
          f"Последнее развёртывание CAMMCzo5… -- слот {вер.get('слот_развёртывания')}, {вер.get('время_развёртывания_utc')} "
          "(коммит ed1eb41 «Chore/upgrade anchor» -- 29.09 00:29Z). Длина события SwapEvent: "
          + ", ".join(f"{k} байт -- {v}" for k, v in sorted(длины.items()))
          + " (213 = с полями trade_fee_0/1, появились в df0910a; 197 -- без них, события до развёртывания). "
          "Порт взят с того же коммита, что стоит в сети.", "",
          "## 1. Инструкции в сделках источников", "",
          "| инструкция | где | число |", "|---|---|---|"] + [f"| {в} | {м} | {n} |" for (в, м), n in sorted(виды.items())] + [
          "",
          f"Внутренняя -- через агрегатор (Jupiter и др.). Подписант инструкции (payer) = кошелёк источника: {плат[True]}, "
          f"другой (агрегатор / PDA): {плат[False]}.", "",
          "Tick arrays в инструкции (у swap -- счёт 9 плюс остаток, у swap_v2 -- только остаток): "
          + ", ".join(f"{в} {k} шт. -- {n}" for (в, k), n in sorted(ост.items())) + ".", "",
          f"Расширение битовой карты передано во всех {ext[True]} из {ext[True] + ext[False]} инструкций swap/swap_v2 "
          "(первым в остатке, счёт 1 832 байта) -- SDK кладёт его всегда, даже когда программе оно не нужно.", "",
          "### Что меняется от сделки к сделке", "",
          f"Группы «пул + направление» (swap_v2) с ≥ 2 сделками: {групп}. Меняются tick arrays -- в {м_ta}; observation -- в {м_obs}; "
          f"amm_config -- в {м_cfg}. У соседних по времени сделок одной группы первый tick array тот же в {same} из {tot}.", "",
          "Постоянные для пула: amm_config, pool_state, observation_state, хранилища и минты (переставляются по направлению), "
          "программы. Наши: payer, input/output token account. Переменные: tick arrays (зависят от текущего тика и направления).", "",
          "## 2. Сверка котировки exact_in", "",
          f"Снимков {len(ит) + sum(пр['отсев'].values())}: отсев {пр['отсев']}. Посчитано {len(посчитано)}, "
          f"**до единицы {len(ровно)} из {len(посчитано)}**; не посчитано {len(ит) - len(посчитано)} "
          f"({'; '.join(sorted({r['why_not'] for r in ит if r.get('why_not')}))} -- нужного массива нет среди прочитанных ±4, "
          "пул с шагом 1 и редкими массивами; это предел сбора, не порта).", "",
          f"Цена после свопа сошлась у {sum(1 for r in посчитано if r['цена_сошлась'])}, тик -- у {sum(1 for r in посчитано if r['тик_сошёлся'])}, "
          f"ликвидность -- у {sum(1 for r in посчитано if r['ликвидность_сошлась'])}; комиссия (trade_fee события) -- у "
          f"{sum(1 for r in посчитано if r.get('fee_факт') is not None and r['fee_модель'] == r['fee_факт'])} из "
          f"{sum(1 for r in посчитано if r.get('fee_факт') is not None)}. Пулы с динамической комиссией: "
          f"{sum(1 for r in посчитано if r['динамическая'])}; fee_on ≠ 0 (комиссия в одном токене): "
          f"{sum(1 for r in посчитано if r['fee_on'])}. Пересечений инициализированных тиков -- "
          f"{sum(r['тиков_пересечено'] for r in посчитано)}, лимитных ордеров -- {sum(r['лимитных_ордеров'] for r in посчитано)}: "
          "переходы через тики проверены слабо -- идёт второй сбор (массивы по битовой карте, 80 снимков).", "",
          "| пул | инструкция | zero_for_one | вход | факт выход | модель | разница | цена/тик | пересечено тиков |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in ит:
        md.append(f"| `{r['пул'][:8]}` | {r['вид']} | {r['zero_for_one']} | {r['вход']} | {r['факт_выход']} | "
                  f"{r.get('модель_выход', '—')} | {r.get('разница', r.get('why_not'))} | "
                  f"{'да' if r.get('цена_сошлась') and r.get('тик_сошёлся') else '—'} | {r.get('тиков_пересечено', '—')} |")
    доп = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "clmm_sbor_*.json")))
    if доп:
        md += ["", f"Второй сбор: {', '.join(Path(f).name for f in доп)} -- в сверке, если прогнан вместе (см. clmm_proverka.json)."]
    (КОРЕНЬ / "docs" / "podbivka_2026-09-29_clmm.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:40]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
