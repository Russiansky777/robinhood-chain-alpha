#!/usr/bin/env python3
"""Владелец, 2026-09-19, промпт 16:10: хронология одной конкретной
сделки пилота -- mint AaEhFTX4naHSWSXz9TVe5QgLbtSLT8ZqYJGZzDDcoroh,
buy_signature 2u7xoQkB... ("JUPCAT" по словам владельца). Только RPC
(getTransaction/getSignaturesForAddress через Alchemy) -- Dune не
трогаем, идёт параллельно с бесплатной диагностикой Фазы 3.

Задача: сделка лидера, за которой мы пошли; ВСЕ покупки этого минта
между сделкой лидера и нашей (кто, сколько SOL, на сколько слотов/
позиций раньше нас, известный кандидат ("Fomo") или неизвестный
кошелёк -- "бот" не утверждаем без прямых оснований); наша наценка к
цене лидера и сколько из неё создали промежуточные покупатели; наш
выход и итог (уже есть в ведомости, sell_signature -- честно берём
оттуда, это тоже RPC-данные, полученные ledger-скриптом, не Dune).

Decode -- ТЕМ ЖЕ engine.decode_tx(), что и вся buyer_200 инфраструктура
(data/solana_buyer_200/prior/current/buyer_100/engine.py), полностью
ончейн (Raydium CPMM SwapEvent / CLMM / DLMM / launch), без Dune."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_fast_price import PRIOR_ROOT  # noqa: E402

sys.path.insert(0, str(PRIOR_ROOT))
import engine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_jupcat_timeline_result.json"

MINT = "AaEhFTX4naHSWSXz9TVe5QgLbtSLT8ZqYJGZzDDcoroh"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
PILOT_WALLET = "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS"
LEADER_SIG = "2MmYq4otDGKeRM1CFWNdVXeK9wbv3wYSHzJ45xof8qyjMTvZh5nBeF2ZWdPkzyVpY7nyw4F9Ped7cYCEEqASpKVb"
PILOT_BUY_SIG = "2u7xoQkBTAzGK5ksA9NyYgF2sANgKeFwZ7bfYzT2Y3L8h3Z7sANPj32LLrjhSgTcvtAbH7rj3jRW83xGLnvQaPGQ"
PILOT_SELL_SIG = "dvRRUiCYb4eJeD1vpbzgVQfDmbr9KhM68hpajhT5YmSuSnkAxsY5PRCaw7L8299giKrYkee8y6YanemXdCZCxb2"
WSOL = "So11111111111111111111111111111111111111112"


def tx_signer(tx: dict) -> str | None:
    keys = tx["transaction"]["message"]["accountKeys"]
    for k in keys:
        if isinstance(k, dict) and k.get("signer"):
            return k["pubkey"]
    return keys[0]["pubkey"] if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)


def mint_event_for_tx(tx: dict) -> dict | None:
    """Событие decode_tx(), где ОДНА из ног -- наш MINT (CPMM/CL/DL/launch)."""
    for e in engine.decode_tx(tx):
        if MINT in (e.get("m0"), e.get("m1")):
            return e
    return None


def cp_trade_amounts(e: dict) -> dict | None:
    """Для kind='cp' (Raydium CPMM) -- реальные суммы входа/выхода этой
    КОНКРЕТНОЙ сделки из SwapEvent-лога (не изменение баланса кошелька за
    всю транзакцию -- на всякий случай, если внутри есть что-то ещё)."""
    ev = e.get("event") or {}
    d0, d1 = e.get("d0"), e.get("d1")
    if d0 is None or d1 is None or "input_amount" not in ev:
        return None
    in_mint, out_mint = e["m0"], e["m1"]
    in_dec = d0 if in_mint == e["m0"] else d1
    out_dec = d1 if out_mint == e["m1"] else d0
    return {"input_mint": in_mint, "input_amount": float(D(ev["input_amount"]) / D(10) ** in_dec),
            "output_mint": out_mint, "output_amount": float(D(ev["output_amount"]) / D(10) ** out_dec)}


def price_from_event(e: dict, mint_is_m0: bool) -> float | None:
    p = e.get("p1_per_0")
    if p is None:
        return None
    p = float(p)
    # p1_per_0 = цена m1 в единицах m0 (per decode_tx). Нам нужна цена MINT в WSOL,
    # т.е. m1_per_mint если mint==m0, или 1/p если mint==m1.
    return p if mint_is_m0 else (1.0 / p if p else None)


def classify_wallet(wallet: str, roles: dict) -> str:
    if wallet == LEADER_WALLET:
        return "ЛИДЕР"
    if wallet == PILOT_WALLET:
        return "ПИЛОТ (мы)"
    if wallet in (roles.get("candidates_29") or []):
        return "известный кандидат (Fomo, топ-29)"
    if wallet in (roles.get("new_from_funnel") or []):
        return "известный кандидат (из воронки новых)"
    return "неизвестный кошелёк -- бот НЕ утверждаю без оснований"


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "mint": MINT, "leader_signature": LEADER_SIG,
                     "pilot_buy_signature": PILOT_BUY_SIG, "pilot_sell_signature": PILOT_SELL_SIG}

    events_path = REPO_ROOT / "data" / "solana_phase2_events_v3.json"
    roles = json.loads(events_path.read_text())["wallet_roles"] if events_path.exists() else {}

    leader_tx = fp.get_transaction(LEADER_SIG)
    pilot_tx = fp.get_transaction(PILOT_BUY_SIG)
    if leader_tx is None or pilot_tx is None:
        result["HONEST_ANSWER"] = "getTransaction вернул null для сделки лидера или пилота -- дальше идти нельзя."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[jupcat] " + result["HONEST_ANSWER"], flush=True)
        return

    leader_ev = mint_event_for_tx(leader_tx)
    pilot_ev = mint_event_for_tx(pilot_tx)
    result["leader_tx_slot"] = leader_tx["slot"]
    result["leader_tx_block_time_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(leader_tx["blockTime"]))
    result["pilot_tx_slot"] = pilot_tx["slot"]
    result["pilot_tx_block_time_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pilot_tx["blockTime"]))
    result["slots_between"] = pilot_tx["slot"] - leader_tx["slot"]
    result["seconds_between"] = pilot_tx["blockTime"] - leader_tx["blockTime"]

    if leader_ev is None or pilot_ev is None:
        result["HONEST_ANSWER"] = ("decode_tx не нашёл событие свопа по нашему минту в одной из двух транзакций "
                                    f"(leader_ev={'найдено' if leader_ev else 'НЕТ'}, pilot_ev={'найдено' if pilot_ev else 'НЕТ'}) "
                                    "-- честно останавливаюсь, дальше без выдумывания пула/цены нельзя.")
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[jupcat] " + result["HONEST_ANSWER"], flush=True)
        return

    pool = leader_ev["pool"]
    result["pool"] = pool
    result["pool_kind"] = leader_ev["kind"]
    result["pool_matches_between_leader_and_pilot"] = (pilot_ev["pool"] == pool)
    if pilot_ev["pool"] != pool:
        result["HONEST_NOTE"] = (f"Пул лидера ({pool}) и пул пилота ({pilot_ev['pool']}) РАЗНЫЕ -- "
                                  f"мог быть роут через второй пул/иная ликвидность. Смотрю окно по пулу лидера, "
                                  f"это может занизить число промежуточных сделок, если часть шла через другой пул.")

    leader_mint_is_m0 = leader_ev["m0"] == MINT
    pilot_mint_is_m0 = pilot_ev["m0"] == MINT
    leader_price_wsol = price_from_event(leader_ev, leader_mint_is_m0)
    pilot_price_wsol = price_from_event(pilot_ev, pilot_mint_is_m0)
    result["leader_price_mint_in_wsol"] = leader_price_wsol
    result["pilot_price_mint_in_wsol"] = pilot_price_wsol
    if leader_price_wsol and pilot_price_wsol:
        result["markup_pct_onchain_wsol_basis"] = (pilot_price_wsol / leader_price_wsol - 1.0) * 100

    lo_time, hi_time = leader_tx["blockTime"] - 3, pilot_tx["blockTime"] + 3
    print(f"[jupcat] окно пула {pool[:12]}.. [{lo_time},{hi_time}] ({hi_time-lo_time}с)", flush=True)
    hist = fp.ensure_pool_window(pool, lo_time, hi_time)
    hist_sorted = sorted(hist, key=lambda h: (h.get("slot") or 0, h.get("blockTime") or 0))

    between = [h for h in hist_sorted
               if h.get("blockTime") is not None and leader_tx["blockTime"] <= h["blockTime"] <= pilot_tx["blockTime"]
               and h["signature"] not in (LEADER_SIG, PILOT_BUY_SIG) and h.get("err") is None]
    print(f"[jupcat] кандидатов между лидером и пилотом (по времени в окне пула): {len(between)}", flush=True)

    rows = []
    for h in between:
        tx = fp.get_transaction(h["signature"])
        if tx is None:
            rows.append({"signature": h["signature"], "slot": h["slot"], "status": "getTransaction_null"})
            continue
        ev = mint_event_for_tx(tx)
        if ev is None:
            continue  # эта tx трогала пул, но не нашим минтом (или не декодировалась) -- не считаем промежуточной покупкой минта
        wallet = tx_signer(tx)
        mint_is_m0 = ev["m0"] == MINT
        price = price_from_event(ev, mint_is_m0)
        amounts = cp_trade_amounts(ev) if ev["kind"] == "cp" else None
        sol_amount = None
        direction = None
        if amounts:
            if amounts["input_mint"] == WSOL:
                sol_amount, direction = amounts["input_amount"], "покупка минта за WSOL"
            elif amounts["output_mint"] == WSOL:
                sol_amount, direction = amounts["output_amount"], "продажа минта за WSOL"
        rows.append({
            "signature": h["signature"], "slot": tx["slot"],
            "slots_after_leader": tx["slot"] - leader_tx["slot"],
            "slots_before_pilot": pilot_tx["slot"] - tx["slot"],
            "block_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tx["blockTime"])),
            "wallet": wallet, "wallet_class": classify_wallet(wallet, roles) if wallet else None,
            "direction": direction, "sol_amount": sol_amount,
            "pool_kind": ev["kind"], "price_mint_in_wsol": price,
            "amounts_resolved": amounts is not None,
        })

    result["n_intermediate_trades_this_pool"] = len(rows)
    result["intermediate_trades"] = rows

    prices_seq = [("лидер (вход)", leader_price_wsol)]
    for r in rows:
        if r.get("price_mint_in_wsol") is not None:
            prices_seq.append((r["wallet"][:8] + ".." if r.get("wallet") else "?", r["price_mint_in_wsol"]))
    prices_seq.append(("пилот (вход)", pilot_price_wsol))

    contrib = []
    for i in range(1, len(prices_seq)):
        who, p_after = prices_seq[i]
        _, p_before = prices_seq[i - 1]
        if p_before and p_after:
            contrib.append({"actor": who, "price_before": p_before, "price_after": p_after,
                             "step_pct": (p_after / p_before - 1.0) * 100})
    result["price_step_decomposition"] = contrib
    result["sum_of_intermediate_steps_pct"] = sum(c["step_pct"] for c in contrib[:-1]) if len(contrib) > 1 else 0.0

    ledger = json.loads((REPO_ROOT / "data" / "solana_dbot_realized_ledger.json").read_text())
    pilot_row = next((t for t in ledger["trades"] if t.get("buy_signature") == PILOT_BUY_SIG), None)
    result["our_exit"] = {
        "sell_signature": PILOT_SELL_SIG,
        "hold_seconds": pilot_row.get("hold_seconds") if pilot_row else None,
        "our_buy_price_usdc_equiv": pilot_row.get("our_buy_price_usdc_equiv") if pilot_row else None,
        "our_sell_price_usdc_equiv": pilot_row.get("our_sell_price_usdc_equiv") if pilot_row else None,
        "markup_at_entry_pct_ledger": pilot_row.get("markup_at_entry_pct") if pilot_row else None,
        "gross_pct": pilot_row.get("gross_pct") if pilot_row else None,
        "net_pct": pilot_row.get("net_pct") if pilot_row else None,
        "net_pct_after_dbot_fee": pilot_row.get("net_pct_after_dbot_fee") if pilot_row else None,
        "note": "числа выхода -- из data/solana_dbot_realized_ledger.json, тоже RPC (getTransaction по sell_signature), не Dune.",
    }

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[jupcat] готово: {len(rows)} промежуточных tx на пуле, "
          f"markup_onchain={result.get('markup_pct_onchain_wsol_basis')}", flush=True)


if __name__ == "__main__":
    main()
