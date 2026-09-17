#!/usr/bin/env python3
"""Задача 5 -- Часть А (обязательный первый шаг перед фондированным
замером доставки): проверить реальный баланс кошелька `PRIVATE_KEY_TASK5_BOT`
на Robinhood Chain, БЕЗ отправки чего-либо.

ЧЕСТНАЯ ОГОВОРКА, ВАЖНО: владелец в своём запросе назвал адрес
`0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75` как связанный с
`PRIVATE_KEY_TASK5_BOT` -- но по коду проекта (`task5_bot_config.py`,
`PRIVATE_KEY_ENV_VAR`/`EXPECTED_WALLET_ENV_VAR`) и по паспорту
(docs/PROJECT_STATE.md, множественные записи) этот КОНКРЕТНЫЙ адрес
документирован как кошелёк `PRIVATE_KEY_NOX` (P5/Lighter-хедж), а НЕ
`PRIVATE_KEY_TASK5_BOT` -- это ДВА РАЗНЫХ секрета проекта. Владелец
прямо указал "PRIVATE_KEY_NOX не трогать" -- поэтому этот скрипт
ЦЕЛЕНАПРАВЛЕННО читает ТОЛЬКО `PRIVATE_KEY_TASK5_BOT` из окружения и
НИКОГДА не обращается к `PRIVATE_KEY_NOX` ни для чтения баланса, ни для
чего-либо ещё -- реальный адрес, к которому относится PRIVATE_KEY_TASK5_BOT,
будет виден в выводе (может СОВПАСТЬ с названным владельцем адресом,
если это на самом деле один кошелёк на несколько ролей -- тогда это
подтвердится числом; может НЕ совпасть -- тогда это будет честно видно
и вопрос уйдёт обратно владельцу, а не будет тихо угадан).

ТОЛЬКО ЧТЕНИЕ: eth_getBalance, eth_gasPrice, eth_getTransactionCount.
Ничего не подписывается и не отправляется."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_account import Account
from eth_utils import to_checksum_address

from task5_bot_config import CHAIN_ID_MAINNET, PRIVATE_KEY_ENV_VAR, EXPECTED_WALLET_ENV_VAR
from alchemy_fallback import _rpc_call

N_PLANNED_SENDS = 3
GAS_LIMIT_SELF_TRANSFER = 21000


def main() -> None:
    result: dict = {
        "chain_id": CHAIN_ID_MAINNET,
        "private_key_env_var_checked": PRIVATE_KEY_ENV_VAR,
        "note_on_address_discrepancy": (
            "Владелец в запросе назвал 0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75 как адрес "
            "PRIVATE_KEY_TASK5_BOT -- по коду проекта и паспорту этот адрес документирован как "
            "кошелёк PRIVATE_KEY_NOX (другой секрет). PRIVATE_KEY_NOX в этом скрипте НЕ используется "
            "ни для чего -- см. докстринг. Реальный адрес PRIVATE_KEY_TASK5_BOT -- ниже, сверьте сами."
        ),
    }

    key = os.environ.get(PRIVATE_KEY_ENV_VAR, "")
    if not key:
        result["error"] = f"{PRIVATE_KEY_ENV_VAR} не задан в окружении этого хоста -- нечего проверять."
        print(json.dumps(result, indent=2, ensure_ascii=False))
        _write(result)
        return

    account = Account.from_key(key)
    address = to_checksum_address(account.address)
    result["derived_address"] = address

    expected = os.environ.get(EXPECTED_WALLET_ENV_VAR, "")
    if expected:
        result["expected_wallet_env_value"] = expected
        result["address_matches_expected_env"] = (to_checksum_address(expected) == address)
    else:
        result["expected_wallet_env_value"] = None
        result["address_matches_expected_env"] = None

    balance_wei = int(_rpc_call("eth_getBalance", [address, "latest"]), 16)
    gas_price_wei = int(_rpc_call("eth_gasPrice", []), 16)
    nonce = int(_rpc_call("eth_getTransactionCount", [address, "latest"]), 16)

    balance_native = balance_wei / 1e18
    cost_per_tx_wei = GAS_LIMIT_SELF_TRANSFER * gas_price_wei
    cost_total_wei = cost_per_tx_wei * N_PLANNED_SENDS
    cost_total_native = cost_total_wei / 1e18

    result.update({
        "balance_wei": balance_wei,
        "balance_native": balance_native,
        "current_gas_price_wei": gas_price_wei,
        "current_nonce": nonce,
        "planned_n_sends": N_PLANNED_SENDS,
        "gas_limit_per_send": GAS_LIMIT_SELF_TRANSFER,
        "estimated_cost_total_wei": cost_total_wei,
        "estimated_cost_total_native": cost_total_native,
        "sufficient_for_planned_test": balance_wei > cost_total_wei * 3,  # честный запас 3x на случай роста gasPrice
        "margin_multiple_used": 3,
    })

    print(json.dumps(result, indent=2, ensure_ascii=False))
    _write(result)


def _write(result: dict) -> None:
    out_path = Path(__file__).resolve().parent.parent / "data" / "task5_wallet_balance_check_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
