#!/usr/bin/env python3
"""DLMM без чтений перед сборкой: котировка только по событию предыдущего свопа пула. Офлайн.

Вход: data/podbivka/dlmm_bez_chteniy_sbor.json (сбор analysis/podbivka_dlmm_bez_chteniy_sbor.py) и
data/podbivka/dlmm_proverka_mnogo4.json (46 свопов сверки v6: вход, факт выхода, котировка по прочитанному состоянию).
Модель строителя без getMultipleAccounts:
  * корзина -- end_bin_id события предыдущего успешного свопа того же пула (Swap; у swap2 -- его событие Swap);
  * ставка комиссии -- fee_bps того же события (полная ставка, точность 1e9, как total_fee в lb_pair.rs);
  * bin_step -- постоянное поле пула (кэш);
  * налог Token-2022 на входе известен строителю по минту: вход пула = amount_in − (amount_in − прирост хранилища − host_fee);
  * содержимого корзин нет -- весь вход меняется по цене одной корзины (analysis/podbivka_dlmm_quote.py: цена_корзины,
    amount_out; комиссия с входа вверх, как compute_fee_from_amount).
Итог на пол min_out = модель × (1 − запас): факт ≥ пола -- сделка прошла бы («в пол»), факт < пола -- отказ по
проскальзыванию; отдельно -- ошибка модели > 1 % по модулю. Для сравнения -- те же числа для котировки по прочитанному
состоянию (сверка v6).
Выход: docs/podbivka_2026-09-30_dlmm_bez_chteniy.md.
"""
from __future__ import annotations

import base64
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_dlmm_quote as Q  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ЗАПАСЫ = (0.05, 0.10)


def модель(ряд: dict, свер: dict, пул_b64: str | None) -> dict:
    пред = ряд.get("предыдущий_своп")
    if not пред:
        return {"why_not": ряд.get("why_not") or "нет предыдущего свопа"}
    ев = [e for e in пред["события"] if e.get("вид") == "Swap" and e.get("fee_bps") is not None]
    if not ев or not пул_b64:
        return {"why_not": "нет события Swap с fee_bps" if not ев else "пул не прочитан"}
    e = ев[-1]                                              # последний своп пула в той транзакции
    lb = Q.разобрать_пул(base64.b64decode(пул_b64))
    вход = свер["вход"]
    д = свер.get("вход_по_хранилищу") or {}
    прирост = д.get("dx") if свер["swap_for_y"] else д.get("dy")
    налог = 0
    if прирост is not None and прирост > 0:
        налог = max(0, вход - прирост - (свер.get("host_fee") or 0))
    в_пул = вход - налог
    ставка = e["fee_bps"]
    комиссия = (в_пул * ставка + Q.FEE_PRECISION - 1) // Q.FEE_PRECISION
    цена = Q.цена_корзины(e["end"], lb["bin_step"])
    out = Q.amount_out(в_пул - комиссия, цена, свер["swap_for_y"], False)
    return {"модель": out, "корзина_пред": e["end"], "ставка_пред": ставка, "налог_вход": налог,
            "слотов_от_пред": свер["slot_сделки"] - (пред.get("slot") or 0), "транзакций_между": пред.get("транзакций_между"),
            "пред_источник": пред.get("подписант_источник"), "bin_step": lb["bin_step"]}


def исход(модель_: int | None, факт: int, запас: float) -> str:
    if модель_ is None:
        return "нет котировки"
    return "в пол" if факт >= int(модель_ * (1 - запас)) else "отказ"


def main() -> int:
    сб = json.loads((КОРЕНЬ / "data" / "podbivka" / "dlmm_bez_chteniy_sbor.json").read_text(encoding="utf-8"))
    свер = {x["сделка"]: x for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "dlmm_proverka_mnogo4.json").read_text(encoding="utf-8"))["итог"]}
    ряды = []
    for р in сб["ряды"]:
        с = свер[р["сделка"]]
        м = модель(р, с, сб["пулы_сейчас"].get(р["пул"]))
        факт = с["факт_выход"]
        ряды.append({"сделка": р["сделка"], "пул": р["пул"], "повод": с["повод"], "корзин_факт": с["корзин_факт"],
                     "вход": с["вход"], "факт": факт, "с_чтением": с["модель_выход"], **м,
                     "ошибка_без_чтений_пп": round((м["модель"] - факт) / факт * 100, 4) if м.get("модель") is not None and факт else None,
                     "ошибка_с_чтением_пп": с["расхождение_пп"]})
    md = ["# DLMM без чтений: котировка по событию предыдущего свопа (46 свопов сверки v6)", "",
          "Модель строителя без getMultipleAccounts: корзина -- end_bin_id предыдущего успешного свопа пула, ставка -- его "
          "fee_bps, bin_step из кэша, налог Token-2022 на входе -- по минту; содержимого корзин нет, весь вход -- по цене "
          "одной корзины. Факт -- amount_out события сделки. Пол min_out = модель × (1 − запас): факт ≥ пола -- «в пол» "
          "(сделка прошла бы), меньше -- «отказ». Для сравнения -- котировка по прочитанному состоянию (сверка v6, "
          "data/podbivka/dlmm_proverka_mnogo4.json). Скрипт: analysis/podbivka_dlmm_bez_chteniy.py.", ""]
    есть = [r for r in ряды if r.get("модель") is not None]
    md += [f"Свопов 46, котировка без чтений посчитана у {len(есть)}; без неё: "
           + (", ".join(f"{r['сделка'][:8]} -- {r['why_not']}" for r in ряды if r.get("модель") is None) or "нет") + ".", ""]
    md += ["| котировка | запас | в пол | отказ | ошибка > 1 % | медиана ошибки, % | p90 модуля ошибки, % |", "|---|---|---|---|---|---|---|"]
    for имя, ключ, ош in (("без чтений", "модель", "ошибка_без_чтений_пп"), ("с чтением (v6)", "с_чтением", "ошибка_с_чтением_пп")):
        рр = [r for r in есть if r.get(ключ) is not None]
        ошибки = [r[ош] for r in рр if r.get(ош) is not None]
        мод = sorted(abs(x) for x in ошибки)
        p90 = мод[min(len(мод) - 1, int(0.9 * len(мод)))] if мод else None
        for з in ЗАПАСЫ:
            исх = [исход(r[ключ], r["факт"], з) for r in рр]
            md.append(f"| {имя} | {int(з * 100)} % | {исх.count('в пол')} | {исх.count('отказ')} | "
                      f"{sum(1 for x in ошибки if abs(x) > 1)} | {statistics.median(ошибки):+.3f} | {p90:.3f} |" if ошибки else f"| {имя} | {int(з * 100)} % | — | — | — | — | — |")
    md += ["", "| сделка | повод | корзин факт | слотов от пред. | транзакций между | пред. -- источник | налог на входе | факт | без чтений | ошибка, % | с чтением, % |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in ряды:
        md.append(f"| `{r['сделка'][:8]}` | {r['повод'][:24]} | {r['корзин_факт']} | {r.get('слотов_от_пред', '—')} | "
                  f"{r.get('транзакций_между', '—')} | {'да' if r.get('пред_источник') else 'нет'} | {r.get('налог_вход', '—')} | "
                  f"{r['факт']} | {r.get('модель', '—')} | {r.get('ошибка_без_чтений_пп', '—')} | {r['ошибка_с_чтением_пп']} |")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-30_dlmm_bez_chteniy.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (КОРЕНЬ / "data" / "podbivka" / "dlmm_bez_chteniy.json").write_text(json.dumps(ряды, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(md[:14]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
