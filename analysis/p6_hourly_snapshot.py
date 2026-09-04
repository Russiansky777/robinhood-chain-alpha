#!/usr/bin/env python3
"""P6 -- часовой снимок живой позиции (владелец, Гейт 2, 2026-09-04):
"тот же скрипт, что P5, параметры пула/позиции/рынка из конфига".

Параметризовано под P6 (Base, Aerodrome Slipstream, USDC-CBBTC,
market1=BTC на Lighter) там, где роли токенов ОБРАТНЫ P5: в P5
token0=WETH(волатильный)/token1=USDG(стейбл); здесь token0=USDC
(стейбл)/token1=cbBTC(волатильный) -- см. `data/p3_guard_cache/
p6_entry_recon_result.json`. Формулы delta/LVR ниже явно используют
token1 как волатильную ногу (не копия P5 с подменёнными именами).

ТРЕБОВАНИЕ владельца (до любых транзакций входа): "проверить сухим
прогоном, что скрипт стартует и честно пишет «позиции нет» вместо
падения" -- если `data/p6_live_position_state.json` не существует ИЛИ
`positions()` не находит tokenId из него, скрипт печатает честный
результат `{"found_position": false}` и возвращает 0 (не крах), не
жди tokenId, вбитого заранее.

Kill-критерии `docs/P6_HEDGED_LP.md` (владелец, переписаны 2026-09-04)
-- посчитаны и записаны В КАЖДУЮ строку явными булевыми флагами, не
вычисляются потом из сырых чисел:
  kill_flag_1_fee_lvr_ratio_lt_3       -- отношение комиссии/LVR при
                                           30-дневной (реализованной по
                                           своему ряду) sigma < 3
  kill_flag_2_fee_capture_lt_0_4       -- fee_capture_ratio_cumulative < 0.4
  kill_flag_3_basis_persistent_gt_2pct -- суточный VWAP-базис cbBTC/BTC
                                           >2%, >=2 суток подряд (по
                                           своему растущему ряду, метод
                                           тот же, что analysis/p6_basis_recompute.py)
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from Crypto.Hash import keccak

# --- КОНФИГ (владелец: "параметры пула/позиции/рынка из конфига") ---
CONFIG = {
    "chain_rpc": "https://mainnet.base.org",
    "chain_id": 8453,
    "pool_address": "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb",  # USDC-CBBTC, Aerodrome Slipstream
    "nfpm_address": "0x827922686190790b37229fd06084350E74485b72",  # подтверждено p6_entry_recon.py (совпадение factory())
    "wallet": "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75",  # тот же кошелёк, что P5
    "token0": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "token0_symbol": "USDC", "token0_decimals": 6,
    "token1": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "token1_symbol": "cbBTC", "token1_decimals": 8,
    "volatile_leg": "token1",  # cbBTC -- ОБРАТНО P5, где волатильная нога -- token0
    "pool_fee_fraction": 0.00033,  # 0.033%, docs/PROJECT_STATE.md, раздел "Скринер пулов"
    "gt_network": "base",
    "lighter_api_base": "https://api.rh.lighter.xyz",
    "lighter_account_index": 22012,
    "lighter_market_symbol": "BTC",
}

STATE_PATH = Path("data/p6_live_position_state.json")
ACCRUAL_LOG_PATH = Path("data/p6_fee_accrual.jsonl")
OUT_PATH = Path("data/p3_guard_cache/p6_live_snapshot_result.json")

# Kill-критерии (владелец, docs/P6_HEDGED_LP.md, переписаны 2026-09-04)
KILL_1_RATIO_THRESHOLD = 3.0
KILL_2_FEE_CAPTURE_THRESHOLD = 0.4
KILL_3_BASIS_THRESHOLD_PCT = 2.0
KILL_3_MIN_CONSECUTIVE_DAYS = 2

RPC_MIN_INTERVAL_S = 1.5
RPC_RETRY_BACKOFF_S = 15.0
RPC_MAX_RETRIES = 3
_last_rpc_call = 0.0


def _topic0(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


def _selector(sig: str) -> str:
    return _topic0(sig)[:10]


def _throttle() -> None:
    global _last_rpc_call
    wait = _last_rpc_call + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_rpc_call = time.monotonic()


def rpc(method: str, params: list):
    for attempt in range(RPC_MAX_RETRIES + 1):
        _throttle()
        r = requests.post(CONFIG["chain_rpc"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        if r.status_code == 429 and attempt < RPC_MAX_RETRIES:
            time.sleep(RPC_RETRY_BACKOFF_S)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method} {params}: {body['error']}")
        return body["result"]
    raise RuntimeError("RPC 429 после ретраев")


def eth_call(to: str, sig: str, extra: str = "") -> str:
    return rpc("eth_call", [{"to": to, "data": _selector(sig) + extra}, "latest"])


def nfpm_position(token_id: int) -> dict:
    calldata = hex(token_id)[2:].rjust(64, "0")
    raw = eth_call(CONFIG["nfpm_address"], "positions(uint256)", calldata)
    if raw is None or raw == "0x":
        return {"found": False}
    from eth_abi import decode as abi_decode
    fields = abi_decode(
        ["uint96", "address", "address", "address", "int24", "int24", "int24",
         "uint128", "uint256", "uint256", "uint128", "uint128"],
        bytes.fromhex(raw[2:]),
    )
    keys = ["nonce", "operator", "token0", "token1", "tick_spacing", "tick_lower", "tick_upper", "liquidity",
            "fee_growth0", "fee_growth1", "tokens_owed0", "tokens_owed1"]
    return {"found": True, **dict(zip(keys, fields))}


def collect_static_call(token_id: int) -> tuple[int, int]:
    """eth_call-симуляция collect() -- НЕ транзакция, ничего не отправляет
    и не меняет ончейн-состояние (тот же метод, что p5_live_position_snapshot.py:
    NFPM.collect() внутри делает pool.burn(tickLower,tickUpper,0) перед transfer,
    поэтому даёт РЕАЛЬНУЮ актуальную сумму комиссий, не устаревший tokensOwed)."""
    from eth_abi import decode as abi_decode, encode as abi_encode
    from eth_utils import to_checksum_address
    selector = bytes.fromhex(_selector("collect((uint256,address,uint128,uint128))")[2:])
    data = selector + abi_encode(
        ["(uint256,address,uint128,uint128)"],
        [(token_id, to_checksum_address(CONFIG["wallet"]), 2 ** 128 - 1, 2 ** 128 - 1)],
    )
    raw = rpc("eth_call", [{"to": to_checksum_address(CONFIG["nfpm_address"]),
                             "from": to_checksum_address(CONFIG["wallet"]), "data": "0x" + data.hex()}, "latest"])
    amount0, amount1 = abi_decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    return amount0, amount1


def read_pool_state() -> dict:
    slot0 = eth_call(CONFIG["pool_address"], "slot0()")
    liquidity = int(eth_call(CONFIG["pool_address"], "liquidity()"), 16)
    hexdata = slot0[2:]
    sqrt_price_x96 = int(hexdata[0:64], 16)
    tick_word = int(hexdata[64:128], 16)
    tick = tick_word - (1 << 256) if tick_word >= (1 << 255) else tick_word
    return {"sqrtPriceX96": sqrt_price_x96, "tick": tick, "liquidity_raw": liquidity}


def price_cbbtc_usd_from_sqrt(sqrt_price_x96: int) -> float:
    """token0=USDC/token1=cbBTC -- raw = (sqrtP/2^96)^2 даёт cbBTC-на-USDC
    (крошечное число, token1/token0 в сыром виде домножено на 10^(dec0-dec1)).
    Инвертируем в USD-цену cbBTC явно (та же логика, что p6_dry_run_entry.py,
    уже провалидирована реальными числами в этом проекте)."""
    raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    price_cbbtc_per_usdc = raw * (10 ** (CONFIG["token0_decimals"] - CONFIG["token1_decimals"]))
    return 1.0 / price_cbbtc_per_usdc


def price_from_tick_usd(tick: int) -> float:
    raw = (1.0001 ** tick) * (10 ** (CONFIG["token0_decimals"] - CONFIG["token1_decimals"]))
    return 1.0 / raw


def lighter_account_full() -> dict | None:
    r = requests.get(f"{CONFIG['lighter_api_base']}/api/v1/account",
                      params={"by": "index", "value": str(CONFIG["lighter_account_index"])}, timeout=20)
    r.raise_for_status()
    accounts = r.json().get("accounts", [])
    return accounts[0] if accounts else None


def lighter_market() -> dict | None:
    r = requests.get(f"{CONFIG['lighter_api_base']}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    r.raise_for_status()
    markets = r.json().get("order_book_details", [])
    exact = [m for m in markets if str(m.get("symbol", "")).upper() == CONFIG["lighter_market_symbol"]]
    return exact[0] if exact else None


def btc_price_usd_coingecko() -> float | None:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "bitcoin", "vs_currencies": "usd"},
                          headers={"User-Agent": "robinhood-chain-alpha-p6/1.0"}, timeout=20)
        r.raise_for_status()
        return float(r.json()["bitcoin"]["usd"])
    except Exception as exc:  # noqa: BLE001
        print(f"[p6_snapshot]   BTC/USD CoinGecko недоступен: {exc}")
        return None


def read_last_accrual_entry() -> dict | None:
    if not ACCRUAL_LOG_PATH.exists():
        return None
    last_line = None
    with ACCRUAL_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                last_line = line
    return json.loads(last_line) if last_line else None


def read_all_accrual_rows() -> list[dict]:
    if not ACCRUAL_LOG_PATH.exists():
        return []
    rows = []
    with ACCRUAL_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sigma_realized_annualized_from_series(rows: list[dict]) -> dict:
    """Тот же метод, что p5_live_position_snapshot.py::sigma_realized_annualized_from_series
    -- log-returns между соседними точками своего же ряда, квадратичная
    вариация / фактическое прошедшее время в годах. Честный null, пока
    точек <3 (нужно минимум 2 интервала)."""
    points = [(datetime.strptime(r["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), r["pool_price_cbbtc_usd"])
              for r in rows if r.get("pool_price_cbbtc_usd")]
    if len(points) < 3:
        return {"sigma_realized_annualized": None, "n_points": len(points), "note": "нужно минимум 3 точки ряда"}
    points.sort()
    sum_sq_log_ret = 0.0
    total_years = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        dt_years = (t1 - t0).total_seconds() / (365.25 * 24 * 3600)
        if dt_years <= 0 or p0 <= 0 or p1 <= 0:
            continue
        log_ret = math.log(p1 / p0)
        sum_sq_log_ret += log_ret ** 2
        total_years += dt_years
    if total_years <= 0:
        return {"sigma_realized_annualized": None, "n_points": len(points), "note": "нулевой суммарный интервал"}
    variance_annualized = sum_sq_log_ret / total_years
    return {"sigma_realized_annualized": math.sqrt(variance_annualized), "n_points": len(points)}


def compute_k_concentration(pool: dict, price_cbbtc_usd: float, tvl_usd: float | None) -> dict:
    """Тот же метод, что analysis/pool_screener_concentration.py::compute_k --
    k = L_active(реальная ончейн liquidity()) / L_full-range-эквивалент той
    же $-стоимости. Пересчитывается заново каждый снимок (реальные RPC,
    не кэш) -- концентрация других LP в диапазоне меняется со временем."""
    if not tvl_usd:
        return {"k": None, "note": "TVL пула недоступен в этом прогоне (GT) -- k не посчитан."}
    L_active_human = pool["liquidity_raw"] / (10 ** ((CONFIG["token0_decimals"] + CONFIG["token1_decimals"]) // 2))
    # full-range эквивалент: L_full = sqrt(reserve0_usd * reserve1_usd) в
    # "человеческой" L-шкале при равном 50/50 сплите TVL (симметричная
    # формула L=sqrt(x*y), не зависит от порядка token0/token1).
    half_tvl = tvl_usd / 2
    amount0_equiv = half_tvl  # USDC ~= $1
    amount1_equiv = half_tvl / price_cbbtc_usd
    L_full_human = math.sqrt(amount0_equiv * amount1_equiv) if amount0_equiv > 0 and amount1_equiv > 0 else None
    if not L_full_human:
        return {"k": None, "note": "L_full_human вычислить не удалось."}
    k = L_active_human / L_full_human
    return {"k": k, "L_active_human": L_active_human, "L_full_human": L_full_human, "tvl_usd_used": tvl_usd}


def fetch_gt_pool_tvl_soft() -> float | None:
    try:
        r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{CONFIG['gt_network']}/pools/{CONFIG['pool_address']}",
                          headers={"Accept": "application/json;version=20230302"}, timeout=20)
        if r.status_code != 200:
            return None
        return float(r.json()["data"]["attributes"]["reserve_in_usd"])
    except Exception:  # noqa: BLE001
        return None


def compute_basis_kill_flag(rows: list[dict], current_basis_pct: float | None) -> dict:
    """Тот же метод, что analysis/p6_basis_recompute.py -- суточный
    (календарные сутки UTC) базис, но здесь считается ИНКРЕМЕНТАЛЬНО из
    собственного растущего ряда (не 90-дневный внешний пересчёт заново
    каждый час). Простое (не объёмно-взвешенное) среднее за сутки --
    почасовой ряд P6 не хранит объём на каждую точку, честная
    аппроксимация, помечена явно."""
    by_day: dict[str, list[float]] = {}
    for r in rows:
        if r.get("basis_pct") is None:
            continue
        day = r["timestamp_utc"][:10]
        by_day.setdefault(day, []).append(r["basis_pct"])
    if current_basis_pct is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        by_day.setdefault(today, []).append(current_basis_pct)
    daily_avg = {d: sum(v) / len(v) for d, v in by_day.items()}
    days_sorted = sorted(daily_avg.keys())
    flags = [abs(daily_avg[d]) > KILL_3_BASIS_THRESHOLD_PCT for d in days_sorted]
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return {"method": "простое среднее по календарным суткам (не объёмно-взвешенное, аппроксимация)",
            "daily_avg_basis_pct": daily_avg, "max_consecutive_days_gt_2pct": best,
            "triggered": best >= KILL_3_MIN_CONSECUTIVE_DAYS}


def run() -> int:
    now_utc = datetime.now(timezone.utc)
    result: dict = {"generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}

    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else None
    if not state or not state.get("token_id"):
        result["found_position"] = False
        result["note"] = "data/p6_live_position_state.json не найден или без token_id -- позиции ещё нет, честный null (не крах)."
        print(f"[p6_snapshot] {result['note']}")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return 0

    token_id = state["token_id"]
    print(f"=== P6 снимок: tokenId={token_id} ===")
    pos = nfpm_position(token_id)
    if not pos.get("found") or pos.get("liquidity", 0) == 0:
        result["found_position"] = False
        result["note"] = f"positions({token_id}) не найдена или liquidity=0 -- честный null."
        print(f"[p6_snapshot] {result['note']}")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return 0
    result["found_position"] = True

    fees0_raw, fees1_raw = collect_static_call(token_id)
    fees0_usdc = fees0_raw / 10 ** CONFIG["token0_decimals"]
    fees1_cbbtc = fees1_raw / 10 ** CONFIG["token1_decimals"]

    pool = read_pool_state()
    pool_price_cbbtc_usd = price_cbbtc_usd_from_sqrt(pool["sqrtPriceX96"])
    fees_usd_unclaimed = fees0_usdc + fees1_cbbtc * pool_price_cbbtc_usd

    range_lower = price_from_tick_usd(pos["tick_upper"])  # выше tick -> ниже raw -> выше USD price (обратный порядок, см. p6_dry_run_entry.py)
    range_upper = price_from_tick_usd(pos["tick_lower"])
    in_range = pos["tick_lower"] <= pool["tick"] < pos["tick_upper"]
    print(f"[p6_snapshot] price=${pool_price_cbbtc_usd:.2f} диапазон=[${range_lower:.2f},${range_upper:.2f}] in_range={in_range}")

    account_full = lighter_account_full()
    market = lighter_market()
    btc_pos = None
    if account_full:
        btc_pos = next((p for p in account_full.get("positions", []) if str(p.get("symbol", "")).upper() == "BTC"
                         and abs(float(p.get("position", 0))) > 1e-9), None)
    hedge: dict = {"found": btc_pos is not None}
    net_delta_btc = None
    if btc_pos and account_full:
        size_btc = abs(float(btc_pos.get("position", 0)))
        avg_entry = float(btc_pos.get("avg_entry_price", 0))
        mark_now = float(market["mark_price"]) if market else None
        liq_price = float(btc_pos["liquidation_price"]) if btc_pos.get("liquidation_price") not in (None, "") else None
        collateral_usd = float(account_full.get("collateral", 0))
        available_usd = float(account_full.get("available_balance", 0))
        free_margin_pct = (available_usd / collateral_usd * 100) if collateral_usd else None
        leverage = 100.0 / float(btc_pos["initial_margin_fraction"]) if btc_pos.get("initial_margin_fraction") else None
        mmf = float(market["maintenance_margin_fraction"]) / 10000 if market and market.get("maintenance_margin_fraction") is not None else None
        p_liq_formula = ((collateral_usd + size_btc * avg_entry) / (size_btc * (1 + mmf))) if (mmf and size_btc) else None
        hedge.update({
            "size_btc": size_btc, "avg_entry_price_usd": avg_entry, "mark_price_now_usd": mark_now,
            "unrealized_pnl_usd": float(btc_pos.get("unrealized_pnl", 0)),
            "liquidation_price_exchange_usd": liq_price, "liquidation_price_formula_usd": p_liq_formula,
            "liquidation_formula_vs_exchange_diff": (p_liq_formula - liq_price) if (p_liq_formula and liq_price) else None,
            "collateral_usd": collateral_usd, "available_balance_usd": available_usd, "free_margin_pct": free_margin_pct,
            "leverage_confirmed": leverage,
            "total_funding_paid_out_usd": float(btc_pos["total_funding_paid_out"]) if btc_pos.get("total_funding_paid_out") not in (None, "") else None,
            "realized_pnl_usd": float(btc_pos["realized_pnl"]) if btc_pos.get("realized_pnl") not in (None, "") else None,
        })
        # LP-нога cbBTC требуемая сейчас (та же raw-sqrt формула, зажатая в диапазон)
        sqrt_p = pool["sqrtPriceX96"] / (2 ** 96)
        sqrt_pa = (1.0001 ** pos["tick_lower"]) ** 0.5
        sqrt_pb = (1.0001 ** pos["tick_upper"]) ** 0.5
        sqrt_p_clamped = min(max(sqrt_p, sqrt_pa), sqrt_pb)
        amount1_required_raw = max(pos["liquidity"] * (sqrt_p_clamped - sqrt_pa), 0.0)
        amount1_cbbtc_required_now = amount1_required_raw / 10 ** CONFIG["token1_decimals"]
        net_delta_btc = amount1_cbbtc_required_now - size_btc
        hedge["lp_cbbtc_required_now"] = amount1_cbbtc_required_now
        hedge["net_delta_btc"] = net_delta_btc
        print(f"[p6_snapshot] хедж: size={size_btc} BTC avg_entry=${avg_entry} liq(биржа)=${liq_price} "
              f"liq(формула)=${p_liq_formula} free_margin={free_margin_pct}% дельта={net_delta_btc}")
    result["hedge"] = hedge

    # --- базис cbBTC/BTC текущей точки ---
    btc_usd = btc_price_usd_coingecko()
    basis_pct = ((pool_price_cbbtc_usd / btc_usd - 1) * 100) if btc_usd else None

    rows = read_all_accrual_rows()
    sigma_info = sigma_realized_annualized_from_series(rows + [{"timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "pool_price_cbbtc_usd": pool_price_cbbtc_usd}])
    tvl_now = fetch_gt_pool_tvl_soft()
    sqrt_p2 = pool["sqrtPriceX96"] / (2 ** 96)
    sqrt_pa2 = (1.0001 ** pos["tick_lower"]) ** 0.5
    sqrt_pb2 = (1.0001 ** pos["tick_upper"]) ** 0.5
    sqrt_p2c = min(max(sqrt_p2, sqrt_pa2), sqrt_pb2)
    amount0_now = max(pos["liquidity"] * (1 / sqrt_p2c - 1 / sqrt_pb2), 0.0) / 10 ** CONFIG["token0_decimals"]
    amount1_now = max(pos["liquidity"] * (sqrt_p2c - sqrt_pa2), 0.0) / 10 ** CONFIG["token1_decimals"]
    our_reserve_usd_now = amount0_now + amount1_now * pool_price_cbbtc_usd

    k_info = compute_k_concentration(pool, pool_price_cbbtc_usd, tvl_now)
    sigma = sigma_info.get("sigma_realized_annualized")
    k_val = k_info.get("k")
    lvr_annualized_frac = (k_val * sigma ** 2 / 8) if (sigma is not None and k_val is not None) else None

    our_fees_usd_cum = fees_usd_unclaimed
    our_yield_cum = (our_fees_usd_cum / our_reserve_usd_now) if our_reserve_usd_now else None
    opened_at = datetime.fromisoformat(state["opened_at_utc"].replace("Z", "+00:00")) if state.get("opened_at_utc") else None
    hours_covered = (now_utc - opened_at).total_seconds() / 3600 if opened_at else None
    fee_apr_annualized = (our_yield_cum * 24 * 365 / hours_covered) if (our_yield_cum is not None and hours_covered) else None
    fee_lvr_ratio = (fee_apr_annualized / lvr_annualized_frac) if (fee_apr_annualized is not None and lvr_annualized_frac) else None

    # fee_capture_ratio_cumulative (владелец, П6_HEDGED_LP.md kill-критерий №2)
    # требует РЕАЛЬНОГО часового объёма пула (тот же метод, что
    # p5_live_position_snapshot.py::fetch_hourly_volume_usd_since) -- НЕ
    # реализовано в этой версии, честный null вместо приближения/выдумки.
    fee_capture_ratio_cumulative = None
    fee_capture_note = "TODO: требует реального часового объёма пула (fetch_hourly_volume_usd_since), не реализовано в этой версии -- честный null, не приближение."

    basis_kill = compute_basis_kill_flag(rows, basis_pct)

    kill_flags = {
        "kill_flag_1_fee_lvr_ratio_lt_3": (fee_lvr_ratio < KILL_1_RATIO_THRESHOLD) if fee_lvr_ratio is not None else None,
        "kill_flag_2_fee_capture_lt_0_4": (fee_capture_ratio_cumulative < KILL_2_FEE_CAPTURE_THRESHOLD) if fee_capture_ratio_cumulative is not None else None,
        "kill_flag_3_basis_persistent_gt_2pct": basis_kill["triggered"],
    }

    row = {
        "timestamp_utc": result["generated_at_utc"], "block": None, "token_id": token_id,
        "pool_price_cbbtc_usd": pool_price_cbbtc_usd, "in_range": in_range,
        "fees0_usdc": fees0_usdc, "fees1_cbbtc": fees1_cbbtc, "fees_usd_unclaimed": fees_usd_unclaimed,
        "our_reserve_usd": our_reserve_usd_now,
        "hedge_size_btc": hedge.get("size_btc"), "hedge_unrealized_pnl_usd": hedge.get("unrealized_pnl_usd"),
        "hedge_liquidation_price_usd": hedge.get("liquidation_price_exchange_usd"),
        "hedge_free_margin_pct": hedge.get("free_margin_pct"), "net_delta_btc": net_delta_btc,
        "btc_price_usd": btc_usd, "basis_pct": basis_pct,
        "sigma_realized_annualized": sigma, "k_concentration": k_val,
        "lvr_annualized_frac": lvr_annualized_frac, "fee_apr_annualized": fee_apr_annualized,
        "fee_lvr_ratio": fee_lvr_ratio, "fee_capture_ratio_cumulative": fee_capture_ratio_cumulative,
        "fee_capture_note": fee_capture_note,
        "basis_kill_detail": basis_kill,
        **kill_flags,
    }
    result["row"] = row
    result["kill_flags"] = kill_flags
    print(f"[p6_snapshot] kill_flags={kill_flags}")

    ACCRUAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ACCRUAL_LOG_PATH.open("a") as f:
        f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[p6_snapshot] записано в {ACCRUAL_LOG_PATH} и {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
