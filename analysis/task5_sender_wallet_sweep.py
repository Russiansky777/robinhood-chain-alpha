#!/usr/bin/env python3
"""РЕАЛЬНАЯ, НЕОБРАТИМАЯ отправка. Владелец (явно, трижды подтверждено
в чате, в т.ч. точным указанием кошелька и причины): перевести остаток
нативного ETH с кошелька PRIVATE_KEY_NOX (0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75
-- тот же адрес, что и во всём проекте фигурирует как "owner"; тот же
ключ, с которого деплоился ClosedCycleExecutorV4, см. scripts/
deploy_executor_v4.sh) на его собственный рабочий кошелёк
0x38b69eB7fEC46C8aDac7e456723FFF50C855022b, т.к. "бот не состоялся".

Метод сборки/подписи -- ТОТ ЖЕ проверенный паттерн, что в task5_bot_
sender.py (EIP-1559, Account.sign_transaction, w3.eth.send_raw_
transaction) -- не изобретается заново для реальной отправки денег.
Nonce/gasPrice читаются ЗАНОВО непосредственно перед подписью (не
переиспользуется более ранний read-only снимок) -- избегаем гонки с
устаревшим nonce."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

RPC_URL = os.environ.get("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
CHAIN_ID = 4663
DESTINATION_RAW = "0x38b69eB7fEC46C8aDac7e456723FFF50C855022b"
GAS_LIMIT = 21000

PRIVATE_KEY_NOX = os.environ.get("PRIVATE_KEY_NOX")
if not PRIVATE_KEY_NOX:
    print(json.dumps({"error": "PRIVATE_KEY_NOX не найден в окружении"}))
    sys.exit(1)

# Явная проверка checksum адреса получателя -- ловит опечатку ДО подписи.
try:
    destination = Web3.to_checksum_address(DESTINATION_RAW)
except ValueError as exc:
    print(json.dumps({"error": f"адрес получателя не прошёл checksum-проверку: {exc}"}))
    sys.exit(1)

w3 = Web3(Web3.HTTPProvider(RPC_URL))
account = Account.from_key(PRIVATE_KEY_NOX)
address = account.address

if address.lower() != "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75".lower():
    print(json.dumps({"error": f"адрес, полученный из PRIVATE_KEY_NOX ({address}), "
                                f"НЕ совпадает с ожидаемым владельцем -- остановлено"}))
    sys.exit(1)

balance_wei = w3.eth.get_balance(address)
nonce = w3.eth.get_transaction_count(address, "latest")
try:
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    max_fee = base_fee * 2 + 1_000_000_000  # +1 gwei priority, тот же стиль, что task5_bot_sender.py
    priority_fee = 1_000_000_000
    tx_type = 2
except Exception:  # noqa: BLE001
    max_fee = w3.eth.gas_price
    priority_fee = None
    tx_type = 0

gas_cost_wei = GAS_LIMIT * max_fee
value_wei = balance_wei - gas_cost_wei

if value_wei <= 0:
    print(json.dumps({"error": "баланс не покрывает даже газ на перевод",
                       "balance_wei": balance_wei, "gas_cost_wei": gas_cost_wei}))
    sys.exit(1)

tx = {
    "chainId": CHAIN_ID, "nonce": nonce, "to": destination, "value": value_wei, "gas": GAS_LIMIT,
}
if tx_type == 2:
    tx.update({"type": 2, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee})
else:
    tx.update({"gasPrice": max_fee})

signed = account.sign_transaction(tx)
raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
local_tx_hash = (signed.hash.hex() if hasattr(signed, "hash") else Web3.keccak(raw).hex())
if not local_tx_hash.startswith("0x"):
    local_tx_hash = "0x" + local_tx_hash

result = {
    "from": address, "to": destination, "value_wei": value_wei, "value_eth": value_wei / 1e18,
    "nonce": nonce, "local_tx_hash": local_tx_hash,
}
try:
    sent_hash = w3.eth.send_raw_transaction(raw)
    result["broadcast_ok"] = True
    result["broadcast_tx_hash"] = sent_hash.hex() if hasattr(sent_hash, "hex") else str(sent_hash)
except Exception as exc:  # noqa: BLE001
    result["broadcast_ok"] = False
    result["broadcast_error"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(result, indent=2, default=str))
Path(__file__).parent.parent.joinpath("data", "task5_sender_wallet_sweep_result.json").write_text(
    json.dumps(result, indent=2, default=str)
)
