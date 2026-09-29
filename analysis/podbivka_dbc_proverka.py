#!/usr/bin/env python3
"""Сверка порта analysis/podbivka_dbc_quote.py на живых свопах Meteora DBC -- офлайн, по data/podbivka/dbc_sbor*.json.

Снимок: пул и config одним getMultipleAccounts (слот S), сделка -- первая успешная транзакция пула после S. В сверку идёт
первое событие EvtSwap2 этого пула (emit_cpi): вход = included_fee_input_amount, факт выхода = output_amount,
next_sqrt_price, комиссии (trading / protocol / referral); current_point -- слот сделки (activation_type 0) или
current_timestamp события (1). Только exact_in (swap_mode 0). Отсев: цена после сдвинулась против направления свопа
(между чтением и сделкой было другое), несколько успешных транзакций пула в слоте сделки.
Выход: data/podbivka/dbc_proverka.json.
"""
from __future__ import annotations

import base64
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_dbc_quote as Q  # noqa: E402
import podbivka_dbc_sbor as DS  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def сверить(сн: dict) -> dict:
    т, пул, дан = сн["транзакция"], сн["пул"], сн["данные"]
    if not дан.get(пул) or not дан.get(сн["config"]):
        return {"отсев": "пул или config не прочитан"}
    pool = Q.разобрать_пул(base64.b64decode(дан[пул]))
    cfg = Q.разобрать_конфиг(base64.b64decode(дан[сн["config"]]))
    ев = [e for e in DS.события(т) if e["pool"] == пул]
    if not ев:
        return {"отсев": "не своп по пулу"}
    if (сн.get("успешных_в_слоте_сделки") or 1) > 1:
        return {"отсев": "в слоте сделки несколько успешных транзакций пула"}
    e = ев[0]
    if e["swap_mode"] != 0:
        return {"отсев": f"swap_mode {e['swap_mode']} (не exact_in)"}
    q2b = e["trade_direction"] == 1
    if (q2b and e["next_sqrt_price"] < pool["sqrt_price"]) or (not q2b and e["next_sqrt_price"] > pool["sqrt_price"]):
        return {"отсев": "цена после сдвинулась против направления свопа (между чтением и сделкой было другое)"}
    точка = т.get("slot") if cfg["activation_type"] == 0 else e["current_timestamp"]
    первый = bool(cfg["enable_first_swap_with_min_fee"]) and pool["has_swap"] == 0
    из_ = {"пул": пул, "slot_чтения": сн["slot"], "сделка": сн["сделка"], "quote_to_base": q2b,
           "вход": e["included_fee_input_amount"], "факт_выход": e["output_amount"], "has_referral": e["has_referral"],
           "base_fee_mode": cfg["base_fee"]["base_fee_mode"], "динамическая": bool(cfg["dynamic_fee"]["initialized"]),
           "collect_fee_mode": cfg["collect_fee_mode"], "activation_type": cfg["activation_type"],
           "первый_своп": pool["has_swap"] == 0, "длина_события": e["длина"]}
    try:
        q = Q.котировка_точный_вход(pool, cfg, e["included_fee_input_amount"], q2b, точка, e["has_referral"], первый)
    except Q.ОшибкаDBC as exc:
        из_["why_not"] = str(exc)[:160]
        return из_
    из_.update(модель_выход=q["output_amount"], разница=q["output_amount"] - e["output_amount"],
               расхождение_пп=round((q["output_amount"] - e["output_amount"]) / e["output_amount"] * 100, 6) if e["output_amount"] else None,
               цена_сошлась=q["next_sqrt_price"] == e["next_sqrt_price"],
               комиссии_сошлись=(q["trading_fee"], q["protocol_fee"], q["referral_fee"]) == (e["trading_fee"], e["protocol_fee"], e["referral_fee"]),
               fee_numerator=q["fee_numerator"])
    return из_


def main() -> int:
    файлы = sys.argv[1:] or ["data/podbivka/dbc_sbor.json"]
    сн_все = [сн for ф in файлы for сн in json.loads((КОРЕНЬ / ф).read_text(encoding="utf-8")).get("снимки") or []]
    итог, отсев = [], collections.Counter()
    for сн in сн_все:
        r = сверить(сн)
        if r.get("отсев"):
            отсев[r["отсев"]] += 1
            continue
        итог.append(r)
    (КОРЕНЬ / "data" / "podbivka" / "dbc_proverka.json").write_text(
        json.dumps({"итог": итог, "отсев": dict(отсев)}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"сверено {len(итог)}, до единицы {sum(1 for r in итог if r.get('разница') == 0)}, отсев {dict(отсев)}")
    for r in итог:
        print(r["пул"][:8], r["quote_to_base"], r["вход"], r["факт_выход"], r.get("модель_выход"), r.get("разница"),
              r.get("цена_сошлась"), r.get("комиссии_сошлись"), r["base_fee_mode"], r["динамическая"], r.get("why_not"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
