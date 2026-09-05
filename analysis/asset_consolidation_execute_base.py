#!/usr/bin/env python3
"""Консолидация активов -- ИСПОЛНЕНИЕ, часть 1 (Base): cbBTC -> WETH,
USDC -> WETH через Uniswap V3 SwapRouter02 (владелец дал "да" на свапы,
2026-09-05). РЕАЛЬНЫЕ ТРАНЗАКЦИИ.

SwapRouter02 = `0x2626664c2603336E57B271c5C0b26F421741e481` -- РЕАЛЬНО
подтверждён (`asset_consolidation_router_probe_result.json`: bytecode
есть, factory() совпадает с каноническим Uniswap V3 Factory на Base).

ABI struct exactInputSingle -- SwapRouter02 (в отличие от classic
SwapRouter v1 / Aerodrome Slipstream, использованных в P5/P6) НЕ
содержит `deadline` в структуре параметров. ПЕРЕД реальной отправкой
-- эмпирическая проверка через eth_call-СИМУЛЯЦИЮ (0 газа, не меняет
состояние): пробуем 7-поле (без deadline) структуру; если реально
revert -- СТОП, не гадаем, не пробуем другой вариант вслепую на
реальные деньги.

Резилентность -- тот же паттерн, что p6_live_step1.py: pre-flight
баланс перед каждым свопом, gas-estimate с ретраями (лаг RPC-реплики),
gas-price с ретраями (gas-race), потолок $3/tx, progress-JSON
(идемпотентно -- повторный запуск не задвоит уже подтверждённые tx)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import to_checksum_address
from Crypto.Hash import keccak


def _selector(sig: str) -> bytes:
    """Реальный keccak-селектор, вычисляется, НЕ хардкодится (тот же
    метод, что везде в проекте, p6_live_step1.py::_selector) -- на
    реальные деньги не полагаемся на память о значении селектора."""
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return k.digest()[:4]

WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
BASE_RPC = "https://mainnet.base.org"
BASE_CHAIN_ID = 8453
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"

CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
CBBTC_DECIMALS, USDC_DECIMALS, WETH_DECIMALS = 8, 6, 18

CBBTC_WETH_POOL_FEE = 2500  # реальный fee tier, подтверждён plan_v2 (0.25%)
USDC_WETH_POOL_FEE = 3000  # реальный fee tier, подтверждён plan_v2 (0.30%)
SWAP_SLIPPAGE = 0.03  # тот же широкий допуск, что P5/P6 -- приоритет гарантированного исполнения
PER_TX_GAS_CEILING_USD = 3.0

OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_execute_base_result.json")
DRYRUN_PATH = Path("data/p3_guard_cache/asset_consolidation_dryrun_result.json")

APPROVE_SELECTOR = _selector("approve(address,uint256)")
BALANCE_OF_SELECTOR = "0x70a08231"
# exactInputSingle((address,address,uint24,address,uint256,uint256,uint160)) --
# SwapRouter02 (БЕЗ deadline в структуре, в отличие от classic SwapRouter v1 /
# Aerodrome Slipstream, использованных в P5/P6) -- вычисляется, не хардкодится.
EXACT_INPUT_SINGLE_SELECTOR_NO_DEADLINE = _selector(
    "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")


def rpc(method: str, params: list):
    r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def erc20_balance(token: str) -> int:
    padded = WALLET[2:].lower().rjust(64, "0")
    return int(rpc("eth_call", [{"to": token, "data": BALANCE_OF_SELECTOR + padded}, "latest"]), 16)


ALLOWANCE_SELECTOR = _selector("allowance(address,address)")


def erc20_allowance(token: str, spender: str) -> int:
    data = "0x" + ALLOWANCE_SELECTOR.hex() + WALLET[2:].lower().rjust(64, "0") + spender[2:].lower().rjust(64, "0")
    return int(rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def build_swap_calldata(token_in: str, token_out: str, fee: int, recipient: str, amount_in: int, amount_out_min: int) -> bytes:
    params = (to_checksum_address(token_in), to_checksum_address(token_out), fee, to_checksum_address(recipient),
              amount_in, amount_out_min, 0)
    encoded = abi_encode(["(address,address,uint24,address,uint256,uint256,uint160)"], [params])
    return EXACT_INPUT_SINGLE_SELECTOR_NO_DEADLINE + encoded


def simulate_swap(token_in: str, token_out: str, fee: int, amount_in: int) -> int:
    """eth_call СИМУЛЯЦИЯ (0 газа, не меняет состояние) -- эмпирически
    подтверждает, что структура параметров реально принимается
    контрактом, ДО того как тратить реальный газ. amount_out_min=0 для
    симуляции (не для реальной отправки)."""
    calldata = build_swap_calldata(token_in, token_out, fee, WALLET, amount_in, 0)
    result = rpc("eth_call", [{"from": WALLET, "to": ROUTER, "data": "0x" + calldata.hex()}, "latest"])
    return int(result, 16)


def eth_gas_price() -> int:
    return int(rpc("eth_gasPrice", []), 16)


def eth_estimate_gas(to: str, data: bytes) -> int:
    return int(rpc("eth_estimateGas", [{"from": WALLET, "to": to, "data": "0x" + data.hex()}]), 16)


def eth_nonce() -> int:
    return int(rpc("eth_getTransactionCount", [WALLET, "pending"]), 16)


def send_tx(account, to: str, data: bytes, nonce: int, gas_limit: int, gas_price: int, buffer_mult: float) -> str:
    tx = {"chainId": BASE_CHAIN_ID, "nonce": nonce, "to": to_checksum_address(to), "value": 0,
          "gas": int(gas_limit * 1.3), "gasPrice": int(gas_price * buffer_mult), "data": "0x" + data.hex()}
    signed = Account.sign_transaction(tx, account.key)
    return rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])


def wait_for_receipt(tx_hash: str, timeout_s: int = 300, poll_s: int = 5) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            return receipt
        time.sleep(poll_s)
    raise RuntimeError(f"{tx_hash} не замайнилась за {timeout_s}с -- проверить вручную, НЕ повторять отправку.")


GAS_RETRY_BUFFERS = [1.15, 1.4, 1.75]


def send_and_wait(account, label: str, to: str, data: bytes, nonce: int, eth_usd: float, progress: dict) -> dict:
    print(f"[execute_base] --- {label}: подготовка (nonce={nonce}) ---")
    gas_est = eth_estimate_gas(to, data)
    gas_price_now = eth_gas_price()
    est_cost_usd = gas_est * gas_price_now / 1e18 * eth_usd
    print(f"[execute_base] {label}: оценка газа ~{gas_est} units, ~${est_cost_usd:.4f}")
    if est_cost_usd > PER_TX_GAS_CEILING_USD:
        raise RuntimeError(f"{label}: оценка газа ${est_cost_usd:.4f} > потолка ${PER_TX_GAS_CEILING_USD} -- СТОП.")
    last_err = None
    tx_hash = None
    for buf in GAS_RETRY_BUFFERS:
        gp = eth_gas_price()
        try:
            tx_hash = send_tx(account, to, data, nonce, gas_est, gp, buf)
            break
        except RuntimeError as e:
            if "max fee per gas" not in str(e).lower():
                raise
            last_err = e
            print(f"[execute_base] {label}: gas-race, повтор со следующим буфером")
    if tx_hash is None:
        raise RuntimeError(f"{label}: gas-race не преодолён -- {last_err}")
    print(f"[execute_base] {label}: ОТПРАВЛЕНО {tx_hash}, жду квитанцию...")
    receipt = wait_for_receipt(tx_hash)
    status = int(receipt["status"], 16)
    entry = {"label": label, "tx_hash": tx_hash, "status": "success" if status == 1 else "REVERTED",
              "gas_used": int(receipt["gasUsed"], 16), "block_number": int(receipt["blockNumber"], 16)}
    progress.setdefault("txs", []).append(entry)
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    if status != 1:
        raise RuntimeError(f"{label} REVERTED: {tx_hash} -- СТОП.")
    print(f"[execute_base] {label}: SUCCESS, gas_used={entry['gas_used']}")
    return receipt


def do_swap(account, label: str, token_in: str, token_out: str, fee: int, decimals_in: int,
            eth_usd: float, progress: dict) -> None:
    """Свопает ВЕСЬ реальный (только что прочитанный, RAW-целое, БЕЗ
    прохода через float human-round-trip -- на реальные деньги не
    полагаемся на округление float64 при пересчёте назад в raw) баланс
    token_in."""
    print(f"\n=== {label}: {token_in} -> {token_out}, fee={fee/10000:.2f}% ===")

    print("--- PRE-FLIGHT: реальный баланс перед свопом ---")
    amount_raw = erc20_balance(token_in)
    print(f"[execute_base] реальный баланс token_in (RAW): {amount_raw} ({amount_raw / 10**decimals_in})")
    if amount_raw <= 0:
        print(f"[execute_base] {label}: баланс 0 -- нечего свопать, пропуск")
        return

    print("--- Эмпирическая проверка структуры calldata (eth_call-симуляция, 0 газа) ---")
    simulated_out = simulate_swap(token_in, token_out, fee, amount_raw)
    print(f"[execute_base] симуляция прошла, ожидаемый output raw = {simulated_out}")
    if simulated_out <= 0:
        raise RuntimeError(f"{label}: симуляция вернула 0 или отрицательное -- СТОП, не гадаем структуру calldata дальше.")

    min_out_raw = int(simulated_out * (1 - SWAP_SLIPPAGE))

    allowance = erc20_allowance(token_in, ROUTER)
    nonce = eth_nonce()
    if allowance < amount_raw:
        approve_data = APPROVE_SELECTOR + abi_encode(["address", "uint256"], [to_checksum_address(ROUTER), amount_raw])
        send_and_wait(account, f"{label}_approve", token_in, approve_data, nonce, eth_usd, progress)
        nonce += 1
    else:
        print(f"[execute_base] {label}: allowance уже достаточен ({allowance/10**decimals_in}), approve не нужен")

    swap_data = build_swap_calldata(token_in, token_out, fee, WALLET, amount_raw, min_out_raw)
    send_and_wait(account, f"{label}_swap", ROUTER, swap_data, nonce, eth_usd, progress)


def run() -> int:
    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении -- СТОП.")
    account = Account.from_key(bytes.fromhex(priv_hex[2:] if priv_hex.startswith("0x") else priv_hex))
    if account.address.lower() != WALLET.lower():
        raise RuntimeError(f"PRIVATE_KEY_NOX даёт {account.address}, ожидался {WALLET} -- СТОП.")

    chain_id = int(rpc("eth_chainId", []), 16)
    if chain_id != BASE_CHAIN_ID:
        raise RuntimeError(f"chainId Base {chain_id} != {BASE_CHAIN_ID} -- СТОП")
    print(f"[execute_base] RPC подтверждён: chainId={chain_id}, аккаунт={account.address}")

    dry = json.loads(DRYRUN_PATH.read_text())
    eth_usd = dry["prices"]["eth_usd"]

    progress = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "txs": []}
    if OUT_PATH.exists():
        progress = json.loads(OUT_PATH.read_text())
        print(f"[execute_base] найден progress-файл с {len(progress.get('txs', []))} уже отправленными tx -- продолжаю идемпотентно")

    print(f"[execute_base] РЕАЛЬНЫЕ балансы сейчас: cbBTC={erc20_balance(CBBTC)/10**CBBTC_DECIMALS}, "
          f"USDC={erc20_balance(USDC)/10**USDC_DECIMALS}")

    already_done = {t["label"] for t in progress.get("txs", []) if t["status"] == "success"}
    if "cbbtc_to_weth_swap" not in already_done:
        do_swap(account, "cbbtc_to_weth", CBBTC, WETH, CBBTC_WETH_POOL_FEE, CBBTC_DECIMALS, eth_usd, progress)
    else:
        print("[execute_base] cbBTC->WETH уже сделан -- пропуск")

    if "usdc_to_weth_swap" not in already_done:
        do_swap(account, "usdc_to_weth", USDC, WETH, USDC_WETH_POOL_FEE, USDC_DECIMALS, eth_usd, progress)
    else:
        print("[execute_base] USDC->WETH уже сделан -- пропуск")

    weth_final = erc20_balance(WETH) / 10 ** WETH_DECIMALS
    print(f"\n[execute_base] РЕАЛЬНЫЙ финальный баланс WETH на Base: {weth_final}")
    progress["weth_final_base"] = weth_final
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"[execute_base] ГОТОВО. Результат в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
