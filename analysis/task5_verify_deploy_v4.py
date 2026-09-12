#!/usr/bin/env python3
"""Задача 5, деплой V4 -- честная read-only проверка РЕАЛЬНОГО результата
деплоя `ClosedCycleExecutorV4` (после `scripts/deploy_executor_v4.sh`,
запущенного владельцем через `run_task5_deploy_executor_v4.yml`).

Отличие от `task5_verify_deploy.py` (V3): контракт V4 имеет ДВА immutable
(owner, poolManager), не один. Оба -- адреса, оба инлайнятся компилятором
как `PUSH32 <32-байтное слово>` в КАЖДОМ месте чтения (не один раз) --
шаблон `deployedBytecode.txt` содержит placeholder `7f` + 32 нулевых
байта на КАЖДОЙ такой позиции, ОДИНАКОВЫЙ байт-в-байт для owner и
poolManager (до подстановки они неразличимы по виду). Прямая замена
"все нули -> один адрес" была бы НЕВЕРНА -- часть позиций должна стать
owner, часть -- poolManager.

Метод: для каждой найденной позиции плейсхолдера сравниваем РЕАЛЬНЫЙ
ончейн-байт-код в той же позиции с ОБОИМИ ожидаемыми словами (owner,
poolManager) -- какое совпало, то там и было; если НИ ОДНО не совпало --
честно фиксируем несовпадение, не гадаем. Все остальные (не-placeholder)
байты шаблона и ончейн-кода должны совпасть побайтово без всяких замен.

Три проверки, все -- read-only JSON-RPC (`eth_getCode`, `eth_call`),
НИКАКОЙ подписи/отправки:
1. eth_getCode == deployedBytecode.txt после подстановки обоих immutable.
2. eth_call на owner() (0x8da5cb5b) и poolManager() (0xdc4c90d3, обе
   сигнатуры пересчитаны независимо через eth_utils, не из памяти)."""
from __future__ import annotations

import json
import os
import sys

from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")

OWNER_SELECTOR = "0x8da5cb5b"        # keccak256("owner()")[:4]
POOL_MANAGER_SELECTOR = "0xdc4c90d3"  # keccak256("poolManager()")[:4] -- пересчитан этой сессией

PLACEHOLDER = "7f" + "00" * 32  # PUSH32 <32 нулевых байт>, 33 байта = 66 hex-символов


def _find_all(haystack: str, needle: str) -> list[int]:
    positions = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 2  # шаг в 1 байт (2 hex-символа) -- на случай перекрывающихся вхождений
    return positions


def main() -> int:
    deploy_result_path = os.path.join(REPO_ROOT, "contracts", "build", "deploy_result_v4.json")
    deploy_params_path = os.path.join(REPO_ROOT, "contracts", "build", "deploy_params_v4.json")
    deployed_bytecode_path = os.path.join(REPO_ROOT, "contracts", "build",
                                           "ClosedCycleExecutorV4.deployedBytecode.txt")

    deploy_result = json.load(open(deploy_result_path))
    deploy_params = json.load(open(deploy_params_path))
    expected_deployed_bytecode = open(deployed_bytecode_path).read().strip()

    contract_address = deploy_result["contract_address"]
    expected_owner = deploy_params["expected_owner_address"]
    expected_pool_manager = deploy_params["expected_pool_manager_address"]

    rpc = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    checksum_addr = Web3.to_checksum_address(contract_address)

    onchain_code = rpc.eth.get_code(checksum_addr).hex()
    if not onchain_code.startswith("0x"):
        onchain_code = "0x" + onchain_code

    template_body = expected_deployed_bytecode[2:].lower()
    onchain_body = onchain_code[2:].lower()

    owner_word = expected_owner[2:].lower().rjust(64, "0")
    pool_manager_word = expected_pool_manager[2:].lower().rjust(64, "0")

    bytecode_match_raw = template_body == onchain_body

    placeholder_positions = _find_all(template_body, PLACEHOLDER)
    n_placeholders = len(placeholder_positions)

    # Реконструируем "исправленный" шаблон посимвольно: на каждой позиции
    # placeholder'а подставляем РЕАЛЬНЫЙ ончейн-байт-код в этой позиции
    # (не гадаем заранее owner/poolManager) -- потом отдельно проверяем,
    # что подставленное значение равно ОДНОМУ из двух ожидаемых слов.
    patched = list(template_body)
    resolutions: list[dict] = []
    same_length = len(template_body) == len(onchain_body)
    if same_length:
        for pos in placeholder_positions:
            onchain_slice = onchain_body[pos:pos + 66]  # "7f" + 64 hex симв. слова
            onchain_word = onchain_slice[2:]
            if onchain_word == owner_word:
                resolved_as = "owner"
            elif onchain_word == pool_manager_word:
                resolved_as = "poolManager"
            else:
                resolved_as = None
            resolutions.append({"byte_offset": pos // 2, "resolved_as": resolved_as,
                                 "onchain_word": onchain_word})
            if resolved_as is not None:
                patched[pos:pos + 66] = onchain_slice

    patched_str = "".join(patched)
    n_resolved_owner = sum(1 for r in resolutions if r["resolved_as"] == "owner")
    n_resolved_pool_manager = sum(1 for r in resolutions if r["resolved_as"] == "poolManager")
    n_unresolved = sum(1 for r in resolutions if r["resolved_as"] is None)

    bytecode_match_after_immutable_substitution = (
        same_length and n_placeholders > 0 and n_unresolved == 0 and patched_str == onchain_body
    )

    owner_call_result = rpc.eth.call({"to": checksum_addr, "data": OWNER_SELECTOR})
    onchain_owner = Web3.to_checksum_address("0x" + owner_call_result.hex()[-40:])
    owner_match = onchain_owner.lower() == expected_owner.lower()

    pool_manager_call_result = rpc.eth.call({"to": checksum_addr, "data": POOL_MANAGER_SELECTOR})
    onchain_pool_manager = Web3.to_checksum_address("0x" + pool_manager_call_result.hex()[-40:])
    pool_manager_match = onchain_pool_manager.lower() == expected_pool_manager.lower()

    result = {
        "contract_address": contract_address,
        "tx_hash": deploy_result.get("tx_hash"),
        "block": deploy_result.get("block"),
        "rpc_url": RPC_URL,
        "onchain_code_len_bytes": len(onchain_body) // 2,
        "expected_code_len_bytes": len(template_body) // 2,
        "code_lengths_equal": same_length,
        "deployed_bytecode_matches_expected_raw": bytecode_match_raw,
        "n_immutable_placeholders_in_template": n_placeholders,
        "n_resolved_as_owner": n_resolved_owner,
        "n_resolved_as_pool_manager": n_resolved_pool_manager,
        "n_unresolved_placeholders": n_unresolved,
        "deployed_bytecode_matches_after_immutable_substitution": bytecode_match_after_immutable_substitution,
        "onchain_owner": onchain_owner,
        "expected_owner": expected_owner,
        "owner_matches_expected": owner_match,
        "onchain_pool_manager": onchain_pool_manager,
        "expected_pool_manager": expected_pool_manager,
        "pool_manager_matches_expected": pool_manager_match,
        "all_checks_passed": bool(
            (bytecode_match_raw or bytecode_match_after_immutable_substitution)
            and owner_match and pool_manager_match
        ),
    }
    print(json.dumps(result, indent=2))

    out_path = os.path.join(REPO_ROOT, "contracts", "build", "deploy_verification_v4.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    return 0 if result["all_checks_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
