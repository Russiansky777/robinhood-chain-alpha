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


def is_signer(tx: dict, wallet: str) -> bool:
    """Тот же тест, что select.py/solana_buyer200_select_extend.py::classify()
    (signed = any(k.get("signer") and k["pubkey"]==WALLET for k in keys)) --
    отсеивает пассивные "входы" (спам/пыль-airdrop токенов на заметный
    кошелёк без его подписи), которые pre_balance==0 сам по себе не ловит."""
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    return any(isinstance(k, dict) and k.get("signer") and k.get("pubkey") == wallet for k in keys)


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


MAX_RESUME_ROUNDS = 20  # honest предохранитель от бесконечного дозаписывания -- см. docstring scan_wallet_chunk


def scan_wallet_chunk(address: str, cutoff_time: int, resume: dict | None) -> tuple[bool, dict]:
    """Один КУСОК разбора истории кошелька (до PER_WALLET_TIME_BUDGET_S на
    вызов), резюмируемо ЧЕРЕЗ ПРОГОНЫ (не только между кошельками): гиперактивный
    кошелёк (как выяснилось на лидере -- сотни подписей в сутки) не укладывается
    в один вызов, а раньше "упёрлись в бюджет" молча становилось окончательным
    результатом (truncated=true навсегда, часть окна так и не считана). Теперь
    курсор (before) и накопленные счётчики сохраняются в data["_scan_progress"]
    и следующий прогон продолжает СТРОГО с того же места, не считает заново.
    Возвращает (done, state); done=False значит -- сохранить state и повторить
    в следующем прогоне."""
    if resume:
        before = resume["before"]
        n_sigs, n_swaps, n_first_entries = resume["n_sigs"], resume["n_swaps"], resume["n_first_entries"]
        first_entry_events: list[dict] = resume["first_entry_events"]
        earliest_bt_seen = resume["earliest_bt_seen"]
        rounds = resume.get("rounds", 0) + 1
    else:
        before = None
        n_sigs = n_swaps = n_first_entries = 0
        first_entry_events = []
        earliest_bt_seen = None
        rounds = 1
    started_at = time.monotonic()
    done = False
    history_shorter_than_window = False
    for _ in range(MAX_PAGES_PER_WALLET):
        batch = fp.get_signatures_for_address(address, before=before)
        if not batch:
            done = True
            history_shorter_than_window = earliest_bt_seen is not None and earliest_bt_seen > cutoff_time
            break
        stop = False
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            earliest_bt_seen = bt if earliest_bt_seen is None else min(earliest_bt_seen, bt)
            if bt < cutoff_time:
                done = True
                stop = True
                break
            if time.monotonic() - started_at > PER_WALLET_TIME_BUDGET_S:
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
            if touched and not is_signer(tx, address):
                # пассивный получатель (спам/пыль-airdrop) -- не подписывал,
                # значит это не своп кошелька; та же проверка, что в
                # основном конвейере (classify(): wallet_not_signer)
                continue
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
            done = True
            history_shorter_than_window = earliest_bt_seen is not None and earliest_bt_seen > cutoff_time
            break
    if not done and rounds >= MAX_RESUME_ROUNDS:
        # честный предохранитель -- после стольких дозаписей (гиперактивный
        # кошелёк) фиксируем как truncated=true, а не крутим бесконечно
        done = True
    state = {
        "before": before, "n_sigs": n_sigs, "n_swaps": n_swaps, "n_first_entries": n_first_entries,
        "first_entry_events": first_entry_events, "earliest_bt_seen": earliest_bt_seen, "rounds": rounds,
        "history_shorter_than_window": history_shorter_than_window,
    }
    return done, state


def finalize_wallet(state: dict, cutoff_time: int) -> dict:
    n_sigs, n_swaps, n_first_entries = state["n_sigs"], state["n_swaps"], state["n_first_entries"]
    first_entry_events = state["first_entry_events"]
    earliest_bt_seen = state["earliest_bt_seen"]
    history_shorter_than_window = state["history_shorter_than_window"]
    reached_cutoff = earliest_bt_seen is not None and earliest_bt_seen <= cutoff_time
    # Два разных честных случая "меньше 7 дней покрыто": (а) исчерпали
    # MAX_RESUME_ROUNDS дозаписей, так и не дойдя до cutoff -- truncated=true,
    # недостаток метода; (б) у кошелька просто нет 7 дней истории (страница
    # пришла пустой раньше cutoff) -- это факт о кошельке, не truncated.
    truncated = not reached_cutoff and not history_shorter_than_window
    if reached_cutoff:
        days_covered = LOOKBACK_DAYS
    elif earliest_bt_seen is not None:
        days_covered = round((int(time.time()) - earliest_bt_seen) / 86400, 2)
    else:
        days_covered = 0

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
        "truncated": truncated,
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
    data.setdefault("_scan_progress", {})

    cutoff_time = int(time.time()) - LOOKBACK_DAYS * 86400
    started_at = time.monotonic()
    last_commit_at = started_at

    def process(address: str, label: str) -> bool:
        """Дожимает ОДИН кошелёк кусок за куском (не переключается на
        следующий кандидат после первого куска) -- иначе гиперактивный
        кошелёк никогда бы не закончился, размазывая прогресс тонким
        слоем по всем 30. Возвращает False, если общий бюджет прогона
        исчерпан ДО завершения (в т.ч. этого кошелька) -- тогда его
        _scan_progress уже сохранён, следующий прогон продолжит с него же."""
        nonlocal last_commit_at
        while True:
            if time.monotonic() - started_at > TOTAL_TIME_BUDGET_S:
                return False
            try:
                resume = data["_scan_progress"].get(address)
                done, state = scan_wallet_chunk(address, cutoff_time, resume)
            except RuntimeError as exc:
                if label == "leader":
                    data["leader_control"] = {"error": str(exc)[:300]}
                else:
                    data["wallets"][address] = {"error": str(exc)[:300]}
                data["_scan_progress"].pop(address, None)
                return True
            if not done:
                data["_scan_progress"][address] = state
                print(f"[flow29] {label} {address[:12]}.. кусок #{state['rounds']} "
                      f"(накоплено сигнатур={state['n_sigs']}, первых входов={state['n_first_entries']})", flush=True)
                if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
                    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
                    fp._git_commit_progress("flow29", [OUT_PATH])
                    last_commit_at = time.monotonic()
                continue
            result = finalize_wallet(state, cutoff_time)
            data["_scan_progress"].pop(address, None)
            if label == "leader":
                data["leader_control"] = result
            else:
                data["wallets"][address] = result
            print(f"[flow29] {label} {address[:12]}.. ГОТОВО swaps={result.get('n_swaps_total')} "
                  f"first_entries={result.get('n_first_entries')} ge4.3={result.get('n_first_entries_ge_4_3_sol')} "
                  f"days={result.get('days_covered')} truncated={result.get('truncated')}", flush=True)
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
