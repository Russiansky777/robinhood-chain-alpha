#!/usr/bin/env python3
"""Консолидация активов в USDG на Robinhood Chain -- ШАГ 1: dry-run,
только чтение (владелец, 2026-09-05, дословно): "Сначала dry-run,
реальные транзакции — после отдельного «да»."

Читает РЕАЛЬНЫЕ балансы кошелька (тот же, что везде в проекте --
`0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75`) на:
- Robinhood Chain (chain_id 4663): нативный ETH, WETH, USDG.
- Base (chain_id 8453): нативный ETH, USDC, cbBTC.
- Arbitrum (chain_id 42161): нативный ETH -- "любые другие цепи, где
  что-то осталось от прошлых прогонов" (в проекте есть Across-related
  разведка на Arbitrum, `across_handler_probe.py`).
- Ethereum mainnet (chain_id 1): нативный ETH -- та же причина.

Все адреса/RPC -- РЕАЛЬНЫЕ, уже подтверждённые и используемые в
проекте (`p5_live_step0.py`, `p5_live_precheck.py`, `p6_live_step1.py`),
не гадаются заново. Каждый RPC проверяется по `eth_chainId` ПЕРЕД
чтением баланса -- если вернувшийся chainId не совпадает с ожидаемым,
результат по этой цепи помечается ошибкой, не тихо принимается.

Цены в USD -- РЕАЛЬНЫЕ, из тех же ончейн-пулов, что уже используются в
проекте (не с биржи, чтобы не тянуть новый источник): ETH/USDG -- из
P5-пула (`0x52e65B17...`, Robinhood Chain) через GeckoTerminal;
cbBTC/USDC -- из P6-пула (Base) аналогично. USDC/USDG считаются $1.00
(стейблкоины) с явной пометкой -- не пересчитываются отдельно, если GT
не дал уверенного числа."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_dryrun_result.json")

BALANCE_OF_SELECTOR = "0x70a08231000000000000000000000000"  # balanceOf(address) + адрес паддится ниже

GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_MIN_INTERVAL_S = 2.6
_last_gt_call = 0.0

RPC_MIN_INTERVAL_S = 0.5
_last_rpc_call: dict[str, float] = {}

CHAINS = {
    "robinhood": {
        "rpc": "https://rpc.mainnet.chain.robinhood.com", "chain_id": 4663,
        "tokens": {
            "USDG": {"address": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", "decimals": 6},
            "WETH": {"address": "0x0bd7d308f8e1639fab988df18a8011f41eacad73", "decimals": 18},
        },
    },
    "base": {
        "rpc": "https://mainnet.base.org", "chain_id": 8453,
        "tokens": {
            "USDC": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "cbBTC": {"address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "decimals": 8},
        },
    },
    "arbitrum": {"rpc": "https://arb1.arbitrum.io/rpc", "chain_id": 42161, "tokens": {}},
    "ethereum": {"rpc": "https://ethereum-rpc.publicnode.com", "chain_id": 1, "tokens": {}},
}

P5_POOL_ROBINHOOD = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca".lower()  # WETH/USDG, тот же пул, что весь P5
P6_POOL_BASE = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"  # cbBTC/USDC, тот же пул, что P6


def _throttle_rpc(rpc_url: str) -> None:
    last = _last_rpc_call.get(rpc_url, 0.0)
    wait = last + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_rpc_call[rpc_url] = time.monotonic()


def _throttle_gt() -> None:
    global _last_gt_call
    wait = _last_gt_call + GT_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_gt_call = time.monotonic()


def rpc_call(rpc_url: str, method: str, params: list):
    _throttle_rpc(rpc_url)
    r = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def verify_chain_id(rpc_url: str, expected: int) -> int:
    result = rpc_call(rpc_url, "eth_chainId", [])
    got = int(result, 16)
    if got != expected:
        raise RuntimeError(f"RPC {rpc_url} вернул chainId={got}, ожидался {expected}")
    return got


def get_native_balance(rpc_url: str) -> int:
    result = rpc_call(rpc_url, "eth_getBalance", [WALLET, "latest"])
    return int(result, 16)


def get_erc20_balance(rpc_url: str, token_address: str) -> int:
    padded_addr = WALLET[2:].lower().rjust(64, "0")
    data = BALANCE_OF_SELECTOR[:10] + padded_addr
    result = rpc_call(rpc_url, "eth_call", [{"to": token_address, "data": data}, "latest"])
    return int(result, 16)


def get_gt_pool_price(network: str, pool_address: str, target_token_address: str) -> float | None:
    """Реальная цена КОНКРЕТНОГО токена (не гадаем base/quote по
    позиции -- сопоставляем по адресу, тот же урок, что уже
    задокументирован в pool_screener_concentration.py про
    base/quote-порядок GT)."""
    _throttle_gt()
    r = requests.get(f"{GT_BASE}/networks/{network}/pools/{pool_address}",
                      headers={"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-consolidation/1.0"},
                      timeout=30)
    if r.status_code != 200:
        return None
    data = r.json().get("data", {})
    attrs = data.get("attributes", {})
    rel = data.get("relationships", {})
    base_addr = (rel.get("base_token", {}).get("data", {}).get("id", "") or "").split("_")[-1].lower()
    quote_addr = (rel.get("quote_token", {}).get("data", {}).get("id", "") or "").split("_")[-1].lower()
    target = target_token_address.lower()
    if target == base_addr:
        return float(attrs["base_token_price_usd"]) if attrs.get("base_token_price_usd") else None
    if target == quote_addr:
        return float(attrs["quote_token_price_usd"]) if attrs.get("quote_token_price_usd") else None
    return None


def run() -> int:
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "wallet": WALLET, "chains": {}}

    for chain_name, cfg in CHAINS.items():
        print(f"\n=== {chain_name} (chain_id={cfg['chain_id']}) ===")
        chain_result = {"rpc": cfg["rpc"]}
        try:
            verify_chain_id(cfg["rpc"], cfg["chain_id"])
            print(f"  RPC подтверждён: chainId={cfg['chain_id']}")
        except Exception as exc:  # noqa: BLE001
            chain_result["error"] = f"chainId не подтверждён: {str(exc)[:300]}"
            print(f"  ОШИБКА: {chain_result['error']}")
            result["chains"][chain_name] = chain_result
            continue

        try:
            native_wei = get_native_balance(cfg["rpc"])
            chain_result["native_eth_balance"] = native_wei / 1e18
            print(f"  нативный ETH: {chain_result['native_eth_balance']:.8f}")
        except Exception as exc:  # noqa: BLE001
            chain_result["native_eth_error"] = str(exc)[:300]
            print(f"  ОШИБКА нативного баланса: {chain_result['native_eth_error']}")

        chain_result["tokens"] = {}
        for tok_name, tok_cfg in cfg["tokens"].items():
            try:
                raw = get_erc20_balance(cfg["rpc"], tok_cfg["address"])
                human = raw / (10 ** tok_cfg["decimals"])
                chain_result["tokens"][tok_name] = {"balance": human, "address": tok_cfg["address"]}
                print(f"  {tok_name}: {human}")
            except Exception as exc:  # noqa: BLE001
                chain_result["tokens"][tok_name] = {"error": str(exc)[:300], "address": tok_cfg["address"]}
                print(f"  ОШИБКА {tok_name}: {str(exc)[:300]}")

        result["chains"][chain_name] = chain_result

    print("\n=== реальные цены (ончейн-пулы, GeckoTerminal) ===")
    eth_usd = get_gt_pool_price("robinhood", P5_POOL_ROBINHOOD, CHAINS["robinhood"]["tokens"]["WETH"]["address"])
    btc_usd = get_gt_pool_price("base", P6_POOL_BASE, CHAINS["base"]["tokens"]["cbBTC"]["address"])
    result["prices"] = {"eth_usd": eth_usd, "btc_usd_for_cbbtc": btc_usd, "usdc_usd": 1.0, "usdg_usd": 1.0,
                         "note": "USDC/USDG приняты за $1.00 (стейблкоины), не пересчитаны отдельным запросом"}
    print(f"  ETH/USD (из P5-пула, robinhood): {eth_usd}")
    print(f"  BTC/USD для cbBTC (из P6-пула, base): {btc_usd}")

    # Итоговая таблица в $ -- сразу здесь, чтобы не пересчитывать вручную.
    print("\n=== таблица: цепь, актив, количество, ~$ ===")
    table = []
    price_map = {"ETH": eth_usd, "WETH": eth_usd, "cbBTC": btc_usd, "USDC": 1.0, "USDG": 1.0}
    for chain_name, cr in result["chains"].items():
        if "native_eth_balance" in cr:
            amt = cr["native_eth_balance"]
            usd = amt * eth_usd if eth_usd else None
            table.append({"chain": chain_name, "asset": "ETH (нативный)", "amount": amt, "usd": usd})
        for tok_name, tok_info in cr.get("tokens", {}).items():
            if "balance" in tok_info:
                amt = tok_info["balance"]
                usd = amt * price_map.get(tok_name, 0) if price_map.get(tok_name) else None
                table.append({"chain": chain_name, "asset": tok_name, "amount": amt, "usd": usd})
    result["balance_table"] = table
    total_usd = sum(r["usd"] for r in table if r["usd"] is not None)
    result["total_usd_approx"] = total_usd
    for r in table:
        usd_str = f"${r['usd']:.4f}" if r["usd"] is not None else "n/a"
        print(f"  {r['chain']:12s} {r['asset']:16s} {r['amount']:.8f}  ~{usd_str}")
    print(f"  ИТОГО ~${total_usd:.2f}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[dryrun] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
