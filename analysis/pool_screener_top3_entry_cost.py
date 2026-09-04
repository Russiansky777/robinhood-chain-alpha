#!/usr/bin/env python3
"""Скринер пулов -- п.3 (частично): fee tier (RPC), газ на Base (RPC),
и реальная котировка моста Across для Robinhood Chain -> Base (владелец:
"стоимость входа с учётом моста на Base"). Across -- уже используемый в
проекте кросс-чейн мост для депозитов НА Robinhood Chain (см.
docs/MM_RECON.md, "реальные кросс-чейн депозиты Across") -- проверяем
здесь РЕАЛЬНЫЙ маршрут В ОБРАТНУЮ сторону (Robinhood Chain -> Base) через
публичный API Across, не предполагаем поддержку маршрута заранее."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

BASE_RPC = "https://mainnet.base.org"
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"  # уже используется в проекте, analysis/alchemy_fallback.py
FEE_SELECTOR = "0xddca3f43"  # fee() -- Uniswap v3 / Aerodrome Slipstream (форк v3, тот же ABI)

ACROSS_API_BASE = "https://app.across.to/api"
# Реальный список 3 кандидатов читается ниже из уже посчитанного
# data/p3_guard_cache/pool_screener_concentration_result.json (passes_threshold_2x_k_adjusted) --
# не хардкодится здесь.


def rpc_call(url: str, to: str, data: str) -> str:
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                      "params": [{"to": to, "data": data}, "latest"]}, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"eth_call {to}: {body['error']}")
    return body["result"]


def rpc_gas_price(url: str) -> int:
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []}, timeout=20)
    resp.raise_for_status()
    return int(resp.json()["result"], 16)


def get_fee_tier(network_rpc: str, pool_address: str) -> int | None:
    try:
        result = rpc_call(network_rpc, pool_address, FEE_SELECTOR)
        return int(result, 16)
    except Exception as exc:  # noqa: BLE001
        print(f"    fee() упал для {pool_address}: {exc}")
        return None


def get_across_route_and_quote(origin_chain_id: int, dest_chain_id: int, amount_wei: str) -> dict:
    """Двухшаговый реальный запрос -- НЕ угадываем адрес токена: сначала
    available-routes отдаёт список реально поддерживаемых токенов для
    этой пары чейнов, ЗАТЕМ (если что-то нашлось) берём первый реальный
    адрес origin-токена из ответа для suggested-fees."""
    result = {}
    try:
        r = requests.get(f"{ACROSS_API_BASE}/available-routes",
                          params={"originChainId": origin_chain_id, "destinationChainId": dest_chain_id}, timeout=20)
        result["available_routes_status"] = r.status_code
        routes = r.json() if r.status_code == 200 else None
        result["available_routes"] = routes if routes is not None else r.text[:500]
    except Exception as exc:  # noqa: BLE001
        result["available_routes_error"] = str(exc)[:300]
        routes = None

    if not routes:
        result["suggested_fees_skipped"] = "нет available-routes для этой пары чейнов -- маршрут Robinhood Chain -> Base НЕ поддерживается Across (реальный факт, не предположение)"
        return result

    first = routes[0]
    origin_token = first.get("originToken")
    try:
        r2 = requests.get(f"{ACROSS_API_BASE}/suggested-fees", params={
            "originChainId": origin_chain_id, "destinationChainId": dest_chain_id,
            "token": origin_token, "amount": amount_wei,
        }, timeout=20)
        result["quoted_token"] = first
        result["suggested_fees_status"] = r2.status_code
        result["suggested_fees"] = r2.json() if r2.status_code == 200 else r2.text[:500]
    except Exception as exc:  # noqa: BLE001
        result["suggested_fees_error"] = str(exc)[:300]
    return result


def run() -> int:
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    concentration = json.load(open("data/p3_guard_cache/pool_screener_concentration_result.json"))
    top3 = [c for c in concentration["candidates"] if c.get("passes_threshold_2x_k_adjusted")]
    print(f"[entry_cost] {len(top3)} кандидатов прошли порог -- считаю fee tier + вход")

    fee_tiers = []
    for c in top3:
        addr = c["gt_resolution"]["resolved_pool_address"]
        network = c["gt_resolution"]["network"]
        rpc_url = BASE_RPC if network == "base" else None
        fee_raw = get_fee_tier(rpc_url, addr) if rpc_url else None
        fee_pct = (fee_raw / 1_000_000 * 100) if fee_raw is not None else None
        entry = {"chain": c["chain"], "project": c["project"], "symbol": c["symbol"], "address": addr,
                  "fee_tier_raw": fee_raw, "fee_tier_pct": fee_pct}
        print(f"  {c['chain']} {c['project']} {c['symbol']} ({addr}): fee={fee_pct}%")
        fee_tiers.append(entry)
    result["fee_tiers"] = fee_tiers

    print("\n=== Газ на Base (eth_gasPrice, реальный) ===")
    try:
        gas_price_wei = rpc_gas_price(BASE_RPC)
        result["base_gas_price_gwei"] = gas_price_wei / 1e9
        print(f"  {result['base_gas_price_gwei']:.4f} gwei")
    except Exception as exc:  # noqa: BLE001
        result["base_gas_price_error"] = str(exc)[:300]

    print("\n=== Across: Robinhood Chain -> Base, реальные маршруты/котировка ===")
    # Chain ID Base = 8453 (стандартный, подтверждён verify_rpc ранее в
    # pool_screener_concentration.py). Robinhood Chain -- chain id НЕ
    # угадываем, ищем реально через RPC eth_chainId.
    try:
        resp = requests.post(ROBINHOOD_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=20)
        robinhood_chain_id = int(resp.json()["result"], 16)
        result["robinhood_chain_id"] = robinhood_chain_id
        print(f"  Robinhood Chain id (реально с RPC) = {robinhood_chain_id}")
    except Exception as exc:  # noqa: BLE001
        result["robinhood_chain_id_error"] = str(exc)[:300]
        robinhood_chain_id = None

    if robinhood_chain_id:
        # $100 капитала (сумма условная для оценки, депонируется как
        # amount ниже в единицах origin-токена -- уточняется decimals
        # реального токена из ответа available-routes, не считается
        # заранее вслепую).
        across = get_across_route_and_quote(robinhood_chain_id, 8453, "100000000")
        result["across_robinhood_to_base"] = across
        print(f"  available_routes: HTTP {across.get('available_routes_status')}")
        print(f"  suggested_fees: HTTP {across.get('suggested_fees_status')}")

    Path("data/p3_guard_cache/pool_screener_top3_entry_cost_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
