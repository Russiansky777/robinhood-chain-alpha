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
состояние): пробуем 7-поле (без deadline) структуру, затем 8-поле
(с deadline, classic SwapRouter v1) как fallback.

РЕАЛЬНАЯ НАХОДКА (run 33975504003, 2026-09-05): первая попытка сообщила
"ОБЕ структуры ABI провалились" -- но реальная причина была НЕ ABI, а
порядок действий: симуляция exactInputSingle шла ДО approve, а router
не может выполнить внутренний transferFrom без allowance (падает
одинаково для любой структуры). Деньги не потрачены (симуляция
сработала как задумано), но диагноз был вводящим в заблуждение. Фикс:
approve СНАЧАЛА (дёшево, стандартно, не трогает основную сумму),
симуляция ПОСЛЕ, когда allowance уже реально в состоянии сети -- только
тогда результат симуляции говорит именно про структуру ABI, не про
allowance.

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

CBBTC_WETH_POOL_FEE = 3000  # РЕАЛЬНО перепроверено on-chain (diag run 33976559782): fee=2500
# из plan_v2 -- пул с ТАКИМ fee НЕ СУЩЕСТВУЕТ на каноническом Uniswap V3 Factory
# (factory.getPool() вернул нулевой адрес) -- offchain-атрибуция (Dune/GT) была
# неверной. Реально существующие тиры: 100/500/3000/10000; fee=3000 самый
# ликвидный (2524 WETH резерва) И eth_call-симуляция exactInputSingle на нём
# реально прошла (output=0.0146488 WETH).
USDC_WETH_POOL_FEE = 3000  # РЕАЛЬНО перепроверено on-chain (diag run 33976559782):
# тот же адрес пула, что и в plan_v2, реально самый ликвидный (19484 WETH резерва) -- подтверждено, не менялось.
SWAP_SLIPPAGE = 0.03  # тот же широкий допуск, что P5/P6 -- приоритет гарантированного исполнения
PER_TX_GAS_CEILING_USD = 3.0

OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_execute_base_result.json")
DRYRUN_PATH = Path("data/p3_guard_cache/asset_consolidation_dryrun_result.json")

APPROVE_SELECTOR = _selector("approve(address,uint256)")
BALANCE_OF_SELECTOR = "0x70a08231"
# Два реальных кандидата на ABI exactInputSingle -- проверяются по
# очереди через eth_call-симуляцию (0 газа), НЕ угадывается вслепую на
# реальные деньги. Первая попытка (SwapRouter02, БЕЗ deadline) реально
# провалилась (run 33975314247, "execution reverted", деньги НЕ
# потрачены -- симуляция сработала как задумано) -- пробуем classic
# SwapRouter v1 (С deadline), тот же struct-стиль, что уже реально
# работал в P5/P6 (там с int24 tickSpacing вместо uint24 fee).
EXACT_INPUT_SINGLE_SIG_NO_DEADLINE = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
EXACT_INPUT_SINGLE_SIG_WITH_DEADLINE = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
EXACT_INPUT_SINGLE_SELECTOR_NO_DEADLINE = _selector(EXACT_INPUT_SINGLE_SIG_NO_DEADLINE)
EXACT_INPUT_SINGLE_SELECTOR_WITH_DEADLINE = _selector(EXACT_INPUT_SINGLE_SIG_WITH_DEADLINE)


def rpc(method: str, params: list):
    r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        # РЕАЛЬНАЯ НАХОДКА: если ошибку класть ПОСЛЕ params (particularly
        # длинного calldata у eth_call), любая truncation на фиксированную
        # длину строки (str(exc)[:N]) отрезает саму ошибку, а не params --
        # мы реально теряли текст revert-причины. Кладём ошибку ПЕРВОЙ.
        raise RuntimeError(f"{body['error']} (метод={method})")
    return body["result"]


def erc20_balance(token: str) -> int:
    padded = WALLET[2:].lower().rjust(64, "0")
    return int(rpc("eth_call", [{"to": token, "data": BALANCE_OF_SELECTOR + padded}, "latest"]), 16)


ALLOWANCE_SELECTOR = _selector("allowance(address,address)")


def erc20_allowance(token: str, spender: str) -> int:
    data = "0x" + ALLOWANCE_SELECTOR.hex() + WALLET[2:].lower().rjust(64, "0") + spender[2:].lower().rjust(64, "0")
    return int(rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def build_swap_calldata_no_deadline(token_in: str, token_out: str, fee: int, recipient: str, amount_in: int, amount_out_min: int) -> bytes:
    params = (to_checksum_address(token_in), to_checksum_address(token_out), fee, to_checksum_address(recipient),
              amount_in, amount_out_min, 0)
    encoded = abi_encode(["(address,address,uint24,address,uint256,uint256,uint160)"], [params])
    return EXACT_INPUT_SINGLE_SELECTOR_NO_DEADLINE + encoded


def build_swap_calldata_with_deadline(token_in: str, token_out: str, fee: int, recipient: str, deadline: int, amount_in: int, amount_out_min: int) -> bytes:
    params = (to_checksum_address(token_in), to_checksum_address(token_out), fee, to_checksum_address(recipient),
              deadline, amount_in, amount_out_min, 0)
    encoded = abi_encode(["(address,address,uint24,address,uint256,uint256,uint256,uint160)"], [params])
    return EXACT_INPUT_SINGLE_SELECTOR_WITH_DEADLINE + encoded


def simulate_call(data: bytes) -> tuple[bool, int | None, str | None]:
    """eth_call СИМУЛЯЦИЯ (0 газа, не меняет состояние). Возвращает
    (успех, output_raw|None, ошибка|None) -- НЕ бросает исключение при
    revert, чтобы вызывающий код мог перебрать несколько кандидатов
    ABI, не падая на первом же."""
    try:
        result = rpc("eth_call", [{"from": WALLET, "to": ROUTER, "data": "0x" + data.hex()}, "latest"])
        return True, int(result, 16), None
    except RuntimeError as e:
        return False, None, str(e)[:300]


def simulate_swap(token_in: str, token_out: str, fee: int, amount_in: int) -> tuple[str, int]:
    """Перебирает РЕАЛЬНЫЕ кандидаты ABI через eth_call-симуляцию (0
    газа, деньги не тратятся), возвращает (какая структура сработала,
    ожидаемый output). Обе попытки провалились -- explicit RuntimeError,
    не гадаем дальше на реальные деньги."""
    data_no_dl = build_swap_calldata_no_deadline(token_in, token_out, fee, WALLET, amount_in, 0)
    ok, out, err = simulate_call(data_no_dl)
    if ok and out and out > 0:
        print(f"[execute_base] структура БЕЗ deadline (SwapRouter02) сработала, output={out}")
        return "no_deadline", out
    print(f"[execute_base] структура БЕЗ deadline провалилась ({err}) -- пробую С deadline (classic SwapRouter v1)")

    deadline = int(time.time()) + 600
    data_with_dl = build_swap_calldata_with_deadline(token_in, token_out, fee, WALLET, deadline, amount_in, 0)
    ok2, out2, err2 = simulate_call(data_with_dl)
    if ok2 and out2 and out2 > 0:
        print(f"[execute_base] структура С deadline (classic SwapRouter v1) сработала, output={out2}")
        return "with_deadline", out2

    raise RuntimeError(f"ОБЕ структуры ABI провалились на реальном router {ROUTER} -- "
                        f"без deadline: {err}; с deadline: {err2} -- СТОП, не гадаем дальше на реальные деньги.")


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
            eth_usd: float, progress: dict, already_done: set[str]) -> None:
    """Свопает ВЕСЬ реальный (только что прочитанный, RAW-целое, БЕЗ
    прохода через float human-round-trip -- на реальные деньги не
    полагаемся на округление float64 при пересчёте назад в raw) баланс
    token_in.

    РЕАЛЬНАЯ НАХОДКА (run 33975504003): симуляция exactInputSingle
    ДО approve всегда падает -- router не может сделать внутренний
    transferFrom без allowance, независимо от того, какая ABI-структура
    верна. Это выглядело как "ОБЕ структуры ABI провалились", хотя
    реальная причина -- порядок действий, не ABI. Фикс: approve СНАЧАЛА
    (дёшево, стандартно, не трогает основную сумму), симуляция ПОСЛЕ,
    когда allowance уже реально в состоянии сети."""
    print(f"\n=== {label}: {token_in} -> {token_out}, fee={fee/10000:.2f}% ===")

    print("--- PRE-FLIGHT: реальный баланс перед свопом ---")
    amount_raw = erc20_balance(token_in)
    print(f"[execute_base] реальный баланс token_in (RAW): {amount_raw} ({amount_raw / 10**decimals_in})")
    if amount_raw <= 0:
        print(f"[execute_base] {label}: баланс 0 -- нечего свопать, пропуск")
        return

    nonce = eth_nonce()
    allowance = erc20_allowance(token_in, ROUTER)
    if f"{label}_approve" in already_done:
        print(f"[execute_base] {label}: approve уже сделан ранее (progress-файл) -- пропуск")
    elif allowance < amount_raw:
        approve_data = APPROVE_SELECTOR + abi_encode(["address", "uint256"], [to_checksum_address(ROUTER), amount_raw])
        send_and_wait(account, f"{label}_approve", token_in, approve_data, nonce, eth_usd, progress)
        nonce += 1
    else:
        print(f"[execute_base] {label}: allowance уже достаточен ({allowance/10**decimals_in}), approve не нужен")

    print("--- Эмпирическая проверка структуры calldata (eth_call-симуляция, 0 газа, ПОСЛЕ approve) ---")
    abi_variant, simulated_out = simulate_swap(token_in, token_out, fee, amount_raw)
    print(f"[execute_base] структура ABI, реально подтверждённая симуляцией: {abi_variant}, ожидаемый output raw = {simulated_out}")

    min_out_raw = int(simulated_out * (1 - SWAP_SLIPPAGE))

    if abi_variant == "no_deadline":
        swap_data = build_swap_calldata_no_deadline(token_in, token_out, fee, WALLET, amount_raw, min_out_raw)
    else:
        swap_data = build_swap_calldata_with_deadline(token_in, token_out, fee, WALLET, int(time.time()) + 600, amount_raw, min_out_raw)
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
        do_swap(account, "cbbtc_to_weth", CBBTC, WETH, CBBTC_WETH_POOL_FEE, CBBTC_DECIMALS, eth_usd, progress, already_done)
    else:
        print("[execute_base] cbBTC->WETH уже сделан -- пропуск")

    if "usdc_to_weth_swap" not in already_done:
        do_swap(account, "usdc_to_weth", USDC, WETH, USDC_WETH_POOL_FEE, USDC_DECIMALS, eth_usd, progress, already_done)
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
