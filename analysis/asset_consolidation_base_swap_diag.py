#!/usr/bin/env python3
"""Консолидация активов -- диагностика (ТОЛЬКО ЧТЕНИЕ, 0 транзакций).

РЕАЛЬНАЯ НАХОДКА (run 33976249528, первый прогон этой диагностики):
на каноническом Uniswap V3 Factory на Base (0x33128a8fc17869897dce68ed026d694621f6fdfd,
подтверждён через router.factory()) для пары cbBTC/WETH **НЕТ пула на
fee=2500 (0.25%)** -- factory.getPool() вернул нулевой адрес,
has_code=False. Именно это было настоящей причиной ВСЕХ провалов
exactInputSingle (не approve, не ABI-структура) -- pool, который
использовался в plan_v2 (`0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1`,
TVL $18.76M, "fee 0.25%"), либо принадлежит другому DEX/фабрике на
Base, либо был неверно атрибутирован по данным Dune/GT -- не
канонический Uniswap V3.

Реально СУЩЕСТВУЮЩИЕ (has_code=True) пулы для cbBTC/WETH на
каноническом Uniswap V3 Factory: fee=100 (0.01%), fee=500 (0.05%),
fee=3000 (0.30%). Первый прогон диагностики упал на HTTP 429 (rate
limit публичного mainnet.base.org) до проверки USDC/WETH и до
сравнения ликвидности -- эта версия добавляет retry-with-backoff на
429 и реальное сравнение резервов (balanceOf токена в адресе пула,
грубая, но реальная прокси для глубины ликвидности) между
существующими fee-tier'ами, чтобы выбрать самый ликвидный БЕЗ
угадывания."""
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
BALANCE_OF_SELECTOR = "0x70a08231"

CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC_DECIMALS, WETH_DECIMALS, USDC_DECIMALS = 8, 18, 6

CANDIDATE_FEES = [100, 500, 3000, 10000]  # 2500 реально подтверждён отсутствующим -- не проверяем повторно

EXACT_INPUT_SINGLE_SIG_NO_DEADLINE = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
EXACT_INPUT_SINGLE_SELECTOR_NO_DEADLINE = _selector(EXACT_INPUT_SINGLE_SIG_NO_DEADLINE)

OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_base_swap_diag_result.json")

RPC_RETRY_DELAYS = [1.5, 3.0, 6.0, 12.0]


def rpc(method: str, params: list):
    last_exc = None
    for delay in [0.0] + RPC_RETRY_DELAYS:
        if delay:
            print(f"[diag] rate-limit/ошибка сети -- жду {delay}с и повторяю")
            time.sleep(delay)
        try:
            r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            if r.status_code == 429:
                last_exc = RuntimeError("429 Too Many Requests")
                continue
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"{body['error']} (метод={method})")
            return body["result"]
        except requests.exceptions.HTTPError as e:
            last_exc = e
    raise RuntimeError(f"RPC {method} не удался после {len(RPC_RETRY_DELAYS)} ретраев: {last_exc}")


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


def erc20_balance_of(token: str, holder: str) -> int:
    padded = holder[2:].lower().rjust(64, "0")
    return int(rpc("eth_call", [{"to": token, "data": BALANCE_OF_SELECTOR + padded}, "latest"]), 16)


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


def scan_pair(factory: str, label: str, token_a: str, token_b: str, dec_a: int, dec_b: int) -> dict:
    result = {}
    for fee in CANDIDATE_FEES:
        pool_addr = get_pool(factory, token_a, token_b, fee)
        entry = {"pool_address": pool_addr, "has_code": False}
        if int(pool_addr, 16) != 0:
            time.sleep(0.5)
            code = get_code(pool_addr)
            entry["has_code"] = code not in ("0x", "0x0", None)
            if entry["has_code"]:
                time.sleep(0.5)
                reserve_a = erc20_balance_of(token_a, pool_addr)
                time.sleep(0.5)
                reserve_b = erc20_balance_of(token_b, pool_addr)
                entry["reserve_a"] = reserve_a / 10 ** dec_a
                entry["reserve_b"] = reserve_b / 10 ** dec_b
        result[str(fee)] = entry
        print(f"[diag] {label} fee={fee} ({fee/1_000_000:.2%}): {entry}")
        time.sleep(0.5)
    return result


def run() -> int:
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    factory = get_factory()
    out["router_factory"] = factory
    print(f"[diag] router.factory() = {factory}")
    time.sleep(0.5)

    out["pools"] = {}
    out["pools"]["cbBTC_WETH"] = scan_pair(factory, "cbBTC_WETH", CBBTC, WETH, CBBTC_DECIMALS, WETH_DECIMALS)
    out["pools"]["USDC_WETH"] = scan_pair(factory, "USDC_WETH", USDC, WETH, USDC_DECIMALS, WETH_DECIMALS)

    # Выбираем реально самый ликвидный (по резерву WETH в пуле) fee tier
    # для каждой пары среди РЕАЛЬНО существующих пулов -- не гадаем.
    out["best_fee"] = {}
    for label in ("cbBTC_WETH", "USDC_WETH"):
        candidates = [(fee, e) for fee, e in out["pools"][label].items() if e.get("has_code") and "reserve_b" in e]
        if candidates:
            best_fee, best_entry = max(candidates, key=lambda kv: kv[1]["reserve_b"])
            out["best_fee"][label] = {"fee": int(best_fee), "reserve_weth": best_entry["reserve_b"]}
            print(f"[diag] {label}: самый ликвидный реально существующий fee tier = {best_fee} "
                  f"(WETH в пуле = {best_entry['reserve_b']:.6f})")
        else:
            out["best_fee"][label] = None
            print(f"[diag] {label}: НИ ОДНОГО реально существующего пула не найдено среди {CANDIDATE_FEES}")

    # РЕАЛЬНАЯ проверка exactInputSingle на выбранном fee tier (если
    # найден) -- убеждаемся, что теперь симуляция реально проходит,
    # прежде чем менять execute_base.py.
    print("\n[diag] --- eth_call-симуляция exactInputSingle на реально подтверждённом fee tier ---")
    if out["best_fee"].get("cbBTC_WETH"):
        fee = out["best_fee"]["cbBTC_WETH"]["fee"]
        sim_result = sim_exact_input_single(CBBTC, WETH, fee, 45290)
        out["cbbtc_to_weth_verified_fee_simulation"] = {"fee": fee, **sim_result}
        print(f"[diag] cbBTC->WETH fee={fee}, amount=45290: {sim_result}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[diag] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
