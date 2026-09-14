#!/usr/bin/env python3
"""READ-ONLY: узнать адрес кошелька PRIVATE_KEY_NOX (тот же, с которого
деплоили ClosedCycleExecutorV4 -- см. scripts/deploy_executor_v4.sh) и
его реальный нативный баланс на Robinhood Chain. НИЧЕГО не подписывает
и не отправляет -- владелец попросил перевести остаток на свой кошелёк,
это диагностика ПЕРЕД отправкой (реальный баланс, не догадка)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from eth_account import Account  # noqa: E402
from alchemy_fallback import _rpc_call  # noqa: E402

PRIVATE_KEY_NOX = os.environ.get("PRIVATE_KEY_NOX")
if not PRIVATE_KEY_NOX:
    print(json.dumps({"error": "PRIVATE_KEY_NOX не найден в окружении"}))
    sys.exit(1)

acct = Account.from_key(PRIVATE_KEY_NOX)
address = acct.address

balance_wei_hex = _rpc_call("eth_getBalance", [address, "latest"])
balance_wei = int(balance_wei_hex, 16)
nonce_hex = _rpc_call("eth_getTransactionCount", [address, "latest"])
nonce = int(nonce_hex, 16)
gas_price_hex = _rpc_call("eth_gasPrice", [])
gas_price = int(gas_price_hex, 16)

# Стандартный перевод нативного ETH -- 21000 газа.
gas_limit = 21000
gas_cost_wei = gas_limit * gas_price
max_sendable_wei = balance_wei - gas_cost_wei

result = {
    "address": address,
    "balance_wei": balance_wei,
    "balance_eth": balance_wei / 1e18,
    "nonce": nonce,
    "gas_price_wei": gas_price,
    "gas_limit_standard_transfer": gas_limit,
    "gas_cost_wei": gas_cost_wei,
    "gas_cost_eth": gas_cost_wei / 1e18,
    "max_sendable_wei_leaving_zero_after": max_sendable_wei,
    "max_sendable_eth_leaving_zero_after": max_sendable_wei / 1e18,
}
print(json.dumps(result, indent=2))
Path(__file__).parent.parent.joinpath("data", "task5_sender_wallet_balance_check_result.json").write_text(
    json.dumps(result, indent=2)
)
