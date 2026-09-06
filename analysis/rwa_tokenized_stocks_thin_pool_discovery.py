#!/usr/bin/env python3
"""«Токенизированные акции на других сетях — тонкие пулы» (владелец,
2026-09-06). Цель — НЕ глубокая ликвидность, а её отсутствие: TVL
$50k-$500k при живом объёме, где нет маркет-мейкера. Универсум:
Backed/bTokens (Base/Arbitrum/Ethereum), xStocks (Solana), Dinari,
Ondo Global Markets ("Ondo Stocks").

Реальное обнаружение (WebSearch этой сессии, НЕ по памяти) дало
конкретные адреса/тикеры-конвенции по эмитентам:
  - Backed: префикс "b" (bCOIN, bTSLA, bNVDA, ...), ERC-20,
    Ethereum/Base/Arbitrum.
  - Dinari: суффикс ".d" (TSLA.d, AAPL.d, ...), ERC-20, Ethereum/
    Arbitrum/Base -- РЕАЛЬНАЯ находка по ходу: v1-токены (Arbitrum)
    похоже мигрируют/останавливаются ("AAPL.D tokens have stopped
    trading on all exchanges listed on CoinGecko", Dinari переходит на
    собственный L1 "Dinari Financial Network" на Avalanche) -- честно
    ПРОВЕРЯЕМ живой объём эмпирически, не полагаемся на факт
    существования адреса.
  - Ondo Stocks (ex-"Ondo Global Markets"): суффикс "on" (AAPLon,
    TSLAon, ...), ERC-20, Ethereum/BNB Chain/Solana.
  - xStocks: суффикс "x" (AAPLx, TSLAx, NVDAx, COINx, ...), SPL-токены,
    Solana (эмитент -- тот же Backed, партнёрство с Kraken).

Метод обнаружения пулов (два источника, ОБА реальные, без придумывания
адресов):
  1. Для токенов с уже известным реальным адресом -- GeckoTerminal
     `/networks/{network}/tokens/{address}/pools` (тот же метод, что
     `pool_screener_resolve_pools.py`).
  2. ДОПОЛНИТЕЛЬНО -- GeckoTerminal `/search/pools?query=<тикер>`
     (реальный публичный поиск, найден по факту -- проверяем в этом же
     прогоне, работает ли эмпирически, не предполагаем заранее) --
     ловит пулы для тикеров, чей точный адрес мы НЕ собрали вручную.

Фильтр "тонкий пул" (владелец, дословно): TVL $50k-$500k, живой
объём > 0. Fee tier -- реальное поле GT, где есть."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

GT_BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-rwa-thin-pools/1.0"}
MIN_REQUEST_INTERVAL_S = 2.6
OUT_PATH = Path("data/p3_guard_cache/rwa_tokenized_stocks_thin_pool_discovery_result.json")

TVL_MIN_USD = 50_000.0
TVL_MAX_USD = 500_000.0

# Реально найденные адреса (WebSearch, эта сессия -- см. докстринг). network -- реальный GT network slug.
KNOWN_TOKENS = [
    {"issuer": "backed", "symbol": "bCOIN", "network": "eth", "address": "0xbbcb0356bb9e6b3faa5cbf9e5f36185d53403ac9"},
    {"issuer": "backed", "symbol": "bCOIN", "network": "base", "address": "0xbbcb0356bb9e6b3faa5cbf9e5f36185d53403ac9"},
    {"issuer": "backed", "symbol": "bTSLA", "network": "eth", "address": "0x14A5f2872396802C3Cc8942A39Ab3E4118EE5038"},
    {"issuer": "backed", "symbol": "bNVDA", "network": "arbitrum", "address": "0xA34C5e0AbE843E10461E2C9586Ea03E55Dbcc495"},
    {"issuer": "dinari", "symbol": "TSLA.d", "network": "base", "address": "0x74Ed07d83999bC5DB0ffd850da0a6Bd782AbD39c"},
    {"issuer": "ondo", "symbol": "AAPLon", "network": "eth", "address": "0x14c3abf95cb9c93a8b82c1cdcb76d72cb87b2d4c"},
    {"issuer": "ondo", "symbol": "AAPLon", "network": "bsc", "address": "0x390a684ef9cade28a7ad0dfa61ab1eb3842618c4"},
    {"issuer": "xstocks", "symbol": "AAPLx", "network": "solana", "address": "XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp"},
    {"issuer": "xstocks", "symbol": "TSLAx", "network": "solana", "address": "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB"},
    {"issuer": "xstocks", "symbol": "NVDAx", "network": "solana", "address": "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh"},
    {"issuer": "xstocks", "symbol": "COINx", "network": "solana", "address": "Xs7ZdzSHLU9ftNJsii5fCeJhoRWSC32SQGzGQtePxNu"},
]

# Дополнительные реальные тикеры (найдены по факту существования эмитентом, точный адрес НЕ собран вручную --
# ищем через GT search, честно помечаем, если не находится).
SEARCH_ONLY_TICKERS = [
    ("backed", "bMSFT"), ("backed", "bGOOGL"), ("backed", "bGME"), ("backed", "bMSTR"), ("backed", "bCSPX"),
    ("dinari", "AAPL.d"), ("dinari", "NVDA.d"), ("dinari", "COIN.d"), ("dinari", "MSFT.d"), ("dinari", "GOOGL.d"),
    ("ondo", "TSLAon"), ("ondo", "NVDAon"), ("ondo", "COINon"), ("ondo", "MSTRon"), ("ondo", "GOOGLon"),
    ("xstocks", "MSTRx"), ("xstocks", "SPYx"), ("xstocks", "GOOGLx"), ("xstocks", "METAx"), ("xstocks", "AMZNx"),
]

_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _last_call + MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _get(url: str, params: dict | None = None) -> tuple[int, dict | str]:
    for attempt in range(3):
        _throttle()
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = r.text[:500]
        if r.status_code == 429 and attempt < 2:
            print(f"    429, жду 65с и повторяю")
            time.sleep(65)
            continue
        return r.status_code, body
    return r.status_code, body


def pools_for_token(network: str, address: str) -> list[dict]:
    status, body = _get(f"{GT_BASE}/networks/{network}/tokens/{address}/pools")
    if status != 200 or not isinstance(body, dict):
        return []
    out = []
    for p in body.get("data", []):
        attrs = p.get("attributes", {})
        out.append({
            "pool_address": attrs.get("address"), "name": attrs.get("name"),
            "reserve_usd": float(attrs["reserve_in_usd"]) if attrs.get("reserve_in_usd") else None,
            "volume_24h_usd": float(attrs["volume_usd"]["h24"]) if attrs.get("volume_usd", {}).get("h24") else None,
            "fee_tier_raw": attrs.get("pool_fee_percentage") or attrs.get("pool_created_at"),  # реальное поле, если есть -- см. вывод для проверки
            "dex": (p.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id"),
        })
    return out


def search_pools(query: str) -> list[dict]:
    status, body = _get(f"{GT_BASE}/search/pools", params={"query": query})
    if status != 200 or not isinstance(body, dict):
        return [{"error": f"HTTP {status}", "raw": str(body)[:300]}]
    out = []
    for p in body.get("data", []):
        attrs = p.get("attributes", {})
        out.append({
            "pool_address": attrs.get("address"), "name": attrs.get("name"),
            "network": (p.get("relationships", {}).get("network", {}).get("data", {}) or {}).get("id"),
            "reserve_usd": float(attrs["reserve_in_usd"]) if attrs.get("reserve_in_usd") else None,
            "volume_24h_usd": float(attrs["volume_usd"]["h24"]) if attrs.get("volume_usd", {}).get("h24") else None,
            "dex": (p.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id"),
        })
    return out


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "tvl_band_usd": [TVL_MIN_USD, TVL_MAX_USD], "known_tokens": {}, "search_only": {}}

    print("=== Часть 1: известные адреса -> реальные пулы GT ===")
    for t in KNOWN_TOKENS:
        key = f"{t['issuer']}:{t['symbol']}:{t['network']}"
        print(f"\n{key} ({t['address']})...")
        pools = pools_for_token(t["network"], t["address"])
        print(f"  реальных пулов найдено: {len(pools)}")
        for p in pools:
            print(f"    {p['dex']} {p['pool_address']}: TVL=${p['reserve_usd']}, vol24h=${p['volume_24h_usd']}")
        result["known_tokens"][key] = {**t, "pools": pools}

    print("\n=== Часть 2: поиск по тикеру (адрес не собран вручную) ===")
    for issuer, ticker in SEARCH_ONLY_TICKERS:
        key = f"{issuer}:{ticker}"
        print(f"\n{key}...")
        pools = search_pools(ticker)
        print(f"  результатов: {len(pools)}")
        for p in pools[:5]:
            if "error" in p:
                print(f"    ОШИБКА: {p['error']}")
            else:
                print(f"    {p.get('network')} {p['dex']} {p['pool_address']}: TVL=${p['reserve_usd']}, vol24h=${p['volume_24h_usd']}")
        result["search_only"][key] = pools

    # Честный сводный список "тонких" пулов (TVL в полосе, живой объём > 0) по ВСЕМ найденным пулам
    thin_pools = []
    for key, entry in result["known_tokens"].items():
        for p in entry["pools"]:
            if p.get("reserve_usd") and TVL_MIN_USD <= p["reserve_usd"] <= TVL_MAX_USD and (p.get("volume_24h_usd") or 0) > 0:
                thin_pools.append({"source_key": key, **p})
    for key, pools in result["search_only"].items():
        for p in pools:
            if "error" in p:
                continue
            if p.get("reserve_usd") and TVL_MIN_USD <= p["reserve_usd"] <= TVL_MAX_USD and (p.get("volume_24h_usd") or 0) > 0:
                thin_pools.append({"source_key": key, **p})
    result["thin_pools_in_band"] = thin_pools
    print(f"\n=== ИТОГО реальных тонких пулов в полосе ${TVL_MIN_USD:,.0f}-${TVL_MAX_USD:,.0f} с живым объёмом: {len(thin_pools)} ===")
    for p in thin_pools:
        print(f"  {p['source_key']}: {p.get('network','?')} {p['dex']} {p['pool_address']} TVL=${p['reserve_usd']:,.0f} vol24h=${p['volume_24h_usd']:,.0f}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[discovery] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
