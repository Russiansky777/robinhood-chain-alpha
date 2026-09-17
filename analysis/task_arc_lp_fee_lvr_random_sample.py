#!/usr/bin/env python3
"""Владелец (2026-09-16, докрутка): вердикт "ЖИВА" на прошлой выборке
(12 пулов из топ-30 ПО ОБЪЁМУ за первый час) отклонён -- ошибка
выжившего. Реальный LP кладёт деньги в СВЕЖИЙ пул НАУГАД, вместе с теми,
что умрут в течение часа -- топ-30-по-объёму УЖЕ отфильтровал мёртвые
пулы (они не успели набрать объём), поэтому выживаемость 100% на той
выборке была гарантирована построением, не результатом измерения.

Прямое свидетельство в прошлом файле: AROS -- единственный пул с ценой,
упавшей на 92%, дал fee/LVR=0.041 (1ч) -- глубоко убыточен для LP.
Остальные 11 пулов прошлой выборки -- выжившие, 20-60. Разрыв определяет
не метод (extsload тот же, оставляем), а то, ВЫЖИЛ ЛИ токен -- и именно
это подгонялось отбором по объёму.

ИСПРАВЛЕНИЕ ВЫБОРКИ: 60 пулов СЛУЧАЙНО из полного реестра Initialize
(24567 пулов, data/task_arc_recon_pools_result.json) -- БЕЗ фильтра по
объёму, БЕЗ фильтра по числу свопов. Фиксированный seed (см.
RANDOM_SEED) -- воспроизводимо. fee=0 пулы НЕ исключаются -- реальный LP
мог попасть и в них.

ИСПРАВЛЕНИЕ КОМИССИИ: прошлая версия брала liquidity_at_window_end как
единственную ("первичную") оценку -- у СВЕЖЕГО пула ликвидность растёт с
нуля весь период, значит liquidity_at_end систематически ЗАВЫШАЕТ вклад
ранних (низколиквидных) интервалов внутри окна. Здесь -- НЕСКОЛЬКО
промежуточных точек внутри каждого окна (см. SUBDIVISIONS_PER_WINDOW),
кусочный расчёт по сегментам: LOWER-граница -- ликвидность НА НАЧАЛО
каждого сегмента (недооценивает при растущей ликвидности), UPPER --
ликвидность НА КОНЕЦ сегмента (переоценивает). Вердикт -- по LOWER.
Стоимость в вызовах -- посчитана и напечатана честно (см.
n_extra_calls_for_subdivision).

ВЫБРОСЫ: окна, где цена почти не сдвинулась (|price_change_frac| <
LVR_NEAR_ZERO_THRESHOLD) -- LVR провалился в почти-ноль не потому что
LP выиграл, а потому что знаменатель обвалился (тот же артефакт метрики,
что и в прошлом раунде на 787735/1157) -- помечаются
lvr_near_zero=true и ИСКЛЮЧАЮТСЯ из медианы/pct_gt_1/pct_unprofitable,
но сырые числа остаются в выводе.

Формула extsload (batch 4 слота: slot0|feeGrowthGlobal0X128|
feeGrowthGlobal1X128|liquidity) и вся арифметика fee/LVR -- та же, что в
task_arc_lp_fee_lvr_extsload_historical.py (уже подтверждена архивным
чтением, 71429 блоков назад)."""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6
USDC_ERC20 = "0x3600000000000000000000000000000000000000"
NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"
USDC_DECIMALS = 6
BLOCK_TIME_S = 0.506
WINDOWS_H = [1, 3, 6, 12]
SUBDIVISIONS_PER_WINDOW = 3  # точки на долях 1/3, 2/3, 3/3 окна -- 3 сегмента на окно
LVR_NEAR_ZERO_THRESHOLD = 0.005  # |price_change_frac| < 0.5% -- считаем LVR-знаменатель непоказательным
RANDOM_SEED = 20260917
SAMPLE_SIZE = 60

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.1
_last_call_ts = [0.0]
_rpc_calls = [0]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    wait = MIN_CALL_INTERVAL_S - (time.time() - _last_call_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_call_ts[0] = time.time()
    _rpc_calls[0] += 1
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
        body["_http_status"] = resp.status_code
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}, "_http_status": None}


def extsload_batch4(state_slot_int: int, block_hex: str) -> dict:
    selector = keccak256(b"extsload(bytes32,uint256)")[:4].hex()
    start_slot_hex = state_slot_int.to_bytes(32, "big").hex()
    n_hex = (4).to_bytes(32, "big").hex()
    calldata = "0x" + selector + start_slot_hex + n_hex
    body = rpc("eth_call", [{"to": POOL_MANAGER, "data": calldata}, block_hex])
    if "error" in body:
        return {"error": body["error"], "http_status": body.get("_http_status")}
    raw = body.get("result")
    if not raw or raw == "0x":
        return {"error": {"message": "empty result (0x)"}, "http_status": body.get("_http_status")}
    data = bytes.fromhex(raw[2:])
    if len(data) < 32 * 6:
        return {"error": {"message": f"result too short: {len(data)} bytes"}, "http_status": body.get("_http_status")}
    words = [int.from_bytes(data[64 + i * 32: 64 + (i + 1) * 32], "big") for i in range(4)]
    slot0_raw, fee_growth0, fee_growth1, liq_raw = words
    tick_raw = (slot0_raw >> 160) & ((1 << 24) - 1)
    tick = tick_raw - (1 << 24) if tick_raw >= (1 << 23) else tick_raw
    return {
        "sqrt_price_x96": slot0_raw & ((1 << 160) - 1), "tick": tick,
        "protocol_fee": (slot0_raw >> 184) & ((1 << 24) - 1),
        "lp_fee": (slot0_raw >> 208) & ((1 << 24) - 1),
        "fee_growth_global0_x128": fee_growth0, "fee_growth_global1_x128": fee_growth1,
        "liquidity": liq_raw & ((1 << 128) - 1),
    }


def get_token_decimals(token: str, cache: dict) -> int | None:
    token = token.lower()
    if token in cache:
        return cache[token]
    if token in (USDC_ERC20.lower(), NATIVE_SENTINEL):
        cache[token] = USDC_DECIMALS if token == USDC_ERC20.lower() else 18
        return cache[token]
    body = rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
    res = body.get("result")
    dec = int(res, 16) if res and res != "0x" else 18
    cache[token] = dec
    return dec


def usdc_equivalent_of_fee(d_fg0: int, d_fg1: int, liq_basis: int, usdc_side: str, price_raw: float | None) -> float:
    """price_raw = (sqrtPriceX96/2**96)**2 = raw_token1/raw_token0 в конце окна --
    та же raw-only конвертация, что в task_arc_lp_fee_lvr_extsload_historical.py."""
    fee_c0_raw = (d_fg0 * liq_basis) >> 128
    fee_c1_raw = (d_fg1 * liq_basis) >> 128
    if usdc_side == "currency0":
        usdc_raw = fee_c0_raw
        other_as_usdc_raw = (fee_c1_raw / price_raw) if price_raw else 0.0
    else:
        usdc_raw = fee_c1_raw
        other_as_usdc_raw = (fee_c0_raw * price_raw) if price_raw else 0.0
    return (usdc_raw + other_as_usdc_raw) / (10 ** USDC_DECIMALS)


def analyze_pool(pool_meta: dict, dec_cache: dict, latest: int) -> dict:
    pool_id = pool_meta["pool_id"]
    c0, c1 = pool_meta["currency0"].lower(), pool_meta["currency1"].lower()
    usdc_side = "currency0" if c0 in (USDC_ERC20.lower(), NATIVE_SENTINEL) else (
        "currency1" if c1 in (USDC_ERC20.lower(), NATIVE_SENTINEL) else None)
    if usdc_side is None:
        return {"pool_id": pool_id, "skipped_reason": "ни одна сторона не USDC/native -- честно исключён, не выдумываем цену"}
    other_token = c1 if usdc_side == "currency0" else c0
    other_dec = get_token_decimals(other_token, dec_cache)

    init_block = pool_meta["block_number"]
    pool_id_bytes = bytes.fromhex(pool_id[2:])
    state_slot_int = int.from_bytes(keccak256(pool_id_bytes + POOLS_SLOT.to_bytes(32, "big")), "big")

    state_init = extsload_batch4(state_slot_int, hex(init_block))
    if "error" in state_init:
        return {"pool_id": pool_id, "error_at_init_block": state_init}
    if state_init.get("sqrt_price_x96", 0) == 0:
        return {"pool_id": pool_id, "skipped_reason": "sqrtPriceX96=0 на init_block -- пул не реально инициализирован (возможен ложный Initialize-лог/decode-артефакт реестра)"}

    fee_initialize_pips_live = state_init["lp_fee"]  # реальный fee из Slot0, не из статичного реестра (у него его и нет)
    is_dynamic_fee_flag = fee_initialize_pips_live == 8388608

    # === Точки внутри каждого окна: доли 1/3, 2/3, 3/3 -- дедуп по итоговому блоку ===
    block_to_state: dict[int, dict] = {init_block: state_init}
    windows_points_blocks: dict[int, list[int]] = {}
    for h in WINDOWS_H:
        total_blocks = int(h * 3600 / BLOCK_TIME_S)
        pts = []
        for frac_i in range(1, SUBDIVISIONS_PER_WINDOW + 1):
            b = init_block + int(total_blocks * frac_i / SUBDIVISIONS_PER_WINDOW)
            pts.append(b)
        windows_points_blocks[h] = pts

    all_needed_blocks = sorted({b for pts in windows_points_blocks.values() for b in pts if b <= latest})
    for b in all_needed_blocks:
        if b in block_to_state:
            continue
        st = extsload_batch4(state_slot_int, hex(b))
        block_to_state[b] = st  # может содержать "error" -- сегмент, зависящий от него, будет честно помечен

    windows_out = {}
    for h in WINDOWS_H:
        pts = windows_points_blocks[h]
        window_end_block = init_block + int(h * 3600 / BLOCK_TIME_S)
        if window_end_block > latest:
            windows_out[f"{h}h"] = {"window_not_yet_elapsed": True, "target_block": window_end_block}
            continue

        segment_blocks = [init_block] + pts
        segment_states = [block_to_state.get(b) for b in segment_blocks]
        if any(s is None or "error" in s for s in segment_states):
            windows_out[f"{h}h"] = {"error_in_one_of_segments": True, "target_block": window_end_block}
            continue

        state_end = segment_states[-1]
        sqrt_p_end_raw = state_end["sqrt_price_x96"] / (2 ** 96) if state_end["sqrt_price_x96"] else None
        price_raw = (sqrt_p_end_raw ** 2) if sqrt_p_end_raw else None

        fee_lower_total, fee_upper_total = 0.0, 0.0
        for i in range(len(segment_states) - 1):
            s_a, s_b = segment_states[i], segment_states[i + 1]
            d_fg0 = s_b["fee_growth_global0_x128"] - s_a["fee_growth_global0_x128"]
            d_fg1 = s_b["fee_growth_global1_x128"] - s_a["fee_growth_global1_x128"]
            fee_lower_total += usdc_equivalent_of_fee(d_fg0, d_fg1, s_a["liquidity"], usdc_side, price_raw)
            fee_upper_total += usdc_equivalent_of_fee(d_fg0, d_fg1, s_b["liquidity"], usdc_side, price_raw)

        d_fg0_total = state_end["fee_growth_global0_x128"] - state_init["fee_growth_global0_x128"]
        d_fg1_total = state_end["fee_growth_global1_x128"] - state_init["fee_growth_global1_x128"]
        survived_any_trading = (d_fg0_total != 0) or (d_fg1_total != 0)

        price_init = state_init["sqrt_price_x96"]
        price_end = state_end["sqrt_price_x96"]
        price_change_frac = (price_end - price_init) / price_init if price_init else None
        lvr_near_zero = price_change_frac is not None and abs(price_change_frac) < LVR_NEAR_ZERO_THRESHOLD
        lvr_frac = (price_change_frac ** 2) / 8 if price_change_frac is not None else None

        liq_end = state_end["liquidity"]
        tvl_usdc_est = None
        if sqrt_p_end_raw:
            reserve_raw = (liq_end / sqrt_p_end_raw) if usdc_side == "currency0" else (liq_end * sqrt_p_end_raw)
            tvl_usdc_est = reserve_raw / (10 ** USDC_DECIMALS)
        lvr_usdc = (lvr_frac * tvl_usdc_est) if (lvr_frac is not None and tvl_usdc_est) else None

        windows_out[f"{h}h"] = {
            "target_block": window_end_block, "n_segments": len(segment_states) - 1,
            "survived_any_trading": survived_any_trading,
            "price_change_frac": price_change_frac, "price_dropped_over_50pct": (price_change_frac is not None and price_change_frac <= -0.5),
            "lvr_near_zero": lvr_near_zero,
            "lvr_frac_of_tvl": lvr_frac, "tvl_usdc_side_estimate": tvl_usdc_est, "lvr_usdc": lvr_usdc,
            "lp_fee_usdc_LOWER_bound": fee_lower_total, "lp_fee_usdc_UPPER_bound": fee_upper_total,
            "fee_over_lvr_LOWER": (fee_lower_total / lvr_usdc) if lvr_usdc else None,
            "fee_over_lvr_UPPER": (fee_upper_total / lvr_usdc) if lvr_usdc else None,
        }

    return {
        "pool_id": pool_id, "usdc_side": usdc_side, "other_token": other_token, "other_token_decimals": other_dec,
        "init_block": init_block, "fee_initialize_pips_LIVE_from_slot0": fee_initialize_pips_live,
        "is_dynamic_fee_flag": is_dynamic_fee_flag,
        "state_at_init": {"sqrt_price_x96": state_init["sqrt_price_x96"], "liquidity": state_init["liquidity"]},
        "windows": windows_out,
    }


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    registry = json.loads(data_dir.joinpath("task_arc_recon_pools_result.json").read_text())
    all_pools = registry.get("initialize_events", {}).get("pools", [])
    result["n_pools_in_full_registry"] = len(all_pools)

    random.seed(RANDOM_SEED)
    sample = random.sample(all_pools, min(SAMPLE_SIZE, len(all_pools)))
    result["random_seed"] = RANDOM_SEED
    result["sample_size_requested"] = SAMPLE_SIZE
    result["sample_pool_ids_in_order"] = [p["pool_id"] for p in sample]

    # === Проба: работает ли исторический eth_call на РЕАЛЬНОМ init_block из ЭТОЙ выборки ===
    probe_pool = sample[0]
    probe_pool_id_bytes = bytes.fromhex(probe_pool["pool_id"][2:])
    probe_state_slot = int.from_bytes(keccak256(probe_pool_id_bytes + POOLS_SLOT.to_bytes(32, "big")), "big")
    latest_probe = rpc("eth_blockNumber", [])
    latest = int(latest_probe.get("result", "0x0"), 16) if not latest_probe.get("error") else None
    result["latest_block"] = latest
    probe_hist_state = extsload_batch4(probe_state_slot, hex(probe_pool["block_number"]))
    archive_works = "error" not in probe_hist_state
    result["archive_probe"] = {"pool_id": probe_pool["pool_id"], "tested_block": probe_pool["block_number"],
                                "raw_result": probe_hist_state, "archive_reads_work": archive_works}
    if not archive_works:
        result["STOPPED"] = "исторический eth_call не работает на этой пробе -- см. archive_probe"
        result["total_rpc_calls"] = _rpc_calls[0]
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_lp_fee_lvr_random_sample_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    dec_cache: dict = {}
    per_pool_results = []
    for p in sample:
        per_pool_results.append(analyze_pool({**p}, dec_cache, latest))
    result["per_pool_results"] = per_pool_results

    n_skipped_no_usdc_side = sum(1 for r in per_pool_results if "skipped_reason" in r)
    n_error_at_init = sum(1 for r in per_pool_results if "error_at_init_block" in r)
    result["sample_composition"] = {
        "n_total": len(per_pool_results), "n_skipped_no_usdc_side_or_invalid": n_skipped_no_usdc_side,
        "n_error_at_init_block": n_error_at_init,
        "n_fee_zero_live": sum(1 for r in per_pool_results if r.get("fee_initialize_pips_LIVE_from_slot0") == 0),
        "n_dynamic_fee_flag_live": sum(1 for r in per_pool_results if r.get("is_dynamic_fee_flag")),
        "note": "fee=0 и dynamic-fee НЕ исключены из расчёта -- формула feeGrowthGlobal x liquidity корректна для ЛЮБОГО реального fee (в т.ч. динамического), точное значение fee не нужно знать заранее.",
    }

    usable = [r for r in per_pool_results if "windows" in r]
    for h in WINDOWS_H:
        key = f"{h}h"
        reached = [r["windows"][key] for r in usable if key in r["windows"] and not r["windows"][key].get("window_not_yet_elapsed")
                   and not r["windows"][key].get("error_in_one_of_segments")]
        n_reached = len(reached)
        n_survived = sum(1 for w in reached if w.get("survived_any_trading"))
        n_price_dropped_50pct = sum(1 for w in reached if w.get("price_dropped_over_50pct"))
        qualifying = [w for w in reached if not w.get("lvr_near_zero") and w.get("fee_over_lvr_LOWER") is not None]
        n_lvr_near_zero = sum(1 for w in reached if w.get("lvr_near_zero"))
        lower_ratios = sorted(w["fee_over_lvr_LOWER"] for w in qualifying)
        upper_ratios = sorted(w["fee_over_lvr_UPPER"] for w in qualifying if w.get("fee_over_lvr_UPPER") is not None)

        def median_of(xs):
            if not xs:
                return None
            m = len(xs) // 2
            return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2

        result.setdefault("window_summary", {})[key] = {
            "n_reached_window": n_reached, "n_survived_any_trading": n_survived,
            "pct_survived": (n_survived / n_reached * 100) if n_reached else None,
            "n_price_dropped_over_50pct": n_price_dropped_50pct,
            "n_lvr_near_zero_excluded_from_median": n_lvr_near_zero,
            "n_qualifying_for_median": len(qualifying),
            "median_fee_over_lvr_LOWER": median_of(lower_ratios),
            "median_fee_over_lvr_UPPER": median_of(upper_ratios),
            "pct_gt_1_LOWER": (sum(1 for r in lower_ratios if r > 1) / len(lower_ratios) * 100) if lower_ratios else None,
            "pct_unprofitable_LOWER_lt_1": (sum(1 for r in lower_ratios if r < 1) / len(lower_ratios) * 100) if lower_ratios else None,
        }

    any_gt_1_5_with_20pct_survival = any(
        (result["window_summary"].get(f"{h}h", {}).get("median_fee_over_lvr_LOWER") or 0) > 1.5
        and (result["window_summary"].get(f"{h}h", {}).get("pct_survived") or 0) >= 20
        for h in WINDOWS_H
    )
    lower_medians = [result["window_summary"][f"{h}h"]["median_fee_over_lvr_LOWER"] for h in WINDOWS_H
                     if result["window_summary"].get(f"{h}h", {}).get("median_fee_over_lvr_LOWER") is not None]
    all_below_1 = bool(lower_medians) and all(m < 1 for m in lower_medians)
    if any_gt_1_5_with_20pct_survival:
        verdict = "ЖИВА"
    elif all_below_1:
        verdict = "НЕТ"
    else:
        verdict = "НЕ РЕШЕНО"
    result["preregistered_verdict"] = verdict
    result["verdict_basis"] = "по НИЖНЕЙ границе (liquidity на начало каждого сегмента), как предписано -- недооценивает fee при растущей ликвидности, значит вердикт ЖИВА на этой границе -- консервативный, не оптимистичный."

    result["cost_accounting"] = {
        "subdivisions_per_window": SUBDIVISIONS_PER_WINDOW,
        "note": (
            f"На пул -- 1 вызов на init_block + окна 1/3/6/12ч x {SUBDIVISIONS_PER_WINDOW} точки = "
            f"{4 * SUBDIVISIONS_PER_WINDOW} запросов ДО дедупа, обычно ~9-10 УНИКАЛЬНЫХ блоков после "
            "(точки соседних окон иногда совпадают или почти совпадают по блоку -- зависит от округления, "
            "не гарантированно) + 1 вызов decimals() стороннего токена + 2 разовых (blockNumber, проба). "
            "Точная честная цена -- см. total_rpc_calls ниже (наивный 2-точечный вариант "
            f"на {SAMPLE_SIZE} пулов стоил бы примерно {SAMPLE_SIZE * 6 + 2} вызовов -- реальная цена "
            "кусочного расчёта выше, но всё ещё на порядки дешевле, чем упёршийся в лимит eth_getLogs)."
        ),
    }

    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_lp_fee_lvr_random_sample_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
