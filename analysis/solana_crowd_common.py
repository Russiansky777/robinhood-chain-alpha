#!/usr/bin/env python3
"""Владелец, ночное задание п.2: общая логика "идёт ли толпа за
первой покупкой" -- переиспользует ровно тот же метод, что
analysis/solana_entry_log.py (только RPC, decode_tx, поиск по минту и
по пулу), применённый не к одной сделке лидера, а к каждой первой
покупке >=2 SOL кошелька за 72ч.

Цена источника = его трата/полученные токены (через decode_tx -- то же
самое отношение, native precision пула, БЕЗ кросс-конвертации в SOL-
эквивалент). Цена через +30с (75 слотов) = цена последней сделки в окне
С ТЕМ ЖЕ quote-активом, что источник -- разные quote-активы честно не
сравниваются (см. entry_log.py: 'same_quote_asset_only'). Ноль чужих
сделок в окне -- «пусто», рост не считается (не 0%, а None)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch5_rpc_check import scan_wallet  # noqa: E402
from solana_entry_log import (  # noqa: E402
    fetch_mint_signatures_in_slot_window, mint_event_for_tx, trade_amounts,
    price_of_mint, tx_signers, block_index, WSOL, STABLE_QUOTES,
)

WINDOW_SLOTS_AFTER = 75  # ~30с при ~400мс/слот
MAX_PURCHASES_PER_WALLET = 10
MAX_WINDOW_TX_DECODE = 60  # владелец, попытка 2: без кэпа окно на горячем
# пуле даёт 60-300 последовательных getTransaction на ОДНУ покупку --
# это и убило попытку 1 таймаутом (30 мин, не дошли даже до Brez).
# Капим decode, честно помечаем capped=True -- не выдумываем недостающие.


def wallet_purchases(address: str, min_sol: float) -> list[dict]:
    """До MAX_PURCHASES_PER_WALLET последних первых покупок >=min_sol SOL
    за 72ч -- переиспользует уже провалидированный scan_wallet (тот же
    метод, что скан 217/BATCH-5), entries уже newest-first."""
    scan = scan_wallet(address)
    entries = [e for e in scan.get("entries", []) if e.get("spend_sol_equiv", 0) >= min_sol]
    return entries[:MAX_PURCHASES_PER_WALLET]


def analyze_purchase(entry: dict, wallet: str) -> dict:
    sig = entry["signature"]
    mint = entry["mint"]
    out = {"signature": sig, "slot": entry["slot"], "mint": mint,
           "spend_sol_equiv": entry.get("spend_sol_equiv")}

    source_tx = fp.get_transaction(sig)
    if source_tx is None:
        out["HONEST_NOTE"] = "getTransaction источника вернул null"
        out["unresolved"] = True
        return out
    ev = mint_event_for_tx(source_tx, mint)
    if ev is None:
        out["HONEST_NOTE"] = "decode_tx не нашёл своп-событие по минту в исходной транзакции"
        out["unresolved"] = True
        return out
    price_source, quote_mint = price_of_mint(ev, mint)
    if price_source is None:
        out["HONEST_NOTE"] = "цена источника не извлечена (см. trade_amounts/price_of_mint)"
        out["unresolved"] = True
        return out
    out["price_source"] = price_source
    out["quote_mint"] = quote_mint
    out["pool"] = ev.get("pool")

    # Владелец, попытка 2: эталон Dune (+17%/+11%) почти наверняка считался
    # по обычным SOL-котируемым запускам (pump.fun/raydium) -- попытка 1
    # честно показала, что >=15 SOL входы лидера в последние часы шли
    # через биржевые пары ток/ток (котировка -- другой синтетический
    # актив, не SOL), где размах цены на тонкой ликвидности несравним с
    # эталоном. Сравнение "то же на то же" требует того же quote-актива --
    # ограничиваем ИМЕННО сравнение с Dune источниками, котируемыми в SOL.
    if quote_mint != WSOL:
        out["quote_not_wsol_excluded"] = True
        out["HONEST_NOTE"] = f"источник котируется не в SOL (quote={quote_mint[:10] if quote_mint else '?'}..) -- несравнимо с эталоном Dune, исключено из медианы"
        return out

    slot = source_tx["slot"]
    lo, hi = slot, slot + WINDOW_SLOTS_AFTER
    ref_time = source_tx["blockTime"]
    sigs = fetch_mint_signatures_in_slot_window(mint, ref_time, lo, hi)
    pool_addr = ev.get("pool")
    if pool_addr:
        pool_sigs = fetch_mint_signatures_in_slot_window(pool_addr, ref_time, lo, hi)
        known = {s["signature"] for s in sigs}
        sigs += [s for s in pool_sigs if s["signature"] not in known]
    if not any(s["signature"] == sig for s in sigs):
        sigs.append({"signature": sig, "slot": slot})

    sigs.sort(key=lambda h: h.get("slot") or 0)
    n_found_total = len(sigs)
    capped = n_found_total > MAX_WINDOW_TX_DECODE
    if capped:
        source_h = next((h for h in sigs if h["signature"] == sig), None)
        sigs = sigs[:MAX_WINDOW_TX_DECODE]
        if source_h and not any(h["signature"] == sig for h in sigs):
            sigs.append(source_h)
    out["n_window_tx_found_total"] = n_found_total
    out["n_window_tx_decoded"] = len(sigs)
    out["window_capped"] = capped

    rows = []
    for h in sigs:
        s = h["signature"]
        if h.get("err") is not None:
            continue
        tx = fp.get_transaction(s)
        if tx is None:
            continue
        tslot = tx["slot"]
        if not (lo <= tslot <= hi):
            continue
        e2 = mint_event_for_tx(tx, mint)
        if e2 is None:
            continue
        p2, qm2 = price_of_mint(e2, mint)
        amounts = trade_amounts(e2, mint)
        idx = block_index(tslot, s)
        rows.append({"signature": s, "slot": tslot, "index_in_block": idx,
                      "price": p2, "quote_mint": qm2,
                      "direction": amounts["direction"] if amounts else None,
                      "quote_amount": amounts["quote_amount"] if amounts else None})
    rows.sort(key=lambda r: (r["slot"], r["index_in_block"] if r["index_in_block"] is not None else 10**9))

    same_quote = [r for r in rows if r["quote_mint"] == quote_mint and r["price"] is not None]
    others_same_quote = [r for r in same_quote if r["signature"] != sig]
    out["n_other_trades_in_window"] = len([r for r in rows if r["signature"] != sig])
    out["n_other_same_quote_trades"] = len(others_same_quote)

    others_any_quote = [r for r in rows if r["signature"] != sig]
    n_other_buys = sum(1 for r in others_any_quote if r["direction"] == "покупка минта")
    sol_other_buys = sum(r["quote_amount"] for r in others_any_quote
                          if r["direction"] == "покупка минта" and r["quote_mint"] == WSOL and r["quote_amount"] is not None)
    n_stable_excluded = sum(1 for r in others_any_quote
                             if r["direction"] == "покупка минта" and r["quote_mint"] in STABLE_QUOTES)
    out["n_other_buys"] = n_other_buys
    out["sol_other_buys_sum"] = round(sol_other_buys, 6)
    out["n_stable_quoted_buys_excluded_from_sol_sum"] = n_stable_excluded

    if not others_same_quote:
        out["empty"] = True
        out["growth_pct_30s"] = None
    else:
        last = others_same_quote[-1]
        out["empty"] = False
        out["growth_pct_30s"] = round((last["price"] / price_source - 1.0) * 100, 4)
        out["last_trade_signature"] = last["signature"]
        out["last_trade_slot"] = last["slot"]
    return out


def analyze_wallet(address: str, name: str, min_sol: float = 2.0) -> dict:
    purchases = wallet_purchases(address, min_sol)
    results = [analyze_purchase(e, address) for e in purchases]
    n_quote_excluded = sum(1 for r in results if r.get("quote_not_wsol_excluded"))
    resolved = [r for r in results if not r.get("unresolved") and not r.get("quote_not_wsol_excluded")]
    non_empty = [r for r in resolved if not r.get("empty")]
    growths = sorted(r["growth_pct_30s"] for r in non_empty)
    others_counts = sorted(r["n_other_buys"] for r in resolved)

    def median(xs):
        if not xs:
            return None
        n = len(xs)
        mid = n // 2
        return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2

    return {
        "address": address, "name": name,
        "n_purchases_analyzed": len(results),
        "n_unresolved": sum(1 for r in results if r.get("unresolved")),
        "n_quote_not_wsol_excluded": n_quote_excluded,
        "n_empty": sum(1 for r in resolved if r.get("empty")),
        "empty_share": round(sum(1 for r in resolved if r.get("empty")) / len(resolved), 3) if resolved else None,
        "median_growth_pct_30s": round(median(growths), 4) if growths else None,
        "median_n_other_buys": median(others_counts),
        "purchases": results,
    }
