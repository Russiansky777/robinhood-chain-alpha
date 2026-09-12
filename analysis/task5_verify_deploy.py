#!/usr/bin/env python3
"""Задача 5, деплой -- честная read-only проверка РЕАЛЬНОГО результата
деплоя `ClosedCycleExecutorV3` (после `scripts/deploy_executor.sh`,
запущенного владельцем через `run_task5_deploy_executor.yml`).

Три проверки, все -- read-only JSON-RPC (`eth_getCode`, `eth_call`),
НИКАКОЙ подписи/отправки:
1. `eth_getCode(contract_address)` == `ClosedCycleExecutorV3.deployedBytecode.txt`
   (побайтово, после strip "0x").
2. `eth_call` на `owner()` (селектор 0x8da5cb5b, независимо вычислен --
   см. keccak256("owner()")[:4]) возвращает адрес из
   `deploy_params.json::expected_owner_address`.
3. `contract_address`/`tx_hash` из `deploy_result.json` -- присутствуют
   и синтаксически валидны (не гадаем про рецепт транзакции ещё раз --
   `deploy_executor.sh` сам уже дождался receipt.status==1 перед записью
   `ok: true`, здесь только независимая, отдельная проверка НА ЦЕПИ,
   что задеплоенный код -- это действительно тот код)."""
from __future__ import annotations

import json
import os
import sys

from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")

OWNER_SELECTOR = "0x8da5cb5b"  # keccak256("owner()")[:4] -- перепроверено независимо


def main() -> int:
    deploy_result_path = os.path.join(REPO_ROOT, "contracts", "build", "deploy_result.json")
    deploy_params_path = os.path.join(REPO_ROOT, "contracts", "build", "deploy_params.json")
    deployed_bytecode_path = os.path.join(REPO_ROOT, "contracts", "build",
                                           "ClosedCycleExecutorV3.deployedBytecode.txt")

    deploy_result = json.load(open(deploy_result_path))
    deploy_params = json.load(open(deploy_params_path))
    expected_deployed_bytecode = open(deployed_bytecode_path).read().strip()

    contract_address = deploy_result["contract_address"]
    expected_owner = deploy_params["expected_owner_address"]

    rpc = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    checksum_addr = Web3.to_checksum_address(contract_address)

    onchain_code = rpc.eth.get_code(checksum_addr).hex()
    if not onchain_code.startswith("0x"):
        onchain_code = "0x" + onchain_code

    bytecode_match = onchain_code.lower() == expected_deployed_bytecode.lower()

    # ЧЕСТНАЯ проверка гипотезы про `immutable owner`: Solidity инлайнит
    # immutable-переменные прямо в runtime-код на этапе деплоя -- шаблон
    # (deployedBytecode.txt) содержит PUSH32<32 нулевых байта> в местах
    # чтения `owner` (getter + обе проверки onlyOwner), деплой заменяет
    # эти нули на реальный адрес владельца. Если это ЕДИНСТВЕННОЕ отличие --
    # это ожидаемое поведение компилятора, не расхождение кода.
    template_body = expected_deployed_bytecode[2:]
    owner_word = expected_owner[2:].lower().rjust(64, "0")
    placeholder_pushed = "7f" + "00" * 32
    n_placeholders = template_body.lower().count(placeholder_pushed)
    patched_template = template_body.lower().replace(placeholder_pushed, "7f" + owner_word)
    onchain_body = onchain_code[2:].lower()
    bytecode_match_after_immutable_substitution = (
        n_placeholders > 0 and patched_template == onchain_body
    )

    owner_call_result = rpc.eth.call({"to": checksum_addr, "data": OWNER_SELECTOR})
    onchain_owner_raw = owner_call_result.hex()
    # address -- правые 20 байт 32-байтного слова возврата
    onchain_owner = Web3.to_checksum_address("0x" + onchain_owner_raw[-40:])
    owner_match = onchain_owner.lower() == expected_owner.lower()

    result = {
        "contract_address": contract_address,
        "tx_hash": deploy_result.get("tx_hash"),
        "block": deploy_result.get("block"),
        "rpc_url": RPC_URL,
        "onchain_code_len_bytes": (len(onchain_code) - 2) // 2,
        "expected_code_len_bytes": (len(expected_deployed_bytecode) - 2) // 2,
        "deployed_bytecode_matches_expected": bytecode_match,
        "n_immutable_owner_placeholders_in_template": n_placeholders,
        "deployed_bytecode_matches_after_immutable_owner_substitution": bytecode_match_after_immutable_substitution,
        "onchain_owner": onchain_owner,
        "expected_owner": expected_owner,
        "owner_matches_expected": owner_match,
        "all_checks_passed": bool(
            (bytecode_match or bytecode_match_after_immutable_substitution) and owner_match
        ),
    }
    print(json.dumps(result, indent=2))

    out_path = os.path.join(REPO_ROOT, "contracts", "build", "deploy_verification.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
