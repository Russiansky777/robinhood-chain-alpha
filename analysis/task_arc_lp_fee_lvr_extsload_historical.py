#!/usr/bin/env python3
"""Владелец (2026-09-16): eth_call не в rate limit (подтверждено --
task_arc_lp_extsload_fee_check_result.json, 35/35 без единой -32005) --
значит fee/LVR можно посчитать БЕЗ единого события Swap, читая
Pool.State напрямую в двух точках (границы окна) через extsload.

Pool.State (Uniswap v4-core, порядок полей подтверждён тем, что
liquidity уже проверен на offset+3 в task5_v4_extsload_slot0_pointcheck.py):
    slot+0: Slot0 (sqrtPriceX96|tick|protocolFee|lpFee) -- уже читали
    slot+1: feeGrowthGlobal0X128 (uint256)                -- НОВОЕ
    slot+2: feeGrowthGlobal1X128 (uint256)                -- НОВОЕ
    slot+3: liquidity (uint128, младшие 128 бит слова)    -- уже читали

Все 4 слота ПОДРЯД -- читаем их ОДНИМ вызовом через batch-вариант
extsload(bytes32 startSlot, uint256 nSlots), а не 4 отдельными
extsload(bytes32) -- отсюда бюджет ~80 вызовов на 16 пулов x 5 точек
(init + 4 границы окна), а не x4.

Комиссия LP за окно = (feeGrowthGlobal_конец - feeGrowthGlobal_начало) x
liquidity / 2**128, по обеим сторонам -- формула владельца. ЧЕСТНАЯ
ОГОВОРКА (не скрываем): это предполагает liquidity ПОСТОЯННОЙ весь
период -- если за окно были mint/burn, точная позиционная бухгалтерия
Uniswap требует интеграла ликвидности по времени, а не 2 точки. Считаем
И на liquidity_at_window_start, И на liquidity_at_window_end, но
ПЕРВИЧНАЯ оценка -- на liquidity_at_end (тот же принцип, что
NFPM.positions(): текущая ликвидность x дельта feeGrowth с контрольной
точки), НЕ min(start,end) -- у свежего пула liquidity РОВНО на
Initialize-блоке часто структурно равна 0 (пул создан раньше первого
mint), из-за чего минимум по двум точкам вырождается в 0 несмотря на
реальное движение feeGrowthGlobal -- это артефакт метода, не
консервативная граница. Явный флаг liquidity_at_window_start_is_
degenerate_zero и обе оценки -- в выводе для прозрачности.

Оборот НЕ считается вообще -- не нужен для fee/LVR (fee уже дан
протоколом через feeGrowthGlobal, LVR -- только по изменению цены).

Выживаемость без событий: если feeGrowthGlobal НЕ изменился между
границами окна (0 delta по обеим сторонам) -- торговли не было, окно
"мертво" для этого пула, как и указано в задании.

ПОРЯДОК (строго): 0) проба -- отдаёт ли rpc.mainnet.arc.io исторический
eth_call вообще (архивные full-node обычно режут глубину ~128 блоков
по умолчанию, у Arc исполнитель Reth -- то же поведение ожидаемо).
Пробуем на РЕАЛЬНОМ init_block живого пула (там sqrtPriceX96 заведомо
не 0 -- если ответ пустой/ошибка, это диагностический сигнал, не
совпадение). Провалилась -- СТОП сразу, ни одного лишнего вызова,
явная рекомендация Chainstack (эмпирическая проверка бесплатного
тарифа необходима -- маркетинговый текст с сайта не даёт точных цифр,
см. task_arc_alt_rpc_research_result.json)."""
from __future__ import annotations

import json
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
ALIVE_THRESHOLD_FEE_GROWTH_CHANGED = True  # выживаемость = было ли ЛЮБОЕ изменение feeGrowth

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.15
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
    """extsload(bytes32 startSlot, uint256 nSlots=4) -- ОДИН вызов,
    возвращает [slot0, feeGrowthGlobal0X128, feeGrowthGlobal1X128, liquidity]."""
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
    if len(data) < 32 * 6:  # offset(32) + length(32) + 4*32
        return {"error": {"message": f"result too short: {len(data)} bytes"}, "http_status": body.get("_http_status")}
    words = [int.from_bytes(data[64 + i * 32: 64 + (i + 1) * 32], "big") for i in range(4)]
    slot0_raw, fee_growth0, fee_growth1, liq_raw = words
    tick_raw = (slot0_raw >> 160) & ((1 << 24) - 1)
    tick = tick_raw - (1 << 24) if tick_raw >= (1 << 23) else tick_raw
    return {
        "sqrt_price_x96": slot0_raw & ((1 << 160) - 1),
        "tick": tick,
        "protocol_fee": (slot0_raw >> 184) & ((1 << 24) - 1),
        "lp_fee": (slot0_raw >> 208) & ((1 << 24) - 1),
        "fee_growth_global0_x128": fee_growth0,
        "fee_growth_global1_x128": fee_growth1,
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


def analyze_pool(pool_meta: dict, dec_cache: dict, latest: int) -> dict:
    pool_id = pool_meta["pool_id"]
    c0, c1 = pool_meta["currency0"].lower(), pool_meta["currency1"].lower()
    usdc_side = "currency0" if c0 in (USDC_ERC20.lower(), NATIVE_SENTINEL) else (
        "currency1" if c1 in (USDC_ERC20.lower(), NATIVE_SENTINEL) else None)
    if usdc_side is None:
        return {"pool_id": pool_id, "error": "ни одна сторона не USDC/native -- пропущен"}
    other_token = c1 if usdc_side == "currency0" else c0
    other_dec = get_token_decimals(other_token, dec_cache)

    init_block = pool_meta["block_number"]
    pool_id_bytes = bytes.fromhex(pool_id[2:])
    state_slot_int = int.from_bytes(keccak256(pool_id_bytes + POOLS_SLOT.to_bytes(32, "big")), "big")

    state_init = extsload_batch4(state_slot_int, hex(init_block))
    if "error" in state_init:
        return {"pool_id": pool_id, "pair": pool_meta.get("pair_symbols"), "error_at_init_block": state_init}

    windows_out = {}
    for h in WINDOWS_H:
        target_block = init_block + int(h * 3600 / BLOCK_TIME_S)
        if target_block > latest:
            windows_out[f"{h}h"] = {"window_not_yet_elapsed": True, "target_block": target_block}
            continue

        state_end = extsload_batch4(state_slot_int, hex(target_block))
        if "error" in state_end:
            windows_out[f"{h}h"] = {"error_at_boundary_block": state_end, "target_block": target_block}
            continue

        d_fg0 = state_end["fee_growth_global0_x128"] - state_init["fee_growth_global0_x128"]
        d_fg1 = state_end["fee_growth_global1_x128"] - state_init["fee_growth_global1_x128"]
        survived = (d_fg0 != 0) or (d_fg1 != 0)

        liq_start, liq_end = state_init["liquidity"], state_end["liquidity"]
        # price_raw = (sqrtPriceX96/2**96)**2 = raw_token1/raw_token0 (RAW integer units,
        # decimals НЕ учтены -- это чисто ончейн-соотношение резервов). Конвертация other-
        # стороны в USDC делается ЦЕЛИКОМ в raw-единицах через это соотношение, ЗАТЕМ один раз
        # переводится в human через 10**USDC_DECIMALS -- смешивать human-суммы с raw-price
        # ЗАПРЕЩЕНО (даёт неверный результат при other_dec != USDC_DECIMALS).
        sqrt_p_end_raw = state_end["sqrt_price_x96"] / (2 ** 96) if state_end["sqrt_price_x96"] else None
        price_raw = (sqrt_p_end_raw ** 2) if sqrt_p_end_raw else None

        def fee_usdc_for_liquidity_basis(liq_basis: int) -> float:
            fee_c0_raw = (d_fg0 * liq_basis) >> 128
            fee_c1_raw = (d_fg1 * liq_basis) >> 128
            if usdc_side == "currency0":
                usdc_raw = fee_c0_raw
                other_as_usdc_raw = (fee_c1_raw / price_raw) if price_raw else 0.0
            else:
                usdc_raw = fee_c1_raw
                other_as_usdc_raw = (fee_c0_raw * price_raw) if price_raw else 0.0
            return (usdc_raw + other_as_usdc_raw) / (10 ** USDC_DECIMALS)

        fee_usdc_start_basis = fee_usdc_for_liquidity_basis(liq_start)
        fee_usdc_end_basis = fee_usdc_for_liquidity_basis(liq_end)
        # liquidity_at_window_start = liquidity РОВНО на init_block -- у свежего пула это
        # СТРУКТУРНО часто 0 (Initialize создаёт пул раньше, чем первый LP успевает
        # заминтить позицию), поэтому min(start,end) вырождается в 0 для ЛЮБОГО пула, где
        # liq_start==0, хотя feeGrowthGlobal реально двигался -- это не консервативная
        # граница, а артефакт метода. ПЕРВИЧНАЯ оценка -- на liquidity_at_end (тот же
        # принцип, что использует NFPM.positions(): текущая ликвидность конкретной позиции x
        # дельта feeGrowth с момента последней контрольной точки -- корректно ТОЧНО для LP,
        # державшего liquidity_at_end неизменной с init, и остаётся разумной аппроксимацией
        # иначе). liq_start==0 -- отдельный явный флаг, не тихо занижаем до 0.
        fee_usdc_primary = fee_usdc_end_basis
        liq_start_degenerate_zero = (liq_start == 0)
        liq_changed_significantly = (liq_start != liq_end) and (
            abs(liq_end - liq_start) / max(liq_start, 1) > 0.05
        )

        price_init = state_init["sqrt_price_x96"]
        price_end = state_end["sqrt_price_x96"]
        price_change_frac = (price_end - price_init) / price_init if price_init else None
        lvr_frac = (price_change_frac ** 2) / 8 if price_change_frac is not None else None

        sqrt_p_end = state_end["sqrt_price_x96"] / (2 ** 96) if state_end["sqrt_price_x96"] else None
        tvl_usdc_est = None
        if sqrt_p_end:
            reserve_raw = (liq_end / sqrt_p_end) if usdc_side == "currency0" else (liq_end * sqrt_p_end)
            tvl_usdc_est = reserve_raw / (10 ** USDC_DECIMALS)
        lvr_usdc = (lvr_frac * tvl_usdc_est) if (lvr_frac is not None and tvl_usdc_est) else None

        windows_out[f"{h}h"] = {
            "target_block": target_block,
            "survived_any_trading": survived,
            "fee_growth_delta0_x128": d_fg0, "fee_growth_delta1_x128": d_fg1,
            "liquidity_at_window_start": liq_start, "liquidity_at_window_end": liq_end,
            "liquidity_at_window_start_is_degenerate_zero": liq_start_degenerate_zero,
            "liquidity_changed_significantly_gt5pct": liq_changed_significantly,
            "lp_fee_usdc_using_liquidity_at_start": fee_usdc_start_basis,
            "lp_fee_usdc_using_liquidity_at_end_PRIMARY": fee_usdc_end_basis,
            "price_change_frac": price_change_frac,
            "lvr_frac_of_tvl": lvr_frac, "tvl_usdc_side_estimate": tvl_usdc_est,
            "lvr_usdc": lvr_usdc,
            "fee_over_lvr": (fee_usdc_primary / lvr_usdc) if lvr_usdc else None,
        }

    return {
        "pool_id": pool_id, "pair": pool_meta.get("pair_symbols"), "fee_initialize_pips": pool_meta.get("fee_initialize_pips"),
        "usdc_side": usdc_side, "other_token": other_token, "other_token_decimals": other_dec, "init_block": init_block,
        "state_at_init": {"sqrt_price_x96": state_init["sqrt_price_x96"], "liquidity": state_init["liquidity"],
                           "fee_growth_global0_x128": state_init["fee_growth_global0_x128"],
                           "fee_growth_global1_x128": state_init["fee_growth_global1_x128"]},
        "windows": windows_out,
    }


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    top = json.loads(data_dir.joinpath("task_arc_top_pools_symbols_result.json").read_text())["top_pools_enriched"]
    registry = json.loads(data_dir.joinpath("task_arc_recon_pools_result.json").read_text())
    reg_by_id = {p["pool_id"]: p for p in registry.get("initialize_events", {}).get("pools", [])}

    pool_metas = []
    for p in top:
        reg = reg_by_id.get(p["pool_id"])
        if not reg:
            continue
        pool_metas.append({**p, "currency0": reg["currency0"], "currency1": reg["currency1"],
                            "block_number": reg["block_number"]})
    result["n_pools_matched_to_registry"] = len(pool_metas)

    dynamic_fee_pools = [p for p in pool_metas if p["fee_initialize_pips"] == 8388608]
    fee_zero_pools = [p for p in pool_metas if p["fee_initialize_pips"] == 0]
    fee_positive_pools = [p for p in pool_metas if p["fee_initialize_pips"] not in (0, 8388608)]
    result["sample_composition"] = {
        "n_matched_total": len(pool_metas),
        "n_fee_positive_used_for_main_calc": len(fee_positive_pools),
        "n_fee_zero_excluded": len(fee_zero_pools),
        "n_dynamic_fee_flag_excluded": len(dynamic_fee_pools),
        "honest_correction": (
            f"Задание говорило про 16 пулов с fee>0 -- реально из 16 registry-matched "
            f"пулов {len(fee_positive_pools)} имеют fee>0 (не 8388608), {len(fee_zero_pools)} "
            f"имеют fee=0 (исключены из основного расчёта, как и просилось для fee=0), "
            f"{len(dynamic_fee_pools)} с флагом dynamic-fee. Число 16 в задании -- это общее "
            f"число registry-matched пулов, не именно fee>0. Используем реальные {len(fee_positive_pools)}, "
            f"не выдумываем 16."
        ),
    }

    if not fee_positive_pools:
        result["STOPPED"] = "0 пулов с fee>0 в выборке -- нечего считать"
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_lp_fee_lvr_extsload_historical_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Шаг 0: проба -- работают ли исторические eth_call вообще ===
    probe_pool = fee_positive_pools[0]
    probe_pool_id_bytes = bytes.fromhex(probe_pool["pool_id"][2:])
    probe_state_slot = int.from_bytes(keccak256(probe_pool_id_bytes + POOLS_SLOT.to_bytes(32, "big")), "big")
    latest_probe = rpc("eth_blockNumber", [])
    latest = int(latest_probe.get("result", "0x0"), 16) if not latest_probe.get("error") else None
    result["latest_block"] = latest
    result["archive_probe"] = {
        "pool_id": probe_pool["pool_id"], "pair": probe_pool.get("pair_symbols"),
        "tested_block": probe_pool["block_number"],
        "blocks_behind_latest": (latest - probe_pool["block_number"]) if latest else None,
    }
    probe_hist_state = extsload_batch4(probe_state_slot, hex(probe_pool["block_number"]))
    archive_works = "error" not in probe_hist_state and probe_hist_state.get("sqrt_price_x96", 0) != 0
    result["archive_probe"]["raw_result"] = probe_hist_state
    result["archive_probe"]["archive_reads_work"] = archive_works

    if not archive_works:
        result["STOPPED"] = (
            "ИСТОРИЧЕСКИЙ eth_call на rpc.mainnet.arc.io НЕ РАБОТАЕТ (проба на реальном init_block "
            f"живого пула {probe_pool['pool_id']} дала error/пустой результат вместо ожидаемого "
            "ненулевого sqrtPriceX96) -- узел, судя по всему, full node без архивного состояния "
            "(типичная глубина ~127 блоков ~64 секунды при 0.506с/блок). НУЖЕН АРХИВНЫЙ УЗЕЛ "
            "(Chainstack Developer -- бесплатный, без карты, но точный лимит логов/состояния "
            "неизвестен без живой проверки, см. task_arc_alt_rpc_research_result.json)."
        )
        result["total_rpc_calls"] = _rpc_calls[0]
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_lp_fee_lvr_extsload_historical_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Архив работает -- полный проход по всем fee>0 пулам ===
    dec_cache: dict = {}
    per_pool_results = []
    for p in fee_positive_pools:
        per_pool_results.append(analyze_pool(p, dec_cache, latest))
    result["per_pool_results"] = per_pool_results

    for h in WINDOWS_H:
        key = f"{h}h"
        ratios = [r["windows"][key]["fee_over_lvr"] for r in per_pool_results
                  if key in r.get("windows", {}) and r["windows"][key].get("fee_over_lvr") is not None]
        reached = [r["windows"][key] for r in per_pool_results
                   if key in r.get("windows", {}) and not r["windows"][key].get("window_not_yet_elapsed")
                   and "error_at_boundary_block" not in r["windows"][key]]
        n_reached = len(reached)
        n_survived = sum(1 for w in reached if w.get("survived_any_trading"))
        ratios_sorted = sorted(ratios)
        median = None
        if ratios_sorted:
            mid = len(ratios_sorted) // 2
            median = ratios_sorted[mid] if len(ratios_sorted) % 2 else (ratios_sorted[mid - 1] + ratios_sorted[mid]) / 2
        result.setdefault("window_summary", {})[key] = {
            "n_pools_with_ratio": len(ratios_sorted), "median_fee_over_lvr": median,
            "pct_gt_1": (sum(1 for r in ratios_sorted if r > 1) / len(ratios_sorted) * 100) if ratios_sorted else None,
            "n_reached_window": n_reached, "n_survived_any_trading": n_survived,
            "pct_survived": (n_survived / n_reached * 100) if n_reached else None,
        }

    any_gt_1_5_with_20pct_survival = any(
        (result["window_summary"].get(f"{h}h", {}).get("median_fee_over_lvr") or 0) > 1.5
        and (result["window_summary"].get(f"{h}h", {}).get("pct_survived") or 0) >= 20
        for h in WINDOWS_H
    )
    medians = [result["window_summary"][f"{h}h"]["median_fee_over_lvr"] for h in WINDOWS_H
               if result["window_summary"].get(f"{h}h", {}).get("median_fee_over_lvr") is not None]
    all_below_1 = bool(medians) and all(m < 1 for m in medians)
    if any_gt_1_5_with_20pct_survival:
        verdict = "ЖИВА"
    elif all_below_1:
        verdict = "НЕТ"
    else:
        verdict = "НЕ РЕШЕНО"
    result["preregistered_verdict"] = verdict
    result["verdict_caveat"] = (
        f"Выборка -- {len(fee_positive_pools)} пулов fee>0 ИЗ ТОП-30 ПО ОБЪЁМУ, только 16/30 "
        "нашлись в частичном реестре -- предвзято по построению, не репрезентативно для всех "
        "24567 пулов. fee считается на liquidity_at_window_end (тот же принцип, что "
        "NFPM.positions() -- текущая ликвидность x дельта feeGrowth с контрольной точки), "
        "НЕ на min(start,end): у свежего пула liquidity_at_init часто СТРУКТУРНО равна 0 "
        "(Initialize создаёт пул раньше первого mint) -- минимум по двум точкам в этом случае "
        "вырождается в 0 несмотря на реальное движение feeGrowthGlobal, это не консервативная "
        "граница, а артефакт метода (см. liquidity_at_window_start_is_degenerate_zero по каждому "
        "пулу/окну). Оценка на liquidity_at_end -- не точная позиционная бухгалтерия (та "
        "требует интеграла ликвидности по времени, недоступного из 2 точек), но не имеет этого "
        "вырождения; оба значения (at_start и at_end) сохранены в выводе для прозрачности."
    )

    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_lp_fee_lvr_extsload_historical_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
