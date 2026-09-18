#!/usr/bin/env python3
"""Задача 5 -- Часть А (обязательный первый шаг перед фондированным
замером доставки): проверить реальные балансы на Robinhood Chain, БЕЗ
отправки чего-либо.

ЧЕСТНАЯ ОГОВОРКА про адрес, важно. Владелец в текущем запросе назвал
`0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75` как кошелёк
`PRIVATE_KEY_TASK5_BOT`. Прямая проверка паспорта (docs/PROJECT_STATE.md)
показала: этот ЖЕ адрес РАНЕЕ в этой сессии (2026-09-13) был явно назван
владельцем "боевым кошельком" для Задачи 5 и проверялся ИМЕННО с целью
"хватит ли ETH на пилот" (`run_task5_wallet_balance_check.yml`, более
ранняя версия) -- то есть владелец, по всей видимости, ПРАВ и это
РЕАЛЬНО тот кошелёк, о котором идёт речь оперативно, даже если код
(`task5_bot_config.py`) формально ожидает ОТДЕЛЬНЫЙ секрет
`PRIVATE_KEY_TASK5_BOT`, а НЕ `PRIVATE_KEY_NOX` (тот же адрес
одновременно фигурирует в паспорте и как кошелёк `PRIVATE_KEY_NOX`/
P5-хедж в других записях -- возможно, один EOA используется на
несколько ролей).

Это скрипт проверяет ОБА варианта, без каких-либо предположений:
1. Баланс АДРЕСА `0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75` НАПРЯМУЮ
   (eth_getBalance по адресу -- ПУБЛИЧНЫЙ вызов, НЕ требует и НЕ
   использует НИКАКОЙ приватный ключ, в том числе PRIVATE_KEY_NOX --
   чтение баланса чужого/любого адреса не "трогает" секрет).
2. Баланс адреса, выведенного из секрета `PRIVATE_KEY_TASK5_BOT`, ЕСЛИ
   он реально задан в окружении этого хоста (может быть другим
   адресом, может быть тем же -- увидим по факту).

PRIVATE_KEY_NOX НИГДЕ в этом скрипте не читается и не используется --
ни для расчёта, ни для потенциальной последующей отправки. Если
единственный реально профинансированный адрес окажется тем же, чей
приватный ключ -- PRIVATE_KEY_NOX, отправка с него в следующей части
задания НЕ производится (владелец: "PRIVATE_KEY_NOX не трогать") --
это будет явно отмечено в поле `usable_for_sending`.

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
NAMED_ADDRESS_FROM_OWNER = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"


def check_balance(address: str, gas_price_wei: int) -> dict:
    address = to_checksum_address(address)
    balance_wei = int(_rpc_call("eth_getBalance", [address, "latest"]), 16)
    nonce = int(_rpc_call("eth_getTransactionCount", [address, "latest"]), 16)
    cost_total_wei = GAS_LIMIT_SELF_TRANSFER * gas_price_wei * N_PLANNED_SENDS
    return {
        "address": address,
        "balance_wei": balance_wei,
        "balance_native": balance_wei / 1e18,
        "current_nonce": nonce,
        "estimated_cost_total_wei_for_3_sends": cost_total_wei,
        "estimated_cost_total_native_for_3_sends": cost_total_wei / 1e18,
        "sufficient_with_3x_margin": balance_wei > cost_total_wei * 3,
    }


def main() -> None:
    gas_price_wei = int(_rpc_call("eth_gasPrice", []), 16)

    result: dict = {
        "chain_id": CHAIN_ID_MAINNET,
        "current_gas_price_wei": gas_price_wei,
        "note": "PRIVATE_KEY_NOX нигде не читается и не используется в этом скрипте. "
                "eth_getBalance по адресу -- публичный вызов, ключ не требуется.",
    }

    result["named_address_from_owner_request"] = check_balance(NAMED_ADDRESS_FROM_OWNER, gas_price_wei)

    key = os.environ.get(PRIVATE_KEY_ENV_VAR, "")
    if not key:
        result["private_key_task5_bot"] = {
            "configured": False,
            "note": f"{PRIVATE_KEY_ENV_VAR} не задан в окружении этого хоста.",
        }
    else:
        account = Account.from_key(key)
        derived_address = to_checksum_address(account.address)
        expected = os.environ.get(EXPECTED_WALLET_ENV_VAR, "")
        entry = {"configured": True, **check_balance(derived_address, gas_price_wei)}
        entry["matches_named_address_from_owner"] = (derived_address == to_checksum_address(NAMED_ADDRESS_FROM_OWNER))
        if expected:
            entry["expected_wallet_env_value"] = expected
            entry["address_matches_expected_env"] = (to_checksum_address(expected) == derived_address)
        result["private_key_task5_bot"] = entry

    # Честный вывод: с какого адреса МОЖНО реально отправлять (ключ есть в
    # окружении И это НЕ PRIVATE_KEY_NOX) и хватает ли там средств.
    task5_entry = result.get("private_key_task5_bot", {})
    if task5_entry.get("configured") and task5_entry.get("sufficient_with_3x_margin"):
        result["usable_for_sending"] = {
            "possible": True,
            "via": "PRIVATE_KEY_TASK5_BOT",
            "address": task5_entry["address"],
        }
    elif result["named_address_from_owner_request"]["sufficient_with_3x_margin"] and not task5_entry.get("configured"):
        result["usable_for_sending"] = {
            "possible": False,
            "reason": (
                f"Названный владельцем адрес {NAMED_ADDRESS_FROM_OWNER} реально профинансирован, НО "
                f"{PRIVATE_KEY_ENV_VAR} не задан в окружении -- неизвестно, каким приватным ключом он "
                f"управляется. Если это PRIVATE_KEY_NOX -- отправка с него запрещена прямым указанием "
                f"владельца ('PRIVATE_KEY_NOX не трогать'). Нужно явное подтверждение владельца, каким "
                f"ключом реально подписывать, прежде чем отправлять что-либо."
            ),
        }
    else:
        result["usable_for_sending"] = {
            "possible": False,
            "reason": "Ни один проверенный адрес не профинансирован (с 3x запасом на газ) -- отправка невозможна.",
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    out_path = Path(__file__).resolve().parent.parent / "data" / "task5_wallet_balance_check_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
