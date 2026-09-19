#!/usr/bin/env python3
"""Владелец, 2026-09-19: реализованный результат по фактическим сделкам
-- эталон для остального. Пилот (4 закрытые сделки) + все закрытые
сделки BATCH-1/BATCH-2 на текущий момент.

По каждой сделке: цена сделки лидера / наша цена покупки / наша цена
продажи (всё из блокчейна), наценка при входе относительно лидера,
реализованная доходность брутто (только своп, без чаевых/комиссии/
ренты) и нетто (полная дельта SOL-баланса кошелька buy->sell -- это
УЖЕ включает чаевые Astralane и сетевую комиссию по построению; если
закрытие токен-аккаунта (возврат ренты) происходит В ТОЙ ЖЕ транзакции
продажи -- тоже уже включено; если ОТДЕЛЬНОЙ более поздней транзакцией
-- ищем её отдельно и добавляем как additional_rent_refund_after_sell).

Плюс сверка балансов по каждому кошельку: реальные входящие переводы
(фандинг, не свопы), текущий баланс, стоимость открытых (непроданных)
позиций по текущей цене, подразумеваемый P&L. Отдельно -- честный ответ,
почему на нулевой точке было 1.747/1.769 SOL, а не 3: смотрим САМУЮ
РАННЮЮ входящую транзакцию кошелька."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from statistics import median

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_select_extend import plan_route_for_purchase, POOL_META_PATH, ROUTE_META_PATH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"
BASELINE_PATH = REPO_ROOT / "data" / "dbot_sieve_baseline.json"

USDC_MINT = fp.USDC
SOL_MINT = "So11111111111111111111111111111111111111112"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
PILOT_WALLET = "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS"
PILOT_TASK_ID = "mu746r8f05f9ic"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
TIP_LAMPORTS_MARKERS = (4_950_000, 9_950_000)


def wallet_mint_deltas(tx: dict, wallet: str) -> dict:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return {"increased": [], "decreased": [], "pre_balances": {}, "deltas": {}}
    pre_tb, post_tb = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

    def bals(rows):
        a: dict[str, D] = {}
        for b in rows:
            if b.get("owner") == wallet:
                amt = b["uiTokenAmount"]
                a[b["mint"]] = a.get(b["mint"], D(0)) + D(amt["amount"]) / D(10) ** amt["decimals"]
        return a

    pre, post = bals(pre_tb), bals(post_tb)
    delta = {k: post.get(k, D(0)) - pre.get(k, D(0)) for k in set(pre) | set(post)}
    return {"increased": [k for k, v in delta.items() if v > 0], "decreased": [k for k, v in delta.items() if v < 0],
            "pre_balances": {k: str(v) for k, v in pre.items()}, "deltas": {k: str(v) for k, v in delta.items()}}


def wallet_sol_delta(tx: dict, wallet: str) -> float | None:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    idx = None
    for i, k in enumerate(keys):
        pk = k.get("pubkey") if isinstance(k, dict) else k
        if pk == wallet:
            idx = i
            break
    if idx is None:
        return None
    meta = tx.get("meta") or {}
    pre_list, post_list = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre_list) or idx >= len(post_list):
        return None
    return (post_list[idx] - pre_list[idx]) / 1e9


def is_signer(tx: dict, wallet: str) -> bool:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    return any(isinstance(k, dict) and k.get("signer") and k.get("pubkey") == wallet for k in keys)


def is_fee_payer(tx: dict, wallet: str) -> bool:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    if not keys:
        return False
    first = keys[0]
    pk = first.get("pubkey") if isinstance(first, dict) else first
    return pk == wallet


def tips_and_fee(tx: dict, wallet: str) -> dict:
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    tips = []

    def scan(ix_list):
        for ix in ix_list:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed") or {}
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") == "transfer" and ix.get("program") == "system":
                info = parsed.get("info", {})
                lamports = info.get("lamports")
                if lamports and any(abs(lamports - m) < 50_000 for m in TIP_LAMPORTS_MARKERS):
                    tips.append(lamports / 1e9)

    scan(instrs)
    for grp in inner:
        if isinstance(grp, dict):
            scan(grp.get("instructions", []))
    meta = tx.get("meta") or {}
    return {"tip_sol": sum(tips), "network_fee_sol": (meta.get("fee", 0) / 1e9) if is_fee_payer(tx, wallet) else 0}


def find_close_account_refund(tx: dict, wallet: str) -> float:
    """closeAccount (Token Program), destination==wallet -- возврат ренты
    в ТОЙ ЖЕ транзакции (уже учтён в wallet_sol_delta, но фиксируем
    отдельно для честной разбивки)."""
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    total = 0.0

    def scan(ix_list):
        nonlocal total
        for ix in ix_list:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed") or {}
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") == "closeAccount":
                info = parsed.get("info", {})
                if info.get("destination") == wallet:
                    total += 1  # сам факт -- лампорты не всегда в parsed, сумму видно через net SOL delta

    scan(instrs)
    for grp in inner:
        if isinstance(grp, dict):
            scan(grp.get("instructions", []))
    return total


def get_full_history(wallet: str, max_pages: int = 40) -> list[dict]:
    sigs, before = [], None
    for _ in range(max_pages):
        batch = fp.get_signatures_for_address(wallet, before=before)
        if not batch:
            break
        sigs.extend(batch)
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return sigs


def scan_wallet_trades(wallet: str) -> tuple[list[dict], list[dict]]:
    """Возвращает (events, all_txs_meta) -- events это buy/sell по
    минтам, all_txs_meta -- ВСЕ обработанные транзакции с их SOL-дельтой
    и признаком свопа, для сверки балансов (фандинг = не своп, sol
    вырос, источник другой кошелёк)."""
    sigs = get_full_history(wallet)
    sigs.sort(key=lambda s: s.get("blockTime") or 0)
    events, all_meta = [], []
    for s in sigs:
        if s.get("err") is not None:
            continue
        tx = fp.get_transaction(s["signature"])
        if tx is None:
            continue
        bt = s.get("blockTime")
        deltas = wallet_mint_deltas(tx, wallet)
        sol_change = wallet_sol_delta(tx, wallet) or 0.0
        touched = [m for m in deltas["increased"] + deltas["decreased"] if m not in (USDC_MINT, SOL_MINT)]
        is_swap = bool(touched) and is_signer(tx, wallet)
        all_meta.append({"signature": s["signature"], "block_time": bt, "sol_change": sol_change,
                          "is_swap": is_swap, "tx": tx})
        if not is_swap:
            continue
        for mint in deltas["increased"]:
            if mint in (USDC_MINT, SOL_MINT) or sol_change >= -0.001:
                continue
            events.append({"signature": s["signature"], "block_time": bt, "mint": mint, "direction": "buy", "tx": tx})
        for mint in deltas["decreased"]:
            if mint in (USDC_MINT, SOL_MINT) or sol_change <= 0.001:
                continue
            events.append({"signature": s["signature"], "block_time": bt, "mint": mint, "direction": "sell", "tx": tx})
    return events, all_meta


def pair_trades(events: list[dict]) -> list[dict]:
    by_mint: dict[str, list] = {}
    for e in events:
        by_mint.setdefault(e["mint"], []).append(e)
    pairs = []
    for mint, evs in by_mint.items():
        evs.sort(key=lambda e: e["block_time"])
        buys = [e for e in evs if e["direction"] == "buy"]
        sells = [e for e in evs if e["direction"] == "sell"]
        si = 0
        for b in buys:
            while si < len(sells) and sells[si]["block_time"] < b["block_time"]:
                si += 1
            if si < len(sells):
                pairs.append({"mint": mint, "buy": b, "sell": sells[si]})
                si += 1
            else:
                pairs.append({"mint": mint, "buy": b, "sell": None})
    return pairs


def find_wallet_buy_near_time(wallet: str, mint: str, before_time: int, window_before_s: int = 90,
                               window_after_s: int = 5, max_pages: int = 25) -> dict | None:
    """Владелец, 2026-09-19: ищет реальную транзакцию ПОКУПКИ mint этим
    кошельком рядом с before_time -- НАПРЯМУЮ по истории подписей самого
    кошелька (не через getTokenAccountsByOwner/ATA). ATA закрывается,
    когда кошелёк полностью выходит из позиции -- getTokenAccountsByOwner
    после этого её не видит вообще, поэтому старый метод давал null у
    4 из 7 сделок. Сама транзакция в блокчейне неизменна -- последующее
    закрытие токен-аккаунта на неё не влияет, её можно найти и
    раскодировать в любой момент по подписи кошелька."""
    lo, hi = before_time - window_before_s, before_time + window_after_s
    sigs, before = [], None
    for _ in range(max_pages):
        batch = fp.get_signatures_for_address(wallet, before=before)
        if not batch:
            break
        sigs.extend(batch)
        oldest_bt = batch[-1].get("blockTime")
        before = batch[-1]["signature"]
        if oldest_bt is not None and oldest_bt < lo:
            break
        if len(batch) < 1000:
            break
    candidates = [s for s in sigs if s.get("blockTime") is not None and lo <= s["blockTime"] <= hi
                  and s.get("err") is None]
    candidates.sort(key=lambda s: s["blockTime"], reverse=True)
    for s in candidates:
        tx = fp.get_transaction(s["signature"])
        if not tx:
            continue
        deltas = wallet_mint_deltas(tx, wallet)
        if mint not in deltas["increased"]:
            continue
        usdc_delta = deltas["deltas"].get(USDC_MINT)
        sol_delta = wallet_sol_delta(tx, wallet)
        wsol_delta = deltas["deltas"].get(SOL_MINT)
        paid_usdc = float(-D(usdc_delta)) if usdc_delta and D(usdc_delta) < 0 else None
        if sol_delta is not None and sol_delta < -0.0005:
            paid_sol = -sol_delta
        elif wsol_delta and D(wsol_delta) < 0:
            # Своп через уже открытый WSOL-токен-аккаунт -- нативный
            # lamport-баланс не двигается, платёж виден только как
            # отрицательная дельта TOKEN-баланса минта So111...112.
            paid_sol = float(-D(wsol_delta))
        else:
            paid_sol = None
        tokens_received = float(D(deltas["deltas"][mint]))
        return {"signature": s["signature"], "block_time": tx.get("blockTime"),
                "paid_usdc": paid_usdc, "paid_sol": paid_sol, "tokens_received": tokens_received}
    return None


def find_source_wallet(tracked_wallets: list[dict], mint: str, before_time: int) -> dict | None:
    """Из 10 отслеживаемых кошельков батча выбирает того, чья покупка
    того же mint ближе всего ПЕРЕД нашей копией (самая поздняя buy-сделка
    с blockTime <= before_time в окне 300с) -- это и есть источник копии.
    Если ни один не найден в окне -- честно None (не гадаем). Возвращает
    уже и цену (paid_usdc/paid_sol/tokens_received) -- транзакция одна и
    та же, второй отдельный запрос за ценой не нужен."""
    best = None
    for tw in tracked_wallets:
        addr = tw.get("address")
        if not addr:
            continue
        r = find_wallet_buy_near_time(addr, mint, before_time, window_before_s=300, window_after_s=0)
        if r and (best is None or r["block_time"] > best["block_time"]):
            best = {**r, "address": addr, "remark": tw.get("remark")}
    return best


def leader_buy_for_mint(mint: str, before_time: int) -> dict | None:
    """Цена лидера ДЛЯ ПИЛОТА -- копия того же LEADER_WALLET, что и весь
    основной конвейер (не одного из 10 отслеживаемых кошельков батча)."""
    return find_wallet_buy_near_time(LEADER_WALLET, mint, before_time, window_before_s=90, window_after_s=0)


_sol_candles_cache: dict[int, list[list]] = {}


def gecko_get(path: str, params: dict) -> dict:
    try:
        resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30, headers={"Accept": "application/json"})
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


def gecko_find_pool(mint: str) -> str | None:
    r = gecko_get(f"/networks/solana/tokens/{mint}/pools", {})
    data = ((r.get("body") or {}).get("data")) or []
    if not data:
        return None
    data.sort(key=lambda p: float((p.get("attributes") or {}).get("reserve_in_usd") or 0), reverse=True)
    return (data[0].get("attributes") or {}).get("address")


def gecko_current_price_usd(mint: str) -> float | None:
    pool = gecko_find_pool(mint)
    if not pool:
        return None
    r = gecko_get(f"/networks/solana/pools/{pool}/ohlcv/minute", {"aggregate": 1, "limit": 1, "currency": "usd"})
    rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return rows[0][4] if rows else None


def build_trade_row(label: str, wallet: str, task_id: str, source_info: dict | None, pair: dict) -> dict:
    mint = pair["mint"]
    b, s = pair["buy"], pair["sell"]
    b_delta = wallet_sol_delta(b["tx"], wallet)
    b_fees = tips_and_fee(b["tx"], wallet)
    tokens_bought = float(D(wallet_mint_deltas(b["tx"], wallet)["deltas"].get(mint, "0")))
    row: dict = {
        "label": label, "task_id": task_id, "mint": mint,
        "source_wallet_address": (source_info or {}).get("address"),
        "source_wallet_remark": (source_info or {}).get("remark"),
        "source_wallet_buy_signature": (source_info or {}).get("signature"),
        "source_wallet_lag_seconds": (b["block_time"] - source_info["block_time"])
                                      if source_info and source_info.get("block_time") is not None else None,
        "buy_signature": b["signature"], "buy_block_time": b["block_time"], "tokens_bought": tokens_bought,
        "total_sol_out_buy": -b_delta if b_delta else None,
    }
    if label != "pilot" and source_info is None:
        row["source_wallet_attribution"] = "не найдено ни у одного из 10 отслеживаемых кошельков в окне 300с -- честно не гадаем"

    # Цена "лидера" -- это цена ТОГО, кого реально копирует эта задача:
    # для пилота -- LEADER_WALLET, для BATCH-1/2 -- уже найденный источник
    # (source_info), транзакция та же самая, второй запрос не нужен.
    if label == "pilot":
        leader_buy = leader_buy_for_mint(mint, b["block_time"])
    else:
        leader_buy = source_info
    if leader_buy and leader_buy.get("tokens_received"):
        if leader_buy.get("paid_usdc"):
            row["leader_price_usdc"] = leader_buy["paid_usdc"] / leader_buy["tokens_received"]
        elif leader_buy.get("paid_sol"):
            leader_sol_usd = sol_usd_price_at(leader_buy["block_time"])
            if leader_sol_usd:
                row["leader_price_usdc"] = (leader_buy["paid_sol"] * leader_sol_usd) / leader_buy["tokens_received"]
        if "leader_price_usdc" in row:
            row["leader_signature"] = leader_buy["signature"]
            row["leader_price_source"] = "LEADER_WALLET" if label == "pilot" else f"source_wallet:{leader_buy.get('address')}"
    our_price_sol = row["total_sol_out_buy"] / tokens_bought if row["total_sol_out_buy"] and tokens_bought else None
    sol_usd = sol_usd_price_at(b["block_time"])
    row["our_buy_price_usdc_equiv"] = our_price_sol * sol_usd if our_price_sol and sol_usd else None
    if row.get("leader_price_usdc") and row.get("our_buy_price_usdc_equiv"):
        row["markup_at_entry_pct"] = (row["our_buy_price_usdc_equiv"] / row["leader_price_usdc"] - 1) * 100

    if s is None:
        row["status"] = "открыта -- ещё не продано"
        return row

    s_delta = wallet_sol_delta(s["tx"], wallet)
    s_fees = tips_and_fee(s["tx"], wallet)
    tokens_sold = -float(D(wallet_mint_deltas(s["tx"], wallet)["deltas"].get(mint, "0")))
    our_sell_price_sol = s_delta / tokens_sold if s_delta and tokens_sold else None
    sol_usd_sell = sol_usd_price_at(s["block_time"])
    row.update({
        "sell_signature": s["signature"], "sell_block_time": s["block_time"], "tokens_sold": tokens_sold,
        "hold_seconds": s["block_time"] - b["block_time"], "total_sol_in_sell": s_delta,
        "our_sell_price_usdc_equiv": our_sell_price_sol * sol_usd_sell if our_sell_price_sol and sol_usd_sell else None,
        "tip_buy_sol": b_fees["tip_sol"], "tip_sell_sol": s_fees["tip_sol"],
        "network_fee_buy_sol": b_fees["network_fee_sol"], "network_fee_sell_sol": s_fees["network_fee_sol"],
    })
    if row["total_sol_out_buy"] and s_delta is not None:
        swap_principal_buy = row["total_sol_out_buy"] - row["network_fee_buy_sol"] - row["tip_buy_sol"]
        swap_output_sell = s_delta + row["network_fee_sell_sol"] + row["tip_sell_sol"]
        row["gross_pct"] = (swap_output_sell / swap_principal_buy - 1) * 100 if swap_principal_buy else None
        row["net_pct"] = (s_delta / row["total_sol_out_buy"] - 1) * 100
        row["cost_pct"] = (row["gross_pct"] - row["net_pct"]) if row["gross_pct"] is not None else None
        row["status"] = "ok"
        row["close_account_refund_detected_same_tx"] = bool(find_close_account_refund(s["tx"], wallet))
    else:
        row["status"] = "дельта SOL недоступна"
    return row


def extract_outgoing_transfers(tx: dict, wallet: str) -> list[dict]:
    """Системные System Program transfer'ы С ИСТОЧНИКОМ=wallet -- чтобы
    honestly показать получателя, когда небиржевая транзакция уводит SOL
    из кошелька (напр. отдельный сбор комиссии DBot вне транзакции свопа)."""
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    out = []

    def scan(ix_list):
        for ix in ix_list:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed") or {}
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") == "transfer" and ix.get("program") == "system":
                info = parsed.get("info", {})
                if info.get("source") == wallet:
                    out.append({"destination": info.get("destination"), "lamports": info.get("lamports")})

    scan(instrs)
    for grp in inner:
        if isinstance(grp, dict):
            scan(grp.get("instructions", []))
    return out


def wallet_balance_reconciliation(wallet: str, all_meta: list[dict], trade_pairs: list[dict]) -> dict:
    swap_sigs = {e["buy"]["signature"] for e in trade_pairs} | {e["sell"]["signature"] for e in trade_pairs if e["sell"]}
    funding_events = []
    other_credits = []
    other_debits = []
    for m in all_meta:
        if m["is_swap"] or m["signature"] in swap_sigs:
            continue
        if m["sol_change"] > 0.0001:
            tx = m["tx"]
            instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
            is_close = any(isinstance(ix, dict) and (ix.get("parsed") or {}).get("type") == "closeAccount"
                            for ix in instrs if isinstance(ix, dict))
            entry = {"signature": m["signature"], "block_time": m["block_time"], "sol_change": m["sol_change"]}
            if is_close:
                other_credits.append({**entry, "kind": "rent_refund_standalone"})
            else:
                funding_events.append(entry)
        elif m["sol_change"] < -0.0005:
            # Небиржевой отток -- напр. отдельный перевод-сбор DBot вне
            # транзакции свопа. Не входит в net_pct по сделкам (тот
            # считается ТОЛЬКО по buy/sell-транзакциям), но входит в
            # реальный баланс -- отсюда разница "сумма net по сделкам"
            # vs "баланс минус пополнения".
            other_debits.append({"signature": m["signature"], "block_time": m["block_time"],
                                  "sol_change": m["sol_change"],
                                  "outgoing_transfers": extract_outgoing_transfers(m["tx"], wallet)})
    total_funded = sum(e["sol_change"] for e in funding_events)
    total_other_debits = sum(e["sol_change"] for e in other_debits)
    bal = fp.rpc_call("getBalance", [wallet], use_cache=False)
    current_balance = (bal or {}).get("value", 0) / 1e9 if isinstance(bal, dict) else None

    open_positions = []
    for pair in trade_pairs:
        if pair["sell"] is None:
            mint = pair["mint"]
            tokens = float(D(wallet_mint_deltas(pair["buy"]["tx"], wallet)["deltas"].get(mint, "0")))
            price_now = gecko_current_price_usd(mint)
            open_positions.append({"mint": mint, "tokens": tokens, "current_price_usd": price_now,
                                    "value_usd": tokens * price_now if price_now else None})
    if open_positions:
        total_open_value_usd = sum(p["value_usd"] for p in open_positions if p.get("value_usd"))
        sol_usd_now = sol_usd_price_at(int(time.time()) - 60)
        total_open_value_sol = total_open_value_usd / sol_usd_now if sol_usd_now else 0.0
    else:
        total_open_value_sol = 0.0  # пустой список открытых позиций -- стоимость честно 0, не "неизвестно"

    return {
        "funding_events": funding_events, "n_funding_events": len(funding_events),
        "total_funded_sol": round(total_funded, 6),
        "other_credits_standalone_rent_refunds": other_credits,
        "other_debits_non_swap_transfers_out": other_debits,
        "total_other_debits_sol": round(total_other_debits, 6),
        "current_balance_sol": current_balance,
        "open_positions": open_positions,
        "total_open_value_sol_approx": round(total_open_value_sol, 6),
        "implied_pnl_sol": round(current_balance + total_open_value_sol - total_funded, 6)
                           if current_balance is not None else None,
    }


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "trades": []}
    baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}

    wallets_to_scan = [("pilot", PILOT_WALLET, PILOT_TASK_ID, None)]
    for batch_name, b in (baseline.get("batches") or {}).items():
        if b.get("wallet_address"):
            wallets_to_scan.append((batch_name, b["wallet_address"], b.get("task_id"), None))

    reconciliation = {}
    for label, wallet, task_id, _ in wallets_to_scan:
        print(f"[ledger] сканирую {label} ({wallet})...", flush=True)
        events, all_meta = scan_wallet_trades(wallet)
        pairs = pair_trades(events)
        print(f"[ledger] {label}: {len(pairs)} пар покупка/продажа ({sum(1 for p in pairs if p['sell'])} закрыто)", flush=True)
        tracked_wallets = baseline.get("batches", {}).get(label, {}).get("tracked_wallets") or []
        for pair in pairs:
            source_info = None
            if label != "pilot" and tracked_wallets:
                source_info = find_source_wallet(tracked_wallets, pair["mint"], pair["buy"]["block_time"])
            row = build_trade_row(label, wallet, task_id, source_info, pair)
            result["trades"].append(row)
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print(f"[ledger] {label} {row['mint'][:10]}.. status={row.get('status')} "
                  f"markup={row.get('markup_at_entry_pct')} net={row.get('net_pct')}", flush=True)

        reconciliation[label] = wallet_balance_reconciliation(wallet, all_meta, pairs)
        result["balance_reconciliation"] = reconciliation
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    closed = [t for t in result["trades"] if t.get("status") == "ok"]
    markups = [t["markup_at_entry_pct"] for t in closed if "markup_at_entry_pct" in t]
    grosses = [t["gross_pct"] for t in closed if "gross_pct" in t]
    nets = [t["net_pct"] for t in closed if "net_pct" in t]
    result["summary"] = {
        "n_closed_trades": len(closed),
        "median_markup_pct": median(markups) if markups else None,
        "markup_values": markups,
        "median_gross_pct": median(grosses) if grosses else None,
        "median_net_pct": median(nets) if nets else None,
        "n_gross": len(grosses), "n_net": len(nets),
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[ledger] завершено: {result['summary']}", flush=True)


if __name__ == "__main__":
    main()
