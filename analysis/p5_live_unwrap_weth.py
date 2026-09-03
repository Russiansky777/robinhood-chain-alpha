#!/usr/bin/env python3
"""P5 LIVE -- разворачивает WETH обратно в нативный ETH (WETH9.withdraw()).

Нужен после реального run 33780888659 (2026-09-03): шаг "1_wrap_ETH_to_WETH"
mint-последовательности прошёл (0.042359 ETH обёрнуто в WETH), но шаг
"4_mint" был отклонён (`maxFeePerGas < baseFee`, gas-race, см.
p5_live_step1.py) -- в кошельке осталось ~0.0424 WETH, которые
`p5_live_precheck.wallet_balances()`/`p5_live_step1.py` НЕ видят (читают
только нативный ETH и USDG) -- без разворота повторный запуск Step1
посчитал бы usable_eth почти нулевым, игнорируя уже обёрнутый WETH.

Возвращает кошелёк в чистое состояние ПЕРЕД повторной попыткой Step1.
approve()-разрешения на NFPM (WETH/USDG) не трогает -- retry их просто
перезапишет свежими значениями, старые не мешают.

По умолчанию -- DRY-RUN (читает баланс WETH, ничего не отправляет).
--confirm-mainnet -- реальная отправка (1 транзакция withdraw()).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import encode as abi_encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

from alchemy_fallback import _rpc_call, topic0  # noqa: E402
from p5_live_step1 import (  # noqa: E402
    WALLET, WETH, WETH_DECIMALS, CHAIN_ID,
    eth_estimate_gas, eth_gas_price, eth_nonce, send_tx, wait_for_receipt,
)

OUT_PATH = Path("data/p3_guard_cache/p5_live_unwrap_weth_result.json")


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def weth_balance() -> int:
    calldata = "0x" + _selector("balanceOf(address)")[2:] + abi_encode(["address"], [WALLET]).hex()
    raw = _rpc_call("eth_call", [{"to": to_checksum_address(WETH), "data": calldata}, "latest"])
    return int(raw, 16)


def main() -> int:
    confirm = "--confirm-mainnet" in sys.argv
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "mode": "REAL" if confirm else "DRY-RUN"}

    bal_wei = weth_balance()
    bal_eth = bal_wei / 10 ** WETH_DECIMALS
    result["weth_balance_wei"] = bal_wei
    result["weth_balance_eth"] = bal_eth
    print(f"[p5_live_unwrap_weth] WETH баланс: {bal_eth} ETH ({bal_wei} wei)")

    if bal_wei == 0:
        result["note"] = "WETH баланс уже 0 -- нечего разворачивать."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_unwrap_weth] {result['note']}")
        return 0

    if not confirm:
        result["note"] = "DRY-RUN -- ничего не отправлялось. Запустите с --confirm-mainnet."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_unwrap_weth] {result['note']}")
        return 0

    chain_id = int(_rpc_call("eth_chainId", []), 16)
    if chain_id != CHAIN_ID:
        result["abort_reason"] = f"chainId {chain_id} != {CHAIN_ID} -- СТОП"
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return 1

    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if priv_hex.startswith("0x"):
        priv_hex = priv_hex[2:]
    account = Account.from_key(bytes.fromhex(priv_hex))
    if account.address.lower() != WALLET.lower():
        raise RuntimeError(f"PRIVATE_KEY_NOX даёт {account.address}, ожидался {WALLET} -- СТОП.")

    calldata = bytes.fromhex(_selector("withdraw(uint256)")[2:]) + abi_encode(["uint256"], [bal_wei])
    gas_price = eth_gas_price()
    gas_est = eth_estimate_gas(WETH, calldata, 0)
    nonce = eth_nonce()
    tx_hash = send_tx(account, WETH, calldata, 0, nonce, gas_est, gas_price)
    print(f"[p5_live_unwrap_weth] ОТПРАВЛЕНО {tx_hash}, жду квитанцию...")
    receipt = wait_for_receipt(tx_hash)
    status = int(receipt["status"], 16)
    result["tx_hash"] = tx_hash
    result["status"] = "success" if status == 1 else "REVERTED"
    result["gas_used"] = int(receipt["gasUsed"], 16)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[p5_live_unwrap_weth] {result['status']}: {tx_hash}")
    return 0 if status == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
