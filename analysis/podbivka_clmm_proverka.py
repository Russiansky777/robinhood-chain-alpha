#!/usr/bin/env python3
"""Сверка порта analysis/podbivka_clmm_quote.py на живых свопах Raydium CLMM -- офлайн, по data/podbivka/clmm_sbor.json.

Снимок: пул, amm_config, расширение битовой карты и tick arrays, прочитанные одним getMultipleAccounts в слоте S
(commitment confirmed); сделка -- первая успешная транзакция пула со слотом > S. В сверку идёт первое событие
SwapEvent этого пула в транзакции (оно считано из прочитанного состояния):
  вход пула = amount_0 (zero_for_one) или amount_1 -- сумма, на которой считает swap_internal (без налога
  Token-2022 на входе; в swap_v2 налог в событии отдельно);
  факт выхода пула = amount выхода + transfer_fee выхода (в событии выход -- за вычетом налога);
  котировка портом на этот вход в прочитанном состоянии, время -- blockTime сделки.
Сравниваются выход до единицы, цена sqrt_price_x64, тик и ликвидность после, комиссия (trade_fee, если событие новое).
Отсев: не своп (инструкции пула нет, ликвидность и т. п.), в слоте сделки несколько успешных транзакций пула
(порядок внутри слота неизвестен), своп exact_out (is_base_input = false).
Выход: data/podbivka/clmm_proverka.json, docs/podbivka_2026-09-29_clmm.md (раздел сверки).
"""
from __future__ import annotations

import base64
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_clmm_quote as Q  # noqa: E402
import podbivka_clmm_sbor as SB  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def сверить(сн: dict) -> dict:
    т = сн["транзакция"]
    пул = сн["пул"]
    дан = сн["данные"]
    if not дан.get(пул):
        return {"отсев": "пул не прочитан"}
    pool = Q.разобрать_пул(base64.b64decode(дан[пул]))
    cfg = Q.разобрать_конфиг(base64.b64decode(дан[сн["amm_config"]]))
    ext = Q.разобрать_расширение(base64.b64decode(дан[сн["расширение"]])) if дан.get(сн["расширение"]) else None
    массивы = {}
    for старт, адр in сн["массивы"].items():
        if дан.get(адр):
            а = Q.разобрать_массив(base64.b64decode(дан[адр]))
            массивы[а["start_tick_index"]] = а
    ixs = [SB.разобрать_ix(ix) for _, ix in SB.инструкции(т)]
    ixs = [x for x in ixs if x and ((x.get("роли") or {}).get("pool_state") == пул or
                                    (x["вид"] == "swap_router_base_in" and пул in (x.get("счета") or [])))]
    ев = [e for e in SB.события(т) if e["pool_state"] == пул]
    if not ev_ok(ixs, ев):
        return {"отсев": "не своп по пулу" if not ев else "инструкции/события не сходятся",
                "виды": [x["вид"] for x in ixs], "событий": len(ев)}
    if (сн.get("успешных_в_слоте_сделки") or 1) > 1:
        return {"отсев": "в слоте сделки несколько успешных транзакций пула"}
    x = ixs[0]
    if x.get("is_base_input") is False:
        return {"отсев": "exact_out"}
    e = ев[0]
    zfo = e["zero_for_one"]
    вход = e["amount_0"] if zfo else e["amount_1"]
    факт = (e["amount_1"] + e["transfer_fee_1"]) if zfo else (e["amount_0"] + e["transfer_fee_0"])
    лимит = x.get("sqrt_price_limit_x64") or 0
    из_ = {"пул": пул, "slot_чтения": сн["slot"], "сделка": сн["сделка"], "slot_сделки": сн["slot_сделки"],
           "вид": x["вид"], "zero_for_one": zfo, "вход": вход, "факт_выход": факт, "лимит_цены": лимит,
           "свопов_по_пулу_в_tx": len(ев), "fee_on": pool["fee_on"], "динамическая": pool["dynamic_fee_info"] is not None,
           "trade_fee_rate": cfg["trade_fee_rate"], "tick_spacing": pool["tick_spacing"], "длина_события": e["длина"],
           "массивов_прочитано": len(массивы)}
    try:
        q = Q.котировка_точный_вход(pool, cfg, массивы, ext, вход, zfo, т.get("blockTime") or 0, лимит)
    except Q.ОшибкаCLMM as exc:
        из_["why_not"] = str(exc)[:160]
        return из_
    из_.update(модель_выход=q["amount_out"], разница=q["amount_out"] - факт,
               расхождение_пп=round((q["amount_out"] - факт) / факт * 100, 6) if факт else None,
               цена_сошлась=q["sqrt_price_x64_после"] == e["sqrt_price_x64"], тик_сошёлся=q["tick_после"] == e["tick"],
               ликвидность_сошлась=q["liquidity_после"] == e["liquidity"], вход_потреблён=q["amount_in_consumed"],
               тиков_пересечено=q["тиков_пересечено"], массивов_пройдено=len(q["массивы"]),
               лимитных_ордеров=q["лимитных_ордеров"], fee_модель=q["fee"],
               fee_факт=(e.get("trade_fee_0", 0) + e.get("trade_fee_1", 0)) if "trade_fee_0" in e else None)
    return из_


def ev_ok(ixs: list, ев: list) -> bool:
    return bool(ев) and bool(ixs)


def main() -> int:
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "clmm_sbor.json").read_text(encoding="utf-8"))
    итог, отсев = [], collections.Counter()
    for сн in д.get("снимки") or []:
        r = сверить(сн)
        if r.get("отсев"):
            отсев[r["отсев"]] += 1
            continue
        итог.append(r)
    (КОРЕНЬ / "data" / "podbivka" / "clmm_proverka.json").write_text(
        json.dumps({"итог": итог, "отсев": dict(отсев)}, ensure_ascii=False, indent=1), encoding="utf-8")
    сошлось = sum(1 for r in итог if r.get("разница") == 0)
    print(f"сверено {len(итог)}, до единицы {сошлось}, отсев {dict(отсев)}")
    for r in итог:
        print(r["пул"][:8], r["вид"], r["zero_for_one"], r["вход"], r["факт_выход"], r.get("модель_выход"),
              r.get("разница"), r.get("цена_сошлась"), r.get("тик_сошёлся"), r.get("тиков_пересечено"),
              r.get("лимитных_ордеров"), r.get("why_not"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
