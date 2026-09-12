#!/usr/bin/env python3
"""Задача 5, стадия 2 (+ правка 2026-09-13 по внешнему ревью): компиляция
contracts/ClosedCycleExecutorV4.sol.

ТОЛЬКО компиляция и статическая проверка -- НЕ деплой, НЕ подпись, НЕ
отправка транзакций. Песочница этой сессии не имеет сетевого доступа к
binaries.soliditylang.org (403 через агент-прокси, проверено), поэтому
компиляция реально запускается здесь -- на Ohio VPS (сеть есть), тем же
каналом (workflow_dispatch -> SSH), что и весь остальной анализ в этой
задаче.

Компилятор: solc 0.8.24 -- та же версия, которой реально скомпилирован
уже развёрнутый ClosedCycleExecutorV3 (contracts/build/
ClosedCycleExecutorV3.metadata.json, compiler.version =
"0.8.24+commit.e11b9ed9") -- не выбрана заново, а взята из уже
существующего проекта, чтобы оставаться в одной версии со старым
контрактом. evmVersion="paris" передаётся ЯВНО (не дефолт solc для этой
версии, который может быть "shanghai" с PUSH0) -- совпадает с
contracts/build/deploy_params_v4.json, сверенным владельцем локально.

ПРАВКА 2026-09-13: переход с compile_files (combined-json) на
compile_standard (полный standard-json), чтобы получить
evm.deployedBytecode.immutableReferences -- РЕАЛЬНЫЕ байтовые позиции
(start, length) каждой immutable-переменной в задеплоенном байткоде,
как их знает САМ компилятор, а не как их выводит верификатор
(task5_verify_deploy_v4.py) угадыванием "любое нулевое 32-байтовое
слово -- потенциальный плейсхолдер" (внешнее ревью: ненадёжно при 2
immutable с одинаковым нулевым паттерном). immutableReferences ключ --
AST id переменной (как строка), не имя -- имя восстанавливается через
AST (VariableDeclaration.mutability == "immutable"), запрошенный тем же
вызовом. Результат сохраняется в contracts/build/
ClosedCycleExecutorV4.immutables.json как {name: {start, length}}."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import solcx

REPO_ROOT = Path(__file__).parent.parent
CONTRACT_PATH = REPO_ROOT / "contracts" / "ClosedCycleExecutorV4.sol"
CONTRACT_NAME = "ClosedCycleExecutorV4"
SOURCE_UNIT_NAME = "ClosedCycleExecutorV4.sol"
SOLC_VERSION = "0.8.24"
EVM_VERSION = "paris"


def _find_immutables(ast_node, found: dict) -> None:
    """Рекурсивный обход AST в поисках VariableDeclaration с
    mutability == "immutable" -- собирает {ast_id_str: name}."""
    if isinstance(ast_node, dict):
        if ast_node.get("nodeType") == "VariableDeclaration" and ast_node.get("mutability") == "immutable":
            found[str(ast_node["id"])] = ast_node["name"]
        for value in ast_node.values():
            _find_immutables(value, found)
    elif isinstance(ast_node, list):
        for item in ast_node:
            _find_immutables(item, found)


def main() -> None:
    result: dict = {"contract_path": str(CONTRACT_PATH), "solc_version": SOLC_VERSION, "evm_version": EVM_VERSION}

    installed = solcx.get_installed_solc_versions()
    if not any(str(v) == SOLC_VERSION for v in installed):
        print(f"[v4_executor_compile_check] устанавливаю solc {SOLC_VERSION}...")
        solcx.install_solc(SOLC_VERSION)
    solcx.set_solc_version(SOLC_VERSION)

    source_text = CONTRACT_PATH.read_text()
    input_json = {
        "language": "Solidity",
        "sources": {SOURCE_UNIT_NAME: {"content": source_text}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "evmVersion": EVM_VERSION,
            "outputSelection": {
                "*": {
                    "": ["ast"],
                    "*": ["abi", "metadata", "evm.bytecode.object", "evm.deployedBytecode.object",
                          "evm.deployedBytecode.immutableReferences"],
                }
            },
        },
    }

    try:
        out = solcx.compile_standard(input_json, solc_version=SOLC_VERSION)
    except solcx.exceptions.SolcError as exc:
        result["ok"] = False
        result["solc_error"] = str(exc)
        print(f"[v4_executor_compile_check] ОШИБКА КОМПИЛЯЦИИ:\n{exc}")
        _save(result)
        raise SystemExit(1)

    errors = [e for e in out.get("errors", []) if e.get("severity") == "error"]
    if errors:
        result["ok"] = False
        result["solc_errors"] = errors
        print(f"[v4_executor_compile_check] ОШИБКА КОМПИЛЯЦИИ:\n{json.dumps(errors, indent=2)}")
        _save(result)
        raise SystemExit(1)
    warnings = [e for e in out.get("errors", []) if e.get("severity") != "error"]
    for w in warnings:
        print(f"[v4_executor_compile_check] предупреждение solc: {w.get('formattedMessage', w)}")

    contract_out = out["contracts"][SOURCE_UNIT_NAME][CONTRACT_NAME]
    abi = contract_out["abi"]
    metadata = contract_out.get("metadata", "{}")
    bytecode = contract_out["evm"]["bytecode"]["object"]
    deployed = contract_out["evm"]["deployedBytecode"]["object"]
    immutable_refs_by_id = contract_out["evm"]["deployedBytecode"].get("immutableReferences", {})

    # Имя immutable-переменной по её AST id (immutableReferences ключ --
    # id, не имя) -- обходим AST того же результата компиляции.
    ast_root = out["sources"][SOURCE_UNIT_NAME]["ast"]
    id_to_name: dict[str, str] = {}
    _find_immutables(ast_root, id_to_name)

    immutables_by_name: dict[str, list[dict]] = {}
    for ref_id, refs in immutable_refs_by_id.items():
        name = id_to_name.get(ref_id, f"<unknown_ast_id_{ref_id}>")
        immutables_by_name[name] = refs

    result["ok"] = True
    result["bytecode_len_bytes"] = len(bytecode) // 2 if bytecode else 0
    result["deployed_bytecode_len_bytes"] = len(deployed) // 2 if deployed else 0
    result["immutables"] = immutables_by_name
    print(f"[v4_executor_compile_check] {CONTRACT_NAME}: bytecode={result['bytecode_len_bytes']} байт, "
          f"deployed={result['deployed_bytecode_len_bytes']} байт")
    print(f"[v4_executor_compile_check] immutable-позиции (из компилятора, не угаданы): "
          f"{json.dumps(immutables_by_name)}")

    out_dir = REPO_ROOT / "contracts" / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{CONTRACT_NAME}.abi.json").write_text(json.dumps(abi, indent=2))
    (out_dir / f"{CONTRACT_NAME}.bytecode.txt").write_text(bytecode)
    (out_dir / f"{CONTRACT_NAME}.deployedBytecode.txt").write_text(deployed)
    (out_dir / f"{CONTRACT_NAME}.metadata.json").write_text(metadata)
    (out_dir / f"{CONTRACT_NAME}.immutables.json").write_text(json.dumps(immutables_by_name, indent=2))

    # --- Хэши для contracts/build/deploy_params_v4.json (владелец,
    # 2026-09-13: "Перекомпилировать, обновить артефакты") -- обновляем
    # ТОЛЬКО хэш-поля и note, остальное (constructor_args, адреса) не
    # трогаем -- это факты о деплое, не о коде. ---
    source_sha256 = hashlib.sha256(source_text.encode()).hexdigest()
    creation_bytecode_sha256 = hashlib.sha256(bytes.fromhex(bytecode)).hexdigest()
    result["source_sha256"] = source_sha256
    result["creation_bytecode_sha256"] = creation_bytecode_sha256

    params_path = out_dir / "deploy_params_v4.json"
    if params_path.exists():
        params = json.loads(params_path.read_text())
        params["source_sha256"] = source_sha256
        params["creation_bytecode_sha256"] = creation_bytecode_sha256
        params["note"] = (
            "Перекомпилировано и хэши обновлены ЭТОЙ СЕССИЕЙ на Ohio VPS "
            "(analysis/task5_v4_executor_compile_check.py, solc 0.8.24, "
            "optimizer 200, evmVersion paris) после исправления closed-currency "
            "guard + minProfit>0 по внешнему ревью 2026-09-13 -- НЕ прежние "
            "хэши владельца (те относились к версии ДО фикса)."
        )
        params_path.write_text(json.dumps(params, indent=2))
        print(f"[v4_executor_compile_check] обновлён {params_path}")

    result_path = REPO_ROOT / "data" / "task5_v4_executor_compile_check_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    # ABI/immutables могут быть большими -- в data/ кладём компактную
    # сводку, полные версии уже сохранены в contracts/build/ отдельно.
    summary = {
        "contract_path": result["contract_path"], "solc_version": SOLC_VERSION, "evm_version": EVM_VERSION,
        "ok": result["ok"], "bytecode_len_bytes": result["bytecode_len_bytes"],
        "deployed_bytecode_len_bytes": result["deployed_bytecode_len_bytes"],
        "immutables": immutables_by_name,
        "source_sha256": source_sha256, "creation_bytecode_sha256": creation_bytecode_sha256,
    }
    result_path.write_text(json.dumps(summary, indent=2))
    print(f"[v4_executor_compile_check] сохранено: {result_path}")


def _save(result: dict) -> None:
    result_path = REPO_ROOT / "data" / "task5_v4_executor_compile_check_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
