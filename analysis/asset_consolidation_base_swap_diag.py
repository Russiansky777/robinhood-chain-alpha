#!/usr/bin/env python3
"""Консолидация активов -- диагностика (ТОЛЬКО ЧТЕНИЕ, 0 транзакций).

После реального approve (tx 0x455df581...bbef0cfb, run 33975825755,
подтверждён) exactInputSingle ВСЁ РАВНО провалился на обеих ABI-
структурах. Раньше реальная причина revert была скрыта: RuntimeError
клал params (длинный calldata) ПЕРЕД самой ошибкой, а truncation
str(exc)[:300] отрезал именно ошибку, не params -- реальный revert
reason никогда не был виден в логах.

Эта диагностика (после фикса форматирования ошибки в
asset_consolidation_execute_base.py):
1. Реально проверяет factory.getPool(cbBTC, WETH, fee) для ВСЕХ
   стандартных tier'ов канонического Uniswap V3 (100, 500, 2500, 3000,
   10000) -- не полагаемся на память "fee=2500 подтверждён", проверяем
   заново, что там реально есть код пула.
2. Повторяет ту же eth_call-симуляцию exactInputSingle, что и в
   execute_base.py, но теперь с исправленным форматированием ошибки --
   печатает РЕАЛЬНЫЙ revert reason, не обрезанный до параметров."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address
from Crypto.Hash import keccak


def _selector(sig: str) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return k.digest()[:4]


WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
BASE_RPC = "https://mainnet.base.org"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
FACTORY_SELECTOR = "0xc45a0155"  # factory()
GET_POOL_SELECTOR = _selector("getPool(address,address,uint24)")

CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

CANDIDATE_FEES = [100, 500, 2500, 3000, 10000]

EXACT_INPUT_SINGLE_SIG_NO_DEADLINE = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
EXACT_INPUT_SINGLE_SELECTOR_NO_DEADLINE = _selector(EXACT_INPUT_SINGLE_SIG_NO_DEADLINE)

OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_base_swap_diag_result.json")


def rpc(method: str, params: list):
    r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{body['error']} (метод={method})")
    return body["result"]


def get_factory() -> str:
    raw = rpc("eth_call", [{"to": ROUTER, "data": FACTORY_SELECTOR}, "latest"])
    return "0x" + raw[-40:]


def get_pool(factory: str, token_a: str, token_b: str, fee: int) -> str:
    data = ("0x" + GET_POOL_SELECTOR.hex()
            + token_a[2:].lower().rjust(64, "0")
            + token_b[2:].lower().rjust(64, "0")
            + hex(fee)[2:].rjust(64, "0"))
    raw = rpc("eth_call", [{"to": factory, "data": data}, "latest"])
    return "0x" + raw[-40:]


def get_code(addr: str) -> str:
    return rpc("eth_getCode", [addr, "latest"])


def sim_exact_input_single(token_in: str, token_out: str, fee: int, amount_in: int) -> dict:
    params = (to_checksum_address(token_in), to_checksum_address(token_out), fee, to_checksum_address(WALLET),
              amount_in, 0, 0)
    encoded = abi_encode(["(address,address,uint24,address,uint256,uint256,uint160)"], [params])
    data = EXACT_INPUT_SINGLE_SELECTOR_NO_DEADLINE + encoded
    try:
        result = rpc("eth_call", [{"from": WALLET, "to": ROUTER, "data": "0x" + data.hex()}, "latest"])
        return {"ok": True, "output_raw": int(result, 16)}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


def run() -> int:
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    factory = get_factory()
    out["router_factory"] = factory
    print(f"[diag] router.factory() = {factory}")

    out["pools"] = {}
    for label, (t0, t1) in [("cbBTC_WETH", (CBBTC, WETH)), ("USDC_WETH", (USDC, WETH))]:
        out["pools"][label] = {}
        for fee in CANDIDATE_FEES:
            pool_addr = get_pool(factory, t0, t1, fee)
            has_code = False
            if int(pool_addr, 16) != 0:
                code = get_code(pool_addr)
                has_code = code not in ("0x", "0x0", None)
            out["pools"][label][str(fee)] = {"pool_address": pool_addr, "has_code": has_code}
            print(f"[diag] {label} fee={fee/10000:.2%}: pool={pool_addr} has_code={has_code}")
            time.sleep(0.3)

    # РЕАЛЬНАЯ проверка exactInputSingle для cbBTC->WETH на fee=2500
    # (тот же вызов, что реально проваливался в execute_base.py, но
    # теперь с исправленным форматированием ошибки -- смотрим настоящий
    # revert reason).
    print("\n[diag] --- повтор eth_call-симуляции exactInputSingle с исправленным форматом ошибки ---")
    sim_result = sim_exact_input_single(CBBTC, WETH, 2500, 45290)
    out["cbbtc_to_weth_2500_simulation"] = sim_result
    print(f"[diag] cbBTC->WETH fee=2500, amount=45290: {sim_result}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[diag] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
