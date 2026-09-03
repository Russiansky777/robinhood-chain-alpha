#!/usr/bin/env python3
"""Владелец, 2026-09-03: новое правило сессии -- "перед любым тяжёлым
прогоном по логам сначала проверить готовые данные в бесплатных
публичных API (GeckoTerminal, DEX Screener, DefiLlama, Uniswap
subgraph); ончейн-перебор -- только фолбэк". Приоритет №1 -- P5
(хеджированное LP ETH/USDG), приоритет №2 -- MM "только дешёвая часть"
(проверка адресов уже сделана в mm_pool_verify.py; здесь -- базис
токен/перп по тикерам, которые есть на Lighter).

Этот скрипт -- РАЗВЕДКА перед основным P5-бэктестом, НЕ сам бэктест:
  1. MM (дёшево, приоритет №2): базис пул-цена vs Lighter-марк для 5
     тикеров (NVDA/SPY/QQQ/GME/MSTR), у которых подтверждён и пул (см.
     data/p3_guard_cache/mm_pool_verify_result.json), и перп-рынок на
     Lighter.
  2. P5 (приоритет №1): верификация пула
     0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca (token0/token1/fee/
     decimals/ликвидность) + проба внешних бесплатных API за 30-дневной
     историей объёма/цены (GeckoTerminal, DexScreener, DefiLlama,
     Uniswap subgraph) -- что реально отдаёт данные для Robinhood Chain
     (chain id 4663), не предполагается заранее. Плюс короткая
     ончейн-калибровка (малый срез) для честной оценки объёма
     ончейн-фолбэка, если внешние API не сработают.

Только чтение, ключ не используется, транзакций нет.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
from eth_abi import decode as abi_decode  # noqa: E402

from alchemy_fallback import _chunked_get_logs, _rpc_call, get_block, get_block_number, topic0  # noqa: E402
from mm_liquidity_prefilter import eth_usd_price  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/mm_p5_setup_result.json")

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
P5_POOL = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca".lower()
LIGHTER_API_BASE = "https://mainnet.zklighter.elliot.ai"

BASIS_MARKETS = {  # symbol -> lighter market_id (data/p4_lighter_cache/p4_lighter_markets_result.json)
    "NVDA": 110, "SPY": 128, "QQQ": 129, "GME": 176, "MSTR": 122,
}

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n


def _eth_call(to: str, data: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"[mm_p5_setup]   eth_call {to} {data[:10]} не удался: {e}")
        return None


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def decimals_of(token: str) -> int | None:
    r = _eth_call(token, _selector("decimals()"))
    return int(r, 16) if r else None


def sqrt_price_to_usd(sqrt_price_x96: int, dec0: int, dec1: int, stock_is_token1: bool) -> float:
    """Цена стока в USD, USDG принят за ~$1 (см. mm_liquidity_prefilter.py).
    raw_price = (sqrtP/2^96)^2 = token1_raw за 1 token0_raw (Uniswap-конвенция)."""
    raw_price = (sqrt_price_x96 / (2 ** 96)) ** 2
    if stock_is_token1:
        # raw_price = stock_raw per USDG_raw -> USDG_per_stock = 1/raw_price
        return (1.0 / raw_price) * (10 ** (dec1 - dec0))
    else:
        # raw_price = USDG_raw per stock_raw
        return raw_price * (10 ** (dec0 - dec1))


def compute_mm_basis() -> dict:
    print("=== Часть 1 (MM, приоритет №2, дёшево): базис пул vs Lighter ===")
    pv = json.loads(Path("data/p3_guard_cache/mm_pool_verify_result.json").read_text())
    part_a = pv["part_a_pool_verification"]

    lighter_markets = {}
    try:
        resp = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
        resp.raise_for_status()
        for m in resp.json().get("order_book_details", []):
            lighter_markets[m.get("market_id")] = m
        print(f"[mm_p5_setup] Lighter orderBookDetails: {len(lighter_markets)} рынков")
    except Exception as e:  # noqa: BLE001
        print(f"[mm_p5_setup] Lighter orderBookDetails недоступен: {e}")

    usdg_decimals = decimals_of(USDG)
    print(f"[mm_p5_setup] USDG decimals = {usdg_decimals}")

    basis_out = {}
    for sym, market_id in BASIS_MARKETS.items():
        v = part_a.get(sym, {})
        if "v3_hypothesis" in v:
            info = v["v3_hypothesis"]
            currency0, currency1 = info["token0"], info["token1"]
        elif "currency0" in v:  # v4-confirmed (SPY/MSTR)
            info = v
            currency0, currency1 = info["currency0"], info["currency1"]
        else:
            basis_out[sym] = {"error": "нет подтверждённых данных пула из mm_pool_verify_result.json"}
            continue
        sqrt_price = info["sqrt_price_x96"]
        stock_is_token1 = currency1.lower() != USDG
        stock_addr = currency1 if stock_is_token1 else currency0
        stock_dec = decimals_of(stock_addr)
        if usdg_decimals is None or stock_dec is None:
            basis_out[sym] = {"error": "не удалось получить decimals()"}
            continue
        # currency0/currency1 упорядочены по адресу (Uniswap-конвенция) --
        # decimals должны соответствовать РЕАЛЬНОЙ роли (USDG или сток), не позиции:
        dec0 = usdg_decimals if currency0.lower() == USDG else stock_dec
        dec1 = usdg_decimals if currency1.lower() == USDG else stock_dec
        pool_price_usd = sqrt_price_to_usd(sqrt_price, dec0, dec1, stock_is_token1)

        market = lighter_markets.get(market_id)
        mark_price = float(market["mark_price"]) if market and market.get("mark_price") is not None else None
        entry = {
            "pool_price_usd": pool_price_usd, "lighter_mark_price_usd": mark_price,
            "lighter_market_id": market_id, "currency0": currency0, "currency1": currency1,
            "stock_decimals": stock_dec, "usdg_decimals": usdg_decimals,
        }
        if mark_price:
            entry["basis_usd"] = pool_price_usd - mark_price
            entry["basis_pct"] = (pool_price_usd - mark_price) / mark_price
        basis_out[sym] = entry
        print(f"[mm_p5_setup] {sym}: пул=${pool_price_usd:.4f} Lighter-марк=${mark_price} "
              f"базис={entry.get('basis_pct', 'н/д')}")
    return basis_out


def verify_p5_pool() -> dict:
    print("\n=== Часть 2 (P5, приоритет №1): верификация пула ===")
    tok0 = _eth_call(P5_POOL, _selector("token0()"))
    tok1 = _eth_call(P5_POOL, _selector("token1()"))
    fee_raw = _eth_call(P5_POOL, _selector("fee()"))
    liq_raw = _eth_call(P5_POOL, _selector("liquidity()"))
    slot0_raw = _eth_call(P5_POOL, _selector("slot0()"))
    result = {"pool": P5_POOL}
    if tok0 and tok1:
        result["token0"] = "0x" + tok0[-40:]
        result["token1"] = "0x" + tok1[-40:]
    if fee_raw:
        result["fee_bps_hundredths"] = int(fee_raw, 16)  # fee() -- в сотых долях бп (Uniswap V3: 500=0.05%, 100=0.01%)
    if liq_raw:
        result["liquidity_raw"] = int(liq_raw, 16)
    if slot0_raw:
        try:
            decoded = abi_decode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], bytes.fromhex(slot0_raw[2:]))
            result["sqrt_price_x96"] = decoded[0]
            result["tick"] = decoded[1]
        except Exception as e:  # noqa: BLE001
            print(f"[mm_p5_setup] slot0 decode не удался: {e}")
    if "token0" in result:
        result["token0_decimals"] = decimals_of(result["token0"])
    if "token1" in result:
        result["token1_decimals"] = decimals_of(result["token1"])
    print(f"[mm_p5_setup] P5 pool: {json.dumps(result, default=str)}")
    return result


def probe_external_apis() -> dict:
    print("\n=== Часть 3: пробa внешних бесплатных API (GeckoTerminal/DexScreener/DefiLlama) ===")
    out = {}

    # GeckoTerminal -- список сетей (ищем Robinhood Chain / chain 4663)
    try:
        r = requests.get("https://api.geckoterminal.com/api/v2/networks", params={"page": 1}, timeout=15,
                          headers={"Accept": "application/json;version=20230302"})
        out["geckoterminal_networks"] = {"status": r.status_code, "reachable": True}
        if r.ok:
            names = [n.get("id") for n in r.json().get("data", [])]
            out["geckoterminal_networks"]["sample_network_ids"] = names[:30]
            out["geckoterminal_networks"]["robinhood_like_found"] = [n for n in names if "robin" in (n or "").lower()]
        print(f"[mm_p5_setup] GeckoTerminal /networks: status={r.status_code}")
    except Exception as e:  # noqa: BLE001
        out["geckoterminal_networks"] = {"reachable": False, "error": str(e)}
        print(f"[mm_p5_setup] GeckoTerminal /networks недоступен: {e}")

    # DexScreener -- поиск по адресу пула (мультичейн, без привязки к network slug)
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search", params={"q": P5_POOL}, timeout=15)
        out["dexscreener_search"] = {"status": r.status_code, "reachable": True}
        if r.ok:
            body = r.json()
            out["dexscreener_search"]["n_pairs_found"] = len(body.get("pairs") or [])
            out["dexscreener_search"]["sample"] = (body.get("pairs") or [])[:2]
        print(f"[mm_p5_setup] DexScreener /search: status={r.status_code}")
    except Exception as e:  # noqa: BLE001
        out["dexscreener_search"] = {"reachable": False, "error": str(e)}
        print(f"[mm_p5_setup] DexScreener /search недоступен: {e}")

    # DefiLlama -- список поддерживаемых DEX-объёмных чейнов
    try:
        r = requests.get("https://api.llama.fi/overview/dexs", timeout=20)
        out["defillama_dexs_overview"] = {"status": r.status_code, "reachable": True}
        if r.ok:
            body = r.json()
            chains = body.get("allChains") or []
            out["defillama_dexs_overview"]["robinhood_like_found"] = [c for c in chains if "robin" in c.lower()]
            out["defillama_dexs_overview"]["n_chains_total"] = len(chains)
        print(f"[mm_p5_setup] DefiLlama /overview/dexs: status={r.status_code}")
    except Exception as e:  # noqa: BLE001
        out["defillama_dexs_overview"] = {"reachable": False, "error": str(e)}
        print(f"[mm_p5_setup] DefiLlama /overview/dexs недоступен: {e}")

    # Uniswap официальный subgraph (The Graph gateway) -- список известных деплойментов
    try:
        r = requests.get("https://raw.githubusercontent.com/Uniswap/interface/main/apps/web/src/graphql/thegraph/apollo.ts",
                          timeout=15)
        out["uniswap_subgraph_source_check"] = {"status": r.status_code, "reachable": True,
                                                  "note": "проверка списка chainId->subgraph URL в исходнике интерфейса Uniswap, не сам subgraph API"}
        if r.ok:
            out["uniswap_subgraph_source_check"]["mentions_4663_or_robinhood"] = ("4663" in r.text) or ("obinhood" in r.text)
        print(f"[mm_p5_setup] Uniswap interface source (chainId map): status={r.status_code}")
    except Exception as e:  # noqa: BLE001
        out["uniswap_subgraph_source_check"] = {"reachable": False, "error": str(e)}
        print(f"[mm_p5_setup] Uniswap interface source недоступен: {e}")

    return out


def calibrate_onchain_fallback() -> dict:
    print("\n=== Часть 4: короткая ончейн-калибровка (фолбэк, если внешние API не отдали данные) ===")
    latest = get_block_number()
    _count()
    lookback = 5_000  # короткий срез -- только чтобы измерить плотность, не покрыть 30 дней
    from_block = max(1, latest - lookback)
    t_latest = int(get_block(latest)["timestamp"], 16)
    _count()
    t_early = int(get_block(from_block)["timestamp"], 16)
    _count()
    dt = max(1, t_latest - t_early)

    swap_topic0 = topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")
    n_calls = 0
    logs = list(_chunked_get_logs(from_block, latest, [swap_topic0], chunk_size=2000, address=P5_POOL,
                                   on_call=lambda lo, hi, n: None))
    n_calls = max(1, (latest - from_block) // 2000 + 1)
    _count(n_calls)

    swaps_per_hour = len(logs) / (dt / 3600)
    window_s = 30 * 86400
    projected_swaps = swaps_per_hour * (window_s / 3600)
    projected_calls = n_calls * (window_s / dt)
    result = {
        "calibration_window_blocks": [from_block, latest], "calibration_window_s": dt,
        "n_swaps_in_calibration": len(logs), "swaps_per_hour": swaps_per_hour,
        "projected_swaps_30d": projected_swaps, "projected_getlogs_calls_30d": projected_calls,
        "projected_minutes_30d_at_45_per_min": projected_calls / 45,
    }
    print(f"[mm_p5_setup] калибровка: {len(logs)} свопов за {dt}с ({swaps_per_hour:.1f}/ч) -> "
          f"оценка 30д: ~{projected_swaps:.0f} свопов, ~{projected_calls:.0f} вызовов, "
          f"~{result['projected_minutes_30d_at_45_per_min']:.0f} мин")
    return result


def run() -> int:
    t0 = time.time()
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    result["mm_basis"] = compute_mm_basis()
    result["p5_pool_verification"] = verify_p5_pool()
    result["external_api_probe"] = probe_external_apis()
    result["onchain_fallback_calibration"] = calibrate_onchain_fallback()
    result["eth_usd_price"], result["eth_usd_source"] = eth_usd_price()
    result["requests_used"] = _request_count
    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[mm_p5_setup] записано {OUT_PATH}, {_request_count} запросов, {time.time()-t0:.0f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
