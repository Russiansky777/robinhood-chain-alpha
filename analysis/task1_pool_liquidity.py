#!/usr/bin/env python3
"""Задача 1, общий модуль для проверки 1 и проверки 4 (владелец,
2026-09-05): по тикеру -- реальный адрес v3-пула, живой fee tier
(eth_call), TVL (GeckoTerminal `reserve_in_usd`, ТЕКУЩИЙ снимок --
явно помечено, не историческое значение на момент конкретного окна),
round-trip издержка на $500 = fee*2 + проскальзывание по резервам.

v3-подмножество -- ТОЛЬКО project=='uniswap' AND version=='3' (тот же
ABI fee()/slot0(), что уже проверено в pool_screener_concentration.py/
pool_screener_top3_entry_cost.py). Uniswap v4 (version='4') -- адрес
из dex.trades это singleton PoolManager, НЕ per-pair пул (см. §7/баг
из P3, task1_liquidity_probe2_result.json) -- "не оценено", не
угадывается. Другие протоколы (ramsesxyz/uponrh/gigadex) -- ABI не
проверен в этой сессии -- тоже "не оценено", не угадывается по
аналогии.

Формула проскальзывания (владелец не дал точную формулу, кроме "по
резервам" -- используется СТАНДАРТНОЕ, явно задокументированное
приближение для constant-product-подобного пула, ЧЕСТНО помечено как
приближение): при TVL=reserve_in_usd (обе стороны вместе), одна
сторона реально в резерве ~= TVL/2 (предположение 50/50 по стоимости,
типичное для этих AMM); проскальзывание ОДНОЙ ноги round-trip для
сделки $500 = 500 / (TVL/2); round-trip (вход+выход) = 2x эта величина
(симметричное приближение, не полная v3-формула с тиками -- v3
концентрированная ликвидность может давать МЕНЬШЕ проскальзывания
вблизи текущей цены, чем эта грубая constant-product-оценка, поэтому
получаемая цифра, скорее, ВЕРХНЯЯ граница издержки, не точная)."""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

FEE_SELECTOR = "0xddca3f43"  # fee() -- Uniswap v3 (подтверждено pool_screener_top3_entry_cost.py)
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"  # публичный, без ключа (config.py PUBLIC_RPC_URL)
RPC_MIN_INTERVAL_S = 0.4  # ~3 запроса/с по факту (config.py, найдено 2026-09-01)
GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_NETWORK = "robinhood"
GT_MIN_INTERVAL_S = 2.6
TRADE_SIZE_USD = 500.0
TVL_MIN_USD = 200_000.0
HEADERS_GT = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-liquidity/1.0"}
HEADERS_RPC = {"User-Agent": "robinhood-chain-alpha-liquidity/1.0"}

_last_rpc_call = 0.0
_last_gt_call = 0.0


def _throttle_rpc() -> None:
    global _last_rpc_call
    wait = _last_rpc_call + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_rpc_call = time.monotonic()


def _throttle_gt() -> None:
    global _last_gt_call
    wait = _last_gt_call + GT_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_gt_call = time.monotonic()


def eth_call(to: str, data: str) -> str:
    _throttle_rpc()
    r = requests.post(ROBINHOOD_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                            "params": [{"to": to, "data": data}, "latest"]},
                       headers=HEADERS_RPC, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"eth_call {to} {data}: {body['error']}")
    return body["result"]


def read_fee_bps(pool_address: str) -> int:
    """fee() -- Uniswap v3 возвращает uint24 в сотых долях бипса
    (3000 = 0.3% = 30 бипс)."""
    raw = eth_call(pool_address, FEE_SELECTOR)
    return int(raw, 16)


def get_gt_reserve_usd(pool_address: str) -> float | None:
    _throttle_gt()
    r = requests.get(f"{GT_BASE}/networks/{GT_NETWORK}/pools/{pool_address}", headers=HEADERS_GT, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    attrs = r.json().get("data", {}).get("attributes", {})
    v = attrs.get("reserve_in_usd")
    return float(v) if v else None


def round_trip_cost_pct(fee_bps: int, reserve_usd: float, trade_usd: float = TRADE_SIZE_USD) -> dict:
    fee_frac = fee_bps / 1_000_000  # fee() -- сотые доли бипса: 3000 -> 0.003 (0.3%)
    fee_round_trip_pct = 2 * fee_frac * 100
    half_reserve = reserve_usd / 2
    slippage_one_leg_pct = (trade_usd / half_reserve) * 100 if half_reserve > 0 else None
    slippage_round_trip_pct = 2 * slippage_one_leg_pct if slippage_one_leg_pct is not None else None
    total_pct = (fee_round_trip_pct + slippage_round_trip_pct) if slippage_round_trip_pct is not None else None
    return {
        "fee_bps": fee_bps, "fee_round_trip_pct": fee_round_trip_pct,
        "slippage_round_trip_pct": slippage_round_trip_pct,
        "round_trip_cost_pct": total_pct,
        "round_trip_cost_usd_on_500": (total_pct / 100 * trade_usd) if total_pct is not None else None,
    }


def find_v3_pool(symbol: str, pool_addresses_df: pd.DataFrame) -> dict | None:
    """Реюз кэша task1_pool_addresses_by_token (task1_liquidity_probe2.py)
    -- строго project=='uniswap' AND version=='3', если несколько --
    выбираем максимальный total_vol_usd (суммируем обе стороны сделки:
    строки symbol->quote и quote->symbol относятся к ОДНОМУ пулу, тот
    же pool_address_hex)."""
    sub = pool_addresses_df[
        ((pool_addresses_df["token_bought_symbol"] == symbol) | (pool_addresses_df["token_sold_symbol"] == symbol))
        & (pool_addresses_df["project"] == "uniswap") & (pool_addresses_df["version"].astype(str) == "3")
    ]
    if not len(sub):
        return None
    by_pool = sub.groupby("pool_address_hex")["total_vol_usd"].sum().sort_values(ascending=False)
    best_pool = by_pool.index[0]
    other_symbol = None
    row = sub[sub["pool_address_hex"] == best_pool].iloc[0]
    other_symbol = row["token_sold_symbol"] if row["token_bought_symbol"] == symbol else row["token_bought_symbol"]
    return {"pool_address_hex": best_pool, "quote_symbol": other_symbol, "n_alt_v3_pools": len(by_pool) - 1}


def evaluate_symbol_liquidity(symbol: str, pool_addresses_df: pd.DataFrame) -> dict:
    """Полная оценка по одному тикеру -- реальный адрес, живой fee,
    живой TVL с GT, round-trip издержка. НЕ ОЦЕНЕНО (explicit), если
    пул не v3 (v4/другой протокол) -- НЕ угадываем ABI/резервы."""
    pool = find_v3_pool(symbol, pool_addresses_df)
    if pool is None:
        return {"symbol": symbol, "status": "не оценено (нет v3-пула по этому тикеру -- только v4/другой протокол/не найден)"}
    addr = "0x" + pool["pool_address_hex"].lower()
    try:
        fee_bps = read_fee_bps(addr)
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "status": f"не оценено (ошибка чтения fee() с {addr}: {str(exc)[:200]})", "pool_address": addr}
    try:
        reserve_usd = get_gt_reserve_usd(addr)
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "status": f"не оценено (ошибка GT для {addr}: {str(exc)[:200]})", "pool_address": addr, "fee_bps": fee_bps}
    if reserve_usd is None:
        return {"symbol": symbol, "status": "не оценено (GT не вернул reserve_in_usd для этого пула)",
                "pool_address": addr, "fee_bps": fee_bps}
    cost = round_trip_cost_pct(fee_bps, reserve_usd)
    tvl_ok = reserve_usd > TVL_MIN_USD
    return {
        "symbol": symbol, "status": "оценено", "pool_address": addr, "quote_symbol": pool["quote_symbol"],
        "n_alt_v3_pools": pool["n_alt_v3_pools"],
        "tvl_usd_now": reserve_usd, "tvl_ok_gt_200k": tvl_ok, "fee_bps": fee_bps,
        **cost,
    }
