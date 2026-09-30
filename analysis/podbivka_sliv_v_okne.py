#!/usr/bin/env python3
"""Слив в окне: чужие продажи того же минта в слотах +1…+12 от нашей покупки и итог сделки по цепи. Офлайн.

Вход: data/podbivka/sliv_vhod_<дата>.json (сделки полосы Code-1 с buy_sig и sell_sig) и data/podbivka/sliv_tx_<дата>.json.gz
(analysis/podbivka_tx_po_podpisyam.py --okno --plus 12: все успешные транзакции минта и хранилища токена пула в слотах
покупки … +12, покупка и продажа). По сделке:
  * резерв -- баланс хранилища токена пула (pool_vault) сразу после нашей покупки (postTokenBalances покупки);
  * продажа -- у подписанта (не наш кошелёк) баланс минта уменьшился; SOL -- изменение его лампортов + WSOL;
  * доля слива -- проданные токены в +1…+12 / резерв;
  * итог по цепи -- изменение SOL нашего кошелька (лампорты + WSOL) в транзакциях покупки и продажи вместе
    (с комиссиями, чаевыми и рентой).
Выход: docs/podbivka_<дата>_sliv_v_okne.md.
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_dve_sdelki as D  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ОКНО = 12
ПОРОГ = 0.05


def sol_наш(т: dict) -> float:
    кл = D.ключи(т)
    if D.НАШ not in кл:
        return 0.0
    и = кл.index(D.НАШ)
    return (т["meta"]["postBalances"][и] - т["meta"]["preBalances"][и] + D.дельта(т, D.НАШ, D.WSOL)) / 1e9


def резерв(т: dict, хранилище: str) -> int | None:
    кл = D.ключи(т)
    for b in т["meta"].get("postTokenBalances") or []:
        if кл[b["accountIndex"]] == хранилище:
            return int(b["uiTokenAmount"]["amount"])
    return None


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--data", default="2026-09-30")
    а = р.parse_args()
    вход = json.loads((П / f"sliv_vhod_{а.data}.json").read_text(encoding="utf-8"))["сделки"]
    д = json.loads(gzip.decompress((П / f"sliv_tx_{а.data}.json.gz").read_bytes()))
    txs, окна = д["транзакции"], д["окна"]
    ряды = []
    for с in вход:
        о = окна.get(с["buy_sig"]) or {}
        пок, прод = txs.get(с["buy_sig"]), txs.get(с["sell_sig"])
        if о.get("why_not") or not пок or not прод:
            ряды.append({"с": с, "why_not": о.get("why_not") or "покупка или продажа не прочитана"})
            continue
        s0 = пок["slot"]
        рез = резерв(пок, с["pool_vault"])
        продажи = []
        for подп, т in txs.items():
            if not т or подп in (с["buy_sig"], с["sell_sig"]) or not (s0 <= т["slot"] <= s0 + ОКНО):
                continue
            кл = D.ключи(т)
            for кто in D.подписанты(т):
                if кто == D.НАШ:
                    continue
                dt = D.дельта(т, кто, с["mint"])
                if dt < 0:
                    и = кл.index(кто)
                    sol = (т["meta"]["postBalances"][и] - т["meta"]["preBalances"][и] + D.дельта(т, кто, D.WSOL)) / 1e9
                    продажи.append({"от": т["slot"] - s0, "токенов": -dt, "sol": sol})
                    break
        окно = [x for x in продажи if x["от"] >= 1]
        доля = (sum(x["токенов"] for x in окно) / рез) if рез else None
        ряды.append({"с": с, "slot": s0, "слот_продажи": прод["slot"], "резерв": рез, "в_слоте_покупки": sum(1 for x in продажи if x["от"] == 0),
                     "продаж": len(окно), "sol": round(sum(x["sol"] for x in окно), 4), "доля": доля,
                     "первая": min((x["от"] for x in окно), default=None), "итог_цепь": round(sol_наш(пок) + sol_наш(прод), 6),
                     "подписей": о.get("подписей"), "верх_окна": о.get("верх_окна")})
    ок = [x for x in ряды if not x.get("why_not")]
    со_р = [x for x in ок if x["доля"] is not None and x["доля"] >= ПОРОГ]
    без_р = [x for x in ок if x["доля"] is not None and x["доля"] < ПОРОГ]
    со, без = [x["итог_цепь"] for x in со_р], [x["итог_цепь"] for x in без_р]
    пп = lambda рр: [x["итог_цепь"] / x["с"]["size_sol"] * 100 for x in рр if x["с"].get("size_sol")]  # noqa: E731
    мпп = lambda v: f"{statistics.median(v):+.1f} %" if v else "—"  # noqa: E731
    плюс = lambda v: f"{sum(1 for t in v if t > 0)} из {len(v)}"  # noqa: E731
    мед = lambda v: f"{statistics.median(v):+.4f}" if v else "—"  # noqa: E731
    md = [f"# Слив в окне: чужие продажи того же минта в слотах +1…+{ОКНО} от нашей покупки ({а.data})", "",
          f"Сделки полосы Code-1: data/sdelki_polosy_{а.data}.json ветки Code-1 (строки с покупкой и продажей: {len(вход)}). "
          f"Транзакции минта и хранилища токена пула в слотах покупки … +{ОКНО} прочитаны по цепи "
          "(analysis/podbivka_tx_po_podpisyam.py --okno --plus 12), разбор -- analysis/podbivka_sliv_v_okne.py. Продажа -- у "
          "подписанта (не наш кошелёк) уменьшился баланс минта; SOL -- изменение его лампортов + WSOL. Доля слива -- проданные "
          "токены в +1…+12 / остаток хранилища токена пула сразу после нашей покупки. Итог по цепи -- изменение SOL нашего "
          "кошелька в транзакциях покупки и продажи (с комиссиями, чаевыми, рентой). Продажи в слоте покупки (+0) -- отдельным "
          "столбцом: порядок внутри слота неизвестен. Ничего не рекомендуется.", "",
          f"**Одной строкой:** при сливе ≥ {int(ПОРОГ * 100)} % резерва в +1…+{ОКНО} медиана итога {мед(со)} SOL "
          f"({мпп(пп(со_р))} билета, в плюсе {плюс(со)}; n = {len(со)}), без такого слива {мед(без)} SOL ({мпп(пп(без_р))} "
          f"билета, в плюсе {плюс(без)}; n = {len(без)}). Билеты разные (0.01–0.3 SOL), выборка мала.", "",
          "| время UTC | группа | минт | билет | продаж +1…+12 | SOL | доля резерва | первая чужая продажа | продаж в слоте покупки | "
          "наша продажа, слот | итог по цепи, SOL | % билета | итог у Code-1, SOL |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in ряды:
        с = x["с"]
        if x.get("why_not"):
            md.append(f"| {с['utc'][11:16]} | {с['group']} | `{с['mint'][:8]}` | {с['size_sol']} | {x['why_not']} ||||||| {с.get('итог_sol')} |")
            continue
        дл = "—" if x["доля"] is None else f"{100 * x['доля']:.2f} %"
        md.append(f"| {с['utc'][11:16]} | {с['group']} | `{с['mint'][:8]}` | {с['size_sol']} | {x['продаж']} | {x['sol']} | {дл} | "
                  f"{'—' if x['первая'] is None else '+' + str(x['первая'])} | {x['в_слоте_покупки']} | +{x['слот_продажи'] - x['slot']} | "
                  f"{x['итог_цепь']:+.6f} | {x['итог_цепь'] / с['size_sol'] * 100:+.1f} | {с.get('итог_sol')} |")
    (КОРЕНЬ / "docs" / f"podbivka_{а.data}_sliv_v_okne.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
