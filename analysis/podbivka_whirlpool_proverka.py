#!/usr/bin/env python3
"""Сверка порта analysis/podbivka_whirlpool_quote.py на живых свопах Orca Whirlpool -- офлайн, по
data/podbivka/whirlpool_sbor.json (снимок в слоте S и первая успешная транзакция пула после S).

В сверку идёт первое событие Traded этого пула в транзакции:
  вход расчёта = input_amount − input_transfer_fee (swap_with_transfer_fee_extension считает на сумме без налога);
  факт выхода пула = output_amount (пул отдаёт; получатель -- за вычетом output_transfer_fee);
  pre_sqrt_price события = цена прочитанного пула -- иначе между чтением и сделкой было ещё что-то (отсев);
  котировка портом на этот вход, время -- blockTime сделки.
Сравниваются выход до единицы, post_sqrt_price, lp_fee и protocol_fee.
Выход: data/podbivka/whirlpool_proverka.json.
"""
from __future__ import annotations

import base64
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_clmm_sbor as SB  # noqa: E402
import podbivka_whirlpool_quote as Q  # noqa: E402
import podbivka_whirlpool_sbor as WS  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def сверить(сн: dict) -> dict:
    т, пул, дан = сн["транзакция"], сн["пул"], сн["данные"]
    if not дан.get(пул):
        return {"отсев": "пул не прочитан"}
    pool = Q.разобрать_пул(base64.b64decode(дан[пул]))
    ор = Q.разобрать_оракул(base64.b64decode(дан[сн["оракул"]])) if дан.get(сн["оракул"]) else None
    массивы = {}
    for старт, адр in сн["массивы"].items():
        if дан.get(адр):
            а = Q.разобрать_массив(base64.b64decode(дан[адр]))
            массивы[а["start_tick_index"]] = а
    ев = [e for e in WS.события(т) if e["whirlpool"] == пул]
    ixs = [WS.разобрать_ix(ix) for _, ix in SB.инструкции(т)]
    ixs = [x for x in ixs if x and ((x.get("роли") or {}).get("whirlpool") == пул or пул in (x.get("счета") or []))]
    if not ев:
        return {"отсев": "не своп по пулу", "виды": [x["вид"] for x in ixs]}
    e = ев[0]
    if e["pre_sqrt_price"] != pool["sqrt_price"]:
        return {"отсев": "цена до сделки не равна прочитанной (между чтением и сделкой было другое)"}
    x = next((x for x in ixs if x["вид"] in ("swap", "swap_v2")), None)
    if x and x.get("amount_specified_is_input") is False:
        return {"отсев": "exact_out"}
    вход = e["input_amount"] - e["input_transfer_fee"]
    лимит = (x or {}).get("sqrt_price_limit") or 0
    из_ = {"пул": пул, "slot_чтения": сн["slot"], "сделка": сн["сделка"], "вид": x["вид"] if x else "two_hop",
           "a_to_b": e["a_to_b"], "вход": вход, "факт_выход": e["output_amount"], "fee_rate": pool["fee_rate"],
           "tick_spacing": pool["tick_spacing"], "адаптивная": ор is not None, "свопов_по_пулу_в_tx": len(ев),
           "лимит_цены": лимит}
    if len(ев) > 1 and not x:
        из_["примечание"] = "two_hop: лимит цены не разобран, считаем без лимита"
    try:
        q = Q.котировка_точный_вход(pool, массивы, ор, вход, e["a_to_b"], т.get("blockTime") or 0, лимит)
    except Q.ОшибкаWP as exc:
        из_["why_not"] = str(exc)[:160]
        return из_
    из_.update(модель_выход=q["amount_out"], разница=q["amount_out"] - e["output_amount"],
               расхождение_пп=round((q["amount_out"] - e["output_amount"]) / e["output_amount"] * 100, 6)
               if e["output_amount"] else None,
               цена_сошлась=q["sqrt_price_после"] == e["post_sqrt_price"], lp_fee_сошлась=q["lp_fee"] == e["lp_fee"],
               protocol_fee_сошлась=q["protocol_fee"] == e["protocol_fee"], вход_потреблён=q["amount_in_consumed"],
               тиков_пересечено=q["тиков_пересечено"], виды_массивов=q["виды_массивов"])
    return из_


def main() -> int:
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "whirlpool_sbor.json").read_text(encoding="utf-8"))
    итог, отсев = [], collections.Counter()
    for сн in д.get("снимки") or []:
        r = сверить(сн)
        if r.get("отсев"):
            отсев[r["отсев"]] += 1
            continue
        итог.append(r)
    (КОРЕНЬ / "data" / "podbivka" / "whirlpool_proverka.json").write_text(
        json.dumps({"итог": итог, "отсев": dict(отсев)}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"сверено {len(итог)}, до единицы {sum(1 for r in итог if r.get('разница') == 0)}, отсев {dict(отсев)}")
    for r in итог:
        print(r["пул"][:8], r["вид"], r["a_to_b"], r["вход"], r["факт_выход"], r.get("модель_выход"), r.get("разница"),
              r.get("цена_сошлась"), r.get("тиков_пересечено"), r.get("адаптивная"), r.get("why_not"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
