#!/usr/bin/env python3
"""Владелец, 2026-09-19: гонка трёх баз на живых закрытых сделках
(Task 3), вместо выбора модели на глаз. Для каждой закрытой сделки из
data/solana_dbot_realized_ledger.json считаем прогноз доходности "от
базы Х до цены на момент нашего фактического выхода" и сравниваем с
РЕАЛИЗОВАННЫМ брутто (gross_pct, уже в ведомости):

  (а) котировка +5с после сделки лидера -- старая база (маршрут+find_price_at
      на leader_time+5, тот же метод, что solana_dbot_entry_price_calibration.py);
  (б) цена лидера (уже в ведомости, leader_price_usdc) x (1 + медианная
      наценка -- пересчитывается на КАЖДОМ прогоне из markup_at_entry_pct
      всех сделок в ЭТОЙ ЖЕ ведомости, не захардкожена);
  (в) цена в тех же пулах (маршрут декодирован из tx ЛИДЕРА) НА СЛОТЕ
      "слот лидера + k", где k = наш_слот - слот_лидера -- по построению
      это ровно момент нашей собственной покупки, но цена берётся из
      НЕЗАВИСИМОГО источника (декодированный маршрут лидера), а не из
      нашей же транзакции -- честная перекрёстная проверка, не тавтология.

Только чтение -- Solana RPC (Alchemy) + GeckoTerminal. Пересчитывать при
каждом новом прогоне ведомости (см. владелец, Task 3 в этом же
сообщении)."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from statistics import median, mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_select_extend import plan_route_for_purchase, POOL_META_PATH, ROUTE_META_PATH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_pilot_three_base_race.json"
LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"


def price_chain_at(route: list[dict], target_time: int, lo: int, hi: int) -> tuple[float | None, str]:
    try:
        value = D(1)
        for leg in route:
            p = fp.find_price_at(leg["pool"], target_time, lo, hi)
            if p.get("status") != "ok":
                return None, "leg_price_not_found"
            e = p["event"]
            v = D(e["p1_per_0"])
            if leg["from"] == e["m0"] and leg["to"] == e["m1"]:
                value *= v
            elif leg["from"] == e["m1"] and leg["to"] == e["m0"]:
                value /= v
            else:
                return None, "leg_mint_mismatch"
        return float(value), "ok"
    except RuntimeError as exc:
        return None, f"rpc_error: {str(exc)[:150]}"


def analyze_trade(t: dict, median_markup_pct: float) -> dict:
    row: dict = {"label": t.get("label"), "mint": t.get("mint"), "buy_signature": t.get("buy_signature"),
                 "sell_signature": t.get("sell_signature")}

    leader_sig = t.get("leader_signature")
    leader_price_usdc = t.get("leader_price_usdc")
    real_exit_price_usdc = t.get("our_sell_price_usdc_equiv")
    realized_gross_pct = t.get("gross_pct")
    row["leader_price_usdc"] = leader_price_usdc
    row["real_exit_price_usdc"] = real_exit_price_usdc
    row["realized_gross_pct"] = realized_gross_pct

    if not leader_sig or leader_price_usdc is None or real_exit_price_usdc is None or realized_gross_pct is None:
        row["status"] = "missing_required_fields"
        return row

    leader_tx = fp.get_transaction(leader_sig)
    our_buy_tx = fp.get_transaction(t["buy_signature"])
    if leader_tx is None or our_buy_tx is None:
        row["status"] = "tx_fetch_failed"
        return row

    leader_slot, our_slot = leader_tx.get("slot"), our_buy_tx.get("slot")
    leader_time = leader_tx.get("blockTime")
    our_time = t.get("buy_block_time") or our_buy_tx.get("blockTime")
    row["leader_slot"] = leader_slot
    row["our_slot"] = our_slot
    row["k_slot_lag"] = (our_slot - leader_slot) if (leader_slot is not None and our_slot is not None) else None
    row["leader_time"] = leader_time
    row["our_time"] = our_time

    global_meta = json.loads(POOL_META_PATH.read_text()) if POOL_META_PATH.exists() else {}
    global_meta.update(json.loads(ROUTE_META_PATH.read_text()) if ROUTE_META_PATH.exists() else {})
    route, _meta = plan_route_for_purchase(t["mint"], leader_tx, global_meta)
    if not route:
        row["status"] = "no_route"
        return row

    # (а) +5с после лидера
    price_a, status_a = price_chain_at(route, leader_time + 5, leader_time, leader_time + 65)
    row["baseline_a_price_5s_usdc"] = price_a
    row["baseline_a_status"] = status_a

    # (б) цена лидера x (1 + медианная наценка, пересчитана из ЭТОЙ ведомости)
    price_b = leader_price_usdc * (1 + median_markup_pct / 100)
    row["baseline_b_price_usdc"] = price_b
    row["baseline_b_median_markup_pct_used"] = median_markup_pct

    # (в) цена в пулах ЛИДЕРА на нашем собственном слоте (независимая перекрёстная проверка)
    if our_time is not None:
        price_c, status_c = price_chain_at(route, our_time, leader_time, our_time + 60)
    else:
        price_c, status_c = None, "no_our_time"
    row["baseline_c_price_at_our_slot_usdc"] = price_c
    row["baseline_c_status"] = status_c

    for key, price in (("a", price_a), ("b", price_b), ("c", price_c)):
        if price:
            predicted_pct = (real_exit_price_usdc / price - 1) * 100
            row[f"predicted_return_{key}_pct"] = predicted_pct
            row[f"error_{key}_pct_points"] = predicted_pct - realized_gross_pct

    row["status"] = "ok"
    return row


def main() -> None:
    ledger = json.loads(LEDGER_PATH.read_text())
    closed = [t for t in ledger.get("trades", []) if t.get("status") == "ok"]
    markups = [t["markup_at_entry_pct"] for t in closed if "markup_at_entry_pct" in t]
    median_markup = median(markups) if markups else 0.0

    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "n_closed_trades_in_ledger": len(closed),
                     "median_markup_pct_used_for_baseline_b": median_markup,
                     "median_markup_n": len(markups), "trades": []}

    for t in closed:
        print(f"[race] анализирую {t.get('label')} {t.get('mint','')[:10]}..", flush=True)
        r = analyze_trade(t, median_markup)
        result["trades"].append(r)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[race]   status={r.get('status')} k={r.get('k_slot_lag')} "
              f"err_a={r.get('error_a_pct_points')} err_b={r.get('error_b_pct_points')} err_c={r.get('error_c_pct_points')}",
              flush=True)

    ok_rows = [r for r in result["trades"] if r.get("status") == "ok"]
    ks = [r["k_slot_lag"] for r in ok_rows if r.get("k_slot_lag") is not None]
    summary = {"n_ok": len(ok_rows)}
    if ks:
        summary["k_slot_lag"] = {"n": len(ks), "median": median(ks), "min": min(ks), "max": max(ks)}
    for key in ("a", "b", "c"):
        errs = [r[f"error_{key}_pct_points"] for r in ok_rows if f"error_{key}_pct_points" in r]
        if errs:
            summary[f"baseline_{key}"] = {
                "n": len(errs), "median_error_pct_points": median(errs),
                "mae_pct_points": mean(abs(e) for e in errs),
            }
    if all(f"baseline_{k}" in summary for k in "abc"):
        best = min("abc", key=lambda k: summary[f"baseline_{k}"]["mae_pct_points"])
        summary["winner_by_mae"] = best
        summary["note"] = (
            f"Победитель по MAE: база ({best}). Таблицу горизонтов переделываем на этой базе ТОЛЬКО когда "
            f"n>=30 (сейчас n={summary['n_ok']}) -- до тех пор это отчёт о гонке без переделки таблицы."
        )
    result["summary"] = summary
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[race] итог: {summary}", flush=True)


if __name__ == "__main__":
    main()
