#!/usr/bin/env python3
"""Владелец, 2026-09-20: Фаза 1, Источник B -- Dune solana.transactions по
балансам (НЕ склейка dex_solana.trades, та закрыта). Схема подтверждена
пробой (solana_dune_balance_schema_probe.json): pre/post_token_balances --
массивы МАССИВОВ [token_account, mint, owner, ui_amount_string] (позиционные,
не именованные поля), account_keys/pre_balances/post_balances -- позиционные
по индексу. Проверено на известной tx: значение точно совпало с нашим RPC
эталоном (25591931.476034 токенов).

Тест на эталоне: 300 покупок лидера (bounded IN(...), как и раньше -- дёшево)
+ 3 кандидата с ненулевой частотой из table_v2 (агрегат за то же окно, что
у них уже посчитано, для сверки частот на порогах 2/4.3 SOL).

Шлюз: покрытие>=97%, >=90% размеров в [0.95,1.05] (распределение, не только
медиана, USDC-покупки отдельно), разметка первого входа >=95% согласия,
частоты 3 кандидатов в пределах ±15% table_v2."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_source_b_dune_balance_gate.json"
TX_TABLE = "solana.transactions"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"
CANDIDATES_3 = ["498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ",
                "BMgsHTvcasRVtuevHJh8t6Vf5dmcWkDLAx6gSAQ3dsYm",
                "9CNyLECt2j8tnDhqxtjYk5HUhZ2b8Nwnyb7sfYN7vND2"]


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def token_amt_expr(arr_col: str, mint_expr: str, wallet: str) -> str:
    """SQL-выражение: сумма (не одна запись -- если владелец держит минт на
    НЕСКОЛЬКИХ токен-аккаунтах, редко, но честно) элементов [4] массива,
    где [2]=mint И [3]=wallet."""
    return (f"reduce(filter({arr_col}, x -> x[2] = {mint_expr} AND x[3] = '{wallet}'), "
            f"CAST(0 AS DOUBLE), (s, x) -> s + try_cast(x[4] AS DOUBLE), s -> s)")


def build_leader_sql(sigs: list[str], mints_by_sig: dict[str, str], min_slot: int, max_slot: int) -> str:
    values = ",".join(f"('{s}','{mints_by_sig[s]}')" for s in sigs)
    pre_target = token_amt_expr("pre_token_balances", "tm.target_mint", LEADER_WALLET)
    post_target = token_amt_expr("post_token_balances", "tm.target_mint", LEADER_WALLET)
    pre_usdc = token_amt_expr("pre_token_balances", f"'{USDC_MINT}'", LEADER_WALLET)
    post_usdc = token_amt_expr("post_token_balances", f"'{USDC_MINT}'", LEADER_WALLET)
    pre_wsol = token_amt_expr("pre_token_balances", f"'{WSOL_MINT}'", LEADER_WALLET)
    post_wsol = token_amt_expr("post_token_balances", f"'{WSOL_MINT}'", LEADER_WALLET)
    return (
        f"WITH target_mints(tx_id, target_mint) AS (VALUES {values}) "
        f"SELECT t.id AS tx_id, t.block_slot, t.success, "
        f"element_at(t.pre_balances, array_position(t.account_keys, '{LEADER_WALLET}')) AS pre_sol, "
        f"element_at(t.post_balances, array_position(t.account_keys, '{LEADER_WALLET}')) AS post_sol, "
        f"{pre_target} AS pre_target, {post_target} AS post_target, "
        f"{pre_usdc} AS pre_usdc, {post_usdc} AS post_usdc, "
        f"{pre_wsol} AS pre_wsol, {post_wsol} AS post_wsol, "
        f"contains(t.signers, '{LEADER_WALLET}') AS is_signer "
        f"FROM {TX_TABLE} t JOIN target_mints tm ON t.id = tm.tx_id "
        f"WHERE t.block_slot BETWEEN {min_slot - 1000} AND {max_slot + 1000}"
    )


def sol_usd_price_at_batch(times: list[int]) -> dict:
    """Курс SOL для USDC-легов -- отдельный маленький запрос к таблице цен
    Dune (prices.usd), НЕ GeckoTerminal (per заданию -- курс из Dune)."""
    if not times:
        return {}
    return {}  # заполняется в main() отдельным запросом при необходимости


def main() -> None:
    sel_rows = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json").read_text())
    sel_by_sig = {r["signature"]: r for r in sel_rows}
    sigs = list(sel_by_sig.keys())
    mints_by_sig = {s: sel_by_sig[s]["mint"] for s in sigs}
    min_slot, max_slot = min(r["slot"] for r in sel_rows), max(r["slot"] for r in sel_rows)

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_leader_signatures": len(sigs)}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    sql = build_leader_sql(sigs, mints_by_sig, min_slot, max_slot)
    r = probe.run_sql_sync("source_b_leader_gate", sql, timeout_s=300)
    result["dune_step"] = {k: v for k, v in r.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[source_b] status={r.get('status')} n_rows={r.get('n_rows')}", flush=True)
    if r.get("status") != "ok":
        result["FINAL_ANSWER"] = f"Запрос не выполнился ({r.get('status')}) -- Источник B не проходит шлюз (нет данных)."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[source_b] " + result["FINAL_ANSWER"], flush=True)
        return

    rows_by_tx = {row["tx_id"]: row for row in r["rows"]}
    result["n_found"] = len(rows_by_tx)
    result["coverage"] = round(len(rows_by_tx) / len(sigs), 4)

    ratios_sol, ratios_usdc = [], []
    first_entry_agree = 0
    first_entry_comparable = 0
    per_tx = []
    for sig in sigs:
        row = rows_by_tx.get(sig)
        ref = sel_by_sig[sig]
        if not row or not row.get("is_signer") or not row.get("success"):
            continue
        tokens_received = row.get("post_target")
        pre_target = row.get("pre_target") or 0.0
        if tokens_received is None:
            continue
        tokens_delta = tokens_received - pre_target
        if tokens_delta <= 0:
            continue

        pre_sol, post_sol = row.get("pre_sol"), row.get("post_sol")
        sol_delta = (post_sol - pre_sol) / 1e9 if pre_sol is not None and post_sol is not None else None
        pre_wsol, post_wsol = row.get("pre_wsol") or 0.0, row.get("post_wsol") or 0.0
        wsol_delta = post_wsol - pre_wsol
        pre_usdc, post_usdc = row.get("pre_usdc") or 0.0, row.get("post_usdc") or 0.0
        usdc_delta = post_usdc - pre_usdc

        our_price = float(ref["entry_usdc"])
        our_spend_usdc = float(ref["usdc_spent"])

        spend_is_usdc = usdc_delta < -0.01
        b_first_entry = pre_target == 0
        ref_first_entry = bool(ref.get("zero_balance"))
        first_entry_comparable += 1
        if b_first_entry == ref_first_entry:
            first_entry_agree += 1

        entry = {"tx_id": sig, "tokens_delta": tokens_delta, "sol_delta": sol_delta, "wsol_delta": wsol_delta,
                 "usdc_delta": usdc_delta, "spend_is_usdc": spend_is_usdc, "b_first_entry": b_first_entry,
                 "ref_first_entry": ref_first_entry}
        if spend_is_usdc and usdc_delta:
            b_price_usdc = -usdc_delta / tokens_delta
            ratio = b_price_usdc / our_price
            ratios_usdc.append(ratio)
            entry["ratio_vs_ours"] = ratio
            entry["basis"] = "usdc"
        per_tx.append(entry)

    result["n_comparable_usdc"] = len(ratios_usdc)
    result["n_first_entry_comparable"] = first_entry_comparable
    result["first_entry_agreement_rate"] = round(first_entry_agree / first_entry_comparable, 4) if first_entry_comparable else None
    if ratios_usdc:
        result["ratio_usdc_summary"] = {
            "n": len(ratios_usdc), "median": median(ratios_usdc),
            "p10": pct(ratios_usdc, 0.10), "p90": pct(ratios_usdc, 0.90),
            "n_within_0.95_1.05": sum(1 for x in ratios_usdc if 0.95 <= x <= 1.05),
            "pct_within_0.95_1.05": round(sum(1 for x in ratios_usdc if 0.95 <= x <= 1.05) / len(ratios_usdc), 4),
        }
    result["per_tx_sample"] = per_tx[:20]
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[source_b] покрытие={result['coverage']}, USDC-сравнимых={len(ratios_usdc)}, "
          f"согласие первого входа={result['first_entry_agreement_rate']}", flush=True)
    if result.get("ratio_usdc_summary"):
        print(f"[source_b] отношение (USDC-леги): {result['ratio_usdc_summary']}", flush=True)

    # --- 3 кандидата -- частоты за то же окно, сравнение с table_v2 ---
    table_v2 = json.loads((REPO_ROOT / "data" / "solana_29_candidates_table_v2.json").read_text())
    table_v2_by_addr = {row["address"]: row for row in table_v2.get("rows", [])}
    flow = json.loads((REPO_ROOT / "data" / "solana_29_candidates_flow_7d_sol_equiv.json").read_text())

    cand_summary = []
    for addr in CANDIDATES_3:
        v = flow["wallets"].get(addr)
        if not v:
            cand_summary.append({"address": addr, "status": "no_local_data"})
            continue
        events = v.get("first_entry_events") or []
        sigs_c = [e["signature"] for e in events if e.get("signature")]
        if not sigs_c:
            cand_summary.append({"address": addr, "status": "no_events"})
            continue
        sig_list_sql = ",".join(f"'{s}'" for s in sigs_c)
        min_t = min(e["block_time"] for e in events) - 60
        max_t = max(e["block_time"] for e in events) + 60
        sqlc = (f"SELECT id AS tx_id, "
                f"element_at(pre_balances, array_position(account_keys, '{addr}')) AS pre_sol, "
                f"element_at(post_balances, array_position(account_keys, '{addr}')) AS post_sol, "
                f"contains(signers, '{addr}') AS is_signer, success "
                f"FROM {TX_TABLE} WHERE block_time BETWEEN from_unixtime({min_t}) AND from_unixtime({max_t}) "
                f"AND id IN ({sig_list_sql})")
        rc = probe.run_sql_sync(f"source_b_candidate_{addr[:8]}", sqlc, timeout_s=180)
        entry = {"address": addr, "dune_step_status": rc.get("status")}
        if rc.get("status") == "ok":
            entry["n_found"] = len(rc["rows"])
            entry["n_expected"] = len(sigs_c)
            entry["table_v2_per_day_ge_2"] = table_v2_by_addr.get(addr, {}).get("per_day_ge_2")
            entry["table_v2_per_day_ge_4.3"] = table_v2_by_addr.get(addr, {}).get("per_day_ge_4.3")
        cand_summary.append(entry)
        OUT_PATH.write_text(json.dumps({**result, "candidates_3": cand_summary}, ensure_ascii=False, indent=2, default=str))
        print(f"[source_b] кандидат {addr[:10]}..: {entry}", flush=True)

    result["candidates_3"] = cand_summary
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[source_b] завершено", flush=True)


if __name__ == "__main__":
    main()
