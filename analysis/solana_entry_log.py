#!/usr/bin/env python3
"""Владелец: повторно используемая команда -- на входе подпись сделки
лидера (плюс опционально минт, наш кошелёк, наша покупка/продажа), на
выходе подробный лог ВСЕХ транзакций по этому минту (во ВСЕХ его
пулах) в узком слотовом окне вокруг входа лидера. Только RPC/ончейн
(engine.decode_tx, getSignaturesForAddress, getBlock) -- Dune и
GeckoTerminal НЕ используются нигде в этом скрипте.

Вход: data/solana_entry_log_request.json, список запросов (можно
несколько за один прогон -- п.1 и п.2 делаются одним диспатчем):
[{"label": "jupcat", "leader_signature": "...", "mint": "..." (опц.,
  иначе определяется по балансу лидера в его транзакции),
  "window_before_slots": 3, "window_after_slots": 7,
  "our_wallet": "...", "our_buy_signature": "...", "our_sell_signature": "...",
  "out_path": "data/solana_entry_log_jupcat.json"}, ...]

Поиск "всех транзакций по минту во всех пулах" -- getSignaturesForAddress
на АДРЕС САМОГО МИНТА (не конкретного пула): любая транзакция, где минт
упомянут среди accountKeys (перевод/своп/что угодно), попадёт в этот
список -- это и есть "все пулы" без необходимости их перечислять.
Якорь+пагинация назад -- тот же метод, что ensure_pool_window
(fp.find_anchor_after), но по адресу минта.

Цена в USD: USDC-нога считается ~1 USD (стандартное приближение,
оракул не нужен). WSOL-нога -- цена остаётся в единицах SOL (конвертация
в USD потребовала бы скана хайтрафикового SOL/USDC пула или
GeckoTerminal -- оба вне рамок "только RPC" для этой задачи); честно
помечаем quote_asset и НЕ смешиваем единицы, если пулы в логе торгуют
против разных активов."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_fast_price import PRIOR_ROOT  # noqa: E402
from solana_batch_fee_change_first_trade import extract_compute_budget  # noqa: E402

sys.path.insert(0, str(PRIOR_ROOT))
import engine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_PATH = REPO_ROOT / "data" / "solana_entry_log_request.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
FOMO_COSIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_QUOTES = {USDC, USDT}


def tx_signers(tx: dict) -> list[str]:
    keys = tx["transaction"]["message"]["accountKeys"]
    return [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")]


def wallet_mint_delta(tx: dict, wallet: str, mint: str) -> D | None:
    meta = tx.get("meta") or {}
    pre = {b["mint"]: D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
           for b in (meta.get("preTokenBalances") or []) if b.get("owner") == wallet}
    post = {b["mint"]: D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
            for b in (meta.get("postTokenBalances") or []) if b.get("owner") == wallet}
    if mint not in pre and mint not in post:
        return None
    return post.get(mint, D(0)) - pre.get(mint, D(0))


def detect_mint(tx: dict, wallet: str) -> str | None:
    """Единственный положительный не-SOL/USDC/USDT минт у wallet в этой tx."""
    meta = tx.get("meta") or {}
    pre = {b["mint"] for b in (meta.get("preTokenBalances") or []) if b.get("owner") == wallet}
    post = {b["mint"] for b in (meta.get("postTokenBalances") or []) if b.get("owner") == wallet}
    candidates = []
    for m in pre | post:
        if m in (WSOL, USDC, USDT):
            continue
        d = wallet_mint_delta(tx, wallet, m)
        if d is not None and d > 0:
            candidates.append(m)
    return candidates[0] if len(candidates) == 1 else None


def mint_event_for_tx(tx: dict, mint: str) -> dict | None:
    for e in engine.decode_tx(tx):
        if mint in (e.get("m0"), e.get("m1")):
            return e
    return None


def trade_amounts(e: dict, mint: str) -> dict | None:
    """Для kind='cp' -- реальные суммы обеих ног из SwapEvent-лога.
    Для kind='pamm' (Pump.fun AMM) -- только направление 'buy', сверено с
    эталоном (20 WSOL -> 14 263 324.112826 CC); 'sell' там расклад полей
    не проверен, суммы не извлекаются (см. engine.py). Для остальных видов
    пулов (cl/dl/launch) суммы здесь не извлекаются -- честно возвращаем
    None, не выдумываем."""
    if e.get("kind") == "pamm":
        if e.get("direction") != "buy" or e.get("m0") is None or e.get("m1") is None:
            return None
        d0, d1 = e.get("d0"), e.get("d1")
        if d0 is None or d1 is None:
            return None
        in_mint, out_mint = e["m0"], e["m1"]
        input_amount = float(D(e["quote_amount_raw"]) / D(10) ** d0)
        output_amount = float(D(e["base_amount_raw"]) / D(10) ** d1)
        return {"input_mint": in_mint, "input_amount": input_amount,
                "output_mint": out_mint, "output_amount": output_amount,
                "quote_mint": out_mint if in_mint == mint else in_mint,
                "quote_amount": output_amount if in_mint == mint else input_amount,
                "direction": "продажа минта" if in_mint == mint else "покупка минта"}
    if e.get("kind") != "cp":
        return None
    ev = e.get("event") or {}
    d0, d1 = e.get("d0"), e.get("d1")
    if d0 is None or d1 is None or "input_amount" not in ev:
        return None
    in_mint, out_mint = e["m0"], e["m1"]
    return {"input_mint": in_mint, "input_amount": float(D(ev["input_amount"]) / D(10) ** d0),
            "output_mint": out_mint, "output_amount": float(D(ev["output_amount"]) / D(10) ** d1),
            "quote_mint": out_mint if in_mint == mint else in_mint,
            "quote_amount": (float(D(ev["output_amount"]) / D(10) ** d1) if in_mint == mint
                              else float(D(ev["input_amount"]) / D(10) ** d0)),
            "direction": "продажа минта" if in_mint == mint else "покупка минта"}


def price_of_mint(e: dict, mint: str) -> tuple[float | None, str | None]:
    """Возвращает (цена минта в единицах quote, mint-адрес quote-актива)."""
    p = e.get("p1_per_0")
    if p is None:
        return None, None
    p = float(p)
    mint_is_m0 = e["m0"] == mint
    quote_mint = e["m1"] if mint_is_m0 else e["m0"]
    price = p if mint_is_m0 else (1.0 / p if p else None)
    return price, quote_mint


def price_usd(price: float | None, quote_mint: str | None) -> tuple[float | None, str]:
    if price is None:
        return None, "нет цены"
    if quote_mint in STABLE_QUOTES:
        return price, "USD (через стейбл ~1:1)"
    if quote_mint == WSOL:
        return None, "SOL (конвертация в USD не сделана -- вне 'только RPC', см. note)"
    return None, f"неизвестный quote-актив {quote_mint[:10] if quote_mint else '?'}.."


def fetch_mint_signatures_in_slot_window(mint: str, ref_time: int, lo_slot: int, hi_slot: int,
                                          time_buffer_s: int = 30) -> list[dict]:
    """Все подписи, где встречается адрес МИНТА (значит -- любая его
    транзакция в ЛЮБОМ пуле), в окне [lo_slot,hi_slot] -- якорь чуть
    позже ref_time+buffer, пагинация назад, фильтр по РЕАЛЬНОМУ slot
    каждой подписи (blockTime -- только для грубой пагинации)."""
    anchor_sig, _ = fp.find_anchor_after(ref_time + time_buffer_s, safety_margin_s=5)
    hist: list[dict] = []
    before = anchor_sig
    while True:
        page = fp.get_signatures_for_address(mint, before=before, limit=1000)
        if not page:
            break
        hist.extend(page)
        oldest = page[-1].get("blockTime")
        before = page[-1]["signature"]
        if oldest is not None and oldest <= ref_time - time_buffer_s:
            break
        if len(page) < 1000:
            break
    out = [h for h in hist if h.get("slot") is not None and lo_slot <= h["slot"] <= hi_slot]
    return out


_block_order_cache: dict[int, list[str]] = {}


def block_index(slot: int, sig: str) -> int | None:
    if slot not in _block_order_cache:
        block = fp.get_block_signatures(slot)
        _block_order_cache[slot] = (block or {}).get("signatures") or []
    order = _block_order_cache[slot]
    try:
        return order.index(sig)
    except ValueError:
        return None


def build_log(req: dict) -> dict:
    leader_sig = req["leader_signature"]
    leader_tx = fp.get_transaction(leader_sig)
    if leader_tx is None:
        return {"label": req.get("label"), "HONEST_ANSWER": "getTransaction лидера вернул null"}
    leader_slot = leader_tx["slot"]
    leader_time = leader_tx["blockTime"]

    leader_wallet = req.get("leader_wallet") or LEADER_WALLET
    mint = req.get("mint") or detect_mint(leader_tx, leader_wallet)
    if not mint:
        return {"label": req.get("label"), "HONEST_ANSWER": "не удалось определить минт по балансу лидера -- укажите mint явно"}

    lo_slot = leader_slot - req.get("window_before_slots", 3)
    hi_slot = leader_slot + req.get("window_after_slots", 7)
    print(f"[entry_log] {req.get('label')}: минт={mint[:10]}.. лидер_слот={leader_slot} "
          f"окно=[{lo_slot},{hi_slot}]", flush=True)

    sigs = fetch_mint_signatures_in_slot_window(mint, leader_time, lo_slot, hi_slot)
    n_via_mint_sigs = len(sigs)
    pool_addr = req.get("pool")
    if pool_addr:
        pool_sigs = fetch_mint_signatures_in_slot_window(pool_addr, leader_time, lo_slot, hi_slot)
        known = {s["signature"] for s in sigs}
        sigs += [s for s in pool_sigs if s["signature"] not in known]
    n_via_address_search = len(sigs)
    # Владелец, п.2: подозрение, что getSignaturesForAddress(минт) может
    # пропускать ALT-транзакции, проверено отдельно (solana_pumpamm_pool_search_probe.py) --
    # на реальной сделке AKBot ОБА адреса (минт и пул) находят её сами по
    # себе. Пробовавшийся getBlock-скан целых блоков оказался и лишним, и
    # практически неприемлемым по времени (10+ минут, падал с ошибкой на
    # реальных данных) -- убран, поиск по адресу достаточен.
    print(f"[entry_log] {req.get('label')}: подписей по минту={n_via_mint_sigs}, "
          f"+по пулу={n_via_address_search - n_via_mint_sigs}", flush=True)
    # добираем нашу покупку/продажу, если их слот вдруг вне окна (честно
    # расширяем, не молчим -- продажа часто происходит на десятки слотов
    # позже, вне узкого окна входа; это ОЖИДАЕМО и не повод её терять,
    # т.к. без неё не посчитать "от покупки до продажи"). Такие "внешние"
    # подписи помечаем force_include=True -- НЕ отфильтровываются по
    # границам окна ниже, попадают в rows и сводку в любом случае.
    force_include_sigs: set[str] = set()
    extra_sigs_info = []
    for extra_key in ("leader_signature", "our_buy_signature", "our_sell_signature"):
        sig = req.get(extra_key)
        if not sig:
            continue
        force_include_sigs.add(sig)
        if not any(s["signature"] == sig for s in sigs):
            tx = fp.get_transaction(sig)
            if tx:
                extra_sigs_info.append({"signature": sig, "slot": tx["slot"], "blockTime": tx["blockTime"], "err": tx.get("meta", {}).get("err")})
    sigs = sigs + extra_sigs_info
    print(f"[entry_log] {req.get('label')}: найдено подписей минта в окне (+доп. наши) = {len(sigs)}", flush=True)

    our_wallet = req.get("our_wallet")
    label_map = {leader_wallet: "ЛИДЕР"}
    if our_wallet:
        label_map[our_wallet] = "МЫ"

    rows = []
    for h in sigs:
        sig = h["signature"]
        if h.get("err") is not None:
            continue
        tx = fp.get_transaction(sig)
        if tx is None:
            continue
        slot = tx["slot"]
        if not (lo_slot <= slot <= hi_slot) and sig not in force_include_sigs:
            continue
        ev = mint_event_for_tx(tx, mint)
        if ev is None:
            if sig in force_include_sigs:
                # Честно показываем, что decode_tx не нашёл своп-событие
                # по этому минту в ЗАПРОШЕННОЙ (лидер/наша) транзакции --
                # не молчим, чтобы не терять строку без объяснения.
                other_events = [{"pool": e.get("pool"), "kind": e.get("kind"), "m0": e.get("m0"), "m1": e.get("m1")}
                                 for e in engine.decode_tx(tx)]
                signers = tx_signers(tx)
                wallet = next((w for w in (leader_wallet, our_wallet) if w and w in signers), None) or (signers[0] if signers else None)
                idx = block_index(slot, sig)
                rows.append({"signature": sig, "slot": slot, "index_in_block": idx,
                             "block_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tx["blockTime"])),
                             "wallet": wallet, "wallet_label": label_map.get(wallet, "прочие"),
                             "has_fomo_cosigner": FOMO_COSIGNER in signers,
                             "HONEST_NOTE": "decode_tx НЕ нашёл своп-событие по этому минту в этой транзакции",
                             "other_decoded_events_in_tx": other_events})
            continue
        signers = tx_signers(tx)
        # Владелец, найдено на JUPCAT: у транзакции лидера ПЕРВЫЙ подписант --
        # Fomo Co-signer, а не сам кошелёк лидера (мульти-подпись) -- если
        # опираться только на signers[0], лидерская сделка ошибочно
        # получает метку "прочие". Явно ищем ЛИДЕРА/НАС среди ВСЕХ
        # подписантов, иначе -- первый подписант.
        wallet = next((w for w in (leader_wallet, our_wallet) if w and w in signers), None) or (signers[0] if signers else None)
        price, quote_mint = price_of_mint(ev, mint)
        p_usd, usd_note = price_usd(price, quote_mint)
        amounts = trade_amounts(ev, mint)
        cb = extract_compute_budget(tx)
        idx = block_index(slot, sig)
        rows.append({
            "signature": sig, "slot": slot, "index_in_block": idx,
            "block_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tx["blockTime"])),
            "wallet": wallet, "wallet_label": label_map.get(wallet, "прочие"),
            "has_fomo_cosigner": FOMO_COSIGNER in signers,
            "direction": amounts["direction"] if amounts else (ev.get("kind") and f"неизвестно (kind={ev['kind']})"),
            "quote_mint": quote_mint, "quote_amount": amounts["quote_amount"] if amounts else None,
            "size_sol": (amounts["quote_amount"] if amounts and quote_mint == WSOL else None),
            "size_note": None if (amounts and quote_mint == WSOL) else (
                "quote не WSOL -- размер в SOL не считаем (см. quote_amount/quote_mint)" if amounts else "суммы не извлечены для этого вида пула"),
            "price_in_quote": price, "price_usd": p_usd, "price_usd_note": usd_note,
            "pool": ev.get("pool"), "pool_kind": ev.get("kind"),
            "compute_unit_price_microlamports": cb.get("compute_unit_price_microlamports"),
        })

    rows.sort(key=lambda r: (r["slot"], r["index_in_block"] if r["index_in_block"] is not None else 10**9))
    for i, r in enumerate(rows):
        r["global_seq"] = i

    leader_rows = [r for r in rows if r["signature"] == leader_sig]
    leader_seq = leader_rows[0]["global_seq"] if leader_rows else None
    leader_price_usd = leader_rows[0].get("price_usd") if leader_rows else None
    leader_price_quote = leader_rows[0].get("price_in_quote") if leader_rows else None
    leader_quote_mint = leader_rows[0].get("quote_mint") if leader_rows else None

    for r in rows:
        r["slots_from_leader"] = r["slot"] - leader_slot
        r["positions_from_leader"] = (r["global_seq"] - leader_seq) if leader_seq is not None else None
        if leader_price_usd is not None and r.get("price_usd") is not None:
            r["price_vs_leader_pct"] = round((r["price_usd"] / leader_price_usd - 1.0) * 100, 4)
        elif leader_price_quote is not None and r.get("price_in_quote") is not None and r.get("quote_mint") == leader_quote_mint:
            r["price_vs_leader_pct_same_quote_asset_only"] = round((r["price_in_quote"] / leader_price_quote - 1.0) * 100, 4)

    result = {"label": req.get("label"), "mint": mint, "leader_signature": leader_sig,
              "leader_slot": leader_slot, "window": [lo_slot, hi_slot],
              "discovery": {"n_via_mint_getSignaturesForAddress": n_via_mint_sigs,
                            "n_via_pool_getSignaturesForAddress": n_via_address_search - n_via_mint_sigs},
              "n_rows": len(rows), "rows": rows}

    our_buy_sig, our_sell_sig = req.get("our_buy_signature"), req.get("our_sell_signature")
    our_buy_row = next((r for r in rows if r["signature"] == our_buy_sig), None) if our_buy_sig else None
    our_sell_row = next((r for r in rows if r["signature"] == our_sell_sig), None) if our_sell_sig else None

    summary = {}
    if our_buy_row and leader_seq is not None:
        between = [r for r in rows if leader_seq < r["global_seq"] < our_buy_row["global_seq"]]
        sol_between = sum(r["size_sol"] for r in between if r.get("size_sol") is not None)
        n_sol_unresolved = sum(1 for r in between if r.get("size_sol") is None)
        summary["n_trades_between_leader_and_us"] = len(between)
        summary["sol_between_leader_and_us"] = round(sol_between, 4)
        summary["n_trades_between_size_unresolved"] = n_sol_unresolved
        summary["our_buy_slot"] = our_buy_row["slot"]
        summary["our_buy_global_seq"] = our_buy_row["global_seq"]
        summary["our_buy_positions_after_leader"] = our_buy_row["positions_from_leader"]
        if our_buy_row.get("price_vs_leader_pct") is not None:
            summary["price_growth_to_our_buy_pct"] = our_buy_row["price_vs_leader_pct"]
        elif our_buy_row.get("price_vs_leader_pct_same_quote_asset_only") is not None:
            summary["price_growth_to_our_buy_pct_same_quote_asset_only"] = our_buy_row["price_vs_leader_pct_same_quote_asset_only"]
    if our_buy_row and our_sell_row:
        if our_buy_row.get("price_usd") and our_sell_row.get("price_usd"):
            summary["our_buy_to_sell_growth_pct"] = round((our_sell_row["price_usd"] / our_buy_row["price_usd"] - 1.0) * 100, 4)
        elif (our_buy_row.get("quote_mint") == our_sell_row.get("quote_mint") and our_buy_row.get("price_in_quote") and our_sell_row.get("price_in_quote")):
            summary["our_buy_to_sell_growth_pct_same_quote_asset_only"] = round(
                (our_sell_row["price_in_quote"] / our_buy_row["price_in_quote"] - 1.0) * 100, 4)
    result["summary"] = summary
    return result


def main() -> None:
    requests = json.loads(IN_PATH.read_text())
    if isinstance(requests, dict):
        requests = [requests]
    for req in requests:
        result = build_log(req)
        out_path = REPO_ROOT / req["out_path"]
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[entry_log] {req.get('label')}: записано в {out_path}, n_rows={result.get('n_rows')}, "
              f"summary={result.get('summary')}", flush=True)


if __name__ == "__main__":
    main()
