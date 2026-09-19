#!/usr/bin/env python3
"""Владелец, 2026-09-19: критерий для допуска Dune к массовому прогону
(Task 4, последнее сообщение) -- прогон на 29 УЖЕ ИЗМЕРЕННЫХ кандидатах
должен ВОСПРОИЗВЕСТИ data/solana_29_candidates_table_v2.json (то же
определение, каким она считалась -- signer в любой позиции accountKeys,
без MIN_SPEND, без "один минт" -- см. solana_29_candidates_flow.py,
которое уже реализует канонику из Task 2 этого же сообщения, отдельный
скрипт под "канонику" не нужен). Если НЕ воспроизводит -- Dune для
массового прогона (100 кошельков) НЕ используем, вердикт финальный.

Метод: 246 УЖЕ ИЗВЕСТНЫХ сигнатур первых входов (со своими block_time/
mint/sol_equivalent, посчитанными по RPC) -- bounded IN(...), НЕ
блуждающий скан по адресу+времени (дёшево, как и с лидером). Проверяем:
  1. Покрытие -- сколько из 246 найдено в dex_solana.trades;
  2. Атрибуция -- у скольких кошелёк-кандидат есть в signers (не только
     у лидера, тест на обобщение находки этой сессии);
  3. Размер -- Dune-производный SOL-эквивалент (net-своп, тот же метод,
     что в v4) против нашего RPC-посчитанного, по этим же событиям;
  4. День-рейт -- пересчитанный по Dune per_day_ge_2/per_day_ge_4.3 на
     кошелёк, сравнение с table_v2."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from statistics import median, mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_29_candidates_validation.json"
SOL_MINT = "So11111111111111111111111111111111111111112"
TX_TABLE, SIG_COL = "solana.transactions", "id"
COL_BOUGHT_MINT, COL_SOLD_MINT = "token_bought_mint_address", "token_sold_mint_address"
COL_BOUGHT_AMT, COL_SOLD_AMT = "token_bought_amount", "token_sold_amount"
THRESHOLDS = [2.0, 4.3]


def main() -> None:
    flow = json.loads((REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json").read_text())
    table_v2 = json.loads((REPO_ROOT / "data" / "solana_29_candidates_table_v2.json").read_text())
    table_v2_by_addr = {r["address"]: r for r in table_v2.get("rows", [])}

    events = []
    for addr, v in flow.get("wallets", {}).items():
        if not v:
            continue
        for e in v.get("first_entry_events") or []:
            if e.get("signature") and e.get("block_time"):
                events.append({"address": addr, **e})
    sigs = [e["signature"] for e in events]
    min_t, max_t = min(e["block_time"] for e in events), max(e["block_time"] for e in events)

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_known_events": len(events)}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    # --- Шаг 1: покрытие + атрибуция (signers) через solana.transactions ---
    sig_list_sql = ",".join(f"'{s}'" for s in sigs)
    sql1 = (f"SELECT {SIG_COL} AS tx_id, signer, signers FROM {TX_TABLE} "
            f"WHERE block_time BETWEEN from_unixtime({min_t - 60}) AND from_unixtime({max_t + 60}) "
            f"AND {SIG_COL} IN ({sig_list_sql})")
    r1 = probe.run_sql_sync("candidates29_signers", sql1, timeout_s=590)
    result["step1_signers"] = {k: v for k, v in r1.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[valid29] шаг1: status={r1.get('status')} n_rows={r1.get('n_rows')}", flush=True)
    if r1.get("status") != "ok":
        result["FINAL_VERDICT"] = f"Шаг 1 не выполнился ({r1.get('status')}) -- Dune для массового прогона НЕ используем."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[valid29] " + result["FINAL_VERDICT"], flush=True)
        return

    signers_by_tx = {row["tx_id"]: row for row in r1["rows"]}
    n_found = len(signers_by_tx)
    n_attributed = sum(1 for e in events if (signers_by_tx.get(e["signature"]) or {}).get("signers")
                        and e["address"] in signers_by_tx[e["signature"]]["signers"])
    result["step1_coverage"] = round(n_found / len(events), 4)
    result["step1_attribution_rate"] = round(n_attributed / len(events), 4) if events else None
    print(f"[valid29] покрытие={result['step1_coverage']} атрибуция(signers)={result['step1_attribution_rate']}", flush=True)

    # --- Шаг 2: полные строки dex_solana.trades по тем же 246 сигнатурам -- размер ---
    sql2 = (f"SELECT tx_id, {COL_BOUGHT_MINT} AS bought_mint, {COL_SOLD_MINT} AS sold_mint, "
            f"{COL_BOUGHT_AMT} AS bought_amount, {COL_SOLD_AMT} AS sold_amount FROM dex_solana.trades "
            f"WHERE tx_id IN ({sig_list_sql})")
    r2 = probe.run_sql_sync("candidates29_dex_trades", sql2, timeout_s=300)
    result["step2_dex_trades"] = {k: v for k, v in r2.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[valid29] шаг2: status={r2.get('status')} n_rows={r2.get('n_rows')}", flush=True)
    if r2.get("status") != "ok":
        result["FINAL_VERDICT"] = f"Шаг 2 не выполнился ({r2.get('status')}) -- размер сверить не удалось."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[valid29] " + result["FINAL_VERDICT"], flush=True)
        return

    by_tx: dict[str, list] = {}
    for row in r2["rows"]:
        by_tx.setdefault(row["tx_id"], []).append(row)

    size_ratios = []
    dune_event_sol_equiv: dict[str, float] = {}
    for e in events:
        legs = by_tx.get(e["signature"])
        if not legs:
            continue
        sol_leg = next((lg for lg in legs if lg.get("sold_mint") == SOL_MINT), None)
        target_leg = next((lg for lg in legs if lg.get("bought_mint") == e["mint"]), None)
        if not sol_leg or not target_leg or not sol_leg.get("sold_amount") or not target_leg.get("bought_amount"):
            continue
        dune_sol_equiv = float(sol_leg["sold_amount"])
        dune_event_sol_equiv[e["signature"]] = dune_sol_equiv
        if e.get("sol_equivalent"):
            size_ratios.append(dune_sol_equiv / e["sol_equivalent"])
    if size_ratios:
        result["step2_size_ratio_summary"] = {"n": len(size_ratios), "median": median(size_ratios), "mean": mean(size_ratios)}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[valid29] размер: n_сравнимых={len(size_ratios)} медиана_отношения={result.get('step2_size_ratio_summary', {}).get('median')}", flush=True)

    # --- Шаг 3: пересчёт per_day_ge_X по Dune-размеру, сравнение с table_v2 ---
    by_addr: dict[str, list] = {}
    for e in events:
        by_addr.setdefault(e["address"], []).append(e)

    per_wallet_compare = []
    for addr, evs in by_addr.items():
        days = flow["wallets"][addr].get("days_covered") or 7
        row_v2 = table_v2_by_addr.get(addr, {})
        entry = {"address": addr, "n_events": len(evs), "days_covered": days}
        for th in THRESHOLDS:
            key = f"{th}".rstrip("0").rstrip(".")
            n_dune = sum(1 for e in evs if dune_event_sol_equiv.get(e["signature"], 0) >= th)
            entry[f"dune_per_day_ge_{key}"] = round(n_dune / days, 3) if days else None
            entry[f"table_v2_per_day_ge_{key}"] = row_v2.get(f"per_day_ge_{key}")
        per_wallet_compare.append(entry)
    result["step3_per_wallet_compare"] = per_wallet_compare

    diffs_43 = [abs((p.get("dune_per_day_ge_4.3") or 0) - (p.get("table_v2_per_day_ge_4.3") or 0)) for p in per_wallet_compare]
    result["step3_summary"] = {"n_wallets": len(per_wallet_compare),
                                "mean_abs_diff_per_day_ge_4.3": mean(diffs_43) if diffs_43 else None,
                                "max_abs_diff_per_day_ge_4.3": max(diffs_43) if diffs_43 else None}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    reproduces = (result["step1_coverage"] >= 0.9 and result.get("step2_size_ratio_summary", {}).get("median")
                  and 0.9 <= result["step2_size_ratio_summary"]["median"] <= 1.1
                  and result["step3_summary"].get("mean_abs_diff_per_day_ge_4.3") is not None
                  and result["step3_summary"]["mean_abs_diff_per_day_ge_4.3"] <= 0.5)
    result["REPRODUCES_TABLE_V2"] = bool(reproduces)
    result["FINAL_VERDICT"] = (
        ("ВОСПРОИЗВОДИТ table_v2 -- Dune можно рассматривать для массового прогона (с учётом стоимости, см. "
         "solana_dune_signer_linkage_v4.json step5_cost_estimate)." if reproduces else
         "НЕ ВОСПРОИЗВОДИТ table_v2 в пределах разумного допуска -- Dune для массового прогона НЕ используем, "
         "вердикт финальный. См. step1/2/3 для того, что именно разошлось (покрытие/размер/день-рейт).")
    )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[valid29] {result['FINAL_VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
