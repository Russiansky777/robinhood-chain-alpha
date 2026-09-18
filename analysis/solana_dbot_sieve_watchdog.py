#!/usr/bin/env python3
"""Владелец, 2026-09-18: сторож сита на ночь -- живые деньги без присмотра.
Каждые 2 часа (расписание в workflow): смотрит on-chain балансы токенов
кошелька сита; если есть купленный и НЕ проданный минт старше 5 минут --
задача падает с ошибкой (упавший workflow = будильник, письмо владельцу).
Если всё чисто -- дописывает строку в data/dbot_sieve_watch.log и
завершается успешно.

НИЧЕГО не продаёт и не останавливает -- только смотрит и сигналит
(явное требование владельца).

"Первый вход"/своп -- ТА ЖЕ проверка (signer), что уже установлена как
правило после бага с аирдропами в solana_29_candidates_flow.py (владелец,
после этой самой задачи: "любой новый скрипт, считающий первые входы,
использует классификатор из основного конвейера") -- is_signer здесь
используется для того же: отличить реальную покупку сита от постороннего
входящего перевода токена на кошелёк."""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "data" / "dbot_sieve_baseline.json"
LOG_PATH = REPO_ROOT / "data" / "dbot_sieve_watch.log"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
STUCK_AGE_THRESHOLD_S = 5 * 60
MAX_SIGS_TO_SCAN_PER_ATA = 20

DBOT_HOST = "https://api-bot-v1.dbotx.com"


def wallet_mint_deltas(tx: dict, wallet: str) -> dict:
    """Дословная копия классификатора первого входа/свопа из основного
    конвейера (solana_29_candidates_flow.py / solana_fomo_onchain_filter.py
    этой же сессии) -- см. docstring модуля."""
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
    return {"delta_sol": (post_list[idx] - pre_list[idx]) / 1e9}


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


def get_token_accounts(wallet: str) -> list[dict]:
    r = fp.rpc_call("getTokenAccountsByOwner", [wallet, {"programId": TOKEN_PROGRAM_ID}, {"encoding": "jsonParsed"}],
                    use_cache=False)
    return (r or {}).get("value") or []


def find_purchase_time(ata_pubkey: str, wallet: str, mint: str) -> dict:
    """Сканируем недавние подписи ATA НАЗАД (newest-first), ищем последнюю
    транзакцию, где баланс этого минта у кошелька УВЕЛИЧИЛСЯ (покупка) --
    это и есть момент, с которого позиция открыта."""
    sigs = fp.get_signatures_for_address(ata_pubkey, limit=MAX_SIGS_TO_SCAN_PER_ATA)
    for s in sigs:
        if s.get("err") is not None:
            continue
        tx = fp.get_transaction(s["signature"])
        if tx is None:
            continue
        deltas = wallet_mint_deltas(tx, wallet)
        if mint in deltas["increased"]:
            sol_delta = wallet_sol_delta(tx, wallet)
            total_sol_out = D(str(-sol_delta["delta_sol"])) if sol_delta else D(0)
            network_fee = D(str((tx.get("meta") or {}).get("fee", 0) / 1e9)) if is_fee_payer(tx, wallet) else D(0)
            return {
                "status": "found",
                "signature": s["signature"],
                "block_time": tx.get("blockTime"),
                "is_signer": is_signer(tx, wallet),
                "approx_sol_spent": float(total_sol_out - network_fee),
            }
    return {"status": "not_found_in_last_%d_signatures" % MAX_SIGS_TO_SCAN_PER_ATA}


def dbot_task_counters(task_id: str) -> dict | None:
    api_key = os.environ.get("DBOT_API_KEY", "")
    if not api_key:
        return None
    import requests
    try:
        resp = requests.get(f"{DBOT_HOST}/automation/follow_orders", headers={"x-api-key": api_key},
                             params={"chain": "solana"}, timeout=30)
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    for t in (body.get("res") or []):
        if t.get("id") == task_id:
            return {"buyTimes": t.get("buyTimes"), "sellTimes": t.get("sellTimes"),
                     "boughtUsd": t.get("boughtUsd"), "soldUsd": t.get("soldUsd")}
    return None


def last_log_counters() -> dict | None:
    if not LOG_PATH.exists():
        return None
    lines = [ln for ln in LOG_PATH.read_text().splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except (ValueError, json.JSONDecodeError):
        return None


def main() -> None:
    if not BASELINE_PATH.exists():
        print("[sieve_watchdog] нулевая точка сита не найдена -- проверять нечего, "
              "падаем честно (это тоже сигнал, что что-то не так с настройкой)", flush=True)
        sys.exit(1)
    baseline = json.loads(BASELINE_PATH.read_text())
    wallet = baseline.get("sieve_task_wallet_address")
    task_id = baseline.get("sieve_task_id")
    if not wallet:
        print("[sieve_watchdog] в нулевой точке нет адреса кошелька сита -- падаем", flush=True)
        sys.exit(1)

    now = int(time.time())
    print(f"[sieve_watchdog] проверка кошелька сита {wallet} в {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}", flush=True)

    accounts = get_token_accounts(wallet)
    stuck = []
    for acc in accounts:
        info = ((acc.get("account") or {}).get("data") or {}).get("parsed", {}).get("info", {})
        mint = info.get("mint")
        amt = (info.get("tokenAmount") or {})
        ui_amount = amt.get("uiAmount")
        if mint in (SOL_MINT, USDC_MINT) or not ui_amount or ui_amount <= 0:
            continue
        purchase = find_purchase_time(acc.get("pubkey"), wallet, mint)
        age_s = (now - purchase["block_time"]) if purchase.get("block_time") else None
        entry = {"mint": mint, "amount": ui_amount, "token_account": acc.get("pubkey"),
                  "purchase_info": purchase, "age_seconds": age_s}
        print(f"[sieve_watchdog] держим {mint[:12]}.. amount={ui_amount} purchase={purchase} age_s={age_s}", flush=True)
        if age_s is None or age_s > STUCK_AGE_THRESHOLD_S:
            stuck.append(entry)

    if stuck:
        print("\n=== ЗАСТРЯВШИЕ ПОЗИЦИИ (куплено, не продано, старше 5 минут) ===", flush=True)
        for e in stuck:
            p = e["purchase_info"]
            print(f"mint={e['mint']} amount={e['amount']} "
                  f"purchase_time={p.get('block_time')} age_s={e['age_seconds']} "
                  f"approx_sol_spent={p.get('approx_sol_spent')}", flush=True)
        sys.exit(1)

    # Всё чисто -- дописываем строку в лог.
    bal = fp.rpc_call("getBalance", [wallet], use_cache=False)
    sol_balance = (bal or {}).get("value", 0) / 1e9 if isinstance(bal, dict) else None
    counters = dbot_task_counters(task_id) if task_id else None
    prev = last_log_counters()
    n_orders_period = None
    if counters is not None and prev is not None and prev.get("counters"):
        prev_c = prev["counters"]
        bt0, st0 = prev_c.get("buyTimes"), prev_c.get("sellTimes")
        bt1, st1 = counters.get("buyTimes"), counters.get("sellTimes")
        if all(isinstance(x, (int, float)) for x in (bt0, st0, bt1, st1)):
            n_orders_period = (bt1 - bt0) + (st1 - st0)

    log_entry = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "status": "clean",
        "n_open_positions": 0,
        "sol_balance": sol_balance,
        "counters": counters,
        "n_orders_since_last_check": n_orders_period,
        "n_rejected_since_last_check": None,
        "rejected_honest_note": "DBot API (/automation/follow_orders) не отдаёт счётчик отказов -- поле недоступно, не выдумываем",
    }
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    print(f"[sieve_watchdog] чисто. баланс={sol_balance} SOL, счётчики={counters}, "
          f"ордеров с прошлой проверки={n_orders_period}", flush=True)


if __name__ == "__main__":
    main()
