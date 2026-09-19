#!/usr/bin/env python3
"""Владелец: 20 последних покупок кошелька DT8hib8jY4CGJcmcqcinVGYh5zzVPZAV3iosdQF9a6jX
(бот через AKBot, 20 WSOL -> CC на Pump.fun AMM) -- только RPC.
Для каждой: слот относительно сделки источника (лидера,
JFPJEgfXH2PEfssK799nob9KE6J5Lk4PLcD1xRqqieTUP6rxksXTZAR7eA1jrVU7ejychsqwf6JvN2pd4M5hAqY),
приоритет в сети (compute_unit_price), ВСЕ system-transfer со кошелька
(честно, без предположения о том, что "чаевые" -- это конкретная сумма
-- см. solana_batch_fee_change_first_trade.py, тот же принцип), размер
сделки (WSOL). Корреляция суммы переводов с размером сделки/числом
других сделок в том же слоте (прокси загрузки) -- считается локально
на этих же 20 точках, без выдумывания механизма AKBot."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch_fee_change_first_trade import extract_compute_budget, extract_all_system_transfers  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_akbot_wallet_probe_result.json"
WALLET = "DT8hib8jY4CGJcmcqcinVGYh5zzVPZAV3iosdQF9a6jX"
LEADER_SIG = "JFPJEgfXH2PEfssK799nob9KE6J5Lk4PLcD1xRqqieTUP6rxksXTZAR7eA1jrVU7ejychsqwf6JvN2pd4M5hAqY"
WSOL = "So11111111111111111111111111111111111111112"
N_RECENT = 20


def wallet_wsol_delta(tx: dict, wallet: str) -> float | None:
    meta = tx.get("meta") or {}
    pre = {b["mint"]: D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
           for b in (meta.get("preTokenBalances") or []) if b.get("owner") == wallet}
    post = {b["mint"]: D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
            for b in (meta.get("postTokenBalances") or []) if b.get("owner") == wallet}
    if WSOL not in pre and WSOL not in post:
        # своп мог идти через нативный SOL (лампорты), не через WSOL-токен-аккаунт
        return None
    return float(post.get(WSOL, D(0)) - pre.get(WSOL, D(0)))


def native_sol_delta(tx: dict, wallet: str) -> float | None:
    meta = tx.get("meta") or {}
    keys = tx["transaction"]["message"]["accountKeys"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    if wallet not in keys:
        return None
    idx = keys.index(wallet)
    pre = (meta.get("preBalances") or [None] * len(keys))[idx]
    post = (meta.get("postBalances") or [None] * len(keys))[idx]
    if pre is None or post is None:
        return None
    return (post - pre) / 1e9


def main() -> None:
    leader_tx = fp.get_transaction(LEADER_SIG)
    leader_slot = leader_tx["slot"] if leader_tx else None

    sigs = fp.get_signatures_for_address(WALLET, limit=N_RECENT)
    rows = []
    for h in sigs:
        sig = h["signature"]
        if h.get("err") is not None:
            rows.append({"signature": sig, "slot": h.get("slot"), "status": "err_transaction_skipped"})
            continue
        tx = fp.get_transaction(sig)
        if tx is None:
            rows.append({"signature": sig, "slot": h.get("slot"), "status": "getTransaction_null"})
            continue
        cb = extract_compute_budget(tx)
        transfers = extract_all_system_transfers(tx, WALLET)
        wsol_delta = wallet_wsol_delta(tx, WALLET)
        native_delta = native_sol_delta(tx, WALLET)
        trade_size_sol = -wsol_delta if (wsol_delta is not None and wsol_delta < 0) else None
        rows.append({
            "signature": sig, "slot": tx["slot"],
            "slot_offset_from_leader": (tx["slot"] - leader_slot) if leader_slot is not None else None,
            "block_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tx["blockTime"])),
            "compute_unit_limit": cb.get("compute_unit_limit"),
            "compute_unit_price_microlamports": cb.get("compute_unit_price_microlamports"),
            "network_fee_sol": (tx.get("meta") or {}).get("fee", 0) / 1e9,
            "wsol_token_account_delta": wsol_delta,
            "native_sol_delta_total": native_delta,
            "trade_size_sol_wsol_leg": trade_size_sol,
            "all_system_transfers_from_wallet": transfers,
            "n_transfers": len(transfers),
            "total_transfers_sol": round(sum(t["sol"] for t in transfers), 9) if transfers else 0.0,
        })

    # честная агрегация получателей переводов -- какие адреса повторяются чаще всего
    dest_counts: dict[str, list[float]] = {}
    for r in rows:
        for t in r.get("all_system_transfers_from_wallet", []) or []:
            dest_counts.setdefault(t["destination"], []).append(t["sol"])
    dest_summary = [{"destination": d, "n_occurrences": len(v), "amounts_sol": v,
                      "amount_constant": len(set(round(x, 6) for x in v)) == 1}
                     for d, v in sorted(dest_counts.items(), key=lambda kv: -len(kv[1]))]

    # корреляция: сумма переводов vs размер сделки (там, где оба известны)
    pairs = [(r["trade_size_sol_wsol_leg"], r["total_transfers_sol"]) for r in rows
             if r.get("trade_size_sol_wsol_leg") is not None and r.get("total_transfers_sol") is not None]
    correlation_note = None
    if len(pairs) >= 3:
        sizes = [p[0] for p in pairs]
        transfers_sol = [p[1] for p in pairs]
        n = len(pairs)
        mean_x, mean_y = sum(sizes) / n, sum(transfers_sol) / n
        cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / n
        var_x = sum((x - mean_x) ** 2 for x in sizes) / n
        var_y = sum((y - mean_y) ** 2 for y in transfers_sol) / n
        corr = cov / ((var_x * var_y) ** 0.5) if var_x > 0 and var_y > 0 else None
        correlation_note = {"n_pairs": n, "pearson_r_transfers_vs_trade_size": corr}

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wallet": WALLET, "leader_signature": LEADER_SIG, "leader_slot": leader_slot,
        "n_recent_requested": N_RECENT, "n_rows": len(rows), "rows": rows,
        "destination_summary": dest_summary,
        "correlation_transfers_vs_trade_size": correlation_note,
        "HONEST_NOTE": ("all_system_transfers_from_wallet -- ВСЕ system-transfer со кошелька, без предположения, "
                         "какой из них 'чаевые' и какой 'перевод на AKBot' -- см. destination_summary: адрес, "
                         "повторяющийся чаще всего с ПОСТОЯННОЙ суммой, похож на приоритетный tip-релей; "
                         "переменные по сумме переводы -- кандидаты на комиссию AKBot, пропорциональную сделке, "
                         "но это ИНТЕРПРЕТАЦИЯ по паттерну, не подтверждённый факт из документации AKBot."),
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"n_rows": len(rows), "destination_summary": dest_summary,
                       "correlation": correlation_note}, ensure_ascii=False, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
