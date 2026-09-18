#!/usr/bin/env python3
"""Владелец, 2026-09-18: реальный поток сигналов по 29 кандидатам с сита
Fomo (data/dbot_copy_wallets_29.txt) за последние 7 суток -- ТОЛЬКО счёт
(свопы, первые входы, размер входа в SOL), без цен и без доходности --
это отдельный, следующий этап. Плюс контрольная строка -- наш лидер
Beqv6dz..., его цифры уже известны по пилоту (43 первых входа за 5.75
суток реального окна, медиана входа $2000 ~= курс дня) -- если метод даёт
по нему сходящееся число, методу можно верить.

Логика "первый вход" -- ТА ЖЕ, что в основном конвейере (баланс минта до
транзакции == 0): т.е. дельта токен-баланса кошелька, пересчитанная из
pre/postTokenBalances, как уже сделано в solana_fomo_onchain_filter.py /
solana_dbot_pilot_summary.py этой же сессии (копия функции, не с нуля).

Окно режем по blockTime, не по числу подписей -- честно резюмируемо
(чекпоинт на кошелёк), с ограничением времени на кошелёк (гиперактивные
не должны съедать весь бюджет прохода -- см. PER_WALLET_TIME_BUDGET_S) и
явной меткой truncated=true + числом реально покрытых суток, если лимит
страниц исчерпан раньше окна в 7 дней.

SOL, потраченный на первый вход, -- нетто SOL out ЗА ВЫЧЕТОМ сетевой
комиссии (только если кошелёк сам платит -- fee payer) И ренты за
создание нового токен-аккаунта (System Program createAccount с этим
кошельком как source, включая CPI внутри createAssociatedTokenAccount).
Если после этого чистая трата SOL пренебрежимо мала -- значит, вход был
не за SOL, а за другой токен (USDC и т.п.) -- тогда quote_mint = тот
минт, sol_spent не считается (не конвертируем, честно отдельная
колонка), как и просил владелец."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WALLETS_FILE = REPO_ROOT / "data" / "dbot_copy_wallets_29.txt"
OUT_PATH = REPO_ROOT / "data" / "solana_29_candidates_flow_7d.json"

USDC_MINT = fp.USDC
SOL_MINT = "So11111111111111111111111111111111111111112"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
LOOKBACK_DAYS = 7
SOL_THRESHOLD = D("4.3")
MAX_PAGES_PER_WALLET = 25  # честный предел страниц (по 1000 подписей) -- не бесконечный
PER_WALLET_TIME_BUDGET_S = 150  # шире, чем в сите (нужен полный разбор транзакций, не только факт активности)
TOTAL_TIME_BUDGET_S = 18 * 60
COMMIT_INTERVAL_S = 90
MIN_SOL_QUOTE_THRESHOLD = D("0.001")  # ниже -- считаем, что SOL не был котируемой валютой (пыль/округление)


def wallet_mint_deltas(tx: dict, wallet: str) -> dict:
    """Дельты токен-баланса кошелька -- та же логика, что в
    solana_fomo_onchain_filter.py / solana_dbot_pilot_summary.py этой
    сессии (скопировано, не написано заново -- сам метод идентичен)."""
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
    return {
        "increased": [k for k, v in delta.items() if v > 0],
        "decreased": [k for k, v in delta.items() if v < 0],
        "pre_balances": {k: str(v) for k, v in pre.items()},
        "deltas": {k: str(v) for k, v in delta.items()},
    }


def wallet_sol_delta(tx: dict, wallet: str) -> dict | None:
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
    return {"delta_sol": (post_list[idx] - pre_list[idx]) / 1e9, "account_index": idx}


def is_fee_payer(tx: dict, wallet: str) -> bool:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    if not keys:
        return False
    first = keys[0]
    pk = first.get("pubkey") if isinstance(first, dict) else first
    return pk == wallet


def rent_paid_by_wallet(tx: dict, wallet: str) -> float:
    """System Program createAccount (в т.ч. CPI внутри создания ATA), где
    source == кошелёк -- это рента за новый аккаунт, не часть суммы свопа."""
    total = 0
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []

    def scan(ix_list):
        nonlocal total
        for ix in ix_list:
            if not isinstance(ix, dict):
                continue  # неразобранная (не-parsed) инструкция приходит сырой base58-строкой, не словарём
            parsed = ix.get("parsed") or {}
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") == "createAccount" and ix.get("program") == "system":
                info = parsed.get("info", {})
                if info.get("source") == wallet:
                    total += info.get("lamports", 0)

    scan(instrs)
    for grp in inner:
        if isinstance(grp, dict):
            scan(grp.get("instructions", []))
    return total / 1e9


def analyze_wallet(address: str, cutoff_time: int) -> dict:
    before = None
    n_sigs = n_swaps = n_first_entries = 0
    first_entry_events: list[dict] = []
    started_at = time.monotonic()
    partial = False
    earliest_bt_seen = None
    n_pages = 0
    for _ in range(MAX_PAGES_PER_WALLET):
        batch = fp.get_signatures_for_address(address, before=before)
        if not batch:
            break
        n_pages += 1
        stop = False
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            earliest_bt_seen = bt if earliest_bt_seen is None else min(earliest_bt_seen, bt)
            if bt < cutoff_time:
                stop = True
                break
            if time.monotonic() - started_at > PER_WALLET_TIME_BUDGET_S:
                partial = True
                stop = True
                break
            n_sigs += 1
            if s.get("err") is not None:
                continue
            tx = fp.get_transaction(s["signature"])
            if tx is None:
                continue
            deltas = wallet_mint_deltas(tx, address)
            touched = [m for m in deltas["increased"] + deltas["decreased"] if m not in (USDC_MINT, SOL_MINT)]
            if touched:
                n_swaps += 1
            for m in deltas["increased"]:
                if m in (USDC_MINT, SOL_MINT):
                    continue
                if D(deltas["pre_balances"].get(m, "0")) != 0:
                    continue
                n_first_entries += 1
                sol_delta = wallet_sol_delta(tx, address)
                total_sol_out = D(str(-sol_delta["delta_sol"])) if sol_delta else D(0)
                network_fee = D(str((tx.get("meta") or {}).get("fee", 0) / 1e9)) if is_fee_payer(tx, address) else D(0)
                rent = D(str(rent_paid_by_wallet(tx, address)))
                net_sol_spent = total_sol_out - network_fee - rent
                event = {"signature": s["signature"], "block_time": bt, "mint": m}
                if net_sol_spent > MIN_SOL_QUOTE_THRESHOLD:
                    event["quote_mint"] = "SOL"
                    event["sol_spent"] = float(net_sol_spent)
                else:
                    decreased_other = [dm for dm in deltas["decreased"] if dm != m]
                    if decreased_other:
                        qm = decreased_other[0]
                        event["quote_mint"] = qm
                        event["quote_amount"] = abs(float(D(deltas["deltas"][qm])))
                        event["sol_spent"] = None
                    else:
                        event["quote_mint"] = "unknown"
                        event["sol_spent"] = None
                first_entry_events.append(event)
        if stop:
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break

    # Два разных честных случая "меньше 7 дней покрыто": (а) упёрлись в НАШ
    # предел (страницы/время) -- это truncated=true, недостаток метода; (б)
    # у кошелька просто нет 7 дней истории (страница пришла пустой раньше
    # cutoff) -- это факт о кошельке, не truncated.
    days_covered = LOOKBACK_DAYS
    history_shorter_than_window = False
    if partial and earliest_bt_seen is not None:
        days_covered = round((int(time.time()) - earliest_bt_seen) / 86400, 2)
    elif n_pages >= MAX_PAGES_PER_WALLET and earliest_bt_seen is not None and earliest_bt_seen > cutoff_time:
        partial = True
        days_covered = round((int(time.time()) - earliest_bt_seen) / 86400, 2)
    elif earliest_bt_seen is not None and earliest_bt_seen > cutoff_time:
        history_shorter_than_window = True
        days_covered = round((int(time.time()) - earliest_bt_seen) / 86400, 2)

    sol_sizes = [e["sol_spent"] for e in first_entry_events if e.get("sol_spent") is not None]
    n_ge = sum(1 for v in sol_sizes if v >= float(SOL_THRESHOLD))

    def pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        k = (len(s) - 1) * p
        f, c = int(k), min(int(k) + 1, len(s) - 1)
        return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)

    return {
        "n_signatures_checked": n_sigs,
        "n_swaps_total": n_swaps,
        "n_first_entries": n_first_entries,
        "n_add_ons": n_swaps - n_first_entries,
        "n_first_entries_sol_quoted": len(sol_sizes),
        "n_first_entries_ge_4_3_sol": n_ge,
        "sol_size_median": median(sol_sizes) if sol_sizes else None,
        "sol_size_p25": pct(sol_sizes, 0.25),
        "sol_size_p75": pct(sol_sizes, 0.75),
        "sol_size_min": min(sol_sizes) if sol_sizes else None,
        "sol_size_max": max(sol_sizes) if sol_sizes else None,
        "days_covered": days_covered,
        "first_entries_per_day": round(n_first_entries / days_covered, 3) if days_covered else None,
        "first_entries_ge_4_3_per_day": round(n_ge / days_covered, 3) if days_covered else None,
        "truncated": partial,
        "history_shorter_than_window": history_shorter_than_window,
        "first_entry_events": first_entry_events,
    }


def main() -> None:
    addresses = [ln.strip() for ln in WALLETS_FILE.read_text().splitlines() if ln.strip()]
    print(f"[flow29] {len(addresses)} кандидатов + 1 контроль (лидер), alchemy_available={fp.alchemy_available()}", flush=True)

    if OUT_PATH.exists():
        data = json.loads(OUT_PATH.read_text())
    else:
        data = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "lookback_days": LOOKBACK_DAYS, "sol_threshold": float(SOL_THRESHOLD),
                "wallets": {a: None for a in addresses}, "leader_control": None}

    cutoff_time = int(time.time()) - LOOKBACK_DAYS * 86400
    started_at = time.monotonic()
    last_commit_at = started_at

    def process(address: str, label: str):
        nonlocal last_commit_at
        if time.monotonic() - started_at > TOTAL_TIME_BUDGET_S:
            return False
        try:
            result = analyze_wallet(address, cutoff_time)
        except RuntimeError as exc:
            result = {"error": str(exc)[:300]}
        if label == "leader":
            data["leader_control"] = result
        else:
            data["wallets"][address] = result
        print(f"[flow29] {label} {address[:12]}.. swaps={result.get('n_swaps_total')} "
              f"first_entries={result.get('n_first_entries')} ge4.3={result.get('n_first_entries_ge_4_3_sol')} "
              f"days={result.get('days_covered')} truncated={result.get('truncated')}", flush=True)
        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            fp._git_commit_progress("flow29", [OUT_PATH])
            last_commit_at = time.monotonic()
        return True

    if data.get("leader_control") is None:
        process(LEADER_WALLET, "leader")
    for addr in addresses:
        if data["wallets"].get(addr) is not None:
            continue
        if not process(addr, "candidate"):
            print("[flow29] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break

    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    n_done = sum(1 for v in data["wallets"].values() if v is not None)
    total_ge_per_day = sum((v.get("first_entries_ge_4_3_per_day") or 0) for v in data["wallets"].values() if v)
    data["summary"] = {
        "n_candidates_done": n_done, "n_candidates_total": len(addresses),
        "summary_is_partial": n_done < len(addresses),
        "sum_first_entries_ge_4_3_per_day_across_29": round(total_ge_per_day, 3),
        "implied_deposit_sol_at_0_1_per_signal": round(total_ge_per_day * 0.1, 3),
    }
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print(f"[flow29] прогон: {n_done}/{len(addresses)} готово. summary={data['summary']}", flush=True)


if __name__ == "__main__":
    main()
