#!/usr/bin/env python3
"""Владелец, 2026-09-18: разбор первой реальной сделки пилота DBot + двух
отказов, на цепи. Части задания:

1) Первая сделка: nub GtDZKA...FxZn, покупка 16:37 (0.5 SOL -> 84820
   токенов), продажа ~29с спустя (84824 -> 0.5820 SOL). Разобрать
   по цепи: комиссия каждого пула на каждой ноге, налог токена, чаевые
   (Astralane, 0.00495 + 0.00995 SOL), число ног, через что шёл маршрут.
   Сделка лидера перед нашей -- блок/слот/разница. Сверить валовый
   +16.41% / чаевые 2.98% / чистый +13.43%.
2) G44sp7...DQfe (16:35, отказ по слippage 0x1771/6001): досчитать цену
   в следующие 30-60с после сделки лидера -- продолжила расти (потеряли
   сигнал) или откатилась (фильтр спас).
3) nub, докупки лидера 16:38 и 16:39 ("already holds"): что дали бы на
   +30с, для сравнения первых входов vs докупок на живом потоке.

Только чтение -- DBot GET + чтение цепи через Alchemy."""
from __future__ import annotations

import calendar
import json
import os
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_buyer200_select_extend import classify, POOL_META_PATH, ROUTE_META_PATH  # noqa: E402
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_fast_price import PRIOR_ROOT  # noqa: E402

sys.path.insert(0, str(PRIOR_ROOT))
import engine  # noqa: E402

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_trade1_analysis_result.json"

DBOT_HOSTS = ["https://api-bot-v1.dbotx.com", "https://servapi.dbotx.com"]
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
PILOT_WALLET = "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS"
SYSTEM_PROGRAM = "11111111111111111111111111111111111111112"[:-1]  # "111...1" (32 chars) -- см. проверку ниже

NUB_PREFIX, NUB_SUFFIX = "GtDZKA", "FxZn"
GNOME2_PREFIX, GNOME2_SUFFIX = "G44sp7", "DQfe"  # переиспользуем имя события из прошлого прохода (не GNOME, другой минт)

BUY_TIME_APPROX = "2026-09-18T16:37:00Z"
LEADER_FAIL_TIME_G44 = 1789749341  # известен точно из прошлого прохода (event1)
NUB_TOPUP_TIMES = ["2026-09-18T16:38:00Z", "2026-09-18T16:39:00Z"]

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def dbot_get(path: str, api_key: str, params: dict | None = None) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        raise RuntimeError("DBOT_API_KEY содержит перевод строки.")
    header_variants = [{"x-api-key": api_key}, {"token": api_key}]
    last: dict = {}
    for host in DBOT_HOSTS:
        for headers in header_variants:
            try:
                resp = requests.get(f"{host}{path}", params=params or {}, headers=headers, timeout=30)
            except Exception as exc:  # noqa: BLE001
                last = {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
                continue
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {"non_json_body": _scrub_all(resp.text[:1000])}
            last = {"http_status": resp.status_code, "body": body, "host": host, "auth_tried": list(headers.keys())}
            if resp.status_code == 200:
                return last
    return last


def wallet_mint_deltas(tx: dict, wallet: str) -> dict:
    """Дельты токен-баланса ЛЮБОГО кошелька в транзакции -- в отличие от
    classify() (см. import выше), НЕ привязано к жёстко зашитому внутри
    него WALLET=лидер. Нужно для анализа сделок ПИЛОТНОГО кошелька --
    classify() на его транзакциях всегда возвращал бы пусто (искал бы
    дельты адреса лидера, которого в этой транзакции нет)."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return {"increased": [], "decreased": [], "deltas": {}, "pre_balances": {}}
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
    return {
        "increased": [k for k, v in delta.items() if v > 0],
        "decreased": [k for k, v in delta.items() if v < 0],
        "deltas": {k: str(v) for k, v in delta.items()},
        "pre_balances": {k: str(v) for k, v in pre.items()},
    }


def find_wallet_tx_near(wallet: str, target_time: int, window_s: int, mint_prefix: str | None = None,
                         mint_suffix: str | None = None) -> list[dict]:
    """Ищет транзакции КОШЕЛЬКА (любого -- лидер ИЛИ пилот) рядом с
    target_time, где токен-баланс этого кошелька по данному минту
    изменился (buy ИЛИ sell). Матчинг минта идёт через wallet_mint_deltas()
    (см. выше) -- она честно привязана к переданному wallet, в отличие от
    classify(), которая жёстко ищет дельты адреса ЛИДЕРА. Для вызовов с
    wallet=LEADER_WALLET дополнительно сохраняем classify()-результат
    (row/check) -- он несёт полезные поля (usdc_spent, zero_balance,
    status и т.д.), которых у wallet_mint_deltas() нет и которые несколько
    мест ниже по коду ожидают именно для лидера."""
    hi, lo = target_time + window_s, target_time - window_s
    before, out = None, []
    for _ in range(15):
        batch = fp.get_signatures_for_address(wallet, before=before, limit=1000)
        if not batch:
            break
        stop = False
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            if bt > hi:
                continue
            if bt < lo:
                stop = True
                break
            tx = fp.get_transaction(s["signature"])
            if tx is None:
                continue
            deltas = wallet_mint_deltas(tx, wallet)
            touched = deltas["increased"] + deltas["decreased"]
            if mint_prefix:
                touched = [mt for mt in touched
                           if mt.startswith(mint_prefix) and (not mint_suffix or mt.endswith(mint_suffix))]
                if not touched:
                    continue
            if wallet == LEADER_WALLET:
                row, check = classify(tx, {"transactionIndex": None})
            else:
                row, check = None, {"status": "n/a_not_leader_wallet", "positive_mints": deltas["increased"],
                                     "negative_mints": deltas["decreased"]}
            out.append({"signature": s["signature"], "block_time": bt, "slot": tx.get("slot"),
                        "row": row, "check": check, "tx": tx, "mint_deltas": deltas})
        if stop:
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return out


def wallet_sol_delta(tx: dict, wallet: str) -> dict | None:
    """Честная дельта НАТИВНОГО SOL-баланса кошелька в транзакции, по
    preBalances/postBalances (в лампортах, из accountKeys). Нужна, потому
    что engine.decode_tx() не распознаёт Jupiter-агрегированный маршрут
    этой конкретной сделки (legs=[] в первом реальном прогоне) -- без
    декодированных нот AMM/налога проверить валовый/чистый результат
    сделки можно только по факту изменения баланса кошелька."""
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
    pre = (meta.get("preBalances") or [None])[idx] if idx < len(meta.get("preBalances") or []) else None
    post = (meta.get("postBalances") or [None])[idx] if idx < len(meta.get("postBalances") or []) else None
    if pre is None or post is None:
        return None
    return {"pre_lamports": pre, "post_lamports": post, "delta_lamports": post - pre, "delta_sol": (post - pre) / 1e9}


def decode_trade_fees_and_tips(tx: dict, wallet: str) -> dict:
    """Разбирает ОДНУ транзакцию (наша покупка/продажа): маршрут через
    engine.decode_tx (та же декодировка, что основной прогон), суммирует
    AMM-комиссию (trade_fee+creator_fee, где есть) и налог токена
    (input/output_transfer_fee) по каждой ноге, ищет чаевые -- System
    Program transfer(ы) на сумму, близкую к 0.00495/0.00995 SOL, среди
    ВСЕХ top-level инструкций транзакции (не только тех, что относятся
    к декодированному свопу) -- получатель берётся из ФАКТА транзакции,
    не угадывается по памяти."""
    events = engine.decode_tx(tx)
    legs = []
    for e in events:
        ev = e.get("event") or {}
        legs.append({
            "pool": e.get("pool"), "kind": e.get("kind"), "m0": e.get("m0"), "m1": e.get("m1"),
            "p1_per_0": e.get("p1_per_0"), "status": e.get("status"),
            "trade_fee": ev.get("trade_fee"), "creator_fee": ev.get("creator_fee"),
            "input_transfer_fee": ev.get("input_transfer_fee"), "output_transfer_fee": ev.get("output_transfer_fee"),
            "input_amount": ev.get("input_amount"), "output_amount": ev.get("output_amount"),
            "amount_in": ev.get("amount_in"), "amount_out": ev.get("amount_out"),
            "mm_fee": ev.get("mm_fee"), "protocol_fee": ev.get("protocol_fee"),
        })

    # Программы верхнего уровня -- честный факт, не гадаем "Jupiter/Raydium" по памяти.
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    account_keys = [k.get("pubkey") if isinstance(k, dict) else k for k in
                    tx.get("transaction", {}).get("message", {}).get("accountKeys", [])]
    program_ids = sorted({i.get("programId") for i in instrs if i.get("programId")})
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    inner_programs = sorted({ix.get("programId") for grp in inner for ix in grp.get("instructions", []) if ix.get("programId")})

    # Чаевые: System Program transfer(ы) с суммой около 4_950_000 / 9_950_000 лампортов.
    tips_found = []
    for i in instrs:
        parsed = i.get("parsed") or {}
        if parsed.get("type") == "transfer" and i.get("program") == "system":
            info = parsed.get("info", {})
            lamports = info.get("lamports")
            if lamports and abs(lamports - 4_950_000) < 50_000 or (lamports and abs(lamports - 9_950_000) < 50_000):
                tips_found.append({"destination": info.get("destination"), "source": info.get("source"),
                                    "lamports": lamports, "sol": lamports / 1e9})
    # Тоже смотрим inner instructions -- переводы чаевых часто идут через CPI, не top-level.
    for grp in inner:
        for ix in grp.get("instructions", []):
            parsed = ix.get("parsed") or {}
            if parsed.get("type") == "transfer" and ix.get("program") == "system":
                info = parsed.get("info", {})
                lamports = info.get("lamports")
                if lamports and (abs(lamports - 4_950_000) < 50_000 or abs(lamports - 9_950_000) < 50_000):
                    tips_found.append({"destination": info.get("destination"), "source": info.get("source"),
                                        "lamports": lamports, "sol": lamports / 1e9, "via": "inner_instruction"})

    meta = tx.get("meta") or {}
    return {
        "legs": legs, "n_legs": len(legs),
        "top_level_programs": program_ids, "inner_programs": inner_programs,
        "tips_found": tips_found,
        "network_fee_sol": meta.get("fee", 0) / 1e9,
        "wallet_sol_delta": wallet_sol_delta(tx, wallet),
        "wallet_mint_deltas": wallet_mint_deltas(tx, wallet),
    }


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    api_key = os.environ.get("DBOT_API_KEY", "")
    if api_key and not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)
    print(f"[trade1] alchemy_available={fp.alchemy_available()}", flush=True)

    # Реальные накопленные метаданные пулов (тот же источник, что основной
    # прогон использует для маршрутизации) -- без них plan_route_for_purchase
    # видит только пулы ИЗ САМОЙ транзакции и часто не может достроить BFS
    # до USDC за 3 хопа (это и есть причина route:null в прошлом проходе).
    global_meta: dict = json.loads(POOL_META_PATH.read_text()) if POOL_META_PATH.exists() else {}
    global_meta.update(json.loads(ROUTE_META_PATH.read_text()) if ROUTE_META_PATH.exists() else {})
    print(f"[trade1] global_meta pools known: {len(global_meta)}", flush=True)

    # ---------- DBot: реальные ордера пилота (теперь должны быть непустые) ----------
    if api_key:
        r = dbot_get("/automation/swap_orders", api_key, params={"chain": "solana"})
        out["dbot_swap_orders_raw"] = r
        print(f"[trade1] DBot swap_orders: http={r.get('http_status')} "
              f"n={len((r.get('body') or {}).get('res', []))}", flush=True)

    # ================= ЧАСТЬ 1: первая сделка (nub) =================
    t_buy = calendar.timegm(time.strptime(BUY_TIME_APPROX, "%Y-%m-%dT%H:%M:%SZ"))
    pilot_matches = find_wallet_tx_near(PILOT_WALLET, t_buy, 180, NUB_PREFIX, NUB_SUFFIX)
    out["part1_pilot_nub_tx_candidates"] = [
        {"signature": m["signature"], "block_time": m["block_time"], "slot": m["slot"],
         "usdc_spent_or_na": (m["row"] or {}).get("usdc_spent"), "check_status": m["check"]["status"]}
        for m in pilot_matches
    ]
    print(f"[trade1] Часть 1: найдено {len(pilot_matches)} транзакций пилота по nub рядом с 16:37", flush=True)

    nub_mint = None
    trades_decoded = []
    for m in pilot_matches:
        tx = m["tx"]
        # Минт этой сделки -- из positive_mints check (может быть buy ИЛИ sell).
        mint_candidates = m["check"].get("positive_mints") or []
        if not mint_candidates:
            # Возможно это ПРОДАЖА (наш минт УХОДИТ, значит positive_mints пуст, дельта отрицательна) --
            # ищем минт напрямую по token balance delta с owner=пилот, дельта<0, префикс совпал.
            meta = tx.get("meta") or {}
            for post in (meta.get("postTokenBalances") or []):
                if post.get("owner") == PILOT_WALLET and post.get("mint", "").startswith(NUB_PREFIX):
                    mint_candidates = [post["mint"]]
                    break
        if mint_candidates:
            nub_mint = mint_candidates[0]
        decoded = decode_trade_fees_and_tips(tx, PILOT_WALLET)
        decoded["signature"] = m["signature"]
        decoded["block_time"] = m["block_time"]
        decoded["slot"] = m["slot"]
        trades_decoded.append(decoded)
        print(f"[trade1]   tx {m['signature'][:20]}.. slot={m['slot']} legs={decoded['n_legs']} "
              f"tips={decoded['tips_found']} programs={decoded['top_level_programs']}", flush=True)

    out["part1_nub_mint"] = nub_mint
    out["part1_trades_decoded"] = trades_decoded

    # Сверка валовый/чаевые/чистый -- по ФАКТИЧЕСКОЙ дельте SOL-баланса
    # кошелька (engine.decode_tx не распознал маршрут -> legs=[], поэтому
    # ног AMM/налога нет; баланс -- прямой и честный источник для этой
    # проверки, не требует декодирования маршрута).
    buy_leg = sell_leg = None
    for d in trades_decoded:
        wmd = d.get("wallet_mint_deltas") or {}
        if nub_mint in (wmd.get("increased") or []):
            buy_leg = d
        elif nub_mint in (wmd.get("decreased") or []):
            sell_leg = d
    if buy_leg and sell_leg and buy_leg.get("wallet_sol_delta") and sell_leg.get("wallet_sol_delta"):
        tip_buy = sum(t["sol"] for t in buy_leg["tips_found"])
        tip_sell = sum(t["sol"] for t in sell_leg["tips_found"])
        total_sol_out_buy = -buy_leg["wallet_sol_delta"]["delta_sol"]  # реально ушло из кошелька (SOL)
        total_sol_in_sell = sell_leg["wallet_sol_delta"]["delta_sol"]  # реально пришло в кошелёк (SOL)
        swap_principal_buy = total_sol_out_buy - buy_leg["network_fee_sol"] - tip_buy
        swap_output_sell = total_sol_in_sell + sell_leg["network_fee_sol"] + tip_sell
        tokens_bought = float(D(buy_leg["wallet_mint_deltas"]["deltas"][nub_mint]))
        tokens_sold = -float(D(sell_leg["wallet_mint_deltas"]["deltas"][nub_mint]))
        out["part1_verification"] = {
            "buy_signature": buy_leg["signature"], "sell_signature": sell_leg["signature"],
            "seconds_between": sell_leg["block_time"] - buy_leg["block_time"],
            "tokens_bought": tokens_bought, "tokens_sold": tokens_sold,
            "total_sol_out_buy": total_sol_out_buy, "total_sol_in_sell": total_sol_in_sell,
            "swap_principal_buy_sol": swap_principal_buy, "swap_output_sell_sol": swap_output_sell,
            "tip_buy_sol": tip_buy, "tip_sell_sol": tip_sell,
            "network_fee_buy_sol": buy_leg["network_fee_sol"], "network_fee_sell_sol": sell_leg["network_fee_sol"],
            "gross_pct_excl_fees_and_tips": (swap_output_sell / swap_principal_buy - 1) * 100 if swap_principal_buy else None,
            "tips_pct_of_total_out": (tip_buy + tip_sell) / total_sol_out_buy * 100 if total_sol_out_buy else None,
            "tips_pct_of_swap_principal": (tip_buy + tip_sell) / swap_principal_buy * 100 if swap_principal_buy else None,
            "net_pct_realized_wallet_to_wallet": (total_sol_in_sell / total_sol_out_buy - 1) * 100 if total_sol_out_buy else None,
        }
        print(f"[trade1] Верификация: {out['part1_verification']}", flush=True)
    else:
        out["part1_verification"] = {"status": "buy_or_sell_leg_not_found_or_no_sol_delta",
                                      "buy_found": bool(buy_leg), "sell_found": bool(sell_leg)}

    if nub_mint and pilot_matches:
        first_pilot_time = min(m["block_time"] for m in pilot_matches)
        leader_matches = find_wallet_tx_near(LEADER_WALLET, first_pilot_time, 300, nub_mint[:6], nub_mint[-4:])
        leader_before = [m for m in leader_matches if m["block_time"] <= first_pilot_time]
        leader_before.sort(key=lambda m: m["block_time"], reverse=True)
        out["part1_leader_matches_all"] = [
            {"signature": m["signature"], "block_time": m["block_time"], "slot": m["slot"],
             "row": m["row"], "check_status": m["check"]["status"]} for m in leader_matches
        ]
        if leader_before:
            lb = leader_before[0]
            pilot_first = min(pilot_matches, key=lambda m: m["block_time"])
            out["part1_leader_vs_pilot"] = {
                "leader_signature": lb["signature"], "leader_slot": lb["slot"], "leader_block_time": lb["block_time"],
                "pilot_signature": pilot_first["signature"], "pilot_slot": pilot_first["slot"],
                "pilot_block_time": pilot_first["block_time"],
                "slot_delta": (pilot_first["slot"] or 0) - (lb["slot"] or 0),
                "seconds_delta": (pilot_first["block_time"] or 0) - (lb["block_time"] or 0),
                "leader_row": lb["row"], "leader_check": lb["check"],
            }
            print(f"[trade1] Лидер перед нашей покупкой: slot_delta={out['part1_leader_vs_pilot']['slot_delta']} "
                  f"seconds_delta={out['part1_leader_vs_pilot']['seconds_delta']}", flush=True)

    # ================= ЧАСТЬ 2: G44sp7 -- цена после отказа =================
    part2 = {}
    route_g44, _ = None, None
    g44_leader = find_wallet_tx_near(LEADER_WALLET, LEADER_FAIL_TIME_G44, 30, GNOME2_PREFIX, GNOME2_SUFFIX)
    if g44_leader:
        g = g44_leader[0]
        from solana_buyer200_select_extend import plan_route_for_purchase
        mint = (g["row"] or {}).get("mint") or next(iter(g["check"].get("positive_mints") or []), None)
        route, _ = plan_route_for_purchase(mint, g["tx"], global_meta) if mint else (None, None)
        part2["mint"] = mint
        part2["leader_tx_time"] = g["block_time"]
        part2["route"] = route
        # Владелец: был ли это первый вход лидера в G44sp7, или докупка?
        # zero_balance НЕ считается classify() для сделок ниже $500 (row=None) --
        # достаём напрямую из той же decoded-транзакции по balance delta.
        if mint:
            meta = g["tx"].get("meta") or {}
            pre_by_idx = {r["accountIndex"]: r for r in (meta.get("preTokenBalances") or [])}
            for post in (meta.get("postTokenBalances") or []):
                if post.get("mint") == mint and post.get("owner") == LEADER_WALLET:
                    pre = pre_by_idx.get(post["accountIndex"])
                    pre_amt = float(pre["uiTokenAmount"]["uiAmount"]) if pre and pre["uiTokenAmount"]["uiAmount"] is not None else 0.0
                    part2["leader_zero_balance_before"] = (pre_amt == 0.0)
                    part2["leader_pre_balance"] = pre_amt
                    break
        if route:
            for sec in [30, 60]:
                t = g["block_time"] + sec
                try:
                    value = D(1)
                    ok = True
                    for leg in route:
                        p = fp.find_price_at(leg["pool"], t, g["block_time"], t)
                        if p.get("status") != "ok":
                            ok = False
                            part2[f"price_at_plus{sec}s_status"] = p.get("status")
                            break
                        e = p["event"]
                        v = D(e["p1_per_0"])
                        if leg["from"] == e["m0"] and leg["to"] == e["m1"]:
                            value *= v
                        elif leg["from"] == e["m1"] and leg["to"] == e["m0"]:
                            value /= v
                        else:
                            ok = False
                            break
                    if ok:
                        part2[f"price_at_plus{sec}s"] = str(value)
                except RuntimeError as exc:
                    part2[f"price_at_plus{sec}s_error"] = str(exc)[:200]
    out["part2_G44sp7_aftermath"] = part2
    print(f"[trade1] Часть 2 (G44sp7): {part2}", flush=True)

    # ================= ЧАСТЬ 3: nub докупки лидера 16:38/16:39 =================
    part3 = []
    if nub_mint:
        for t_str in NUB_TOPUP_TIMES:
            t = calendar.timegm(time.strptime(t_str, "%Y-%m-%dT%H:%M:%SZ"))
            matches = find_wallet_tx_near(LEADER_WALLET, t, 40, nub_mint[:6], nub_mint[-4:])
            if not matches:
                part3.append({"time_str": t_str, "status": "no_leader_tx_found"})
                continue
            m = min(matches, key=lambda x: abs(x["block_time"] - t))
            from solana_buyer200_select_extend import plan_route_for_purchase
            route, _ = plan_route_for_purchase(nub_mint, m["tx"], global_meta)
            entry = {"time_str": t_str, "leader_signature": m["signature"], "leader_block_time": m["block_time"],
                     "leader_row": m["row"], "zero_balance": (m["row"] or {}).get("zero_balance")}
            if route:
                try:
                    value_0 = D(1)
                    for leg in route:
                        p = fp.find_price_at(leg["pool"], m["block_time"], m["block_time"] - 5, m["block_time"])
                        if p.get("status") != "ok":
                            raise RuntimeError(p.get("status"))
                        e = p["event"]
                        v = D(e["p1_per_0"])
                        value_0 = value_0 * v if leg["from"] == e["m0"] else value_0 / v
                    t30 = m["block_time"] + 30
                    value_30 = D(1)
                    for leg in route:
                        p = fp.find_price_at(leg["pool"], t30, m["block_time"], t30)
                        if p.get("status") != "ok":
                            raise RuntimeError(p.get("status"))
                        e = p["event"]
                        v = D(e["p1_per_0"])
                        value_30 = value_30 * v if leg["from"] == e["m0"] else value_30 / v
                    entry["growth_plus30s_pct"] = float((value_30 / value_0 - 1) * 100)
                except (RuntimeError, Exception) as exc:  # noqa: BLE001
                    entry["error"] = str(exc)[:200]
            part3.append(entry)
    out["part3_nub_topups"] = part3
    print(f"[trade1] Часть 3 (докупки nub): {part3}", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[trade1] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
