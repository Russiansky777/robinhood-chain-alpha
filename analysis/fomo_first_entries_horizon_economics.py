#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo, приоритет 3: экономика первых входов --
цена входа (уже есть, из net-flow баланса, `fomo_9wallets_balance_based_
purchases_result.json`) против цены через 10 мин / 1 час / 6 часов / сутки
через slot0() на историческом блоке.

ЧЕСТНАЯ ОГОВОРКА про Initialize: владелец просил не тратить на резолв
пулов через Initialize (в прошлом прогоне это съело весь бюджет на 864
ПОСТОРОННИХ пула, не нужных для нового метода). Но 56 купленных здесь
токенов -- НИ ОДИН не найден в существующем V3-кэше `data/task5_bot_
pool_registry_cache.json` (офлайн-проверка ДО этого скрипта) -- значит
это, вероятно, NATIVE/token V4-пулы (V3 не поддерживает нативную валюту
напрямую, только V4). Для ЛЮБОЙ цены НА БУДУЩЕМ блоке физически нужно
знать currency0/currency1 конкретного пула -- без этого slot0() нечего
интерпретировать. Резолв здесь МИНИМАЛЬНЫЙ и ЦЕЛЕВОЙ: только для ~56-70
пулов, реально использованных в этих 70 первых входах (не 864 посторонних)
-- необходимое, кратно меньшее исключение из общего правила, для ответа
именно на явно запрошенный вопрос."""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path, topic0  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402

IN_PATH = Path("data/fomo_9wallets_balance_based_purchases_result.json")
OUT_PATH = Path("data/fomo_first_entries_horizon_economics_result.json")

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6
SWAP_TOPIC0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
NATIVE = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
WETH_USDG_POOL_V3 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
USDG_DECIMALS = 6
WETH_DECIMALS = 18
SLOT0_SELECTOR = "0x3850c7bd"
DECIMALS_SELECTOR = "0x313ce567"
BLOCK_TIME_S = 0.506

HORIZONS = {"10min": 600, "1h": 3600, "6h": 21600, "24h": 86400}

RPC = rpc_call_trading_path
TIME_BUDGET_S = 800.0


def keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def state_slot_int(pool_id_hex: str) -> int:
    pool_id_bytes = bytes.fromhex(pool_id_hex[2:])
    return int.from_bytes(keccak256(pool_id_bytes + POOLS_SLOT.to_bytes(32, "big")), "big")


def extsload_calldata(slot_int: int) -> str:
    selector = keccak256(b"extsload(bytes32)")[:4].hex()
    return "0x" + selector + slot_int.to_bytes(32, "big").hex()


def decode_slot0_v4(raw_hex: str | None) -> int | None:
    if not raw_hex or raw_hex == "0x":
        return None
    data = bytes.fromhex(raw_hex[2:])
    if len(data) < 32:
        return None
    word0 = int.from_bytes(data[0:32], "big")
    return word0 & ((1 << 160) - 1)


def get_decimals(token: str, cache: dict) -> int:
    token = token.lower()
    if token in cache:
        return cache[token]
    if token == USDG:
        cache[token] = USDG_DECIMALS
        return cache[token]
    if token in (WETH, NATIVE):
        cache[token] = WETH_DECIMALS
        return cache[token]
    try:
        raw = RPC("eth_call", [{"to": token, "data": DECIMALS_SELECTOR}, "latest"])
        dec = int(raw, 16) if raw and raw != "0x" else 18
    except Exception:  # noqa: BLE001
        dec = 18
    cache[token] = dec
    return dec


def price_token1_per_token0(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    raw_price = sqrt_p * sqrt_p
    return raw_price * (10 ** (dec0 - dec1))


def native_usd_price_at_block(block_num: int, cache: dict) -> float | None:
    if block_num in cache:
        return cache[block_num]
    try:
        raw = RPC("eth_call", [{"to": WETH_USDG_POOL_V3, "data": SLOT0_SELECTOR}, hex(block_num)])
        sqrt_price_x96 = int(raw[2:66], 16) if raw and raw != "0x" else None
    except Exception:  # noqa: BLE001
        sqrt_price_x96 = None
    if sqrt_price_x96 is None:
        cache[block_num] = None
        return None
    price = price_token1_per_token0(sqrt_price_x96, WETH_DECIMALS, USDG_DECIMALS)
    cache[block_num] = price
    return price


def find_pool_for_token(tx_hash: str, purchased_token: str, pool_cache: dict) -> dict | None:
    """Из ВСЕХ Swap-логов receipt данной tx находит пул, чья пара валют
    включает purchased_token -- резолвит через Initialize (кэшируется по
    pool_id, не по tx, так что повторные токены в разных tx не платят
    дважды)."""
    try:
        receipt = RPC("eth_getTransactionReceipt", [tx_hash])
    except Exception:  # noqa: BLE001
        return None
    if not receipt:
        return None
    swap_logs = [lg for lg in receipt.get("logs", [])
                 if (lg.get("address") or "").lower() == POOL_MANAGER.lower()
                 and lg.get("topics") and lg["topics"][0].lower() == SWAP_TOPIC0.lower()]
    block_num = int(receipt["blockNumber"], 16)
    for lg in swap_logs:
        pool_id_hex = lg["topics"][1]
        if pool_id_hex not in pool_cache:
            pool_cache[pool_id_hex] = fetch_initialize_event(pool_id_hex, block_num)
        info = pool_cache[pool_id_hex]
        if info is None:
            continue
        if info["currency0"].lower() == purchased_token.lower() or info["currency1"].lower() == purchased_token.lower():
            return info
    return None


def price_at_block(pool_info: dict, purchased_token: str, block_num: int, dec_cache: dict, native_price_cache: dict) -> float | None:
    """USD-цена purchased_token на данном блоке через тот же пул, что и
    вход -- slot0-эквивалент для V4 (extsload на PoolManager)."""
    slot_int = state_slot_int(pool_info["pool_id"])
    try:
        raw = RPC("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata(slot_int)}, hex(block_num)])
    except Exception:  # noqa: BLE001
        return None
    sqrt_price_x96 = decode_slot0_v4(raw)
    if sqrt_price_x96 is None or sqrt_price_x96 == 0:
        return None
    c0, c1 = pool_info["currency0"].lower(), pool_info["currency1"].lower()
    tok_dec = get_decimals(purchased_token, dec_cache)
    other = c1 if c0 == purchased_token.lower() else c0
    other_dec = get_decimals(other, dec_cache)
    if c0 == purchased_token.lower():
        price_other_per_token = price_token1_per_token0(sqrt_price_x96, tok_dec, other_dec)
    else:
        # token -- currency1, значит sqrtPrice даёт token1/token0 = token/other -- инвертируем
        price_token_per_other = price_token1_per_token0(sqrt_price_x96, other_dec, tok_dec)
        price_other_per_token = 1 / price_token_per_other if price_token_per_other else None
    if price_other_per_token is None:
        return None
    if other.lower() == USDG:
        return price_other_per_token
    if other.lower() in (WETH, NATIVE):
        native_usd = native_usd_price_at_block(block_num, native_price_cache)
        return price_other_per_token * native_usd if native_usd is not None else None
    return None  # платная валюта пула -- не USDG/WETH/NATIVE, честно не оцениваем


def run() -> int:
    if not IN_PATH.exists():
        print(f"[horizon] СТОП: {IN_PATH} не найден")
        return 1
    data_in = json.loads(IN_PATH.read_text())
    first_entries = data_in["first_entries"]
    print(f"[horizon] {len(first_entries)} первых входов на входе")

    try:
        latest = int(RPC("eth_blockNumber", []), 16)
    except Exception as exc:  # noqa: BLE001
        print(f"[horizon] СТОП: не удалось получить latest block: {exc}")
        return 1
    print(f"[horizon] latest block = {latest}")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "n_first_entries_input": len(first_entries), "latest_block_at_run": latest}

    start_ts = time.time()
    pool_cache: dict = {}
    dec_cache: dict = {}
    native_price_cache: dict = {}
    budget_exhausted = False
    n_pool_resolved = 0
    n_pool_unresolved = 0

    results = []
    for i, fe in enumerate(first_entries):
        if time.time() - start_ts > TIME_BUDGET_S:
            budget_exhausted = True
            out["budget_exhausted_at_entry_index"] = i
            break
        pool_info = find_pool_for_token(fe["tx_hash"], fe["purchased_token"], pool_cache)
        entry_record: dict = {**fe, "pool_found": pool_info is not None}
        if pool_info is None:
            n_pool_unresolved += 1
            entry_record["horizons"] = {}
            results.append(entry_record)
            continue
        n_pool_resolved += 1
        entry_record["pool_id"] = pool_info["pool_id"]

        horizons_out = {}
        for label, seconds in HORIZONS.items():
            blocks_ahead = int(seconds / BLOCK_TIME_S)
            target_block = fe["block"] + blocks_ahead
            if target_block > latest:
                horizons_out[label] = {"available": False, "reason": "горизонт ещё не наступил (target_block > latest)"}
                continue
            price_usd = price_at_block(pool_info, fe["purchased_token"], target_block, dec_cache, native_price_cache)
            if price_usd is None or fe["entry_price_usd_per_token"] is None or fe["entry_price_usd_per_token"] == 0:
                horizons_out[label] = {"available": False, "reason": "цена не получена"}
                continue
            pct_change = (price_usd - fe["entry_price_usd_per_token"]) / fe["entry_price_usd_per_token"]
            horizons_out[label] = {"available": True, "price_usd": price_usd, "pct_change": pct_change,
                                    "target_block": target_block}
        entry_record["horizons"] = horizons_out
        results.append(entry_record)

        if (i + 1) % 20 == 0:
            print(f"[horizon] прогресс: {i + 1}/{len(first_entries)}, {n_pool_resolved} пулов резолвлено")

    out["budget_exhausted"] = budget_exhausted
    out["n_pool_resolved"] = n_pool_resolved
    out["n_pool_unresolved"] = n_pool_unresolved
    out["entries"] = results

    summary = {}
    for label in HORIZONS:
        pct_changes = [r["horizons"].get(label, {}).get("pct_change") for r in results
                       if r["horizons"].get(label, {}).get("available")]
        pct_changes = [p for p in pct_changes if p is not None]
        if pct_changes:
            summary[label] = {
                "n_samples": len(pct_changes),
                "median_pct_change": statistics.median(pct_changes),
                "frac_profitable": sum(1 for p in pct_changes if p > 0) / len(pct_changes),
                "max_pct_change": max(pct_changes),
                "min_pct_change": min(pct_changes),
            }
        else:
            summary[label] = {"n_samples": 0}
    out["horizon_summary"] = summary

    print(f"\n[horizon] ИТОГ: {n_pool_resolved} пулов резолвлено, {n_pool_unresolved} не резолвлено")
    for label, s in summary.items():
        print(f"[horizon] {label}: {s}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[horizon] Результат: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
