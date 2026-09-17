#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo -- короткие горизонты и есть ли волна.

Три вопроса, все по цепи (slot0 на историческом блоке + Swap-события
пула), без Dune (установлено четырьмя нулевыми попытками -- сделки
атрибутируются на исполнителя, Dune их не видит структурно):
  0. РЕАЛЬНОЕ время блока Robinhood Chain -- измерено, не по памяти
     (владелец явно потребовал).
  1. Есть ли волна: активность ДРУГИХ адресов в том же пуле после входа
     лидера vs до.
  2. Форма ценовой кривой после входа лидера: +5с..+10мин.
  3. Реалистичный вход (A: +2 блока техническая граница; B: +5с
     реалистичный) x горизонт выхода (30с/1мин/3мин/5мин + пик из п.2),
     с реальными издержками (комиссия пула + газ).

Работаем на 42 из 70 первых входов, для которых пул УЖЕ резолвлен
(`data/fomo_first_entries_horizon_economics_result.json`, `pool_found:
true`) -- поиск/резолв пула для остальных 28 НЕ повторяется (владелец:
"42 из 70 уже резолвлены, повторно не резолвить"). currency0/currency1
для этих 42 НЕ были персистированы в прошлый раз (только pool_id) --
здесь ОДИН дополнительный целевой вызов `fetch_initialize_event` НА
УЖЕ ИЗВЕСТНЫЙ pool_id (не поиск, не новый резолв -- то же самое
targeted-по-pool_id обращение, что уже было сделано для этих 42 в
прошлом раунде) -- результат персистируется в отдельный файл, чтобы
третий раз это не понадобилось."""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path, topic0, _chunked_get_logs  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402

IN_PATH = Path("data/fomo_first_entries_horizon_economics_result.json")
POOL_KEYS_CACHE_PATH = Path("data/fomo_resolved_pool_keys_cache.json")
OUT_PATH = Path("data/fomo_short_horizon_result.json")

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

RPC = rpc_call_trading_path
TIME_BUDGET_S = 900.0

CURVE_OFFSETS_S = [5, 15, 30, 60, 120, 180, 300, 420, 600]
EXIT_HORIZONS_S = {"30s": 30, "1min": 60, "3min": 180, "5min": 300}
WAVE_WINDOWS_S = {"1min": 60, "3min": 180, "10min": 600}
BEFORE_WINDOW_S = 600


def keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def state_slot_int(pool_id_hex: str) -> int:
    return int.from_bytes(keccak256(bytes.fromhex(pool_id_hex[2:]) + POOLS_SLOT.to_bytes(32, "big")), "big")


def extsload_calldata(slot_int: int) -> str:
    selector = keccak256(b"extsload(bytes32)")[:4].hex()
    return "0x" + selector + slot_int.to_bytes(32, "big").hex()


def decode_slot0_v4(raw_hex: str | None) -> int | None:
    if not raw_hex or raw_hex == "0x":
        return None
    data = bytes.fromhex(raw_hex[2:])
    if len(data) < 32:
        return None
    return int.from_bytes(data[0:32], "big") & ((1 << 160) - 1)


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
    return (sqrt_p * sqrt_p) * (10 ** (dec0 - dec1))


def native_usd_price_at_block(block_num: int, cache: dict) -> float | None:
    if block_num in cache:
        return cache[block_num]
    try:
        raw = RPC("eth_call", [{"to": WETH_USDG_POOL_V3, "data": SLOT0_SELECTOR}, hex(block_num)])
        sp = int(raw[2:66], 16) if raw and raw != "0x" else None
    except Exception:  # noqa: BLE001
        sp = None
    price = price_token1_per_token0(sp, WETH_DECIMALS, USDG_DECIMALS) if sp else None
    cache[block_num] = price
    return price


def price_at_block(pool_info: dict, purchased_token: str, block_num: int, dec_cache: dict, native_cache: dict) -> float | None:
    slot_int = state_slot_int(pool_info["pool_id"])
    try:
        raw = RPC("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata(slot_int)}, hex(block_num)])
    except Exception:  # noqa: BLE001
        return None
    sqrt_price_x96 = decode_slot0_v4(raw)
    if not sqrt_price_x96:
        return None
    c0, c1 = pool_info["currency0"].lower(), pool_info["currency1"].lower()
    tok_dec = get_decimals(purchased_token, dec_cache)
    other = c1 if c0 == purchased_token.lower() else c0
    other_dec = get_decimals(other, dec_cache)
    if c0 == purchased_token.lower():
        price_other_per_token = price_token1_per_token0(sqrt_price_x96, tok_dec, other_dec)
    else:
        price_token_per_other = price_token1_per_token0(sqrt_price_x96, other_dec, tok_dec)
        price_other_per_token = 1 / price_token_per_other if price_token_per_other else None
    if price_other_per_token is None:
        return None
    if other == USDG:
        return price_other_per_token
    if other in (WETH, NATIVE):
        native_usd = native_usd_price_at_block(block_num, native_cache)
        return price_other_per_token * native_usd if native_usd is not None else None
    return None


def measure_real_block_time() -> dict:
    """РЕАЛЬНОЕ время блока -- измерено по факту, не из памяти/старых
    констант (владелец явно потребовал). Три пары точек: близко к
    деплою общего контракта (блок 5018, известен с прошлого раунда),
    середина периода первых входов, и latest -- чтобы честно увидеть,
    менялось ли среднее время блока со временем чейна, а не считать его
    константой без проверки."""
    latest = int(RPC("eth_blockNumber", []), 16)
    sample_blocks = sorted({5018, 5441863, 27145959, latest - 500_000, latest})
    sample_blocks = [b for b in sample_blocks if 0 <= b <= latest]
    timestamps = {}
    for b in sample_blocks:
        try:
            blk = RPC("eth_getBlockByNumber", [hex(b), False])
            timestamps[b] = int(blk["timestamp"], 16)
        except Exception as exc:  # noqa: BLE001
            print(f"[block_time] не удалось получить блок {b}: {exc}")
    pairs = []
    sorted_blocks = sorted(timestamps.keys())
    for i in range(1, len(sorted_blocks)):
        b0, b1 = sorted_blocks[i - 1], sorted_blocks[i]
        dt = timestamps[b1] - timestamps[b0]
        db = b1 - b0
        if db > 0:
            pairs.append({"from_block": b0, "to_block": b1, "delta_blocks": db, "delta_seconds": dt,
                          "avg_block_time_s": dt / db})
    overall = None
    if len(sorted_blocks) >= 2:
        b0, b1 = sorted_blocks[0], sorted_blocks[-1]
        overall = (timestamps[b1] - timestamps[b0]) / (b1 - b0)
    return {"sample_timestamps": {str(k): v for k, v in timestamps.items()}, "pairwise": pairs,
            "overall_avg_block_time_s": overall}


def resolve_pool_keys(pool_ids: set[str], entries_by_pool: dict) -> dict:
    cache: dict = {}
    if POOL_KEYS_CACHE_PATH.exists():
        cache = json.loads(POOL_KEYS_CACHE_PATH.read_text())
    missing = [pid for pid in pool_ids if pid not in cache]
    print(f"[pool_keys] {len(pool_ids)} пулов нужно, {len(cache)} уже в постоянном кэше, {len(missing)} новых вызовов")
    for pid in missing:
        block_hint = entries_by_pool[pid]["block"]
        info = fetch_initialize_event(pid, block_hint)
        cache[pid] = info
    POOL_KEYS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    POOL_KEYS_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    return cache


def get_swap_logs_in_range(pool_id: str, from_block: int, to_block: int) -> list[dict]:
    if from_block > to_block:
        return []
    logs = list(_chunked_get_logs(from_block, to_block, topics=[SWAP_TOPIC0, pool_id],
                                   address=POOL_MANAGER, chunk_size=max(1, to_block - from_block + 1)))
    return logs


def run() -> int:
    if not IN_PATH.exists():
        print(f"[short_horizon] СТОП: {IN_PATH} не найден")
        return 1
    prior = json.loads(IN_PATH.read_text())
    resolved_entries = [e for e in prior["entries"] if e.get("pool_found")]
    print(f"[short_horizon] {len(resolved_entries)} первых входов с уже резолвленным пулом (из {len(prior['entries'])})")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "n_entries_input": len(resolved_entries),
                 "sample_size_caveat": f"выборка {len(resolved_entries)} наблюдений -- МАЛАЯ, выводы ниже "
                                        "не считать твёрдыми, только направление."}

    # --- Шаг 0: реальное время блока ---
    print("[short_horizon] Шаг 0: измерение РЕАЛЬНОГО времени блока Robinhood Chain")
    block_time_info = measure_real_block_time()
    out["real_block_time_measurement"] = block_time_info
    real_block_time_s = block_time_info["overall_avg_block_time_s"]
    if real_block_time_s is None or real_block_time_s <= 0:
        print("[short_horizon] СТОП: не удалось измерить реальное время блока")
        return 1
    print(f"[short_horizon] РЕАЛЬНОЕ среднее время блока (по всему измеренному диапазону): {real_block_time_s:.6f}с "
          f"(попарно: {block_time_info['pairwise']})")
    blocks_per_5s = max(1, round(5 / real_block_time_s))
    out["blocks_per_5_seconds_measured"] = blocks_per_5s
    print(f"[short_horizon] 5 секунд на этой цепи ~= {blocks_per_5s} блоков (при {real_block_time_s:.6f}с/блок)")

    def blocks_for_seconds(s: float) -> int:
        return max(1, round(s / real_block_time_s))

    # --- Резолв currency0/1 для уже известных 42 pool_id (не новый поиск) ---
    entries_by_pool = {e["pool_id"]: e for e in resolved_entries}
    pool_keys = resolve_pool_keys(set(entries_by_pool.keys()), entries_by_pool)
    n_pool_keys_ok = sum(1 for v in pool_keys.values() if v is not None)
    out["n_pool_keys_resolved"] = n_pool_keys_ok
    out["n_pool_keys_failed"] = len(pool_keys) - n_pool_keys_ok

    start_ts = time.time()
    dec_cache: dict = {}
    native_cache: dict = {}
    budget_exhausted = False

    wave_results = []
    curve_results = []
    entry_exit_results = []

    for i, e in enumerate(resolved_entries):
        if time.time() - start_ts > TIME_BUDGET_S:
            budget_exhausted = True
            out["budget_exhausted_at_index"] = i
            break
        pool_info = pool_keys.get(e["pool_id"])
        if pool_info is None:
            continue
        entry_block = e["block"]
        purchased_token = e["purchased_token"]
        entry_price = e["entry_price_usd_per_token"]
        fee_pips = pool_info.get("fee", 0)
        # V4: если установлен DYNAMIC_FEE_FLAG (бит 0x800000), поле fee из Initialize
        # НЕ является реальной комиссией -- комиссию определяет хук на каждый своп,
        # и Initialize её не публикует. Считать такую комиссию по формуле fee/1e6 --
        # значит выдумать данные (даёт абсурдные ~839% и разворачивает знак после
        # возведения в квадрат). Поэтому для таких пулов издержки по комиссии
        # честно помечаются "неизвестны", а не подставляется произвольное число.
        DYNAMIC_FEE_FLAG = 0x800000
        fee_is_dynamic = bool(fee_pips) and bool(fee_pips & DYNAMIC_FEE_FLAG)
        fee_frac = (fee_pips / 1e6) if (fee_pips and not fee_is_dynamic) else 0.0

        # --- Задача 1: волна ---
        before_blocks = blocks_for_seconds(BEFORE_WINDOW_S)
        after_blocks_10min = blocks_for_seconds(WAVE_WINDOWS_S["10min"])
        try:
            logs_window = get_swap_logs_in_range(e["pool_id"], entry_block - before_blocks, entry_block + after_blocks_10min)
        except Exception as exc:  # noqa: BLE001
            logs_window = []
            print(f"[short_horizon] ошибка получения Swap-логов для {e['pool_id']}: {exc}")
        n_before = sum(1 for lg in logs_window if int(lg["blockNumber"], 16) < entry_block)
        after_logs = [lg for lg in logs_window if int(lg["blockNumber"], 16) > entry_block
                      and lg.get("transactionHash", "").lower() != e["tx_hash"].lower()]
        n_after = {}
        for label, secs in WAVE_WINDOWS_S.items():
            edge = entry_block + blocks_for_seconds(secs)
            n_after[label] = sum(1 for lg in after_logs if int(lg["blockNumber"], 16) <= edge)
        first_other_block = min((int(lg["blockNumber"], 16) for lg in after_logs), default=None)
        first_other_delay_s = (first_other_block - entry_block) * real_block_time_s if first_other_block else None
        ratio_10min = (n_after["10min"] / n_before) if n_before > 0 else (float("inf") if n_after["10min"] > 0 else None)
        wave_results.append({
            "wallet_name": e["wallet_name"], "purchased_symbol": e["purchased_symbol"], "tx_hash": e["tx_hash"],
            "n_trades_before_10min": n_before, "n_trades_after_1min": n_after["1min"],
            "n_trades_after_3min": n_after["3min"], "n_trades_after_10min": n_after["10min"],
            "ratio_after10min_to_before10min": ratio_10min,
            "first_other_trade_delay_seconds": first_other_delay_s,
        })

        # --- Задача 2: форма кривой ---
        curve_points = {}
        for secs in CURVE_OFFSETS_S:
            target_block = entry_block + blocks_for_seconds(secs)
            price = price_at_block(pool_info, purchased_token, target_block, dec_cache, native_cache)
            pct = ((price - entry_price) / entry_price) if (price is not None and entry_price) else None
            curve_points[f"+{secs}s"] = {"price_usd": price, "pct_change": pct}
        curve_results.append({"wallet_name": e["wallet_name"], "purchased_symbol": e["purchased_symbol"],
                               "tx_hash": e["tx_hash"], "curve": curve_points})

        # --- Задача 3: реалистичный вход/выход ---
        try:
            entry_receipt = RPC("eth_getTransactionReceipt", [e["tx_hash"]])
            gas_used = int(entry_receipt.get("gasUsed", "0x0"), 16) if entry_receipt else None
            entry_block_data = RPC("eth_getBlockByNumber", [hex(entry_block), False])
            base_fee_wei = int(entry_block_data.get("baseFeePerGas", "0x0"), 16) if entry_block_data and entry_block_data.get("baseFeePerGas") else None
        except Exception:  # noqa: BLE001
            gas_used, base_fee_wei = None, None
        n_recipients = e.get("n_distinct_non_payment_recipients_in_tx", 1) or 1
        gas_cost_usd_per_trader = None
        if gas_used is not None and base_fee_wei is not None:
            native_usd_entry = native_usd_price_at_block(entry_block, native_cache)
            if native_usd_entry is not None:
                full_tx_gas_cost_native = gas_used * base_fee_wei / 1e18
                gas_cost_usd_per_trader = (full_tx_gas_cost_native * native_usd_entry) / n_recipients

        entry_points = {"A_plus2blocks": entry_block + 2, "B_plus5s": entry_block + blocks_per_5s}
        price_by_entry = {}
        for label, blk in entry_points.items():
            price_by_entry[label] = price_at_block(pool_info, purchased_token, blk, dec_cache, native_cache)

        exit_table = {}
        peak_offset_s = None
        peak_pct = None
        for secs, pt in zip(CURVE_OFFSETS_S, curve_points.values()):
            if pt["pct_change"] is not None and (peak_pct is None or pt["pct_change"] > peak_pct):
                peak_pct = pt["pct_change"]
                peak_offset_s = secs
        exit_offsets = dict(EXIT_HORIZONS_S)
        if peak_offset_s is not None and peak_offset_s not in exit_offsets.values():
            exit_offsets[f"peak_{peak_offset_s}s"] = peak_offset_s

        for entry_label, entry_blk in entry_points.items():
            price_in = price_by_entry[entry_label]
            for exit_label, secs in exit_offsets.items():
                exit_blk = entry_blk + blocks_for_seconds(secs)
                price_out = price_at_block(pool_info, purchased_token, exit_blk, dec_cache, native_cache)
                if price_in is None or price_out is None or price_in == 0:
                    exit_table[f"{entry_label}__{exit_label}"] = {"available": False}
                    continue
                if fee_is_dynamic:
                    exit_table[f"{entry_label}__{exit_label}"] = {
                        "available": False, "reason": "dynamic_fee_pool_real_fee_unknown",
                    }
                    continue
                gross_pct = (price_out - price_in) / price_in
                after_fee_multiplier = (1 - fee_frac) ** 2
                net_price_ratio = (price_out / price_in) * after_fee_multiplier
                net_pct_before_gas = net_price_ratio - 1
                net_pct_after_gas = None
                if gas_cost_usd_per_trader is not None and e.get("paid_amount_human") and native_usd_entry:
                    position_usd = e["paid_amount_human"] * native_usd_entry
                    if position_usd > 0:
                        net_value_after_gas = position_usd * net_price_ratio - gas_cost_usd_per_trader
                        net_pct_after_gas = (net_value_after_gas - position_usd) / position_usd
                exit_table[f"{entry_label}__{exit_label}"] = {
                    "available": True, "gross_pct_change": gross_pct,
                    "net_pct_change_after_pool_fees": net_pct_before_gas,
                    "net_pct_change_after_fees_and_gas": net_pct_after_gas,
                }
        entry_exit_results.append({
            "wallet_name": e["wallet_name"], "purchased_symbol": e["purchased_symbol"], "tx_hash": e["tx_hash"],
            "pool_fee_pips": fee_pips, "fee_is_dynamic": fee_is_dynamic,
            "gas_cost_usd_per_trader_estimate": gas_cost_usd_per_trader,
            "n_distinct_recipients_in_entry_tx": n_recipients, "peak_offset_s": peak_offset_s,
            "exit_table": exit_table,
        })

        if (i + 1) % 10 == 0:
            print(f"[short_horizon] прогресс: {i + 1}/{len(resolved_entries)}")

    out["budget_exhausted"] = budget_exhausted
    out["wave_results"] = wave_results
    out["curve_results"] = curve_results
    out["entry_exit_results"] = entry_exit_results

    # --- Агрегаты: волна ---
    ratios = [w["ratio_after10min_to_before10min"] for w in wave_results
              if w["ratio_after10min_to_before10min"] is not None and w["ratio_after10min_to_before10min"] != float("inf")]
    n_wave_increase = sum(1 for w in wave_results if (w["n_trades_after_10min"] > w["n_trades_before_10min"]))
    delays = [w["first_other_trade_delay_seconds"] for w in wave_results if w["first_other_trade_delay_seconds"] is not None]
    out["wave_summary"] = {
        "n_entries_analyzed": len(wave_results),
        "n_with_activity_increase_after_vs_before_10min": n_wave_increase,
        "median_ratio_after10min_to_before10min_finite_only": statistics.median(ratios) if ratios else None,
        "n_ratio_infinite_zero_before_nonzero_after": sum(1 for w in wave_results if w["ratio_after10min_to_before10min"] == float("inf")),
        "n_zero_activity_before_and_after": sum(1 for w in wave_results if w["n_trades_before_10min"] == 0 and w["n_trades_after_10min"] == 0),
        "median_first_other_trade_delay_seconds": statistics.median(delays) if delays else None,
        "n_with_no_other_trade_within_10min": sum(1 for w in wave_results if w["first_other_trade_delay_seconds"] is None),
    }

    # --- Агрегаты: форма кривой (медианная траектория) ---
    curve_summary = {}
    for secs in CURVE_OFFSETS_S:
        key = f"+{secs}s"
        vals = [c["curve"][key]["pct_change"] for c in curve_results if c["curve"][key]["pct_change"] is not None]
        curve_summary[key] = {"n": len(vals), "median_pct_change": statistics.median(vals) if vals else None}
    out["median_curve_summary"] = curve_summary

    # --- Агрегаты: вход x выход ---
    all_keys = set()
    for r in entry_exit_results:
        all_keys.update(r["exit_table"].keys())
    entry_exit_summary = {}
    for key in sorted(all_keys):
        vals = [r["exit_table"][key]["net_pct_change_after_fees_and_gas"] for r in entry_exit_results
                if key in r["exit_table"] and r["exit_table"][key].get("available")
                and r["exit_table"][key].get("net_pct_change_after_fees_and_gas") is not None]
        vals_before_gas = [r["exit_table"][key]["net_pct_change_after_pool_fees"] for r in entry_exit_results
                            if key in r["exit_table"] and r["exit_table"][key].get("available")]
        if vals:
            entry_exit_summary[key] = {
                "n_samples": len(vals),
                "median_pct_after_all_costs": statistics.median(vals),
                "frac_profitable_after_all_costs": sum(1 for v in vals if v > 0) / len(vals),
                "max_pct": max(vals), "min_pct": min(vals),
                "median_pct_after_pool_fee_only": statistics.median(vals_before_gas) if vals_before_gas else None,
            }
    out["entry_exit_summary"] = entry_exit_summary

    # --- Разница A vs B на одинаковых горизонтах ---
    ab_diff = {}
    for exit_label in EXIT_HORIZONS_S:
        key_a, key_b = f"A_plus2blocks__{exit_label}", f"B_plus5s__{exit_label}"
        if key_a in entry_exit_summary and key_b in entry_exit_summary:
            ab_diff[exit_label] = {
                "median_A": entry_exit_summary[key_a]["median_pct_after_all_costs"],
                "median_B": entry_exit_summary[key_b]["median_pct_after_all_costs"],
                "cost_of_delay_pct_points": entry_exit_summary[key_a]["median_pct_after_all_costs"] - entry_exit_summary[key_b]["median_pct_after_all_costs"],
            }
    out["A_vs_B_delay_cost"] = ab_diff

    # --- Предрегистрация ---
    verdict = "МЕЖДУ -- не решено"
    any_alive = any(s["median_pct_after_all_costs"] > 0.01 and s["frac_profitable_after_all_costs"] >= 0.55
                     for s in entry_exit_summary.values())
    all_negative = all(s["median_pct_after_all_costs"] < 0 for s in entry_exit_summary.values()) if entry_exit_summary else False
    if any_alive:
        verdict = "ЖИВА"
    elif all_negative:
        verdict = "НЕТ"
    out["verdict"] = verdict
    out["verdict_caveat"] = (f"Выборка {len(entry_exit_results)} наблюдений (пул резолвлен) -- МАЛАЯ. "
                              "Вердикт направленный, не твёрдый статистический вывод.")

    print(f"\n[short_horizon] ВОЛНА: {out['wave_summary']}")
    print(f"[short_horizon] КРИВАЯ (медиана): {curve_summary}")
    print(f"[short_horizon] ВХОД x ВЫХОД:")
    for k, v in entry_exit_summary.items():
        print(f"  {k}: {v}")
    print(f"[short_horizon] A vs B: {ab_diff}")
    print(f"[short_horizon] ВЕРДИКТ: {verdict} ({out['verdict_caveat']})")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[short_horizon] Результат: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
