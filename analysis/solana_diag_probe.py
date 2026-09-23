#!/usr/bin/env python3
"""Разовый диагностический прогон (не часть основного конвейера):
1. Полный разбор транзакции 4QTP1J3... (пополнение 1.0 SOL BATCH-4) --
   какие аккаунты изменились и на сколько, какие программы вызваны.
2. Проверка follow_trades с фильтром targetWallet=N_HtuY для BATCH-4 --
   действительно ли так находятся недостающие 4 сделки."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_ledger_run as lr  # noqa: E402

SIG = "4QTP1J31EdYWVE3npr3BgJxY6KaJXSgMSzUbduBo2UyqaYMhkKEaXLVLfS8wAgnc2yf6kMxYoAkyxWSWd974aFXj"
BATCH4_TASK_ID = "mu8fj8uy0c79pk"
BATCH4_WALLET = "EjeXrxabRKmwLxvfQdXYN3oD3d3uWe2fuqqA5Qda2p8N"
N_HTUY = "HtuYE3nYd7y9vxqiUjZdjTscGiFKt5JCcjD41vFwuebT"


def dump_tx() -> None:
    print("=" * 20, "1) Разбор транзакции", SIG, "=" * 20, flush=True)
    tx = lr.rpc_call("getTransaction", [SIG, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    if tx is None:
        print("getTransaction вернул null", flush=True)
        return
    meta = tx.get("meta") or {}
    msg = tx["transaction"]["message"]
    static_keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg["accountKeys"]]
    la = meta.get("loadedAddresses") or {}
    keys = static_keys + list(la.get("writable") or []) + list(la.get("readonly") or [])
    pre_bal, post_bal = meta.get("preBalances") or [], meta.get("postBalances") or []
    print("slot:", tx.get("slot"), "blockTime:", tx.get("blockTime"), "err:", meta.get("err"), "fee:", meta.get("fee"))
    print("-- изменения балансов аккаунтов (лампорты != 0) --")
    for i, key in enumerate(keys):
        if i >= len(pre_bal) or i >= len(post_bal):
            continue
        d = post_bal[i] - pre_bal[i]
        if d != 0:
            print(f"  {key}: {pre_bal[i]} -> {post_bal[i]} (delta {d} лампорт = {d/1e9} SOL)")
    print("-- вызванные программы (верхнего уровня) --")
    for ix in msg.get("instructions", []):
        print(" ", ix.get("programId"), ix.get("program"), ix.get("parsed", ix.get("data")))
    print("-- innerInstructions --")
    for inner in meta.get("innerInstructions") or []:
        print(f"  [instruction #{inner.get('index')}]")
        for ix in inner.get("instructions", []):
            print("    ", ix.get("programId"), ix.get("program"), ix.get("parsed", ix.get("data")))
    print("-- logMessages (последние 30) --")
    for line in (meta.get("logMessages") or [])[-30:]:
        print("   ", line)


def check_targetwallet_pagination() -> None:
    print("=" * 20, "2) follow_trades с targetWallet=N_HtuY (BATCH-4)", "=" * 20, flush=True)
    api_key = os.environ.get("DBOT_API_KEY", "")
    if not api_key:
        print("DBOT_API_KEY пуст -- пропуск", flush=True)
        return
    out = []
    page = 1
    while True:
        status, body = lr.dbot_get(
            "/account/follow_trades",
            {"chain": "solana", "configId": BATCH4_TASK_ID, "myWallet": BATCH4_WALLET,
             "targetWallet": N_HTUY, "page": page, "size": 20},
            api_key,
        )
        items = lr.extract_items(body)
        print(f"  страница {page}: {len(items)} записей (http={status})", flush=True)
        if not items:
            break
        out.extend(items)
        page += 1
        if page > 100:
            print("  ! остановка по защитному лимиту 100 страниц", flush=True)
            break
    print(f"итого записей с targetWallet=N_HtuY: {len(out)}", flush=True)
    target_mints = {
        "72qdjY9F1ZtLaB8nJTrkX8ek3mudB9VFhKAK1Fti2HrY",
        "6oeiky8G8ZnuvadARQPRMkV6FRz8ALKd579ZuXhKSTNK",
        "DkjoTNGY6nFGEPRPGCo79siM1w3ZL3RDyfBDPGNSnGn3",
        "Hg5Ja55T5wESq4vyFoiVCMeHXtGyVA69X2UHq8hgpump",
    }
    found = [r for r in out if lr.dbot_token_contract(r) in target_mints]
    print(f"из них с недостающими минтами: {len(found)}", flush=True)
    for r in found:
        print("  id:", r.get("id"), "state:", r.get("state"), "type:", r.get("type"),
              "mint:", lr.dbot_token_contract(r), "timestamp:", r.get("timestamp"),
              "sig:", lr.dbot_signature_from_record(r))

    print("-- для сравнения: обычный запрос без targetWallet, полная пагинация --", flush=True)
    plain = lr.fetch_follow_trades_for_task(BATCH4_TASK_ID, api_key, my_wallet=BATCH4_WALLET)
    print(f"итого записей (myWallet, полная пагинация до пустой страницы): {len(plain)}", flush=True)
    found_plain = [r for r in plain if lr.dbot_token_contract(r) in target_mints]
    print(f"из них с недостающими минтами: {len(found_plain)}", flush=True)


def check_full_signature_history() -> None:
    """Реконсиляция BATCH-4 не сходится на одну и ту же величину (0.500015
    SOL) в НЕСКОЛЬКИХ отдельных прогонах при НЕИЗМЕННОМ балансе -- значит,
    это не "сделка прошла во время прогона", а реальный пробел в
    закэшированной истории (скорее всего, оставшийся от старых, уже
    исправленных в этой сессии багов синхронизации: forward-проход и
    backward genesis-fill проверяют только края уже известного диапазона
    подписей, не середину). Здесь -- независимый, полный обход ВСЕХ
    подписей кошелька с нуля (без использования кэша вообще), сверка с
    тем, что реально есть в data/chain_tx_cache.json."""
    print("=" * 20, "3) Полный независимый обход подписей BATCH-4 (без кэша)", "=" * 20, flush=True)
    all_sigs = []
    before = None
    page_n = 0
    while True:
        page_n += 1
        page = lr.get_signatures_for_address(BATCH4_WALLET, before=before, limit=1000)
        if not page:
            print(f"  страница {page_n}: пусто -- дошли до генезиса", flush=True)
            break
        all_sigs.extend(page)
        print(f"  страница {page_n}: {len(page)} подписей, самая старая blockTime={page[-1].get('blockTime')}", flush=True)
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
        if page_n > 50:
            print("  ! остановка по защитному лимиту 50 страниц", flush=True)
            break
    print(f"итого подписей на цепочке (полный обход): {len(all_sigs)}", flush=True)

    cache = json.loads(lr.CHAIN_CACHE_PATH.read_text())
    cached_sigs = {v["signature"] for v in cache.values() if isinstance(v, dict) and v.get("_wallet") == BATCH4_WALLET}
    print(f"итого подписей в кэше: {len(cached_sigs)}", flush=True)

    missing = [h for h in all_sigs if h["signature"] not in cached_sigs]
    print(f"ПРОПУЩЕНО (есть на цепочке, нет в кэше): {len(missing)}", flush=True)
    total_missing_delta = 0.0
    for h in missing:
        sig = h["signature"]
        if h.get("err") is not None:
            print(f"  {sig[:20]}.. err={h.get('err')} (не влияет на баланс)")
            continue
        tx = lr.rpc_call("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1}])
        if tx is None:
            print(f"  {sig[:20]}.. getTransaction -> null")
            continue
        parsed = lr.parse_tx_for_wallet(sig, tx, BATCH4_WALLET)
        d = (parsed.get("sol_delta_native") or 0) + (parsed.get("wsol_delta") or 0)
        total_missing_delta += d
        print(f"  {sig[:20]}.. blockTime={h.get('blockTime')} delta={d} SOL is_signer={parsed.get('is_signer')} "
              f"token_deltas={parsed.get('token_deltas')}")
    print(f"сумма дельт пропущенных транзакций: {total_missing_delta} SOL", flush=True)


def main() -> None:
    dump_tx()
    check_targetwallet_pagination()
    check_full_signature_history()


if __name__ == "__main__":
    main()
