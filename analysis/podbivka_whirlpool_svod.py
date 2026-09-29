#!/usr/bin/env python3
"""Свод по Orca Whirlpool: разбор инструкций сделок источников и сверка порта котировки. Офлайн.

Вход: data/podbivka/whirlpool_sbor.json, data/podbivka/whirlpool_proverka.json. Выход: docs/podbivka_2026-09-29_whirlpool.md.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_whirlpool_quote as Q  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def main() -> int:
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "whirlpool_sbor.json").read_text(encoding="utf-8"))
    пр = json.loads((КОРЕНЬ / "data" / "podbivka" / "whirlpool_proverka.json").read_text(encoding="utf-8"))
    виды, ост, плат, ор = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    гр: dict = collections.defaultdict(list)
    for s in д["сделки"]:
        for i in s["инструкции"]:
            виды[(i["вид"], i["место"].split()[0])] += 1
            if i["вид"] not in ("swap", "swap_v2"):
                continue
            р = i["роли"]
            плат[р["token_authority"] == s["кошелёк"]] += 1
            ост[(i["вид"], len(i.get("остаток") or []))] += 1
            ор[р.get("oracle") == Q.адрес_оракула(р["whirlpool"])] += 1
            гр[(р["whirlpool"], i["a_to_b"])].append((s["slot"], (р["tick_array_0"], р["tick_array_1"], р["tick_array_2"])))
    групп = м = same = tot = 0
    for v in гр.values():
        if len(v) < 2:
            continue
        групп += 1
        v.sort()
        м += len({x[1] for x in v}) > 1
        for a, b in zip(v, v[1:]):
            tot += 1
            same += a[1] == b[1]
    длины = collections.Counter(e["длина"] for s in д["сделки"] for e in s["события"])
    вер = д.get("версия") or {}
    ит = пр["итог"]
    пос = [r for r in ит if r.get("модель_выход") is not None]
    виды_м = collections.Counter(tuple(r["виды_массивов"]) for r in пос)
    md = ["# Orca Whirlpool для Code-1: разбор инструкций источников и сверка котировки", "",
          f"Сбор: analysis/podbivka_whirlpool_sbor.py, Helius, окно {д['окно_с']} → 29.09 ~19Z, кошельков {д['кошельков']}, "
          f"сделок с инструкцией Whirlpool {len(д['сделки'])}. Порт: analysis/podbivka_whirlpool_quote.py (orca-so/whirlpools f4b99e79). "
          "Сверка: analysis/podbivka_whirlpool_proverka.py.", "",
          "## Версия программы в сети", "",
          f"Последнее развёртывание whirLbMi… -- слот {вер.get('слот_развёртывания')}, {вер.get('время_развёртывания_utc')}. "
          f"Длина события Traded: {dict(длины)} байт. Сверка ниже -- проверка, что порт с HEAD исходников совпадает с развёрнутым.", "",
          "## 1. Инструкции в сделках источников", "", "| инструкция | где | число |", "|---|---|---|"] + [
          f"| {в} | {мм} | {n} |" for (в, мм), n in sorted(виды.items())] + [
          "", f"Подписант (token_authority) = кошелёк источника: {плат[True]}, другой (агрегатор): {плат[False]}. "
          f"Счёт oracle = PDA [\"oracle\", пул] во всех {ор[True]} из {ор[True] + ор[False]} (передаётся всегда, даже у пулов без адаптивной комиссии -- "
          "тогда это пустой счёт). Остаток (remaining accounts, swap_v2 -- дополнительные tick arrays / transfer hook): "
          + ", ".join(f"{в} {k} шт. -- {n}" for (в, k), n in sorted(ост.items())) + ".", "",
          "Счета swap_v2 по порядку: token_program_a, token_program_b, memo_program, token_authority, whirlpool, token_mint_a, "
          "token_mint_b, token_owner_account_a, token_vault_a, token_owner_account_b, token_vault_b, tick_array_0..2, oracle "
          "(у swap: token_program, token_authority, whirlpool, owner_a, vault_a, owner_b, vault_b, tick_array_0..2, oracle).", "",
          "### Что меняется от сделки к сделке", "",
          f"Группы «пул + направление» с ≥ 2 сделками: {групп}; tick arrays меняются в {м}; у соседних сделок все три те же в {same} из {tot}. "
          "Остальные счета постоянны для пула (хранилища и минты -- поля пула, oracle -- PDA). Последовательность из трёх массивов "
          "программа строит сама от текущего тика (sparse_swap.rs): среди переданных tick_array_0..2 (и дополнительных) должны быть PDA "
          "этих трёх стартов, порядок не важен, несозданный массив передаётся адресом и считается пустым.", "",
          "## 2. Сверка котировки exact_in", "",
          f"Снимков {len(д['снимки'])}: отсев {пр['отсев']} (цена до сделки в событии Traded ≠ прочитанной -- между чтением и сделкой "
          f"было другое изменение пула). Посчитано {len(пос)}, **до единицы {sum(1 for r in пос if r['разница'] == 0)} из {len(пос)}**; "
          f"post_sqrt_price сошлась у {sum(1 for r in пос if r['цена_сошлась'])}, lp_fee и protocol_fee -- у "
          f"{sum(1 for r in пос if r['lp_fee_сошлась'] and r['protocol_fee_сошлась'])}. Пулы с адаптивной комиссией (oracle): "
          f"{sum(1 for r in пос if r['адаптивная'])}. Виды массивов последовательности: "
          + "; ".join(f"{'/'.join(k)} -- {n}" for k, n in виды_м.most_common()) + ". "
          f"Пересечений инициализированных тиков -- {sum(r['тиков_пересечено'] for r in пос)}: переходы через тики не проверены.", "",
          "| пул | инструкция | a_to_b | вход | факт выход | модель | разница | адаптивная |", "|---|---|---|---|---|---|---|---|"]
    for r in ит:
        md.append(f"| `{r['пул'][:8]}` | {r['вид']} | {r['a_to_b']} | {r['вход']} | {r['факт_выход']} | {r.get('модель_выход', '—')} | "
                  f"{r.get('разница', r.get('why_not'))} | {r['адаптивная']} |")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-29_whirlpool.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:30]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
