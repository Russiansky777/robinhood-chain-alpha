#!/usr/bin/env python3
"""Задача 5, деплой V4 -- честная read-only проверка РЕАЛЬНОГО результата
деплоя `ClosedCycleExecutorV4` (после `scripts/deploy_executor_v4.sh`,
запущенного владельцем через `run_task5_deploy_executor_v4.yml`).

ПРАВКА 2026-09-13 (внешнее ревью, пункт 6 -- "верификатор байткода —
сверять immutable по позициям из компиляции, а не по любым нулевым
словам"): раньше верификатор искал ВСЕ вхождения байтового паттерна
`PUSH32 <32 нулевых байта>` в шаблоне и РАЗЛИЧАЛ owner/poolManager
ТОЛЬКО по тому, какое из двух ожидаемых слов совпало с реальным
ончейн-байтом в этой позиции (см. git-историю файла) -- работало, но
полагалось на СЛУЧАЙНОЕ несовпадение owner и poolManager между собой;
если бы оба адреса КОГДА-ЛИБО совпали (или деплой был бы с owner ==
poolManager по ошибке), метод не различил бы позиции вообще.

Теперь: contracts/build/ClosedCycleExecutorV4.immutables.json --
РЕАЛЬНЫЕ байтовые позиции (start, length) каждой immutable-переменной,
полученные ИЗ САМОГО КОМПИЛЯТОРА (forge build ->
evm.deployedBytecode.immutableReferences, ключ -- AST id переменной,
сопоставленный с её именем через AST, см.
task5_v4_executor_compile_check.py) -- НЕ угаданы постфактум. Каждая
позиция ОДНОЗНАЧНО приписана конкретному имени переменной компилятором
на этапе компиляции, а не подбором совпадения на этапе верификации.

Метод: реконструируем ОЖИДАЕМЫЙ задеплоенный байткод, подставив в
ТОЧНЫЕ известные позиции (из immutables.json) ожидаемые 32-байтные
слова owner/poolManager поверх шаблона deployedBytecode.txt (там на
этих позициях -- нулевые placeholder-слова) -- сравниваем результат с
РЕАЛЬНЫМ ончейн-байткодом побайтово. Совпадение везде, включая позиции
immutable, подтверждает: (а) задеплоен именно этот байткод, (б) с
именно этими значениями owner/poolManager -- одной проверкой, без
разрешения неоднозначности постфактум.

Три проверки, все -- read-only JSON-RPC (`eth_getCode`, `eth_call`),
НИКАКОЙ подписи/отправки:
1. eth_getCode == deployedBytecode.txt после подстановки immutable ПО
   ПОЗИЦИЯМ ИЗ КОМПИЛЯЦИИ.
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


def build_expected_bytecode_from_positions(template_hex: str, immutables: dict[str, list[dict]],
                                            values: dict[str, str]) -> tuple[str, list[dict]]:
    """template_hex -- deployedBytecode.txt БЕЗ '0x' (ещё с нулевыми
    placeholder-словами на местах immutable). immutables -- РЕАЛЬНЫЕ
    {name: [{"start","length"}, ...]} из компиляции (байтовые offset'ы,
    не hex-символьные). values -- {name: "0x..."} ожидаемые адреса.
    Возвращает (подставленный hex, список записей о каждой подстановке
    для отчёта)."""
    body = bytearray(bytes.fromhex(template_hex))
    substitutions: list[dict] = []
    for name, refs in immutables.items():
        if name not in values:
            raise ValueError(f"нет ожидаемого значения для immutable-переменной '{name}' "
                              f"(есть только: {list(values.keys())})")
        word = bytes.fromhex(values[name][2:].rjust(64, "0").lower())
        if len(word) != 32:
            raise ValueError(f"ожидаемое значение '{name}' не укладывается в 32-байтное слово: {values[name]}")
        for ref in refs:
            start, length = ref["start"], ref["length"]
            if length != 32:
                raise ValueError(f"неожиданная длина immutable-ссылки для '{name}': {ref} (ожидали 32)")
            before = bytes(body[start:start + length]).hex()
            body[start:start + length] = word
            substitutions.append({"name": name, "byte_offset": start, "template_word_before": before,
                                   "substituted_word": word.hex()})
    return body.hex(), substitutions


def main() -> int:
    deploy_result_path = os.path.join(REPO_ROOT, "contracts", "build", "deploy_result_v4.json")
    deploy_params_path = os.path.join(REPO_ROOT, "contracts", "build", "deploy_params_v4.json")
    deployed_bytecode_path = os.path.join(REPO_ROOT, "contracts", "build",
                                           "ClosedCycleExecutorV4.deployedBytecode.txt")
    immutables_path = os.path.join(REPO_ROOT, "contracts", "build", "ClosedCycleExecutorV4.immutables.json")

    deploy_result = json.load(open(deploy_result_path))
    deploy_params = json.load(open(deploy_params_path))
    expected_deployed_bytecode = open(deployed_bytecode_path).read().strip()

    if not os.path.exists(immutables_path):
        print(f"[verify_deploy_v4] ОШИБКА: {immutables_path} не найден -- запустите "
              f"task5_v4_executor_compile_check.py (forge build) заново, старый метод "
              f"по угадыванию плейсхолдеров убран по внешнему ревью.", file=sys.stderr)
        return 1
    immutables = json.load(open(immutables_path))
    unknown_names = [n for n in immutables if n.startswith("<unknown_ast_id_")]
    if unknown_names:
        print(f"[verify_deploy_v4] ОШИБКА: компилятор дал immutable-позиции, но имена НЕ разрешились "
              f"через AST: {unknown_names} -- не гадаем, честный отказ.", file=sys.stderr)
        return 1

    contract_address = deploy_result["contract_address"]
    expected_owner = deploy_params["expected_owner_address"]
    expected_pool_manager = deploy_params["expected_pool_manager_address"]

    rpc = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    checksum_addr = Web3.to_checksum_address(contract_address)

    onchain_code = rpc.eth.get_code(checksum_addr).hex()
    if not onchain_code.startswith("0x"):
        onchain_code = "0x" + onchain_code

    template_body = expected_deployed_bytecode[2:].lower() if expected_deployed_bytecode.startswith("0x") \
        else expected_deployed_bytecode.lower()
    onchain_body = onchain_code[2:].lower()

    same_length = len(template_body) == len(onchain_body)
    bytecode_match_raw = template_body == onchain_body

    patched_hex, substitutions = ("", [])
    bytecode_match_after_immutable_substitution = False
    if same_length:
        try:
            patched_hex, substitutions = build_expected_bytecode_from_positions(
                template_body, immutables, {"owner": expected_owner, "poolManager": expected_pool_manager})
            bytecode_match_after_immutable_substitution = (patched_hex == onchain_body)
        except ValueError as exc:
            print(f"[verify_deploy_v4] ОШИБКА при подстановке по позициям: {exc}", file=sys.stderr)

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
        "immutables_source": "compiler (evm.deployedBytecode.immutableReferences), не угадано",
        "n_immutable_positions_owner": len(immutables.get("owner", [])),
        "n_immutable_positions_pool_manager": len(immutables.get("poolManager", [])),
        "substitutions": substitutions,
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
