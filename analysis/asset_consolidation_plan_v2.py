#!/usr/bin/env python3
"""Консолидация активов в USDG -- ШАГ 2, план v2 (владелец, 2026-09-05,
обход блокера Across USDC->USDG): "Base cbBTC -> WETH и USDC -> WETH
(Uniswap v3 на Base, самые ликвидные пулы, котировку показать) ->
Across WETH->WETH Base->Robinhood (маршрут подтверждён при разведке
P6, котировку на фактическую сумму показать) -> Robinhood WETH->USDG
на 0x52e65B17... вместе с уже лежащими 0.0607 WETH одним свопом. ETH
на Robinhood: оставить $0.50, остальное обернуть и в тот же своп."

ТОЛЬКО чтение/котировки -- НИ ОДНОЙ транзакции не отправляется.

Реюз: `p6_entry_recon_result.json` подтвердил маршрут Robinhood->Base
WETH/ETH (`0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` ->
`0x4200000000000000000000000000000000000006`, Base канонический
WETH-предеплой) -- ОБРАТНОЕ направление (Base->Robinhood) здесь
проверяется ОТДЕЛЬНО через available-routes, симметрия НЕ
предполагается (тот же урок, что реальный сбой USDC->USDG)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
DRYRUN_PATH = Path("data/p3_guard_cache/asset_consolidation_dryrun_result.json")
OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_plan_v2_result.json")

FEE_SELECTOR = "0xddca3f43"  # fee() -- Uniswap v3 (подтверждено в проекте)
BALANCE_OF_SELECTOR = "0x70a08231"

BASE_RPC = "https://mainnet.base.org"
BASE_CHAIN_ID = 8453
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_CHAIN_ID = 4663

WETH_BASE = "0x4200000000000000000000000000000000000006"  # канонический предеплой Base, подтверждено p6_entry_recon_result.json
CBBTC_BASE = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_ROBINHOOD = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG_ROBINHOOD = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
P5_POOL_ROBINHOOD = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca"

USDG_DECIMALS = 6
CBBTC_DECIMALS = 8
USDC_DECIMALS = 6
WETH_DECIMALS = 18

ETH_RESERVE_USD = 0.50
ASSUMED_GAS_PER_TX = 150_000

GT_BASE = "https://api.geckoterminal.com/api/v2"
HEADERS_GT = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-consolidation/1.0"}
GT_MIN_INTERVAL_S = 2.6
_last_gt_call = 0.0


def _throttle_gt() -> None:
    global _last_gt_call
    wait = _last_gt_call + GT_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_gt_call = time.monotonic()


def rpc_call(rpc_url: str, method: str, params: list):
    r = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def read_fee_bps(rpc_url: str, pool_address: str) -> int:
    raw = rpc_call(rpc_url, "eth_call", [{"to": pool_address, "data": FEE_SELECTOR}, "latest"])
    return int(raw, 16)


def get_erc20_balance(rpc_url: str, token_address: str) -> int:
    padded = WALLET[2:].lower().rjust(64, "0")
    data = BALANCE_OF_SELECTOR + padded
    raw = rpc_call(rpc_url, "eth_call", [{"to": token_address, "data": data}, "latest"])
    return int(raw, 16)


def find_best_pool_vs_weth(network: str, token_address: str) -> dict | None:
    """Самый ликвидный пул token/WETH -- реюз метода
    pool_screener_resolve_pools.py::resolve_pool (GT /tokens/{addr}/pools,
    максимум reserve_in_usd среди пар с WETH)."""
    _throttle_gt()
    r = requests.get(f"{GT_BASE}/networks/{network}/tokens/{token_address}/pools", headers=HEADERS_GT, timeout=30)
    if r.status_code != 200:
        return None
    data = r.json().get("data", [])
    weth_lower = WETH_BASE.lower()
    best = None
    for pool in data:
        attrs = pool.get("attributes", {})
        rel = pool.get("relationships", {})
        base_addr = (rel.get("base_token", {}).get("data", {}).get("id", "") or "").split("_")[-1].lower()
        quote_addr = (rel.get("quote_token", {}).get("data", {}).get("id", "") or "").split("_")[-1].lower()
        if weth_lower not in (base_addr, quote_addr):
            continue
        reserve = float(attrs.get("reserve_in_usd") or 0)
        if best is None or reserve > best["reserve_usd"]:
            best = {"address": attrs.get("address"), "reserve_usd": reserve, "name": attrs.get("name"),
                    "base_token_address": base_addr, "quote_token_address": quote_addr,
                    "target_price_usd": (float(attrs["base_token_price_usd"]) if base_addr == token_address.lower() and attrs.get("base_token_price_usd")
                                          else (float(attrs["quote_token_price_usd"]) if quote_addr == token_address.lower() and attrs.get("quote_token_price_usd") else None))}
    return best


def across_quote_checked(origin_chain_id: int, dest_chain_id: int, origin_token: str, amount: str, symbol: str) -> dict:
    routes_r = requests.get("https://app.across.to/api/available-routes",
                             params={"originChainId": origin_chain_id, "destinationChainId": dest_chain_id}, timeout=20)
    if routes_r.status_code != 200:
        return {"error": f"available-routes HTTP {routes_r.status_code}"}
    routes = routes_r.json()
    match = next((x for x in routes if x.get("originToken", "").lower() == origin_token.lower()), None)
    if match is None:
        match = next((x for x in routes if x.get("originTokenSymbol") == symbol), None)
    if match is None:
        return {"error": f"маршрут {origin_chain_id}->{dest_chain_id} для {symbol} НЕ найден в available-routes"}
    r = requests.get("https://app.across.to/api/suggested-fees", params={
        "originChainId": origin_chain_id, "destinationChainId": dest_chain_id,
        "token": match["originToken"], "amount": amount,
    }, timeout=20)
    if r.status_code != 200:
        return {"error": f"suggested-fees HTTP {r.status_code}: {r.text[:400]}", "matched_route": match}
    out = r.json()
    out["_matched_route"] = match
    return out


def run() -> int:
    dry = json.loads(DRYRUN_PATH.read_text())
    eth_usd = dry["prices"]["eth_usd"]
    btc_usd = dry["prices"]["btc_usd_for_cbbtc"]
    print(f"[plan_v2] реальные цены из dry-run: ETH=${eth_usd} BTC(cbBTC)=${btc_usd}")

    cbbtc_amount = dry["chains"]["base"]["tokens"]["cbBTC"]["balance"]
    usdc_amount = dry["chains"]["base"]["tokens"]["USDC"]["balance"]
    weth_amount_rh = dry["chains"]["robinhood"]["tokens"]["WETH"]["balance"]
    native_eth_rh = dry["chains"]["robinhood"]["native_eth_balance"]

    weth_base_existing_raw = get_erc20_balance(BASE_RPC, WETH_BASE)
    weth_base_existing = weth_base_existing_raw / (10 ** WETH_DECIMALS)
    print(f"[plan_v2] реальный баланс WETH на Base (не было в исходном dry-run): {weth_base_existing}")

    plan = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": []}

    # --- Шаг 1: Base cbBTC -> WETH, самый ликвидный пул ---
    print("\n=== Шаг 1: Base cbBTC -> WETH (самый ликвидный пул) ===")
    cbbtc_pool = find_best_pool_vs_weth("base", CBBTC_BASE)
    if cbbtc_pool is None:
        plan["steps"].append({"action": "cbBTC -> WETH", "error": "не найден пул cbBTC/WETH на GT"})
        return 1
    cbbtc_fee_bps = read_fee_bps(BASE_RPC, cbbtc_pool["address"])
    cbbtc_usd_value = cbbtc_amount * btc_usd
    expected_weth_from_cbbtc = (cbbtc_usd_value * (1 - cbbtc_fee_bps / 1_000_000)) / eth_usd
    step1 = {"action": "swap cbBTC -> WETH", "pool": cbbtc_pool["address"], "pool_reserve_usd": cbbtc_pool["reserve_usd"],
             "fee_bps": cbbtc_fee_bps, "fee_pct": cbbtc_fee_bps / 100 / 100,
             "cbbtc_amount": cbbtc_amount, "cbbtc_usd_value": cbbtc_usd_value, "expected_weth_out": expected_weth_from_cbbtc}
    plan["steps"].append(step1)
    print(f"  пул {cbbtc_pool['address']} (TVL ${cbbtc_pool['reserve_usd']:,.0f}), fee={cbbtc_fee_bps/100:.2f}bp -> ~{expected_weth_from_cbbtc:.8f} WETH")

    # --- Шаг 2: Base USDC -> WETH, самый ликвидный пул ---
    print("\n=== Шаг 2: Base USDC -> WETH (самый ликвидный пул) ===")
    usdc_pool = find_best_pool_vs_weth("base", USDC_BASE)
    if usdc_pool is None:
        plan["steps"].append({"action": "USDC -> WETH", "error": "не найден пул USDC/WETH на GT"})
        return 1
    usdc_fee_bps = read_fee_bps(BASE_RPC, usdc_pool["address"])
    expected_weth_from_usdc = (usdc_amount * (1 - usdc_fee_bps / 1_000_000)) / eth_usd
    step2 = {"action": "swap USDC -> WETH", "pool": usdc_pool["address"], "pool_reserve_usd": usdc_pool["reserve_usd"],
             "fee_bps": usdc_fee_bps, "fee_pct": usdc_fee_bps / 100 / 100,
             "usdc_amount": usdc_amount, "expected_weth_out": expected_weth_from_usdc}
    plan["steps"].append(step2)
    print(f"  пул {usdc_pool['address']} (TVL ${usdc_pool['reserve_usd']:,.0f}), fee={usdc_fee_bps/100:.2f}bp -> ~{expected_weth_from_usdc:.8f} WETH")

    total_weth_base = weth_base_existing + expected_weth_from_cbbtc + expected_weth_from_usdc
    print(f"\n  Итого WETH на Base для моста: {weth_base_existing:.8f} (уже было) + {expected_weth_from_cbbtc:.8f} + {expected_weth_from_usdc:.8f} = {total_weth_base:.8f} (~${total_weth_base*eth_usd:.2f})")

    # --- Шаг 3: Across WETH Base -> Robinhood ---
    print("\n=== Шаг 3: Across мост WETH Base -> Robinhood ===")
    amount_raw = str(int(total_weth_base * (10 ** WETH_DECIMALS)))
    quote = across_quote_checked(BASE_CHAIN_ID, ROBINHOOD_CHAIN_ID, WETH_BASE, amount_raw, "WETH")
    step3 = {"action": "bridge WETH Base -> Robinhood (Across)", "amount_weth": total_weth_base, "quote_raw": quote}
    if "error" not in quote:
        try:
            total_relay_fee_pct = float(quote.get("totalRelayFee", {}).get("pct", 0)) / 1e18 * 100
            step3["total_relay_fee_pct"] = total_relay_fee_pct
            output_amount_raw = int(quote.get("outputAmount", 0))
            step3["expected_weth_out_robinhood"] = output_amount_raw / (10 ** WETH_DECIMALS)
            kill = total_relay_fee_pct > 10.0
            step3["decision"] = "ОСТАВИТЬ КАК ЕСТЬ (комиссия > 10%)" if kill else "ИСПОЛНИТЬ"
            print(f"  реальная котировка Across: totalRelayFee={total_relay_fee_pct:.4f}% -> {step3['decision']}, "
                  f"ожидаемо на Robinhood: {step3.get('expected_weth_out_robinhood')} WETH")
        except Exception as exc:  # noqa: BLE001
            step3["decision"] = f"НЕ УДАЛОСЬ РАЗОБРАТЬ КОТИРОВКУ: {str(exc)[:200]}"
            print(f"  ВНИМАНИЕ: {step3['decision']}, сырой ответ: {json.dumps(quote)[:600]}")
    else:
        step3["decision"] = f"ОШИБКА: {quote.get('error')}"
        print(f"  ОШИБКА: {quote.get('error')}")
    plan["steps"].append(step3)

    weth_from_bridge = step3.get("expected_weth_out_robinhood", 0) if step3.get("decision") == "ИСПОЛНИТЬ" else 0.0

    # --- Шаг 4: нативный ETH на Robinhood -- оставить $0.50, обернуть остаток ---
    print("\n=== Шаг 4: нативный ETH на Robinhood -- оставить $0.50 ===")
    eth_reserve_amount = ETH_RESERVE_USD / eth_usd
    eth_remainder = native_eth_rh - eth_reserve_amount
    gas_price_wei = int(rpc_call(ROBINHOOD_RPC, "eth_gasPrice", []), 16)
    cost_two_tx_usd = 2 * ASSUMED_GAS_PER_TX * gas_price_wei / 1e18 * eth_usd
    step4 = {"action": "wrap remainder ETH -> WETH", "native_eth_now": native_eth_rh, "reserve_eth_target": eth_reserve_amount,
             "remainder_eth": eth_remainder, "remainder_usd": eth_remainder * eth_usd,
             "gas_price_gwei": gas_price_wei / 1e9, "cost_two_tx_usd_estimate": cost_two_tx_usd}
    wrap_remainder = eth_remainder if (eth_remainder * eth_usd >= cost_two_tx_usd) else 0.0
    step4["decision"] = "ОБЕРНУТЬ" if wrap_remainder > 0 else "НЕ ТРОГАТЬ (остаток дешевле двух транзакций)"
    plan["steps"].append(step4)
    print(f"  остаток сверх $0.50: {eth_remainder:.8f} ETH (~${eth_remainder*eth_usd:.4f}), "
          f"стоимость 2 tx: ~${cost_two_tx_usd:.6f} -> {step4['decision']}")

    # --- Шаг 5: Robinhood -- ОДИН своп всего WETH (существующий + мост + обёрнутый остаток) -> USDG ---
    print("\n=== Шаг 5: Robinhood -- один своп WETH -> USDG (пул P5) ===")
    p5_fee_bps = read_fee_bps(ROBINHOOD_RPC, P5_POOL_ROBINHOOD)
    total_weth_robinhood = weth_amount_rh + weth_from_bridge + wrap_remainder
    total_weth_usd_value = total_weth_robinhood * eth_usd
    expected_usdg_out = total_weth_usd_value * (1 - p5_fee_bps / 1_000_000)
    step5 = {
        "action": "swap (WETH-существующий + WETH-с-моста + обёрнутый-остаток) -> USDG, ОДНОЙ транзакцией",
        "pool": P5_POOL_ROBINHOOD, "fee_bps": p5_fee_bps, "fee_pct": p5_fee_bps / 100 / 100,
        "weth_existing": weth_amount_rh, "weth_from_bridge": weth_from_bridge, "weth_wrapped_remainder": wrap_remainder,
        "total_weth_in": total_weth_robinhood, "total_weth_usd_value": total_weth_usd_value,
        "expected_usdg_out": expected_usdg_out,
    }
    plan["steps"].append(step5)
    print(f"  P5-пул fee={p5_fee_bps/100:.2f}bp, всего WETH в своп: {total_weth_robinhood:.8f} (~${total_weth_usd_value:.2f}) -> ожидаемо ~{expected_usdg_out:.4f} USDG")

    plan["steps"].append({"action": "Lighter", "decision": "НЕ ТРОГАТЬ (владелец)"})

    # --- Итог ---
    print("\n=== Итог: было -> станет (оценка) ===")
    usdg_now = dry["chains"]["robinhood"]["tokens"]["USDG"]["balance"]
    usdg_after = usdg_now + expected_usdg_out
    fee_cost_1 = cbbtc_usd_value * (cbbtc_fee_bps / 1_000_000)
    fee_cost_2 = usdc_amount * (usdc_fee_bps / 1_000_000)
    bridge_fee_usd = (total_weth_base * (step3.get("total_relay_fee_pct") or 0) / 100 * eth_usd) if step3.get("decision") == "ИСПОЛНИТЬ" else 0.0
    fee_cost_5 = total_weth_usd_value * (p5_fee_bps / 1_000_000)
    wrap_tx_cost = cost_two_tx_usd / 2 if wrap_remainder > 0 else 0.0  # только 1 из 2 tx реально нужна отдельно (wrap); своп -- уже отдельная tx но её издержка это fee пула, не газ здесь
    total_cost_usd = fee_cost_1 + fee_cost_2 + bridge_fee_usd + fee_cost_5 + wrap_tx_cost
    plan["summary"] = {
        "usdg_now": usdg_now, "usdg_estimated_after": usdg_after,
        "total_estimated_cost_usd": total_cost_usd, "total_usd_before": dry["total_usd_approx"],
        "cost_breakdown": {"base_cbbtc_weth_fee_usd": fee_cost_1, "base_usdc_weth_fee_usd": fee_cost_2,
                            "across_bridge_fee_usd": bridge_fee_usd, "robinhood_swap_fee_usd": fee_cost_5,
                            "wrap_tx_gas_usd_estimate": wrap_tx_cost},
    }
    print(f"  USDG сейчас: {usdg_now:.6f} -> оценочно после: {usdg_after:.6f}")
    print(f"  Суммарные оценочные издержки: ~${total_cost_usd:.4f} (из ~${dry['total_usd_approx']:.2f})")
    print("\n[plan_v2] СТОП -- ждём явного 'да' владельца перед любой реальной транзакцией.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
    print(f"[plan_v2] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
