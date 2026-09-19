#!/usr/bin/env python3
"""Владелец, 2026-09-19: v4 -- две реальные ошибки найдены при разборе
результата, эта версия их чинит (шаг1/2 v4 уже успешны -- 298/300 tx,
298 схлопнуто, медиана dune/наша цена=0.989 -- НЕ переделываем):

  - шаг3 (+30с): окно block_time было ГЛОБАЛЬНЫМ (min..max по ВСЕМ 43
    первым входам, т.е. фактически весь диапазон 5.76 суток) вместо
    ПЕР-СОБЫТИЙНОГО +30с -- отсюда 775 272 строки / 154МБ результата и
    results_failed (не влезло). Чиним: VALUES-join, у каждого mint --
    СВОЁ узкое окно [t0-2, t0+32].
  - шаг4 (разметка первого входа): PARTITION BY tt.signer -- ОШИБКА,
    signer это Agm/Fomo-спонсор, ОБЩИЙ для тысяч разных трейдеров
    Fomo в этом диапазоне слотов, не только нашего лидера -- окно
    ранжирования получилось по факту "первый вход КАЖДОГО пользователя
    Fomo", а JOIN сканировал ВСЮ активность Fomo в диапазоне слотов, не
    только лидера -- отсюда timeout за 590с. Чиним: фильтруем WHERE
    contains(tt.signers, ЛИДЕР) -- сужает JOIN до ~300 известных
    транзакций лидера, как в успешных шагах 1/2."""
from __future__ import annotations

import calendar
import json
import os
import sys
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402
from solana_dune_signer_linkage_v4 import sol_usd_price_at  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_signer_linkage_v5.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
SOL_MINT = "So11111111111111111111111111111111111111112"
TX_TABLE, SIG_COL = "solana.transactions", "id"
COL_BOUGHT_MINT, COL_SOLD_MINT = "token_bought_mint_address", "token_sold_mint_address"
COL_BOUGHT_AMT, COL_SOLD_AMT = "token_bought_amount", "token_sold_amount"


def parse_bt(s: str) -> int:
    return calendar.timegm(time.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S"))


def main() -> None:
    sel_rows = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json").read_text())
    sel_by_sig = {r["signature"]: r for r in sel_rows}
    sigs = list(sel_by_sig.keys())
    min_slot, max_slot = min(r["slot"] for r in sel_rows), max(r["slot"] for r in sel_rows)
    first_entry_sigs = [s for s in sigs if sel_by_sig[s].get("zero_balance")]

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "fixes": "см. docstring -- шаг3 пер-событийное окно, шаг4 фильтр по signers вместо partition by signer"}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    # --- Шаг 3 (исправлено): VALUES-join, окно НА КАЖДОЕ событие ---
    values_rows = ",".join(
        f"('{sel_by_sig[s]['mint']}', {sel_by_sig[s]['time'] - 2}, {sel_by_sig[s]['time'] + 32})"
        for s in first_entry_sigs
    )
    sql3 = (
        f"WITH events(mint, lo, hi) AS (VALUES {values_rows}) "
        f"SELECT dt.tx_id, dt.block_time, dt.{COL_BOUGHT_MINT} AS bought_mint, dt.{COL_SOLD_MINT} AS sold_mint, "
        f"dt.{COL_BOUGHT_AMT} AS bought_amount, dt.{COL_SOLD_AMT} AS sold_amount, e.mint AS event_mint, e.lo, e.hi "
        f"FROM dex_solana.trades dt JOIN events e "
        f"ON (dt.{COL_BOUGHT_MINT} = e.mint OR dt.{COL_SOLD_MINT} = e.mint) "
        f"WHERE dt.block_time >= from_unixtime(e.lo) AND dt.block_time <= from_unixtime(e.hi)"
    )
    r3 = probe.run_sql_sync("price_at_30s_v5_fixed", sql3, timeout_s=300)
    result["step3_price_at_30s"] = {k: v for k, v in r3.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v5] шаг3: status={r3.get('status')} n_rows={r3.get('n_rows')}", flush=True)
    if r3.get("status") == "ok":
        rows_by_mint: dict[str, list] = {}
        for row in r3["rows"]:
            rows_by_mint.setdefault(row["event_mint"], []).append(row)

        short = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "step_extended_result.json").read_text())
        short_by_key = {(r["signature"], r["seconds"]): r for r in short if "seconds" in r}

        per_trade = []
        for sig in first_entry_sigs:
            row = sel_by_sig[sig]
            t0, mint = row["time"], row["mint"]
            our30 = short_by_key.get((sig, 30))
            if not our30 or our30.get("mine_status") != "ok" or our30.get("mine_price") is None:
                per_trade.append({"signature": sig, "status": "no_our_price"})
                continue
            sol_usd_30 = sol_usd_price_at(t0 + 30)
            our_price_sol = float(our30["mine_price"]) / sol_usd_30 if sol_usd_30 else None
            cands = [r_ for r_ in rows_by_mint.get(mint, [])
                     if t0 < parse_bt(r_["block_time"]) <= t0 + 30
                     and r_.get("bought_mint") == mint and r_.get("sold_mint") == SOL_MINT
                     and r_.get("sold_amount") and r_.get("bought_amount")]
            if not cands or our_price_sol is None:
                per_trade.append({"signature": sig, "status": "no_comparable_dune_row_or_our_price"})
                continue
            last = max(cands, key=lambda r_: parse_bt(r_["block_time"]))
            dune_price_sol = float(last["sold_amount"]) / float(last["bought_amount"])
            per_trade.append({"signature": sig, "status": "ok", "our_price_sol_per_token_at_30s": our_price_sol,
                               "dune_price_sol_per_token_at_30s": dune_price_sol,
                               "ratio_dune_over_ours": dune_price_sol / our_price_sol if our_price_sol else None})
        result["step3_per_trade"] = per_trade
        ok3 = [p for p in per_trade if p.get("ratio_dune_over_ours")]
        if ok3:
            ratios3 = [p["ratio_dune_over_ours"] for p in ok3]
            result["step3_summary"] = {"n": len(ratios3), "median_ratio_dune_over_ours": median(ratios3)}
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[v5] шаг3 сравнимых: {len(ok3)}, медиана={result.get('step3_summary', {}).get('median_ratio_dune_over_ours')}", flush=True)

    # --- Шаг 4 (исправлено): фильтр по signers лидера, не partition by signer ---
    sql4 = (
        f"WITH ranked AS (SELECT dt.tx_id, dt.{COL_BOUGHT_MINT} AS mint, dt.block_time, "
        f"row_number() OVER (PARTITION BY dt.{COL_BOUGHT_MINT} ORDER BY dt.block_time) AS rn "
        f"FROM dex_solana.trades dt JOIN {TX_TABLE} tt ON dt.tx_id = tt.{SIG_COL} "
        f"WHERE tt.block_slot BETWEEN {min_slot - 1000} AND {max_slot + 1000} "
        f"AND contains(tt.signers, '{LEADER_WALLET}')) "
        f"SELECT tx_id, mint, block_time FROM ranked WHERE rn = 1"
    )
    r4 = probe.run_sql_sync("dune_first_entry_v5_fixed", sql4, timeout_s=300)
    result["step4_first_entry_classification"] = {k: v for k, v in r4.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v5] шаг4: status={r4.get('status')} n_rows={r4.get('n_rows')}", flush=True)
    if r4.get("status") == "ok":
        dune_first_tx = {row["tx_id"] for row in r4["rows"]}
        comparable = set(sigs)
        agree = sum(1 for s in comparable if bool(s in dune_first_tx) == bool(sel_by_sig[s].get("zero_balance")))
        result["step4_n_comparable"] = len(comparable)
        result["step4_n_agree"] = agree
        result["step4_agreement_rate"] = round(agree / len(comparable), 4) if comparable else None
        disagree = [s for s in comparable if bool(s in dune_first_tx) != bool(sel_by_sig[s].get("zero_balance"))]
        result["step4_disagree_sample"] = [{"signature": s, "our_zero_balance": sel_by_sig[s].get("zero_balance"),
                                             "dune_first_via_signers_filter": s in dune_first_tx} for s in disagree[:15]]
        print(f"[v5] шаг4 согласие={result['step4_agreement_rate']} ({agree}/{len(comparable)})", flush=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    credits = []
    for key in ("step3_price_at_30s", "step4_first_entry_classification"):
        meta = (result.get(key) or {}).get("status_meta") or {}
        if meta.get("execution_cost_credits"):
            credits.append({"step": key, "credits": meta["execution_cost_credits"]})
    result["cost_this_run"] = {"credits_by_step": credits, "total": sum(c["credits"] for c in credits)}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v5] стоимость этого прогона: {result['cost_this_run']}", flush=True)


if __name__ == "__main__":
    main()
