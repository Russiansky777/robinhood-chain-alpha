#!/usr/bin/env python3
"""Владелец (2026-09-17): fee/LVR как ОТНОШЕНИЕ структурно не видит
главный риск свежего пула -- когда торговли нет, цена замирает на
последней сделке, LVR формально ноль, "убыток" не регистрируется, хотя
LP реально сидит в неликвидном токене и выйти не может. Нужна ПРЯМАЯ
доходность в USDC, не отношение, и отбор входа ТОЛЬКО по признакам,
видимым LP в момент входа T (без заглядывания вперёд).

Методология (всё через eth_call/extsload на исторических блоках, БЕЗ
единого eth_getLogs):

Отбор в момент T (видимо LP при входе, lookahead запрещён):
  liquidity_pool(T) > 0 И feeGrowthGlobal вырос за час ПЕРЕД T (T-1ч -> T)
  И TVL(T) >= $500. fee=0 НЕ исключается.

Наша гипотетическая позиция: full-range, задаётся ОДНИМ параметром --
NOTIONAL_USDC (см. константу), поделённым 50/50 по стоимости в T между
USDC-стороной и другой стороной по текущей цене. Из этого выводится
liquidity L, КОНСТАНТНАЯ на весь период вперёд -- проблема "растущей
ликвидности пула" не участвует в расчёте вообще, потому что L -- наша,
а не пула.

Стоимость full-range позиции при цене P (тот же sqrtPriceX96/2**96,
что и everywhere в этой сессии): reserve0=L/sqrtP, reserve1=L*sqrtP
(стандартная формула V3/V4, если usdc_side=currency0 -- иначе зеркально).
Комиссия за окно = L x ΔfeeGrowthGlobal (та же формула, что в
task_arc_lp_fee_lvr_extsload_historical.py, но L -- НАША постоянная,
не пула -- поэтому здесь это ТОЧНАЯ, не приближённая, бухгалтерия для
позиции, открытой РОВНО в T с фиксированной L, что и требуется).

ОПТИМИСТИЧНАЯ доходность = (стоимость_позиции(T+H) + комиссии - стоимость
той же исходной пары токенов, если бы просто держали, по цене T+H) / V.
ПЕССИМИСТИЧНАЯ доходность (специально для мёртвых/неторговавших окон,
где не-USDC сторона неликвидна и продать некому) = (USDC-сторона
позиции(T+H) + USDC-часть комиссий - V) / V -- др. сторона считается
СТОИМОСТЬЮ НОЛЬ, сравнение НЕ с hodl, а напрямую с вложенным V (честно
показывает "сколько капитала реально уцелело", а не относительный
перформанс против альтернативы, которая тоже неликвидна).

Ёмкость (Шаг 3, только T=24ч): размер ОДНОСТОРОННЕГО свопа в USDC,
двигающий цену не более чем на 1% (вывод формулы из стандартного
инварианта sqrtP: для token0-in свопа Δx = L*(1/sqrt(r)-1)/sqrtP, где
r=0.99; для token1-in Δy = L*sqrtP*(sqrt(r)-1), r=1.01 -- обе формулы
дают ~0.50% от соответствующего резерва на 1% движения цены, что
согласуется с sqrt-структурой AMM), ограниченный сверху 20% от TVL пула."""
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
T_HOURS = [3, 6, 12, 24]
H_HOURS = [1, 3, 6]
MIN_TVL_USDC = 500.0
NOTIONAL_USDC = 200.0  # наша гипотетическая позиция -- $200, середина заявленных владельцем $150-300
MAX_POOLS_PER_T = 60
CANDIDATE_POOL_SIZE = 500
RANDOM_SEED = 20260917

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.08
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
    calldata = "0x" + selector + state_slot_int.to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex()
    body = rpc("eth_call", [{"to": POOL_MANAGER, "data": calldata}, block_hex])
    if "error" in body:
        return {"error": body["error"]}
    raw = body.get("result")
    if not raw or raw == "0x":
        return {"error": {"message": "empty result"}}
    data = bytes.fromhex(raw[2:])
    if len(data) < 192:
        return {"error": {"message": f"short result {len(data)}"}}
    words = [int.from_bytes(data[64 + i * 32: 64 + (i + 1) * 32], "big") for i in range(4)]
    slot0_raw, fg0, fg1, liq_raw = words
    return {
        "sqrt_price_x96": slot0_raw & ((1 << 160) - 1),
        "lp_fee": (slot0_raw >> 208) & ((1 << 24) - 1),
        "fee_growth_global0_x128": fg0, "fee_growth_global1_x128": fg1,
        "liquidity": liq_raw & ((1 << 128) - 1),
    }


def get_token_decimals(token: str, cache: dict) -> int:
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


def get_token_symbol(token: str) -> str:
    body = rpc("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"])  # symbol()
    res = body.get("result")
    if not res or res == "0x":
        return token[:10]
    try:
        raw = bytes.fromhex(res[2:])
        offset = int.from_bytes(raw[:32], "big")
        length = int.from_bytes(raw[offset:offset + 32], "big")
        return raw[offset + 32: offset + 32 + length].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return token[:10]


def usdc_value_of_raw(other_raw: float, usdc_side: str, price_raw: float | None) -> float:
    """price_raw = (sqrtP/2**96)**2 = raw_token1/raw_token0 в конце окна."""
    if price_raw is None:
        return 0.0
    if usdc_side == "currency0":
        return other_raw / price_raw / (10 ** USDC_DECIMALS)
    return other_raw * price_raw / (10 ** USDC_DECIMALS)


def analyze_pool_at_T(pool_meta: dict, T_h: int, dec_cache: dict, latest: int) -> dict | None:
    pool_id = pool_meta["pool_id"]
    c0, c1 = pool_meta["currency0"].lower(), pool_meta["currency1"].lower()
    usdc_side = "currency0" if c0 in (USDC_ERC20.lower(), NATIVE_SENTINEL) else (
        "currency1" if c1 in (USDC_ERC20.lower(), NATIVE_SENTINEL) else None)
    if usdc_side is None:
        return None
    other_token = c1 if usdc_side == "currency0" else c0
    init_block = pool_meta["block_number"]
    pool_id_bytes = bytes.fromhex(pool_id[2:])
    state_slot_int = int.from_bytes(keccak256(pool_id_bytes + POOLS_SLOT.to_bytes(32, "big")), "big")

    block_T_minus_1h = init_block + int((T_h - 1) * 3600 / BLOCK_TIME_S)
    block_T = init_block + int(T_h * 3600 / BLOCK_TIME_S)
    if block_T > latest:
        return None

    st_pre = extsload_batch4(state_slot_int, hex(block_T_minus_1h))
    st_T = extsload_batch4(state_slot_int, hex(block_T))
    if "error" in st_pre or "error" in st_T:
        return None
    if st_T["sqrt_price_x96"] == 0 or st_T["liquidity"] <= 0:
        return None
    grew_last_hour = (st_T["fee_growth_global0_x128"] != st_pre["fee_growth_global0_x128"]
                       or st_T["fee_growth_global1_x128"] != st_pre["fee_growth_global1_x128"])
    if not grew_last_hour:
        return None

    sqrt_p_T = st_T["sqrt_price_x96"] / (2 ** 96)
    price_raw_T = sqrt_p_T ** 2
    liq_pool_T = st_T["liquidity"]
    reserve0_pool = liq_pool_T / sqrt_p_T
    reserve1_pool = liq_pool_T * sqrt_p_T
    if usdc_side == "currency0":
        tvl_usdc = reserve0_pool / (10 ** USDC_DECIMALS) + usdc_value_of_raw(reserve1_pool, "currency0", price_raw_T)
    else:
        tvl_usdc = usdc_value_of_raw(reserve0_pool, "currency1", price_raw_T) + reserve1_pool / (10 ** USDC_DECIMALS)
    if tvl_usdc < MIN_TVL_USDC:
        return None

    other_dec = get_token_decimals(other_token, dec_cache)

    # === Наша позиция: L из NOTIONAL_USDC/2 на каждой стороне при цене T ===
    half_usdc_raw = (NOTIONAL_USDC / 2) * (10 ** USDC_DECIMALS)
    L_ours = half_usdc_raw * sqrt_p_T if usdc_side == "currency0" else half_usdc_raw / sqrt_p_T
    entry_reserve0 = L_ours / sqrt_p_T
    entry_reserve1 = L_ours * sqrt_p_T

    return {
        "pool_id": pool_id, "usdc_side": usdc_side, "other_token": other_token, "other_token_decimals": other_dec,
        "init_block": init_block, "T_h": T_h, "block_T": block_T,
        "state_slot_int": state_slot_int, "tvl_usdc_at_T": tvl_usdc, "L_ours": L_ours,
        "sqrt_p_T": sqrt_p_T, "state_T": st_T, "entry_reserve0": entry_reserve0, "entry_reserve1": entry_reserve1,
    }


def measure_window(entry: dict, H_h: int, latest: int) -> dict | None:
    block_TH = entry["init_block"] + int((entry["T_h"] + H_h) * 3600 / BLOCK_TIME_S)
    if block_TH > latest:
        return {"window_not_yet_elapsed": True, "target_block": block_TH}
    st_TH = extsload_batch4(entry["state_slot_int"], hex(block_TH))
    if "error" in st_TH:
        return {"error_at_boundary": st_TH, "target_block": block_TH}
    if st_TH["sqrt_price_x96"] == 0:
        return {"error_at_boundary": "sqrtPrice=0 at T+H", "target_block": block_TH}

    st_T = entry["state_T"]
    usdc_side = entry["usdc_side"]
    L = entry["L_ours"]
    d_fg0 = st_TH["fee_growth_global0_x128"] - st_T["fee_growth_global0_x128"]
    d_fg1 = st_TH["fee_growth_global1_x128"] - st_T["fee_growth_global1_x128"]
    traded_in_window = (d_fg0 != 0) or (d_fg1 != 0)

    sqrt_p_TH = st_TH["sqrt_price_x96"] / (2 ** 96)
    price_raw_TH = sqrt_p_TH ** 2

    fee0_raw = (d_fg0 * L) / (2 ** 128)  # L -- НАША liquidity, float (не int пула) -- >>128 недопустим для float
    fee1_raw = (d_fg1 * L) / (2 ** 128)
    if usdc_side == "currency0":
        fees_usdc_total = fee0_raw / (10 ** USDC_DECIMALS) + usdc_value_of_raw(fee1_raw, "currency0", price_raw_TH)
        fees_usdc_side_only = fee0_raw / (10 ** USDC_DECIMALS)
    else:
        fees_usdc_total = usdc_value_of_raw(fee0_raw, "currency1", price_raw_TH) + fee1_raw / (10 ** USDC_DECIMALS)
        fees_usdc_side_only = fee1_raw / (10 ** USDC_DECIMALS)

    pos_reserve0_TH = L / sqrt_p_TH
    pos_reserve1_TH = L * sqrt_p_TH
    if usdc_side == "currency0":
        position_value_full = pos_reserve0_TH / (10 ** USDC_DECIMALS) + usdc_value_of_raw(pos_reserve1_TH, "currency0", price_raw_TH)
        position_value_usdc_side_only = pos_reserve0_TH / (10 ** USDC_DECIMALS)
        hodl_value_full = entry["entry_reserve0"] / (10 ** USDC_DECIMALS) + usdc_value_of_raw(entry["entry_reserve1"], "currency0", price_raw_TH)
    else:
        position_value_full = usdc_value_of_raw(pos_reserve0_TH, "currency1", price_raw_TH) + pos_reserve1_TH / (10 ** USDC_DECIMALS)
        position_value_usdc_side_only = pos_reserve1_TH / (10 ** USDC_DECIMALS)
        hodl_value_full = usdc_value_of_raw(entry["entry_reserve0"], "currency1", price_raw_TH) + entry["entry_reserve1"] / (10 ** USDC_DECIMALS)

    optimistic_itog = (position_value_full + fees_usdc_total) - hodl_value_full
    optimistic_return_pct = optimistic_itog / NOTIONAL_USDC * 100
    pessimistic_realized = position_value_usdc_side_only + fees_usdc_side_only
    pessimistic_return_pct = (pessimistic_realized - NOTIONAL_USDC) / NOTIONAL_USDC * 100

    # РЕАЛЬНОЕ изменение цены = отношение КВАДРАТОВ sqrtPrice (price=sqrtP^2) --
    # не путать с изменением самого sqrtPrice (в 2 раза меньше в первом порядке).
    price_change_frac_real = (price_raw_TH - (st_T["sqrt_price_x96"] / (2 ** 96)) ** 2) / (st_T["sqrt_price_x96"] / (2 ** 96)) ** 2

    return {
        "target_block": block_TH, "traded_in_window": traded_in_window, "price_change_frac_real": price_change_frac_real,
        "fees_usdc_total": fees_usdc_total, "position_value_full": position_value_full, "hodl_value_full": hodl_value_full,
        "optimistic_return_pct": optimistic_return_pct, "pessimistic_return_pct": pessimistic_return_pct,
        "profitable_optimistic": optimistic_return_pct > 0, "profitable_pessimistic": pessimistic_return_pct > 0,
    }


def compute_capacity(entry: dict) -> dict:
    """1% price-impact односторонний своп + верхний потолок 20% TVL."""
    st_T = entry["state_T"]
    liq_pool = st_T["liquidity"]
    sqrt_p = entry["sqrt_p_T"]
    usdc_side = entry["usdc_side"]
    r_minus = 0.99
    r_plus = 1.01
    # token0-in своп двигает цену ВНИЗ (r=0.99): Δx = L*(1/sqrt(r)-1)/sqrtP
    delta_x_token0_in = liq_pool * (1 / (r_minus ** 0.5) - 1) / sqrt_p
    # token1-in своп двигает цену ВВЕРХ (r=1.01): Δy = L*sqrtP*(sqrt(r)-1)
    delta_y_token1_in = liq_pool * sqrt_p * (r_plus ** 0.5 - 1)
    if usdc_side == "currency0":
        usdc_swap_size_raw = delta_x_token0_in  # USDC -- token0, свопаем USDC IN, цена падает
    else:
        usdc_swap_size_raw = delta_y_token1_in
    usdc_swap_size = usdc_swap_size_raw / (10 ** USDC_DECIMALS)
    cap_20pct_tvl = 0.20 * entry["tvl_usdc_at_T"]
    capacity_usdc = min(usdc_swap_size, cap_20pct_tvl)
    return {"price_impact_1pct_swap_usdc": usdc_swap_size, "cap_20pct_tvl_usdc": cap_20pct_tvl, "capacity_usdc": capacity_usdc}


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "notional_usdc": NOTIONAL_USDC, "random_seed": RANDOM_SEED}

    registry = json.loads(data_dir.joinpath("task_arc_recon_pools_result.json").read_text())
    all_pools = registry.get("initialize_events", {}).get("pools", [])
    random.seed(RANDOM_SEED)
    candidates = random.sample(all_pools, min(CANDIDATE_POOL_SIZE, len(all_pools)))
    result["n_candidates"] = len(candidates)

    probe = rpc("eth_blockNumber", [])
    latest = int(probe.get("result", "0x0"), 16) if not probe.get("error") else None
    result["latest_block"] = latest

    dec_cache: dict = {}
    by_T: dict[int, list[dict]] = {t: [] for t in T_HOURS}
    for T_h in T_HOURS:
        shuffled = candidates[:]
        random.Random(RANDOM_SEED + T_h).shuffle(shuffled)
        for p in shuffled:
            if len(by_T[T_h]) >= MAX_POOLS_PER_T:
                break
            entry = analyze_pool_at_T(p, T_h, dec_cache, latest)
            if entry is not None:
                by_T[T_h].append(entry)

    result["n_eligible_per_T"] = {str(t): len(v) for t, v in by_T.items()}

    table = {}
    all_rows_for_best = []
    for T_h in T_HOURS:
        table[str(T_h)] = {}
        for H_h in H_HOURS:
            rows = []
            for entry in by_T[T_h]:
                m = measure_window(entry, H_h, latest)
                if m is None or m.get("window_not_yet_elapsed") or "error_at_boundary" in m:
                    continue
                rows.append({**m, "pool_id": entry["pool_id"], "other_token": entry["other_token"],
                             "usdc_side": entry["usdc_side"], "tvl_usdc_at_T": entry["tvl_usdc_at_T"]})
                if T_h == 24 and H_h == 6:
                    all_rows_for_best.append({**rows[-1], "entry": entry})

            n = len(rows)
            n_no_trading = sum(1 for r in rows if not r["traded_in_window"])
            opt_sorted = sorted(r["optimistic_return_pct"] for r in rows)
            pes_sorted = sorted(r["pessimistic_return_pct"] for r in rows)

            def median_of(xs):
                if not xs:
                    return None
                m = len(xs) // 2
                return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2

            table[str(T_h)][str(H_h)] = {
                "n": n, "n_no_trading_in_window": n_no_trading,
                "median_return_pct_OPTIMISTIC": median_of(opt_sorted),
                "median_return_pct_PESSIMISTIC": median_of(pes_sorted),
                "pct_profitable_OPTIMISTIC": (sum(1 for r in rows if r["profitable_optimistic"]) / n * 100) if n else None,
                "pct_profitable_PESSIMISTIC": (sum(1 for r in rows if r["profitable_pessimistic"]) / n * 100) if n else None,
            }

    result["table_T_by_H"] = table

    # === Ёмкость, только T=24ч ===
    capacities = [compute_capacity(entry)["capacity_usdc"] for entry in by_T[24]]
    capacities_sorted = sorted(capacities)
    result["capacity_T24h"] = {
        "n": len(capacities_sorted),
        "median_usdc": (capacities_sorted[len(capacities_sorted) // 2] if capacities_sorted else None),
        "max_usdc": (capacities_sorted[-1] if capacities_sorted else None),
    }

    # === Три лучших пула T=24, H=6 ===
    all_rows_for_best.sort(key=lambda r: -r["pessimistic_return_pct"])
    best3 = []
    for r in all_rows_for_best[:3]:
        cap = compute_capacity(r["entry"])
        sym = get_token_symbol(r["other_token"])
        best3.append({
            "pool_id": r["pool_id"], "pair": f"USDC/{sym}", "other_token": r["other_token"],
            "optimistic_return_pct": r["optimistic_return_pct"], "pessimistic_return_pct": r["pessimistic_return_pct"],
            "capacity_usdc": cap["capacity_usdc"], "tvl_usdc_at_T": r["tvl_usdc_at_T"],
        })
    result["best3_T24_H6"] = best3

    # === Вердикт по предрегистрации ===
    any_alive = any(
        (table.get(str(t), {}).get("6", {}).get("median_return_pct_PESSIMISTIC") or -999) > 2
        and (table.get(str(t), {}).get("6", {}).get("pct_profitable_PESSIMISTIC") or 0) >= 60
        and (result["capacity_T24h"]["median_usdc"] or 0) >= 200
        for t in T_HOURS
    )
    pes_medians_h6 = [table[str(t)]["6"]["median_return_pct_PESSIMISTIC"] for t in T_HOURS
                      if table.get(str(t), {}).get("6", {}).get("median_return_pct_PESSIMISTIC") is not None]
    all_negative = bool(pes_medians_h6) and all(m < 0 for m in pes_medians_h6)
    low_capacity = (result["capacity_T24h"]["median_usdc"] or 0) < 50
    if any_alive:
        verdict = "ЖИВА"
    elif all_negative or low_capacity:
        verdict = "НЕТ"
    else:
        verdict = "НЕ РЕШЕНО"
    result["preregistered_verdict"] = verdict

    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_lp_entry_age_curve_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
