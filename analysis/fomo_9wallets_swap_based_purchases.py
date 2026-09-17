#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo -- поправка к приоритету 2: парность живёт
в СОБЫТИЯХ ПУЛА (Swap на PoolManager), не в ERC-20-переводах кошелька,
раз оплата идёт ВНУТРИ вызова исполнителя `0xb92fe925...` (найден этой
же сессией: реально вызывает настоящий V4 PoolManager, кошелёк никогда
не tx.from).

Метод (переиспользует УЖЕ ПРОВЕРЕННЫЙ на реальных данных код этой сессии,
не пишет разбор Swap-события заново):
  - `task5_v4_pool_math.decode_v4_swap_log_data` -- разбор data-поля
    PoolManager.Swap, независимо воспроизвёл реальную tx побайтово
    (см. докстринг модуля).
  - `task5_v4_hook_route_audit.fetch_initialize_event` -- узкий topic-
    фильтр по pool_id, даёт настоящие currency0/currency1/fee из
    Initialize-события (не предположение).

Шаги:
  1. Кандидатные tx -- из уже собранной data/fomo_9wallets_alchemy_raw_
     transfers.csv: где кошелёк получил ERC-20 токен НАПРЯМУЮ от
     исполнителя (direction=in, from_addr=EXECUTOR). 1720 уникальных tx
     на 9 кошельков (офлайн-подсчёт).
  2. Для каждой -- реальная receipt (eth_getTransactionReceipt), разбор
     ВСЕХ логов Swap с address=PoolManager, по порядку logIndex.
  3. Резолв уникальных pool_id через Initialize (кэш -- один запрос на
     каждый уникальный пул, не на каждую tx).
  4. Маршрут: первая нога -- вход (валюта с положительной amount),
     последняя нога -- выход (валюта с отрицательной amount). Сверка:
     выходная валюта/сумма должны соответствовать РЕАЛЬНОМУ ERC-20
     переводу кошельку (из CSV) -- если нет совпадения, честно не
     засчитываем как подтверждённую покупку.
  5. Цена входа: если платёжная валюта -- канонический USDG, цена
     тривиальна ($1); если WETH -- через WETH/USDG референс-пул на ТОМ
     ЖЕ блоке (тот же slot0()-метод, что везде в сессии); иначе -- цена
     недоступна, честно помечено, не выдумана.
  6. Первый вход = самая ранняя подтверждённая покупка данного токена
     данным кошельком."""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import csv  # noqa: E402

from alchemy_fallback import rpc_call_trading_path, topic0  # noqa: E402
from task5_v4_pool_math import decode_v4_swap_log_data  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402

OUT_PATH = Path("data/fomo_9wallets_swap_based_purchases_result.json")
IN_CSV = Path("data/fomo_9wallets_alchemy_raw_transfers.csv")

EXECUTOR = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
SWAP_TOPIC0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG_DECIMALS = 6
WETH_DECIMALS = 18
WETH_USDG_POOL_V3 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"  # V3-стиль, slot0() -- см. task5_bot_config.py
SLOT0_SELECTOR = "0x3850c7bd"
DECIMALS_SELECTOR = "0x313ce567"

RPC = rpc_call_trading_path
TIME_BUDGET_S = 900.0  # 15 мин честный потолок на весь разбор receipts+Initialize+decimals


def get_decimals(token: str, cache: dict) -> int:
    token = token.lower()
    if token in cache:
        return cache[token]
    if token == USDG:
        cache[token] = USDG_DECIMALS
        return cache[token]
    if token == WETH:
        cache[token] = WETH_DECIMALS
        return cache[token]
    try:
        raw = RPC("eth_call", [{"to": token, "data": DECIMALS_SELECTOR}, "latest"])
        dec = int(raw, 16) if raw and raw != "0x" else 18
    except Exception:  # noqa: BLE001
        dec = 18
    cache[token] = dec
    return dec


def weth_usdg_price_at_block(block_num: int, cache: dict) -> float | None:
    key = block_num
    if key in cache:
        return cache[key]
    try:
        raw = RPC("eth_call", [{"to": WETH_USDG_POOL_V3, "data": SLOT0_SELECTOR}, hex(block_num)])
        sqrt_price_x96 = int(raw[2:66], 16) if raw and raw != "0x" else None
    except Exception:  # noqa: BLE001
        sqrt_price_x96 = None
    if sqrt_price_x96 is None:
        cache[key] = None
        return None
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    raw_price = sqrt_p * sqrt_p  # token1/token0 raw = USDG_raw / WETH_raw (token0=WETH, token1=USDG)
    price = raw_price * (10 ** (WETH_DECIMALS - USDG_DECIMALS))
    cache[key] = price
    return price


def run() -> int:
    if not IN_CSV.exists():
        print(f"[swap_purchases] СТОП: {IN_CSV} не найден")
        return 1
    with IN_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    candidates = [r for r in rows if r["direction"] == "in" and (r["from_addr"] or "").lower() == EXECUTOR]
    by_tx: dict[str, list] = defaultdict(list)
    for r in candidates:
        by_tx[r["tx_hash"]].append(r)
    print(f"[swap_purchases] {len(candidates)} кандидатных erc20-переводов от исполнителя, "
          f"{len(by_tx)} уникальных tx")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "n_candidate_tx": len(by_tx), "executor": EXECUTOR}

    start_ts = time.time()
    dec_cache: dict = {}
    weth_price_cache: dict = {}
    pool_key_cache: dict = {}
    n_receipts_fetched = 0
    n_receipts_no_swap_log = 0
    n_rpc_errors = 0
    budget_exhausted = False

    confirmed_purchases: list[dict] = []
    n_swap_logs_total = 0

    tx_items = list(by_tx.items())
    for i, (tx_hash, wallet_rows) in enumerate(tx_items):
        if time.time() - start_ts > TIME_BUDGET_S:
            budget_exhausted = True
            out["budget_exhausted_at_tx_index"] = i
            break
        try:
            receipt = RPC("eth_getTransactionReceipt", [tx_hash])
        except Exception:  # noqa: BLE001
            n_rpc_errors += 1
            continue
        n_receipts_fetched += 1
        if not receipt:
            n_rpc_errors += 1
            continue

        swap_logs = [lg for lg in receipt.get("logs", [])
                     if (lg.get("address") or "").lower() == POOL_MANAGER.lower()
                     and lg.get("topics") and lg["topics"][0].lower() == SWAP_TOPIC0.lower()]
        if not swap_logs:
            n_receipts_no_swap_log += 1
            continue
        swap_logs.sort(key=lambda lg: int(lg["logIndex"], 16))
        n_swap_logs_total += len(swap_logs)

        legs = []
        ok = True
        for lg in swap_logs:
            pool_id_hex = lg["topics"][1]
            if pool_id_hex not in pool_key_cache:
                info = fetch_initialize_event(pool_id_hex, int(receipt["blockNumber"], 16))
                pool_key_cache[pool_id_hex] = info
            info = pool_key_cache[pool_id_hex]
            if info is None:
                ok = False
                break
            decoded = decode_v4_swap_log_data(lg["data"])
            legs.append({"pool_id": pool_id_hex, "currency0": info["currency0"], "currency1": info["currency1"],
                         **decoded})
        if not ok or not legs:
            continue

        first_leg, last_leg = legs[0], legs[-1]
        if first_leg["amount0"] > 0:
            paid_currency, paid_raw = first_leg["currency0"], first_leg["amount0"]
        else:
            paid_currency, paid_raw = first_leg["currency1"], first_leg["amount1"]
        if last_leg["amount0"] < 0:
            recv_currency, recv_raw = last_leg["currency0"], -last_leg["amount0"]
        else:
            recv_currency, recv_raw = last_leg["currency1"], -last_leg["amount1"]

        # Сверка: выходная валюта/сумма маршрута должны реально совпасть с
        # ERC-20-переводом, который кошелёк получил в ЭТОЙ ЖЕ tx (не
        # предполагаем, проверяем).
        matched_row = None
        recv_dec = get_decimals(recv_currency, dec_cache)
        recv_human = recv_raw / (10 ** recv_dec)
        for wr in wallet_rows:
            if wr["token_address"].lower() == recv_currency.lower():
                try:
                    csv_amount = float(wr["value_human"])
                except ValueError:
                    continue
                if csv_amount == 0:
                    continue
                rel_diff = abs(csv_amount - recv_human) / csv_amount
                if rel_diff < 0.02:  # 2% допуск на комиссию исполнителя/округление
                    matched_row = wr
                    break
        if matched_row is None:
            continue  # честно не засчитываем -- выход маршрута не совпал с реальным переводом

        paid_dec = get_decimals(paid_currency, dec_cache)
        paid_human = paid_raw / (10 ** paid_dec)
        block_num = int(receipt["blockNumber"], 16)

        entry_price_usd = None
        price_note = None
        if paid_currency.lower() == USDG:
            entry_price_usd = paid_human / recv_human if recv_human else None
        elif paid_currency.lower() == WETH:
            weth_usd = weth_usdg_price_at_block(block_num, weth_price_cache)
            if weth_usd is not None and recv_human:
                entry_price_usd = (paid_human * weth_usd) / recv_human
            else:
                price_note = "WETH/USDG на этом блоке не получен"
        else:
            price_note = f"платёжная валюта {paid_currency} -- не USDG/WETH, цена не выведена (честно, не выдумано)"

        confirmed_purchases.append({
            "wallet_name": matched_row["wallet_name"], "wallet_address": matched_row["wallet_address"],
            "tx_hash": tx_hash, "block": block_num, "block_time_utc": matched_row.get("block_time_utc"),
            "n_legs": len(legs), "paid_currency": paid_currency, "paid_amount_human": paid_human,
            "received_currency": recv_currency, "received_symbol": matched_row.get("asset_symbol"),
            "received_amount_human": recv_human, "entry_price_usd_per_token": entry_price_usd,
            "price_note": price_note,
        })

        if (i + 1) % 200 == 0:
            print(f"[swap_purchases] прогресс: {i + 1}/{len(tx_items)} tx обработано, "
                  f"{len(confirmed_purchases)} подтверждённых покупок пока")

    out["n_receipts_fetched"] = n_receipts_fetched
    out["n_receipts_no_swap_log"] = n_receipts_no_swap_log
    out["n_rpc_errors"] = n_rpc_errors
    out["n_swap_logs_total"] = n_swap_logs_total
    out["n_unique_pools_resolved"] = sum(1 for v in pool_key_cache.values() if v is not None)
    out["n_pools_unresolved"] = sum(1 for v in pool_key_cache.values() if v is None)
    out["budget_exhausted"] = budget_exhausted
    out["n_confirmed_purchases_total"] = len(confirmed_purchases)

    per_wallet_purchases: dict[str, int] = defaultdict(int)
    for p in confirmed_purchases:
        per_wallet_purchases[p["wallet_name"]] += 1
    out["n_confirmed_purchases_by_wallet"] = dict(per_wallet_purchases)

    # Первые входы = самая ранняя подтверждённая покупка (wallet, token).
    by_wallet_token: dict[tuple, list] = defaultdict(list)
    for p in confirmed_purchases:
        by_wallet_token[(p["wallet_name"], p["received_currency"])].append(p)
    first_entries = []
    for (wallet, tok), plist in by_wallet_token.items():
        plist.sort(key=lambda x: x["block"])
        first_entries.append(plist[0])
    first_entries.sort(key=lambda x: x["block"])
    per_wallet_first_entries: dict[str, int] = defaultdict(int)
    for fe in first_entries:
        per_wallet_first_entries[fe["wallet_name"]] += 1
    out["n_first_entries_total"] = len(first_entries)
    out["n_first_entries_by_wallet"] = dict(per_wallet_first_entries)
    out["first_entries"] = first_entries
    out["all_confirmed_purchases"] = confirmed_purchases

    n_priced_entries = sum(1 for fe in first_entries if fe["entry_price_usd_per_token"] is not None)
    out["n_first_entries_with_usd_entry_price"] = n_priced_entries

    print(f"\n[swap_purchases] ИТОГ: {out['n_confirmed_purchases_total']} подтверждённых покупок, "
          f"{out['n_first_entries_total']} первых входов, {n_priced_entries} из них с ценой в USD")
    print(f"[swap_purchases] по кошелькам (подтверждённые покупки): {dict(per_wallet_purchases)}")
    print(f"[swap_purchases] по кошелькам (первые входы): {dict(per_wallet_first_entries)}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[swap_purchases] Результат: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
