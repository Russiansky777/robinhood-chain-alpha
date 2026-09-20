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
(не 0%, а None). Источники, котируемые не в SOL/WSOL/USDC/USDT (пары
токен/токен) -- владелец: это НЕ покупка в смысле этого замера, честно
пропускаются без пометки ошибки (см. not_a_purchase), не входят ни в
n_приценено, ни в n_decode_fail.

decode_fail (getTransaction вернул null / decode_tx не нашёл событие /
цена не извлеклась) -- честная неудача метода, не чинится сейчас, но
кандидаты программы свопа собираются в общий файл
(data/solana_crowd_decode_fail_programs.json, см.
record_decode_fail_programs) для последующего разбора топ-5 по частоте.

Окно капится на MAX_WINDOW_TX_DECODE сделок (первые по времени после
покупки) -- владелец: не более 30, partial=True при превышении, честно
не выдумываем недостающие. getTransaction внутри окна -- JSON-RPC batch
по 20 (fp.get_transactions_batch, тот же метод, что скан 217), не по
одной."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch5_rpc_check import scan_wallet  # noqa: E402
from solana_entry_log import (  # noqa: E402
    fetch_mint_signatures_in_slot_window, mint_event_for_tx, trade_amounts,
    price_of_mint, tx_signers, block_index, WSOL, STABLE_QUOTES,
    candidate_program_ids_for_mint, KIND_PROGRAM_IDS, DEX_LABELS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
# Владелец, п.3: 4 параллельных шарда писали в ОДИН общий файл -> конфликт
# при синхронизации git между job'ами. Пошардовый файл, объединяется в
# aggregate (см. solana_crowd_night_aggregate.py).
_SHARD_ID = int(os.environ.get("SHARD_ID", "-1"))
DECODE_FAIL_PROGRAMS_PATH = (REPO_ROOT / "data" / f"solana_crowd_decode_fail_shard_{_SHARD_ID}.json"
                              if _SHARD_ID >= 0 else REPO_ROOT / "data" / "solana_crowd_decode_fail_programs.json")
ALLOWED_QUOTES = {WSOL} | STABLE_QUOTES
WINDOW_SLOTS_AFTER = 75  # ~30с при ~400мс/слот
MAX_PURCHASES_PER_WALLET_SCAN = 5  # владелец: было 10 -- достаточно для медианы/доли пустых
MAX_PURCHASES_PER_WALLET_CONTROL = 8
MAX_WINDOW_TX_DECODE = 30  # владелец: было 40 -- достаточно для медианы/доли пустых
get_transactions_batch = fp.get_transactions_batch  # владелец: батчи по 20, как в скане 217 (см. fp)


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


def record_decode_fail_programs(program_ids: list[str]) -> None:
    """Владелец, п.2 крауд-скана: не чинить decode_fail сейчас, только
    собрать program IDs свопа в общий файл (для последующего разбора
    топ-5 по частоте). Один вызов на одну неудачную покупку -- program_ids
    уже дедуплицированы вызывающей стороной внутри одного события."""
    if not program_ids:
        return
    try:
        data = json.loads(DECODE_FAIL_PROGRAMS_PATH.read_text()) if DECODE_FAIL_PROGRAMS_PATH.exists() else {}
    except (ValueError, OSError):
        data = {}
    counts = data.setdefault("counts", {})
    labels = data.setdefault("labels", {})
    for pid in program_ids:
        counts[pid] = counts.get(pid, 0) + 1
        if pid not in labels:
            labels[pid] = DEX_LABELS.get(pid)
    data["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    DECODE_FAIL_PROGRAMS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def wallet_purchases(address: str, min_sol: float, max_purchases: int) -> list[dict]:
    """До max_purchases последних первых покупок >=min_sol SOL за 72ч --
    переиспользует уже провалидированный scan_wallet (тот же метод, что
    скан 217/BATCH-5), entries уже newest-first."""
    scan = scan_wallet(address)
    entries = [e for e in scan.get("entries", []) if e.get("spend_sol_equiv", 0) >= min_sol]
    return entries[:max_purchases]


def analyze_purchase(entry: dict, wallet: str) -> dict:
    """Владелец, найдено на реальном прогоне 2026-09-20: один зависший
    RPC-вызов внутри _analyze_purchase_inner (retry-цикл rpc_call или
    пагинация fetch_mint_signatures_in_slot_window) может сам по себе
    съесть весь бюджет тика и убить job -- см. fp.set_soft_deadline.
    Здесь -- последний рубеж: RuntimeError от честно истёкшего мягкого
    дедлайна (или любой другой RPC-сбой) НЕ должен прерывать весь цикл
    по кошельку/скану, а честно засчитывается decode_fail на ЭТОЙ ОДНОЙ
    покупке, следующая обрабатывается как обычно."""
    try:
        return _analyze_purchase_inner(entry, wallet)
    except RuntimeError as exc:
        return {"signature": entry["signature"], "slot": entry.get("slot"), "mint": entry.get("mint"),
                "spend_sol_equiv": entry.get("spend_sol_equiv"),
                "decode_fail": True, "rpc_error": True, "HONEST_NOTE": f"RPC-сбой: {exc}"}


def _analyze_purchase_inner(entry: dict, wallet: str) -> dict:
    sig = entry["signature"]
    mint = entry["mint"]
    out = {"signature": sig, "slot": entry["slot"], "mint": mint,
           "spend_sol_equiv": entry.get("spend_sol_equiv")}

    source_tx = fp.get_transaction(sig)
    if source_tx is None:
        out["decode_fail"] = True
        out["HONEST_NOTE"] = "getTransaction источника вернул null"
        return out
    ev = mint_event_for_tx(source_tx, mint)
    if ev is None:
        out["decode_fail"] = True
        out["HONEST_NOTE"] = "decode_tx не нашёл своп-событие по минту в исходной транзакции"
        out["program_id_candidates"] = candidate_program_ids_for_mint(source_tx, mint)
        return out
    price_source, quote_mint = price_of_mint(ev, mint)
    if price_source is None:
        out["decode_fail"] = True
        out["HONEST_NOTE"] = "цена источника не извлечена (см. trade_amounts/price_of_mint)"
        out["kind"] = ev.get("kind")
        out["program_id_candidates"] = KIND_PROGRAM_IDS.get(ev.get("kind"), [])
        return out
    out["price_source"] = price_source
    out["quote_mint"] = quote_mint
    out["pool"] = ev.get("pool")

    # Владелец: токен/токен (котировка не в SOL/WSOL/USDC/USDT) -- НЕ
    # покупка в смысле этого замера (рост % внутри пула не зависит от
    # котируемой валюты, но пул третьего SPL-токена не сравним с эталоном
    # Dune и не интересен для копирования толпой) -- пропускаем без
    # пометки ошибки, не decode_fail.
    if quote_mint not in ALLOWED_QUOTES:
        out["not_a_purchase"] = True
        out["HONEST_NOTE"] = (f"котировка не в SOL/WSOL/USDC/USDT (quote="
                               f"{quote_mint[:10] if quote_mint else '?'}..) -- не покупка, пропущено")
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
    try:
        purchases = wallet_purchases(address, min_sol, max_purchases)
    except RuntimeError as exc:
        # Мягкий дедлайн тика (или иной RPC-сбой) истёк ещё во время
        # получения списка покупок этого кошелька (scan_wallet) -- честно
        # не считаем этот кошелёк сделанным, следующий тик начнёт заново.
        print(f"[crowd_common] {name} ({address[:10]}..): сбой при получении покупок -- {exc}", flush=True)
        return {"address": address, "name": name, "wallet_budget_cut": True,
                "n_purchases_total": 0, "n_priced": 0, "n_decode_fail": 0,
                "n_not_a_purchase_token_token": 0, "n_empty": 0, "empty_share": None,
                "median_growth_pct_30s": None, "median_n_other_buys": None,
                "median_purchase_size_sol_equiv": None, "purchases": []}
    results = []
    wallet_budget_cut = False
    for e in purchases:
        if deadline is not None and time.monotonic() > deadline:
            wallet_budget_cut = True
            break
        r = analyze_purchase(e, address)
        results.append(r)
        if r.get("decode_fail"):
            record_decode_fail_programs(r.get("program_id_candidates") or [])

    not_purchase = [r for r in results if r.get("not_a_purchase")]
    decode_fail = [r for r in results if r.get("decode_fail")]
    priced = [r for r in results if not r.get("decode_fail") and not r.get("not_a_purchase")]
    non_empty = [r for r in priced if not r.get("empty")]
    growths = [r["growth_pct_30s"] for r in non_empty]
    others_counts = [r["n_other_buys"] for r in priced]
    sizes = [r["spend_sol_equiv"] for r in priced if r.get("spend_sol_equiv") is not None]

    return {
        "address": address, "name": name,
        "wallet_budget_cut": wallet_budget_cut,
        "n_purchases_total": len(priced) + len(decode_fail),
        "n_priced": len(priced),
        "n_decode_fail": len(decode_fail),
        "n_not_a_purchase_token_token": len(not_purchase),
        "n_empty": sum(1 for r in priced if r.get("empty")),
        "empty_share": round(sum(1 for r in priced if r.get("empty")) / len(priced), 3) if priced else None,
        "median_growth_pct_30s": round(median(growths), 4) if growths else None,
        "median_n_other_buys": median(others_counts),
        "median_purchase_size_sol_equiv": median(sizes),
        "purchases": results,
    }
