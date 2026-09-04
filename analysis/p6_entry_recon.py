#!/usr/bin/env python3
"""P6, ПРЕДПОЛЁТНАЯ разведка ПЕРЕД любой реальной транзакцией входа
(владелец, Гейт 2, 2026-09-04): подтвердить РЕАЛЬНЫЙ NonfungiblePositionManager
и SwapRouter для целевого пула USDC-CBBTC на Base -- Aerodrome Slipstream
опубликовал ДВЕ схемы деплоя в README (`Initial Deployment` и `Gauges V3
Deployment`, github.com/aerodrome-finance/slipstream) с РАЗНЫМИ адресами
NFPM/Factory/SwapRouter. Только пул, реально владеющий нашей позицией,
знает, какой NFPM его обслуживает -- определяется по СОВПАДЕНИЮ
pool.factory() с factory() кандидата NFPM (оба -- реальные view-вызовы,
не предположение по "текущая схема лучше старой").

Также: реальные балансы WALLET на Base (ETH/USDC/cbBTC) ДО любого моста --
чтобы не предполагать нулевой старт, реальный token0/token1/decimals пула
(подтверждение, не повтор из памяти прошлых прогонов), tickSpacing реальный.
Ничего не отправляется -- только чтение."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

BASE_RPC = "https://mainnet.base.org"
RPC_MIN_INTERVAL_S = 1.5
RPC_RETRY_BACKOFF_S = 15.0
RPC_MAX_RETRIES = 3

POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"  # тот же кошелёк, что P5 (Robinhood Chain)

# Кандидаты -- ДОСЛОВНО из README github.com/aerodrome-finance/slipstream (2026-09-04)
CANDIDATES = {
    "initial": {
        "nfpm": "0x827922686190790b37229fd06084350E74485b72",
        "factory": "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A",
        "router": "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5",
    },
    "gauges_v3": {
        "nfpm": "0xe1f8cd9AC4e4A65F54f38a5CdAfCA44f6dD68b53",
        "factory": "0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef",
        "router": "0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F",
    },
}

_last_rpc_call = 0.0


def _topic0(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


def _throttle() -> None:
    global _last_rpc_call
    wait = _last_rpc_call + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_rpc_call = time.monotonic()


def rpc(method: str, params: list):
    for attempt in range(RPC_MAX_RETRIES + 1):
        _throttle()
        r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        if r.status_code == 429 and attempt < RPC_MAX_RETRIES:
            time.sleep(RPC_RETRY_BACKOFF_S)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method} {params}: {body['error']}")
        return body["result"]
    raise RuntimeError("RPC 429 после ретраев")


def eth_call(to: str, selector_sig: str, extra_data: str = "") -> str:
    selector = _topic0(selector_sig)[:10]
    return rpc("eth_call", [{"to": to, "data": selector + extra_data}, "latest"])


def addr_from_result(raw: str) -> str:
    return "0x" + raw[-40:]


def erc20_balance(token: str, holder: str) -> int:
    data = "0x70a08231" + holder[2:].rjust(64, "0").lower()
    raw = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    return int(raw, 16)


def erc20_decimals(token: str) -> int:
    return int(eth_call(token, "decimals()"), 16)


def erc20_symbol_guess(token: str) -> str:
    raw = eth_call(token, "symbol()")
    try:
        # ABI-encoded dynamic string: offset(32) + length(32) + data
        length = int(raw[2:][64:128], 16)
        data_hex = raw[2:][128:128 + length * 2]
        return bytes.fromhex(data_hex).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return f"<undecoded: {raw[:20]}>"


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "note": "ТОЛЬКО ЧТЕНИЕ, ничего не отправлено"}

    print("=== 1. Реальный factory() пула ===")
    pool_factory_raw = eth_call(POOL_ADDRESS, "factory()")
    pool_factory = addr_from_result(pool_factory_raw)
    print(f"[recon] pool.factory() = {pool_factory}")
    result["pool_factory"] = pool_factory

    print("\n=== 2. factory() каждого кандидата NFPM (реальный view-вызов) ===")
    matched = None
    candidate_factories = {}
    for name, c in CANDIDATES.items():
        try:
            nfpm_factory_raw = eth_call(c["nfpm"], "factory()")
            nfpm_factory = addr_from_result(nfpm_factory_raw)
        except Exception as exc:  # noqa: BLE001
            nfpm_factory = f"<ошибка: {exc}>"
        candidate_factories[name] = nfpm_factory
        print(f"[recon] {name}: NFPM={c['nfpm']} -> factory()={nfpm_factory}")
        if nfpm_factory.lower() == pool_factory.lower():
            matched = name
    result["candidate_nfpm_factories"] = candidate_factories
    result["matched_deployment"] = matched
    if matched is None:
        result["abort_reason"] = "НИ ОДИН кандидат NFPM не совпал с pool.factory() -- реальный NFPM неизвестен, СТОП."
        Path("data/p3_guard_cache/p6_entry_recon_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[recon] СТОП: {result['abort_reason']}")
        return 1
    print(f"[recon] СОВПАДЕНИЕ: реальный деплой -- '{matched}' (NFPM={CANDIDATES[matched]['nfpm']}, router={CANDIDATES[matched]['router']})")
    result["confirmed_nfpm"] = CANDIDATES[matched]["nfpm"]
    result["confirmed_router"] = CANDIDATES[matched]["router"]
    result["confirmed_factory"] = CANDIDATES[matched]["factory"]

    print("\n=== 3. Реальные token0/token1/decimals/tickSpacing пула ===")
    token0 = addr_from_result(eth_call(POOL_ADDRESS, "token0()"))
    token1 = addr_from_result(eth_call(POOL_ADDRESS, "token1()"))
    tick_spacing_raw = int(eth_call(POOL_ADDRESS, "tickSpacing()"), 16)
    tick_spacing = tick_spacing_raw - (1 << 256) if tick_spacing_raw >= (1 << 255) else tick_spacing_raw
    dec0 = erc20_decimals(token0)
    dec1 = erc20_decimals(token1)
    sym0 = erc20_symbol_guess(token0)
    sym1 = erc20_symbol_guess(token1)
    print(f"[recon] token0={token0} ({sym0}, dec={dec0}), token1={token1} ({sym1}, dec={dec1}), tickSpacing={tick_spacing}")
    result["pool_tokens"] = {"token0": token0, "token0_symbol": sym0, "token0_decimals": dec0,
                              "token1": token1, "token1_symbol": sym1, "token1_decimals": dec1,
                              "tick_spacing": tick_spacing}
    result["token0_is_usdc"] = token0.lower() == USDC.lower()
    result["token1_is_cbbtc"] = token1.lower() == CBBTC.lower()

    print("\n=== 4. Реальные балансы WALLET на Base ДО моста ===")
    eth_balance_wei = int(rpc("eth_getBalance", [WALLET, "latest"]), 16)
    usdc_balance = erc20_balance(USDC, WALLET)
    cbbtc_balance = erc20_balance(CBBTC, WALLET)
    print(f"[recon] WALLET на Base: ETH={eth_balance_wei / 1e18}, USDC={usdc_balance / 1e6}, cbBTC={cbbtc_balance / 1e8}")
    result["wallet_balances_base_before_bridge"] = {
        "eth_human": eth_balance_wei / 1e18, "usdc_human": usdc_balance / 1e6, "cbbtc_human": cbbtc_balance / 1e8,
    }

    Path("data/p3_guard_cache/p6_entry_recon_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    print("\n[recon] ГОТОВО.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
