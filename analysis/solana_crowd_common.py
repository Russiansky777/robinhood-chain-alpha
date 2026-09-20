#!/usr/bin/env python3
"""Владелец, п.2 "толпа за первой покупкой": переиспользует ровно тот
же метод, что analysis/solana_entry_log.py (только RPC, decode_tx,
поиск по минту и по пулу), применённый не к одной сделке лидера, а к
каждой первой покупке кошелька за 72ч.

Цена источника = его трата/полученные токены (через decode_tx -- то же
самое отношение, native precision пула, БЕЗ кросс-конвертации в SOL-
эквивалент). Цена через +30с (75 слотов) = цена последней сделки в окне
С ТЕМ ЖЕ quote-активом, что источник -- разные quote-активы честно не
сравниваются. Ноль чужих сделок в окне -- «пусто», рост не считается
(не 0%, а None). Источники, котируемые не в SOL/WSOL/USDC/USDT
(пары токен/токен), исключены -- рост % внутри одного пула не зависит
от котируемой валюты, поэтому SOL и стейблы равноправны -- см.
quote_not_wsol_excluded.

Окно капится на MAX_WINDOW_TX_DECODE сделок (первые по времени после
покупки) -- владелец: не более 40, partial=True при превышении, честно
не выдумываем недостающие. getTransaction внутри окна -- JSON-RPC batch
(один HTTP POST на пачку), не по одной."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch5_rpc_check import scan_wallet  # noqa: E402
from solana_entry_log import (  # noqa: E402
    fetch_mint_signatures_in_slot_window, mint_event_for_tx, trade_amounts,
    price_of_mint, tx_signers, block_index, WSOL, STABLE_QUOTES,
)

ALLOWED_QUOTES = {WSOL} | STABLE_QUOTES
WINDOW_SLOTS_AFTER = 75  # ~30с при ~400мс/слот
MAX_PURCHASES_PER_WALLET_SCAN = 10
MAX_PURCHASES_PER_WALLET_CONTROL = 8
MAX_WINDOW_TX_DECODE = 40  # владелец: не больше 40 сделок токена после покупки
BATCH_SIZE = 30
_TX_PARAMS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}


def get_transactions_batch(sigs: list[str]) -> dict[str, dict | None]:
    """JSON-RPC batch (один HTTP POST на пачку подписей) -- владелец:
    'запросы getTransaction пачками, как в скане'. Кэш -- тот же файл-
    кэш, что fp.get_transaction (та же ключевая функция _cache_path),
    чтобы дальнейшие одиночные вызовы тоже брали из кэша. Откат на
    одиночные вызовы для чанка, если пачка не удалась -- не роняем весь
    прогон из-за одного отказавшегося провайдера/чанка."""
    out: dict[str, dict | None] = {}
    todo = []
    for s in sigs:
        cache_f = fp._cache_path("getTransaction", [s, _TX_PARAMS])
        if cache_f.exists():
            try:
                out[s] = json.loads(cache_f.read_text())
                continue
            except (ValueError, OSError):
                pass
        todo.append(s)
    if not todo:
        return out

    url = fp._endpoint()
    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start:start + BATCH_SIZE]
        body = [{"jsonrpc": "2.0", "id": i, "method": "getTransaction", "params": [s, _TX_PARAMS]}
                for i, s in enumerate(chunk)]
        backoff = 0.0
        ok = False
        for _attempt in range(8):
            try:
                resp = requests.post(url, json=body, timeout=45)
            except Exception:  # noqa: BLE001
                backoff = min(max(backoff * 2, 0.5), 20.0)
                time.sleep(backoff)
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                backoff = min(max(backoff * 2, 0.5), 20.0)
                time.sleep(backoff)
                continue
            if not resp.ok:
                break
            try:
                results = resp.json()
            except ValueError:
                break
            if not isinstance(results, list):
                break
            by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
            for i, s in enumerate(chunk):
                r = by_id.get(i)
                tx = r.get("result") if r and "error" not in r else None
                out[s] = tx
                fp._cache_path("getTransaction", [s, _TX_PARAMS]).write_text(json.dumps(tx))
            ok = True
            break
        if not ok:
            # честно -- пачка не удалась, откатываемся на одиночные вызовы
            for s in chunk:
                out[s] = fp.get_transaction(s)
    return out


MINT_AGE_MAX_PAGES = 20  # владелец, диагностика Brez: не более 20000 подписей на минт --
# честный предел, не бесконечная пагинация; если генезис не найден в пределах, возраст = None


def mint_creation_time(mint: str) -> tuple[int | None, bool]:
    """Возраст токена = время его первой транзакции (getSignaturesForAddress,
    страница за страницей назад до последней/неполной страницы = генезис
    аккаунта минта). Возвращает (earliest_block_time, reached_genesis) --
    честно None, если генезис не найден в пределах MINT_AGE_MAX_PAGES."""
    earliest_time = None
    earliest_sig = None
    before = None
    for _ in range(MINT_AGE_MAX_PAGES):
        page = fp.get_signatures_for_address(mint, before=before, limit=1000)
        if not page:
            return earliest_time, True
        for row in page:
            bt = row.get("blockTime")
            if bt is not None and (earliest_time is None or bt < earliest_time):
                earliest_time = bt
        earliest_sig = page[-1].get("signature")
        if len(page) < 1000:
            return earliest_time, True
        before = earliest_sig
    return earliest_time, False


def purchase_age_minutes(entry: dict) -> float | None:
    """Возраст токена на момент покупки (мин от первой транзакции минта
    до покупки) -- честно None, если генезис минта не найден в пределах
    MINT_AGE_MAX_PAGES или если время покупки не удалось получить."""
    tx = fp.get_transaction(entry["signature"])
    if tx is None or tx.get("blockTime") is None:
        return None
    creation_time, reached_genesis = mint_creation_time(entry["mint"])
    if creation_time is None or not reached_genesis:
        return None
    return round((tx["blockTime"] - creation_time) / 60.0, 2)


def wallet_purchases(address: str, min_sol: float, max_purchases: int) -> list[dict]:
    """До max_purchases последних первых покупок >=min_sol SOL за 72ч --
    переиспользует уже провалидированный scan_wallet (тот же метод, что
    скан 217/BATCH-5), entries уже newest-first."""
    scan = scan_wallet(address)
    entries = [e for e in scan.get("entries", []) if e.get("spend_sol_equiv", 0) >= min_sol]
    return entries[:max_purchases]


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

    # Владелец: рост в % внутри ОДНОГО пула не зависит от котируемой
    # валюты -- разрешаем SOL/WSOL и стейблы (USDC/USDT), исключаем
    # только пары токен/токен без стейбла и без SOL (там волатильность
    # обеих ног мешает сравнению).
    if quote_mint not in ALLOWED_QUOTES:
        out["quote_not_wsol_excluded"] = True
        out["HONEST_NOTE"] = (f"источник котируется не в SOL/WSOL/стейбле (quote="
                               f"{quote_mint[:10] if quote_mint else '?'}..) -- пара токен/токен, исключено")
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

    sigs.sort(key=lambda h: h.get("slot") or 0)  # "первые по времени после покупки"
    n_found_total = len(sigs)
    partial = n_found_total > MAX_WINDOW_TX_DECODE
    if partial:
        source_h = next((h for h in sigs if h["signature"] == sig), None)
        sigs = sigs[:MAX_WINDOW_TX_DECODE]
        if source_h and not any(h["signature"] == sig for h in sigs):
            sigs.append(source_h)
    out["n_window_tx_found_total"] = n_found_total
    out["n_window_tx_decoded"] = len(sigs)
    out["partial"] = partial

    ok_sigs = [h["signature"] for h in sigs if h.get("err") is None]
    tx_by_sig = get_transactions_batch(ok_sigs)

    rows = []
    for h in sigs:
        s = h["signature"]
        if h.get("err") is not None:
            continue
        tx = tx_by_sig.get(s)
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


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def analyze_wallet(address: str, name: str, min_sol: float, max_purchases: int,
                    deadline: float | None = None) -> dict:
    purchases = wallet_purchases(address, min_sol, max_purchases)
    results = []
    wallet_budget_cut = False
    for e in purchases:
        if deadline is not None and time.monotonic() > deadline:
            wallet_budget_cut = True
            break
        results.append(analyze_purchase(e, address))
    n_quote_excluded = sum(1 for r in results if r.get("quote_not_wsol_excluded"))
    resolved = [r for r in results if not r.get("unresolved") and not r.get("quote_not_wsol_excluded")]
    non_empty = [r for r in resolved if not r.get("empty")]
    growths = [r["growth_pct_30s"] for r in non_empty]
    others_counts = [r["n_other_buys"] for r in resolved]

    return {
        "address": address, "name": name,
        "wallet_budget_cut": wallet_budget_cut,
        "n_purchases_analyzed": len(results),
        "n_unresolved": sum(1 for r in results if r.get("unresolved")),
        "n_quote_not_wsol_excluded": n_quote_excluded,
        "n_empty": sum(1 for r in resolved if r.get("empty")),
        "empty_share": round(sum(1 for r in resolved if r.get("empty")) / len(resolved), 3) if resolved else None,
        "median_growth_pct_30s": round(median(growths), 4) if growths else None,
        "median_n_other_buys": median(others_counts),
        "purchases": results,
    }
