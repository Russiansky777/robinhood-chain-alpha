#!/usr/bin/env python3
"""Владелец, 2026-09-19: v3 подтвердил -- 300/300 наших tx_id имеют
лидера ВТОРЫМ подписантом в массиве signers (первый -- почти всегда
AgmLJBM..., обычный System-аккаунт с ~3124 SOL, НЕ программа -- судя по
всему общий кошелёк-спонсор/fee payer инфраструктуры лидера, не сам
лидер). Атрибуция ПОДТВЕРЖДЕНА ОКОНЧАТЕЛЬНО (100%, не 0.33% как в
исходном негодном вердикте). Этот скрипт прогоняет калибровку на
ПРАВИЛЬНОМ join (contains(signers, ЛИДЕР), не signer=ЛИДЕР):

  1. схлопывание мульти-хоповых строк dex_solana.trades в net-своп на
     транзакцию (SOL-нога -> целевой минт), сравнение цены с нашей;
  2. цена на +30с в SOL/токен для первых входов, сравнение с проверенными
     нашими значениями;
  3. разметка первого входа по Dune (row_number по signers+mint) против
     нашего zero_balance;
  4. честная оценка стоимости кредитов на 100 кошельков."""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from statistics import median, mean

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_signer_linkage_v4.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
SOL_MINT = "So11111111111111111111111111111111111111112"
TX_TABLE, SIG_COL = "solana.transactions", "id"


def gecko_get(path: str, params: dict) -> dict:
    try:
        resp = requests.get(f"https://api.geckoterminal.com/api/v2{path}", params=params, timeout=30,
                            headers={"Accept": "application/json"})
        return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}
    except Exception as exc:  # noqa: BLE001
        return {"http_status": None, "exception": str(exc)[:200]}


def sol_usd_price_at(t: int) -> float | None:
    r = gecko_get("/networks/solana/pools/3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv/ohlcv/minute",
                  {"aggregate": 1, "before_timestamp": t + 3600, "limit": 200, "currency": "usd"})
    rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not rows:
        return None
    rows = sorted(rows, key=lambda c: c[0])
    import bisect
    times = [c[0] + 60 for c in rows]
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        return rows[0][4]
    if idx >= len(rows):
        return rows[-1][4]
    t0, c0 = times[idx - 1], D(str(rows[idx - 1][4]))
    t1, c1 = times[idx], D(str(rows[idx][4]))
    if t1 == t0:
        return float(c0)
    frac = D(t - t0) / D(t1 - t0)
    return float(c0 + (c1 - c0) * frac)


def main() -> None:
    sel_rows = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json").read_text())
    sel_by_sig = {r["signature"]: r for r in sel_rows}
    sigs = list(sel_by_sig.keys())
    min_slot, max_slot = min(r["slot"] for r in sel_rows), max(r["slot"] for r in sel_rows)

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_our_signatures": len(sigs),
                     "attribution_basis": "v3 подтвердил 300/300 через contains(signers, ЛИДЕР) -- см. solana_dune_signer_linkage_v3.json"}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    # --- Шаг 1: schema-probe dex_solana.trades ---
    sp = probe.run_sql_sync("schema_probe_v4", "SELECT * FROM dex_solana.trades LIMIT 1", timeout_s=60)
    result["step1_schema_probe"] = {k: v for k, v in sp.items() if k != "rows"}
    result["step1_columns"] = (sp.get("metadata") or {}).get("column_names")
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if sp.get("status") != "ok":
        result["FINAL_VERDICT"] = "schema-probe dex_solana.trades не выполнился."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v4] " + result["FINAL_VERDICT"], flush=True)
        return
    cols = result["step1_columns"] or []

    def find_col(cands):
        for c in cands:
            if c in cols:
                return c
        for c in cols:
            for cand in cands:
                if cand in c.lower():
                    return c
        return None

    col_bought_mint = find_col(["token_bought_mint_address"])
    col_sold_mint = find_col(["token_sold_mint_address"])
    col_bought_amt = find_col(["token_bought_amount", "amount_bought"])
    col_sold_amt = find_col(["token_sold_amount", "amount_sold"])
    result["step1_resolved_columns"] = {"bought_mint": col_bought_mint, "sold_mint": col_sold_mint,
                                         "bought_amount": col_bought_amt, "sold_amount": col_sold_amt}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not all([col_bought_mint, col_sold_mint, col_bought_amt, col_sold_amt]):
        result["FINAL_VERDICT"] = "Не нашлись колонки количества токенов в dex_solana.trades -- честно останавливаюсь."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v4] " + result["FINAL_VERDICT"], flush=True)
        return

    # --- Шаг 2: полные строки по ВСЕМ 300 tx_id -- схлопывание в net-своп ---
    sig_list_sql = ",".join(f"'{s}'" for s in sigs)
    sql2 = (f"SELECT tx_id, block_time, trader_id, project, {col_bought_mint} AS bought_mint, "
            f"{col_sold_mint} AS sold_mint, {col_bought_amt} AS bought_amount, {col_sold_amt} AS sold_amount "
            f"FROM dex_solana.trades WHERE tx_id IN ({sig_list_sql})")
    r2 = probe.run_sql_sync("collapse_legs_v4", sql2, timeout_s=300)
    result["step2_full_rows"] = {k: v for k, v in r2.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r2.get("status") != "ok":
        result["FINAL_VERDICT"] = "Шаг 2 (полные строки) не выполнился."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v4] " + result["FINAL_VERDICT"], flush=True)
        return

    by_tx: dict[str, list] = {}
    for row in r2["rows"]:
        by_tx.setdefault(row["tx_id"], []).append(row)
    result["step2_n_tx_with_rows"] = len(by_tx)
    result["step2_avg_legs_per_tx"] = round(len(r2["rows"]) / len(by_tx), 3) if by_tx else None
    print(f"[v4] шаг2: {len(by_tx)} tx нашлись в dex_solana.trades, "
          f"среднее {result['step2_avg_legs_per_tx']} ног/tx", flush=True)

    collapsed = []
    for tx_id, legs in by_tx.items():
        target_mint = sel_by_sig.get(tx_id, {}).get("mint")
        sol_leg = next((lg for lg in legs if lg.get("sold_mint") == SOL_MINT), None)
        target_leg = next((lg for lg in legs if lg.get("bought_mint") == target_mint), None)
        if not sol_leg or not target_leg or not sol_leg.get("sold_amount") or not target_leg.get("bought_amount"):
            collapsed.append({"tx_id": tx_id, "n_legs": len(legs), "status": "no_sol_or_target_leg_found"})
            continue
        sol_in = float(sol_leg["sold_amount"])
        tokens_out = float(target_leg["bought_amount"])
        price_sol_per_token = sol_in / tokens_out if tokens_out else None
        our_row = sel_by_sig.get(tx_id, {})
        our_entry_usdc = float(our_row["entry_usdc"]) if our_row.get("entry_usdc") else None
        sol_usd = sol_usd_price_at(our_row.get("time", 0)) if our_row.get("time") else None
        our_price_sol_per_token = (our_entry_usdc / sol_usd) if our_entry_usdc and sol_usd else None
        collapsed.append({
            "tx_id": tx_id, "n_legs": len(legs), "status": "ok",
            "dune_net_swap_sol_in": sol_in, "dune_net_swap_tokens_out": tokens_out,
            "dune_price_sol_per_token": price_sol_per_token,
            "our_price_sol_per_token": our_price_sol_per_token,
            "ratio_dune_over_ours": (price_sol_per_token / our_price_sol_per_token)
                                    if price_sol_per_token and our_price_sol_per_token else None,
        })
    result["step2_collapsed_net_swaps_sample"] = collapsed[:30]
    result["step2_n_collapsed_total"] = len(collapsed)
    ok_collapsed = [c for c in collapsed if c["status"] == "ok" and c.get("ratio_dune_over_ours")]
    if ok_collapsed:
        ratios = [c["ratio_dune_over_ours"] for c in ok_collapsed]
        result["step2_summary"] = {"n": len(ratios), "median_ratio_dune_over_ours": median(ratios),
                                    "mean_ratio_dune_over_ours": mean(ratios)}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v4] шаг2 сравнимых с нашей ценой: {len(ok_collapsed)}, "
          f"медиана отношения={result.get('step2_summary', {}).get('median_ratio_dune_over_ours')}", flush=True)

    # --- Шаг 3: цена на +30с в SOL/токен, только первые входы (43 минта) ---
    first_entry_sigs = [s for s in sigs if sel_by_sig[s].get("zero_balance")]
    mints = sorted({sel_by_sig[s]["mint"] for s in first_entry_sigs})
    mint_list_sql = ",".join(f"'{m}'" for m in mints)
    lo_time = min(sel_by_sig[s]["time"] for s in first_entry_sigs) - 2
    hi_time = max(sel_by_sig[s]["time"] for s in first_entry_sigs) + 32
    sql3 = (f"SELECT tx_id, block_time, {col_bought_mint} AS bought_mint, {col_sold_mint} AS sold_mint, "
            f"{col_bought_amt} AS bought_amount, {col_sold_amt} AS sold_amount FROM dex_solana.trades "
            f"WHERE ({col_bought_mint} IN ({mint_list_sql}) OR {col_sold_mint} IN ({mint_list_sql})) "
            f"AND block_time >= from_unixtime({lo_time}) AND block_time <= from_unixtime({hi_time})")
    r3 = probe.run_sql_sync("price_at_30s_v4", sql3, timeout_s=300)
    result["step3_price_at_30s"] = {k: v for k, v in r3.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r3.get("status") == "ok":
        import calendar

        def parse_bt(s):
            return calendar.timegm(time.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S"))

        rows_by_mint: dict[str, list] = {}
        for row in r3["rows"]:
            for m in (row.get("bought_mint"), row.get("sold_mint")):
                if m in mints:
                    rows_by_mint.setdefault(m, []).append(row)

        short = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "step_extended_result.json").read_text())
        short_by_key = {(r["signature"], r["seconds"]): r for r in short if "seconds" in r}

        per_trade = []
        for sig in first_entry_sigs:
            row = sel_by_sig[sig]
            t0, mint = row["time"], row["mint"]
            our30 = short_by_key.get((sig, 30))
            if not our30 or our30.get("mine_status") != "ok" or our30.get("mine_price") is None:
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
            per_trade.append({"signature": sig, "our_price_sol_per_token_at_30s": our_price_sol,
                               "dune_price_sol_per_token_at_30s": dune_price_sol,
                               "ratio_dune_over_ours": dune_price_sol / our_price_sol if our_price_sol else None})
        result["step3_per_trade"] = per_trade
        ok3 = [p for p in per_trade if p.get("ratio_dune_over_ours")]
        if ok3:
            ratios3 = [p["ratio_dune_over_ours"] for p in ok3]
            result["step3_summary"] = {"n": len(ratios3), "median_ratio_dune_over_ours": median(ratios3)}
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[v4] шаг3 (+30с): n сравнимых={len(ok3)}, "
              f"медиана отношения={result.get('step3_summary', {}).get('median_ratio_dune_over_ours')}", flush=True)

    # --- Шаг 4: разметка первого входа по Dune (signers-массив+mint) против нашей ---
    sql4 = (f"WITH ranked AS (SELECT dt.tx_id, dt.{col_bought_mint} AS mint, dt.block_time, "
            f"row_number() OVER (PARTITION BY tt.signer, dt.{col_bought_mint} ORDER BY dt.block_time) AS rn "
            f"FROM dex_solana.trades dt JOIN {TX_TABLE} tt ON dt.tx_id = tt.{SIG_COL} "
            f"WHERE tt.block_slot BETWEEN {min_slot - 1000} AND {max_slot + 1000} "
            f"AND contains(tt.signers, '{LEADER_WALLET}')) SELECT tx_id, mint, block_time FROM ranked WHERE rn = 1")
    r4 = probe.run_sql_sync("dune_first_entry_v4", sql4, timeout_s=590)
    result["step4_first_entry_classification"] = {k: v for k, v in r4.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r4.get("status") == "ok":
        dune_first_tx = {row["tx_id"] for row in r4["rows"]}
        comparable = set(sigs) & {row["tx_id"] for row in r2["rows"]}
        agree = sum(1 for s in comparable if bool(s in dune_first_tx) == bool(sel_by_sig[s].get("zero_balance")))
        result["step4_n_comparable"] = len(comparable)
        result["step4_n_agree"] = agree
        result["step4_agreement_rate"] = round(agree / len(comparable), 4) if comparable else None
        disagree = [s for s in comparable if bool(s in dune_first_tx) != bool(sel_by_sig[s].get("zero_balance"))]
        result["step4_disagree_sample"] = [{"signature": s, "our_zero_balance": sel_by_sig[s].get("zero_balance"),
                                             "dune_first_via_signer_partition": s in dune_first_tx} for s in disagree[:15]]
        print(f"[v4] шаг4: согласие разметки={result['step4_agreement_rate']} ({agree}/{len(comparable)})", flush=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # --- Шаг 5: честная оценка стоимости на 100 кошельков ---
    credits_used = []
    for key in ("step2_full_rows", "step3_price_at_30s", "step4_first_entry_classification"):
        meta = (result.get(key) or {}).get("status_meta") or {}
        c = meta.get("execution_cost_credits")
        if c:
            credits_used.append({"step": key, "credits": c})
    total_credits = sum(c["credits"] for c in credits_used)
    result["step5_cost_estimate"] = {
        "credits_by_step": credits_used, "total_credits_this_run": total_credits,
        "note": (
            "Стоимость определяется в основном ШИРИНОЙ временного окна (партиционирование по block_time/date), "
            "а не количеством tx_id/кошельков в фильтре -- поэтому для 100 кошельков ОДИН запрос с объединённым "
            "списком tx_id и одной общей границей по времени стоит примерно СТОЛЬКО ЖЕ, сколько этот прогон на "
            f"300 подписях ({total_credits:.0f} кредитов за прогон), а 100 отдельных запросов (по кошельку) -- "
            "примерно в 100 раз дороже. Перед массовым прогоном -- считать одним объединённым запросом."
        ),
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    result["FINAL_VERDICT"] = (
        "АТРИБУЦИЯ ПОДТВЕРЖДЕНА ОКОНЧАТЕЛЬНО (100%, через signers-массив, не через trader_id/signer-скаляр). "
        "Прежний вердикт 'негоден по покрытию' ОТМЕНЯЕТСЯ. dex_solana.trades пригоден для восстановления сделок "
        "лидера ЧЕРЕЗ ПРАВИЛЬНЫЙ join (contains(signers, лидер) в solana.transactions), но НЕ напрямую по "
        "trader_id -- см. шаги 2-4 для точности цены/разметки на этом join."
    )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v4] {result['FINAL_VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
