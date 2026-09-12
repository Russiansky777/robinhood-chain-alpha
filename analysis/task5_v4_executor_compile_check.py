#!/usr/bin/env python3
"""Задача 5, стадия 2 (+ правка 2026-09-13 по внешнему ревью): компиляция
contracts/ClosedCycleExecutorV4.sol.

ТОЛЬКО компиляция и статическая проверка -- НЕ деплой, НЕ подпись, НЕ
отправка транзакций. Песочница этой сессии не имеет сетевого доступа к
binaries.soliditylang.org (403 через агент-прокси, проверено), поэтому
компиляция реально запускается здесь -- на Ohio VPS (сеть есть), тем же
каналом (workflow_dispatch -> SSH), что и весь остальной анализ в этой
задаче.

ПРАВКА 2026-09-13 (реальная находка на живом прогоне, не предположение):
Ohio VPS -- aarch64 (data/task5_v4_diag_platform_result.json,
platform_machine="aarch64"), а `solcx.install_solc()` (использовался
раньше в этом скрипте) тянет ОФИЦИАЛЬНЫЙ бинарь solc с
binaries.soliditylang.org, который для Linux собирается ТОЛЬКО под
amd64/x86_64 -- реальный прогон 2026-09-13 упал с `OSError: [Errno 8]
Exec format error: '/home/bot/.solcx/solc-v0.8.24'` (см. data/
task5_v4_executor_compile_check_result.json той попытки). Solc через
solcx-раньше НИКОГДА реально не запускался на Ohio -- существующие
contracts/build/*.bytecode.txt были скомпилированы ВЛАДЕЛЬЦЕМ локально
(см. историю deploy_params_v4.json), не этой сессией.

Переход на `forge build` (Foundry, УЖЕ реально установлен и работает на
этой aarch64 VPS -- v1.8.1, использовался весь этот проект для anvil/
cast): встроенный в forge менеджер версий solc (svm) реально скачивает
aarch64-линковки solc (в отличие от официальных бинарей
binaries.soliditylang.org) -- тот же принцип, по которому anvil/cast уже
были выбраны вместо голого solc/geth на этой VPS в этой сессии.

Компилятор: solc 0.8.24, evmVersion=paris, optimizer 200 runs -- те же
параметры, что contracts/build/deploy_params_v4.json (сверено владельцем
локально для оригинальной версии контракта, ДО этой правки)."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CONTRACT_SRC = REPO_ROOT / "contracts" / "ClosedCycleExecutorV4.sol"
CONTRACT_NAME = "ClosedCycleExecutorV4"
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
FORGE = str(FOUNDRY_BIN / "forge")
SOLC_VERSION = "0.8.24"
EVM_VERSION = "paris"
BUILD_ROOT = REPO_ROOT / "contracts" / ".forge_build_tmp"


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)


def _find_immutables(ast_node, found: dict) -> None:
    """Рекурсивный обход AST в поисках VariableDeclaration с
    mutability == "immutable" -- собирает {ast_id_str: name} (см. ниже,
    зачем нужна эта карта -- immutableReferences ключ это AST id, не
    имя переменной)."""
    if isinstance(ast_node, dict):
        if ast_node.get("nodeType") == "VariableDeclaration" and ast_node.get("mutability") == "immutable":
            found[str(ast_node["id"])] = ast_node["name"]
        for value in ast_node.values():
            _find_immutables(value, found)
    elif isinstance(ast_node, list):
        for item in ast_node:
            _find_immutables(item, found)


def main() -> None:
    result: dict = {"contract_path": str(CONTRACT_SRC), "solc_version": SOLC_VERSION, "evm_version": EVM_VERSION,
                     "compiler_toolchain": "forge build (Foundry svm -- аналог solcx официальных бинарей "
                                           "не работает на aarch64, см. докстринг)"}

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    (BUILD_ROOT / "src").mkdir(parents=True)
    shutil.copy(CONTRACT_SRC, BUILD_ROOT / "src" / CONTRACT_SRC.name)
    foundry_toml = f"""[profile.default]
src = "src"
out = "out"
libs = []
solc_version = "{SOLC_VERSION}"
evm_version = "{EVM_VERSION}"
optimizer = true
optimizer_runs = 200
build_info = true
extra_output = ["metadata"]
ast = true
"""
    (BUILD_ROOT / "foundry.toml").write_text(foundry_toml)

    print(f"[v4_executor_compile_check] forge build (solc {SOLC_VERSION}, evmVersion={EVM_VERSION})...")
    proc = run([FORGE, "build", "--root", str(BUILD_ROOT)], timeout=300)
    result["forge_build_returncode"] = proc.returncode
    result["forge_build_stdout"] = proc.stdout[-4000:]
    result["forge_build_stderr"] = proc.stderr[-4000:]
    if proc.returncode != 0:
        result["ok"] = False
        print(f"[v4_executor_compile_check] ОШИБКА КОМПИЛЯЦИИ (forge build):\n{proc.stdout}\n{proc.stderr}")
        _save(result)
        raise SystemExit(1)

    artifact_path = BUILD_ROOT / "out" / CONTRACT_SRC.name / f"{CONTRACT_NAME}.json"
    if not artifact_path.exists():
        result["ok"] = False
        result["error"] = f"forge build не создал ожидаемый артефакт: {artifact_path}"
        print(f"[v4_executor_compile_check] {result['error']}")
        _save(result)
        raise SystemExit(1)
    artifact = json.loads(artifact_path.read_text())

    abi = artifact["abi"]
    bytecode = artifact["bytecode"]["object"]
    deployed = artifact["deployedBytecode"]["object"]
    immutable_refs_by_id = artifact["deployedBytecode"].get("immutableReferences", {})
    metadata_raw = artifact.get("rawMetadata") or artifact.get("metadata")
    if isinstance(metadata_raw, dict):
        metadata_raw = json.dumps(metadata_raw)

    bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode
    deployed_hex = deployed[2:] if deployed.startswith("0x") else deployed

    # --- Имя immutable-переменной по AST id -- из build-info (полный
    # standard-json output, включает "ast"), а не угадано по порядку. ---
    id_to_name: dict[str, str] = {}
    build_info_dir = BUILD_ROOT / "out" / "build-info"
    if build_info_dir.exists():
        for bi_file in build_info_dir.glob("*.json"):
            bi = json.loads(bi_file.read_text())
            sources = bi.get("output", {}).get("sources", {})
            src_entry = sources.get(CONTRACT_SRC.name) or next(iter(sources.values()), None)
            if src_entry and "ast" in src_entry:
                _find_immutables(src_entry["ast"], id_to_name)
    if not id_to_name:
        print("[v4_executor_compile_check] ПРЕДУПРЕЖДЕНИЕ: не нашёл AST для сопоставления имён immutable "
              "(build-info отсутствует или не содержит ast) -- immutables останутся ключами по AST id, не именами.",
              )

    immutables_by_name: dict[str, list[dict]] = {}
    for ref_id, refs in immutable_refs_by_id.items():
        name = id_to_name.get(ref_id, f"<unknown_ast_id_{ref_id}>")
        immutables_by_name[name] = refs

    result["ok"] = True
    result["bytecode_len_bytes"] = len(bytecode_hex) // 2
    result["deployed_bytecode_len_bytes"] = len(deployed_hex) // 2
    result["immutables"] = immutables_by_name
    print(f"[v4_executor_compile_check] {CONTRACT_NAME}: bytecode={result['bytecode_len_bytes']} байт, "
          f"deployed={result['deployed_bytecode_len_bytes']} байт")
    print(f"[v4_executor_compile_check] immutable-позиции (из компилятора, не угаданы): "
          f"{json.dumps(immutables_by_name)}")

    out_dir = REPO_ROOT / "contracts" / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{CONTRACT_NAME}.abi.json").write_text(json.dumps(abi, indent=2))
    (out_dir / f"{CONTRACT_NAME}.bytecode.txt").write_text("0x" + bytecode_hex)
    (out_dir / f"{CONTRACT_NAME}.deployedBytecode.txt").write_text("0x" + deployed_hex)
    if metadata_raw:
        (out_dir / f"{CONTRACT_NAME}.metadata.json").write_text(metadata_raw)
    (out_dir / f"{CONTRACT_NAME}.immutables.json").write_text(json.dumps(immutables_by_name, indent=2))

    # --- Хэши для contracts/build/deploy_params_v4.json (владелец,
    # 2026-09-13: "Перекомпилировать, обновить артефакты") -- обновляем
    # ТОЛЬКО хэш-поля и note, остальное (constructor_args, адреса) не
    # трогаем -- это факты о деплое, не о коде. ---
    source_sha256 = hashlib.sha256(CONTRACT_SRC.read_bytes()).hexdigest()
    creation_bytecode_sha256 = hashlib.sha256(bytes.fromhex(bytecode_hex)).hexdigest()
    result["source_sha256"] = source_sha256
    result["creation_bytecode_sha256"] = creation_bytecode_sha256

    params_path = out_dir / "deploy_params_v4.json"
    if params_path.exists():
        params = json.loads(params_path.read_text())
        params["source_sha256"] = source_sha256
        params["creation_bytecode_sha256"] = creation_bytecode_sha256
        params["note"] = (
            "Перекомпилировано и хэши обновлены ЭТОЙ СЕССИЕЙ на Ohio VPS "
            "(analysis/task5_v4_executor_compile_check.py, forge build -- solc 0.8.24, "
            "optimizer 200, evmVersion paris) после исправления closed-currency "
            "guard + minProfit>0 по внешнему ревью 2026-09-13 -- НЕ прежние "
            "хэши владельца (те относились к версии ДО фикса)."
        )
        params_path.write_text(json.dumps(params, indent=2))
        print(f"[v4_executor_compile_check] обновлён {params_path}")

    result_path = REPO_ROOT / "data" / "task5_v4_executor_compile_check_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "contract_path": result["contract_path"], "solc_version": SOLC_VERSION, "evm_version": EVM_VERSION,
        "compiler_toolchain": result["compiler_toolchain"], "ok": result["ok"],
        "bytecode_len_bytes": result["bytecode_len_bytes"],
        "deployed_bytecode_len_bytes": result["deployed_bytecode_len_bytes"],
        "immutables": immutables_by_name,
        "source_sha256": source_sha256, "creation_bytecode_sha256": creation_bytecode_sha256,
    }
    result_path.write_text(json.dumps(summary, indent=2))
    print(f"[v4_executor_compile_check] сохранено: {result_path}")

    shutil.rmtree(BUILD_ROOT, ignore_errors=True)


def _save(result: dict) -> None:
    result_path = REPO_ROOT / "data" / "task5_v4_executor_compile_check_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
    shutil.rmtree(BUILD_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
