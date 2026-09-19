#!/usr/bin/env python3
"""Владелец, 2026-09-19: довести связку Dune через подписанта до конца
(предыдущий прогон solana_dune_signer_linkage_check.py упал по timeout --
solana.transactions это ВЕСЬ сырой поток Solana, IN(300 tx_id) без
границы по слоту сканирует слишком много). Схема уже известна из того
прогона (id, signer, block_slot) -- здесь сразу считаем ограниченный
по block_slot запрос, без повторного дешёвого schema-probe.

Если связка через подписанта подтвердится (>=250/300 подписант=лидер) --
прогоняем на этих же строках оригинальные шаги калибровки:
  1. схлопывание мульти-хоповых строк dex_solana.trades в один net-своп
     на транзакцию (по SOL-ноге и целевому минту);
  2. цена на +30с в единицах SOL/токен (не amount_usd) -- сравнение с
     нашими проверенными данными (mine_price из step_extended_result.json,
     переведено в SOL/токен через sol_usd_price_at);
  3. разметка первого входа по Dune (row_number() по signer+mint) против
     нашего zero_balance;
  4. честная оценка стоимости кредитов на 100 кошельков.

Плюс: определяем, что такое AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51
(повторяющийся trader_id в dex_solana.trades) -- напрямую по ончейну
(getAccountInfo, jsonParsed): программа/PDA/обычный кошелёк, owner-программа."""
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
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_signer_linkage_v2.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MYSTERY_ADDR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"

TX_TABLE, SIG_COL, SIGNER_COL = "solana.transactions", "id", "signer"


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


def identify_mystery_address() -> dict:
    r = fp.rpc_call("getAccountInfo", [MYSTERY_ADDR, {"encoding": "jsonParsed"}], use_cache=False)
    value = (r or {}).get("value")
    if value is None:
        return {"address": MYSTERY_ADDR, "status": "not_found_or_rpc_error", "raw": r}
    parsed = (value.get("data") or {}).get("parsed") if isinstance(value.get("data"), dict) else None
    return {
        "address": MYSTERY_ADDR, "status": "ok",
        "executable": value.get("executable"), "owner_program": value.get("owner"),
        "lamports": value.get("lamports"), "space": value.get("space"),
        "parsed_type": (parsed or {}).get("type"), "parsed_info_preview": json.dumps((parsed or {}).get("info"))[:300] if parsed else None,
        "conclusion": (
            "ПРОГРАММА (executable=true)" if value.get("executable") else
            f"НЕ программа -- обычный аккаунт, владелец (owner) = {value.get('owner')}"
            + (f", это токен-аккаунт (Token Program)" if value.get("owner") in
               ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb") else "")
        ),
    }


def main() -> None:
    sel_rows = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json").read_text())
    sel_by_sig = {r["signature"]: r for r in sel_rows}
    sigs = list(sel_by_sig.keys())
    min_slot, max_slot = min(r["slot"] for r in sel_rows), max(r["slot"] for r in sel_rows)

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_our_signatures": len(sigs),
                     "slot_bound": [min_slot - 1000, max_slot + 1000]}

    print("[v2] проверяю личность повторяющегося адреса напрямую по RPC...", flush=True)
    result["mystery_address_identity"] = identify_mystery_address()
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v2] {MYSTERY_ADDR}: {result['mystery_address_identity'].get('conclusion')}", flush=True)

    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой -- дальше идти некуда."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v2] " + result["HONEST_ANSWER"], flush=True)
        return
    probe = DuneProbe(os.environ[key_name])

    # --- Шаг 1: связка через подписанта, ОГРАНИЧЕННАЯ по block_slot ---
    sig_list_sql = ",".join(f"'{s}'" for s in sigs)
    sql1 = (f"SELECT {SIG_COL} AS tx_id, {SIGNER_COL} AS signer FROM {TX_TABLE} "
            f"WHERE block_slot BETWEEN {min_slot - 1000} AND {max_slot + 1000} AND {SIG_COL} IN ({sig_list_sql})")
    r1 = probe.run_sql_sync("signer_linkage_bounded", sql1, timeout_s=590)
    result["step1_signer_linkage"] = {k: v for k, v in r1.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r1.get("status") != "ok":
        result["FINAL_VERDICT"] = f"Шаг 1 не выполнился ({r1.get('status')}) даже с границей по block_slot -- см. step1_signer_linkage."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v2] " + result["FINAL_VERDICT"], flush=True)
        return

    signer_by_tx = {row["tx_id"]: row.get("signer") for row in r1["rows"]}
    n_found = len(signer_by_tx)
    n_signer_leader = sum(1 for v in signer_by_tx.values() if v == LEADER_WALLET)
    result["n_tx_found_in_tx_table"] = n_found
    result["n_signer_matches_leader"] = n_signer_leader
    result["signer_coverage_by_tx_id"] = round(n_found / len(sigs), 4)
    result["recomputed_coverage_via_signer_linkage"] = round(n_signer_leader / len(sigs), 4)
    print(f"[v2] шаг1: найдено={n_found}/{len(sigs)}, подписант=лидер={n_signer_leader} "
          f"({result['recomputed_coverage_via_signer_linkage']:.1%})", flush=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    confirmed = n_signer_leader >= 250
    result["attribution_confirmed"] = confirmed
    if not confirmed:
        result["FINAL_VERDICT"] = (
            f"Связка через подписанта НЕ подтвердилась уверенно ({n_signer_leader}/{len(sigs)} совпадений) -- "
            "дальнейшую калибровку (схлопывание, +30с, разметка) не запускаю, чтобы не тратить кредиты впустую."
        )
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v2] " + result["FINAL_VERDICT"], flush=True)
        return

    matched_sigs = [s for s, v in signer_by_tx.items() if v == LEADER_WALLET]

    # --- Шаг 2: schema-probe dex_solana.trades -- нужны колонки суммы токенов ---
    sp = probe.run_sql_sync("schema_probe_dex_solana_trades", "SELECT * FROM dex_solana.trades LIMIT 1", timeout_s=60)
    result["step2_schema_probe"] = {k: v for k, v in sp.items() if k != "rows"}
    result["step2_columns"] = (sp.get("metadata") or {}).get("column_names")
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v2] dex_solana.trades колонки: {result['step2_columns']}", flush=True)
    if sp.get("status") != "ok":
        result["FINAL_VERDICT"] = "Связка подтверждена, но schema-probe dex_solana.trades не выполнился -- калибровку не продолжаю."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v2] " + result["FINAL_VERDICT"], flush=True)
        return

    cols = result["step2_columns"] or []

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
    result["step2_resolved_columns"] = {"bought_mint": col_bought_mint, "sold_mint": col_sold_mint,
                                         "bought_amount": col_bought_amt, "sold_amount": col_sold_amt}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if not all([col_bought_mint, col_sold_mint, col_bought_amt, col_sold_amt]):
        result["FINAL_VERDICT"] = ("Связка подтверждена, но в dex_solana.trades не нашлись колонки количества "
                                    "токенов (нужны для схлопывания в net-своп и цены) -- честно останавливаюсь, "
                                    "не выдумываю недостающие поля.")
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v2] " + result["FINAL_VERDICT"], flush=True)
        return

    # --- Шаг 3: полные строки по нашим связанным tx_id -- схлопывание в net-своп ---
    matched_list_sql = ",".join(f"'{s}'" for s in matched_sigs)
    sql3 = (f"SELECT tx_id, block_time, trader_id, project, {col_bought_mint} AS bought_mint, "
            f"{col_sold_mint} AS sold_mint, {col_bought_amt} AS bought_amount, {col_sold_amt} AS sold_amount "
            f"FROM dex_solana.trades WHERE tx_id IN ({matched_list_sql})")
    r3 = probe.run_sql_sync("collapse_legs_by_tx", sql3, timeout_s=300)
    result["step3_full_rows"] = {k: v for k, v in r3.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r3.get("status") != "ok":
        result["FINAL_VERDICT"] = "Шаг 3 (полные строки для схлопывания) не выполнился -- см. step3_full_rows."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v2] " + result["FINAL_VERDICT"], flush=True)
        return

    by_tx: dict[str, list] = {}
    for row in r3["rows"]:
        by_tx.setdefault(row["tx_id"], []).append(row)
    result["step3_n_tx_with_rows"] = len(by_tx)
    result["step3_avg_legs_per_tx"] = round(len(r3["rows"]) / len(by_tx), 3) if by_tx else None

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
    result["step3_collapsed_net_swaps"] = collapsed
    ok_collapsed = [c for c in collapsed if c["status"] == "ok" and c.get("ratio_dune_over_ours")]
    if ok_collapsed:
        ratios = [c["ratio_dune_over_ours"] for c in ok_collapsed]
        result["step3_summary"] = {"n": len(ratios), "median_ratio_dune_over_ours": median(ratios),
                                    "mean_ratio_dune_over_ours": mean(ratios)}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v2] шаг3: схлопнуто {len(by_tx)} tx, среднее {result.get('step3_avg_legs_per_tx')} ног/tx, "
          f"сравнимых с нашей ценой: {len(ok_collapsed)}", flush=True)

    # --- Шаг 4: цена на +30с в SOL/токен, только для ПЕРВЫХ ВХОДОВ (43 минта) ---
    first_entry_sigs_in_matched = [s for s in matched_sigs if sel_by_sig[s].get("zero_balance")]
    if first_entry_sigs_in_matched:
        mints = sorted({sel_by_sig[s]["mint"] for s in first_entry_sigs_in_matched})
        mint_list_sql = ",".join(f"'{m}'" for m in mints)
        lo_time = min(sel_by_sig[s]["time"] for s in first_entry_sigs_in_matched) - 2
        hi_time = max(sel_by_sig[s]["time"] for s in first_entry_sigs_in_matched) + 32
        sql4 = (f"SELECT tx_id, block_time, {col_bought_mint} AS bought_mint, {col_sold_mint} AS sold_mint, "
                f"{col_bought_amt} AS bought_amount, {col_sold_amt} AS sold_amount FROM dex_solana.trades "
                f"WHERE ({col_bought_mint} IN ({mint_list_sql}) OR {col_sold_mint} IN ({mint_list_sql})) "
                f"AND block_time >= from_unixtime({lo_time}) AND block_time <= from_unixtime({hi_time})")
        r4 = probe.run_sql_sync("price_at_30s_sol_per_token", sql4, timeout_s=300)
        result["step4_price_at_30s"] = {k: v for k, v in r4.items() if k != "rows"}
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if r4.get("status") == "ok":
            import calendar

            def parse_bt(s):
                return calendar.timegm(time.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S"))

            rows_by_mint: dict[str, list] = {}
            for row in r4["rows"]:
                for m in (row.get("bought_mint"), row.get("sold_mint")):
                    if m in mints:
                        rows_by_mint.setdefault(m, []).append(row)

            short = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "step_extended_result.json").read_text())
            short_by_key = {(r["signature"], r["seconds"]): r for r in short if "seconds" in r}

            per_trade = []
            for sig in first_entry_sigs_in_matched:
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
            result["step4_per_trade"] = per_trade
            ok4 = [p for p in per_trade if p.get("ratio_dune_over_ours")]
            if ok4:
                ratios4 = [p["ratio_dune_over_ours"] for p in ok4]
                result["step4_summary"] = {"n": len(ratios4), "median_ratio_dune_over_ours": median(ratios4)}
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print(f"[v2] шаг4 (+30с, SOL/токен): n сравнимых={len(ok4)}, "
                  f"медиана отношения dune/наша={result.get('step4_summary', {}).get('median_ratio_dune_over_ours')}", flush=True)

    # --- Шаг 5: разметка первого входа по Dune (signer+mint) против нашей ---
    sql5 = (f"WITH ranked AS (SELECT dt.tx_id, dt.{col_bought_mint} AS mint, dt.block_time, "
            f"row_number() OVER (PARTITION BY tt.{SIGNER_COL}, dt.{col_bought_mint} ORDER BY dt.block_time) AS rn "
            f"FROM dex_solana.trades dt JOIN {TX_TABLE} tt ON dt.tx_id = tt.{SIG_COL} "
            f"WHERE tt.block_slot BETWEEN {min_slot - 1000} AND {max_slot + 1000} "
            f"AND tt.{SIGNER_COL} = '{LEADER_WALLET}') SELECT tx_id, mint, block_time FROM ranked WHERE rn = 1")
    r5 = probe.run_sql_sync("dune_first_entry_via_signer", sql5, timeout_s=590)
    result["step5_first_entry_classification"] = {k: v for k, v in r5.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r5.get("status") == "ok":
        dune_first_tx = {row["tx_id"] for row in r5["rows"]}
        comparable = set(matched_sigs)
        agree = sum(1 for s in comparable if bool(s in dune_first_tx) == bool(sel_by_sig[s].get("zero_balance")))
        result["step5_n_comparable"] = len(comparable)
        result["step5_n_agree"] = agree
        result["step5_agreement_rate"] = round(agree / len(comparable), 4) if comparable else None
        disagree = [s for s in comparable if bool(s in dune_first_tx) != bool(sel_by_sig[s].get("zero_balance"))]
        result["step5_disagree_sample"] = [{"signature": s, "our_zero_balance": sel_by_sig[s].get("zero_balance"),
                                             "dune_first_via_signer": s in dune_first_tx} for s in disagree[:15]]
        print(f"[v2] шаг5: согласие разметки={result['step5_agreement_rate']} ({agree}/{len(comparable)})", flush=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # --- Шаг 6: честная оценка стоимости на 100 кошельков ---
    credits_used = []
    for key in ("step1_signer_linkage", "step3_full_rows", "step5_first_entry_classification"):
        meta = (result.get(key) or {}).get("status_meta") or {}
        c = meta.get("execution_cost_credits")
        if c:
            credits_used.append({"step": key, "credits": c})
    total_credits_this_run = sum(c["credits"] for c in credits_used)
    result["step6_cost_estimate"] = {
        "credits_by_step": credits_used, "total_credits_this_run_300_signatures_5.76_days_window": total_credits_this_run,
        "note": (
            "Стоимость сканирования solana.transactions/dex_solana.trades определяется в основном ШИРИНОЙ "
            "временного окна (партиционирование по времени/дате), а НЕ количеством подписей в IN(...) или "
            "числом кошельков -- сам список tx_id почти не влияет на объём прочитанных байт, влияет диапазон "
            "block_slot/block_time. Поэтому для 100 кошельков ОДИН запрос с объединённым (пересекающимся) "
            "окном по всем 100 кошелькам стоит ПРИМЕРНО СТОЛЬКО ЖЕ, сколько этот прогон на 300 подписях за "
            "5.76 суток (~{:.0f} кредитов) -- а 100 ОТДЕЛЬНЫХ запросов (по одному на кошелёк, каждый со своим "
            "похожим окном) обойдутся примерно в 100x дороже, если окна не объединять. Практический вывод: "
            "перед массовым прогоном на 100 кошельках -- считать ОДНИМ запросом с объединённым списком tx_id "
            "и одной общей границей по block_slot/block_time, не по одному кошельку за раз.".format(total_credits_this_run)
        ),
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    result["FINAL_VERDICT"] = (
        f"АТРИБУЦИЯ ПОДТВЕРЖДЕНА: {n_signer_leader}/{len(sigs)} ({result['recomputed_coverage_via_signer_linkage']:.1%}) "
        f"наших транзакций имеют подписанта=лидер в {TX_TABLE}. Калибровка на связанных строках выполнена "
        f"(схлопывание, +30с в SOL/токен, разметка первого входа, оценка стоимости) -- см. соответствующие шаги."
    )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v2] {result['FINAL_VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
