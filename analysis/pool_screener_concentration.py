#!/usr/bin/env python3
"""Скринер пулов -- поправка на концентрацию (владелец, 2026-09-04, п.2).

Для каждого пула читаем РЕАЛЬНОЕ ончейн-состояние (RPC eth_call, не GT/
DefiLlama -- liquidity()/slot0() не публикуются агрегаторами):
  - slot0() -> sqrtPriceX96, tick (Uniswap v3 / Aerodrome Slipstream --
    тот же ABI, Slipstream -- форк v3)
  - liquidity() -> L_raw (текущая АКТИВНАЯ ликвидность у тика, ВСЕ LP
    вместе, не наша)
  - decimals() обоих токенов пары (не берём из памяти -- ERC20 decimals
    формально не гарантированы стандартом, читаем реально)

Формула k (степень концентрации относительно full-range той же
стоимости):
  L_active_human = L_raw / 10**((dec0+dec1)/2)   -- стандартная
    конвертация raw->human для Uniswap v3 liquidity (та же, что
    L_HUMAN_DIVISOR в analysis/p5_live_position_snapshot.py)
  L_full_human = sqrt((TVL_usd/2/price0_usd) * (TVL_usd/2/price1_usd))
    -- ликвидность гипотетической full-range позиции ТОЙ ЖЕ стоимости
    (геометрическое среднее amount0*amount1 при 50/50 сплите по
    стоимости -- инвариант amount0*amount1=L^2 для ЛЮБОЙ full-range
    точки, sqrtPriceX96 не нужен в этой формуле явно)
  k = L_active_human / L_full_human

sqrtPriceX96 используется ОТДЕЛЬНО как сверка (не для k): считаем цену
из него (с реальными decimals) и сравниваем с price0_usd/price1_usd из
GT -- расхождение больше чем на пару % сигнализирует об ошибке в
decimals/адресах, а не игнорируется молча.

LVR_pool = k * sigma^2/8 (вместо full-range sigma^2/8) -- пересчитываем
ratio = apyBase/LVR_pool, порог >=2 остаётся.

uniswap-v4: пул -- НЕ отдельный контракт (singleton PoolManager,
"адрес" от GT -- bytes32 poolId, не адрес слота0/liquidity в привычном
виде) -- ЭТОТ метод здесь не применяется, помечается явно, не
угадывается по аналогии с v3.
aerodrome-v1: классический (некоцентрированный) AMM -- k=1 по
определению, RPC не нужен."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import requests

RPC_ENDPOINTS = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "bsc": "https://bsc-dataseed.binance.org/",
}
EXPECTED_CHAIN_ID = {"base": 8453, "arbitrum": 42161, "bsc": 56}

GT_BASE = "https://api.geckoterminal.com/api/v2"
MIN_REQUEST_INTERVAL_S = 2.6
HEADERS_GT = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-screener/1.0"}

SLOT0_SELECTOR = "0x3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"
DECIMALS_SELECTOR = "0x313ce567"

_last_gt_call = 0.0
_decimals_cache: dict[str, int] = {}


def _throttle_gt() -> None:
    global _last_gt_call
    wait = _last_gt_call + MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_gt_call = time.monotonic()


def rpc_call(network: str, to: str, data: str) -> str:
    url = RPC_ENDPOINTS[network]
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                      "params": [{"to": to, "data": data}, "latest"]}, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"eth_call {to} {data}: {body['error']}")
    return body["result"]


def verify_rpc(network: str) -> None:
    url = RPC_ENDPOINTS[network]
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=20)
    resp.raise_for_status()
    chain_id = int(resp.json()["result"], 16)
    expected = EXPECTED_CHAIN_ID[network]
    if chain_id != expected:
        raise RuntimeError(f"RPC {url} вернул chainId={chain_id}, ожидался {expected} для '{network}'")
    print(f"[concentration] RPC {network} ({url}) подтверждён: chainId={chain_id}")


def get_decimals(network: str, token_address: str) -> int:
    key = f"{network}:{token_address.lower()}"
    if key in _decimals_cache:
        return _decimals_cache[key]
    result = rpc_call(network, token_address, DECIMALS_SELECTOR)
    dec = int(result, 16)
    _decimals_cache[key] = dec
    return dec


def get_slot0_and_liquidity(network: str, pool_address: str) -> dict:
    slot0_raw = rpc_call(network, pool_address, SLOT0_SELECTOR)
    liquidity_raw = rpc_call(network, pool_address, LIQUIDITY_SELECTOR)
    hexdata = slot0_raw[2:]
    sqrt_price_x96 = int(hexdata[0:64], 16)
    # int24 tick -- Solidity sign-extends ЛЮБОЙ signed-return к полному
    # 256-битному слову перед кодированием, не только к 24 битам -- decode
    # как знаковое 256-битное число (стандартный ABI-паттерн), НЕ как
    # знаковое 24-битное (та ошибка дала бы огромное неверное число для
    # отрицательных тиков, которых в v3-пулах большинство).
    tick_word = int(hexdata[64:128], 16)
    tick = tick_word - (1 << 256) if tick_word >= (1 << 255) else tick_word
    liquidity = int(liquidity_raw, 16)
    return {"sqrtPriceX96": sqrt_price_x96, "tick": tick, "liquidity_raw": liquidity}


def get_gt_pool_prices(network: str, pool_address: str) -> dict:
    _throttle_gt()
    resp = requests.get(f"{GT_BASE}/networks/{network}/pools/{pool_address}", headers=HEADERS_GT, timeout=30)
    resp.raise_for_status()
    attrs = resp.json().get("data", {}).get("attributes", {})
    return {
        "base_token_price_usd": float(attrs["base_token_price_usd"]) if attrs.get("base_token_price_usd") else None,
        "quote_token_price_usd": float(attrs["quote_token_price_usd"]) if attrs.get("quote_token_price_usd") else None,
        "reserve_in_usd": float(attrs["reserve_in_usd"]) if attrs.get("reserve_in_usd") else None,
    }


def compute_k(candidate: dict) -> dict:
    project = candidate["project"]
    r = {"k": None, "error": None, "method": None}

    if project == "aerodrome-v1":
        r["k"] = 1.0
        r["method"] = "classic AMM (некоцентрированный) -- k=1 по определению, RPC не требуется"
        return r

    if project == "uniswap-v4":
        r["error"] = "Uniswap v4 -- singleton PoolManager, адрес от GT это poolId (bytes32), не контракт с slot0()/liquidity() -- метод не применяется"
        return r

    network = candidate["gt_resolution"]["network"]
    address = candidate["gt_resolution"]["resolved_pool_address"]
    tokens = candidate.get("underlying_tokens") or []
    if len(tokens) != 2:
        r["error"] = "нет пары underlying_tokens"
        return r
    token0, token1 = tokens

    try:
        dec0 = get_decimals(network, token0)
        dec1 = get_decimals(network, token1)
        onchain = get_slot0_and_liquidity(network, address)
        gt_prices = get_gt_pool_prices(network, address)
    except Exception as exc:  # noqa: BLE001
        r["error"] = f"RPC/GT вызов упал: {str(exc)[:300]}"
        return r

    L_raw = onchain["liquidity_raw"]
    L_active_human = L_raw / (10 ** ((dec0 + dec1) / 2))

    price0_usd = gt_prices["base_token_price_usd"]
    price1_usd = gt_prices["quote_token_price_usd"]
    tvl_usd = gt_prices["reserve_in_usd"] or candidate["tvl_usd"]
    if not price0_usd or not price1_usd or not tvl_usd:
        r["error"] = f"нет цен/TVL от GT для L_full (price0={price0_usd}, price1={price1_usd}, tvl={tvl_usd})"
        r["onchain_raw"] = onchain
        return r

    amount0_full_human = (tvl_usd / 2) / price0_usd
    amount1_full_human = (tvl_usd / 2) / price1_usd
    L_full_human = math.sqrt(amount0_full_human * amount1_full_human)

    k = L_active_human / L_full_human if L_full_human else None

    # Сверка sqrtPriceX96 -> цена -- НЕ используется в формуле k, только диагностика.
    price_raw = (onchain["sqrtPriceX96"] / (2 ** 96)) ** 2
    price_human_token0_in_token1 = price_raw * (10 ** (dec0 - dec1))
    price_ratio_from_gt = price0_usd / price1_usd if price1_usd else None
    price_check_deviation_pct = (
        abs(price_human_token0_in_token1 / price_ratio_from_gt - 1) * 100
        if price_ratio_from_gt else None
    )

    r.update({
        "k": k, "method": "uniswap-v3-style RPC (slot0+liquidity)",
        "L_active_human": L_active_human, "L_full_human": L_full_human,
        "decimals0": dec0, "decimals1": dec1, "liquidity_raw": L_raw,
        "sqrtPriceX96": onchain["sqrtPriceX96"], "tick": onchain["tick"],
        "price0_usd_gt": price0_usd, "price1_usd_gt": price1_usd, "tvl_usd_used": tvl_usd,
        "price_from_sqrtPriceX96_token0_in_token1": price_human_token0_in_token1,
        "price_check_deviation_pct": price_check_deviation_pct,
        "price_check_suspect": (price_check_deviation_pct is not None and price_check_deviation_pct > 5.0),
    })
    return r


def run() -> int:
    for net in RPC_ENDPOINTS:
        verify_rpc(net)

    d = json.load(open("data/p3_guard_cache/pool_screener_sigma_lvr_result.json"))
    results = []
    for i, c in enumerate(d["candidates"]):
        print(f"\n=== {i+1}/{len(d['candidates'])}: {c['chain']} {c['project']} {c['symbol']} ===")
        conc = compute_k(c)
        print(f"    k={conc.get('k')}, error={conc.get('error')}, price_check_suspect={conc.get('price_check_suspect')}")

        entry = {**c, "concentration": conc}
        sigma = c.get("sigma_realized_annualized")
        apy_base = c.get("apyBase") or 0.0
        if conc.get("k") is not None and sigma is not None:
            lvr_pool_frac = conc["k"] * (sigma ** 2) / 8
            entry["lvr_pool_k_adjusted_frac"] = lvr_pool_frac
            entry["ratio_k_adjusted"] = (apy_base / 100.0) / lvr_pool_frac if lvr_pool_frac else None
            entry["passes_threshold_2x_k_adjusted"] = (
                entry["ratio_k_adjusted"] is not None and entry["ratio_k_adjusted"] >= 2.0
            )
        results.append(entry)

    ok = [r for r in results if r.get("ratio_k_adjusted") is not None]
    ok.sort(key=lambda r: -r["ratio_k_adjusted"])
    n_pass = sum(1 for r in ok if r["passes_threshold_2x_k_adjusted"])
    n_error = sum(1 for r in results if r["concentration"].get("error"))

    print(f"\n[concentration] k посчитан для {len(ok)}/{len(results)}, ошибок/не применимо: {n_error}")
    print(f"[concentration] проходят порог >=2 С УЧЁТОМ k: {n_pass}")
    print("\n=== Отсортировано по ratio_k_adjusted (убывание) ===")
    for r in ok:
        print(f"  {r['chain']:10} {r['project']:22} {r['symbol']:22} k={r['concentration']['k']:.2f} "
              f"ratio_full_range={r.get('apy_base_over_lvr_full_range'):.2f} ratio_k_adj={r['ratio_k_adjusted']:.2f} "
              f"pass={r['passes_threshold_2x_k_adjusted']}")

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_total": len(results), "n_k_computed": len(ok), "n_error_or_na": n_error,
        "n_pass_threshold_2x_k_adjusted": n_pass,
        "candidates": results,
    }
    Path("data/p3_guard_cache/pool_screener_concentration_result.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
