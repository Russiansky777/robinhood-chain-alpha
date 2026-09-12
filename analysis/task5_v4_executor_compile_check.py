#!/usr/bin/env python3
"""Задача 5, стадия 2: компиляция contracts/ClosedCycleExecutorV4.sol.

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
контрактом."""
from __future__ import annotations

import json
from pathlib import Path

import solcx

CONTRACT_PATH = Path(__file__).parent.parent / "contracts" / "ClosedCycleExecutorV4.sol"
SOLC_VERSION = "0.8.24"


def main() -> None:
    result: dict = {"contract_path": str(CONTRACT_PATH), "solc_version": SOLC_VERSION}

    installed = solcx.get_installed_solc_versions()
    if not any(str(v) == SOLC_VERSION for v in installed):
        print(f"[v4_executor_compile_check] устанавливаю solc {SOLC_VERSION}...")
        solcx.install_solc(SOLC_VERSION)
    solcx.set_solc_version(SOLC_VERSION)

    try:
        compiled = solcx.compile_files(
            [str(CONTRACT_PATH)],
            output_values=["abi", "bin", "bin-runtime", "metadata"],
            solc_version=SOLC_VERSION,
            optimize=True,
            optimize_runs=200,
        )
    except solcx.exceptions.SolcError as exc:
        result["ok"] = False
        result["solc_error"] = str(exc)
        print(f"[v4_executor_compile_check] ОШИБКА КОМПИЛЯЦИИ:\n{exc}")
        _save(result)
        raise SystemExit(1)

    result["ok"] = True
    result["contracts"] = {}
    for key, entry in compiled.items():
        # key вида "contracts/ClosedCycleExecutorV4.sol:ClosedCycleExecutorV4"
        name = key.split(":")[-1]
        bytecode = entry.get("bin", "")
        runtime = entry.get("bin-runtime", "")
        result["contracts"][name] = {
            "abi": entry.get("abi"),
            "bytecode_len_bytes": len(bytecode) // 2 if bytecode else 0,
            "deployed_bytecode_len_bytes": len(runtime) // 2 if runtime else 0,
        }
        print(f"[v4_executor_compile_check] {name}: bytecode={len(bytecode)//2 if bytecode else 0} байт, "
              f"deployed={len(runtime)//2 if runtime else 0} байт")

    out_dir = Path(__file__).parent.parent / "contracts" / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, entry in result["contracts"].items():
        (out_dir / f"{name}.abi.json").write_text(json.dumps(entry["abi"], indent=2))
        full = compiled[f"{CONTRACT_PATH}:{name}"] if f"{CONTRACT_PATH}:{name}" in compiled else None
        if full is None:
            for k in compiled:
                if k.endswith(f":{name}"):
                    full = compiled[k]
                    break
        if full:
            (out_dir / f"{name}.bytecode.txt").write_text(full.get("bin", ""))
            (out_dir / f"{name}.deployedBytecode.txt").write_text(full.get("bin-runtime", ""))
            (out_dir / f"{name}.metadata.json").write_text(full.get("metadata", "{}"))

    result_path = Path(__file__).parent.parent / "data" / "task5_v4_executor_compile_check_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    # ABI может быть большим -- в data/ кладём компактную сводку, полный
    # ABI/байткод уже сохранён в contracts/build/ отдельными файлами.
    summary = {
        "contract_path": result["contract_path"], "solc_version": result["solc_version"], "ok": result["ok"],
        "contracts": {name: {"bytecode_len_bytes": e["bytecode_len_bytes"],
                              "deployed_bytecode_len_bytes": e["deployed_bytecode_len_bytes"]}
                      for name, e in result["contracts"].items()},
    }
    result_path.write_text(json.dumps(summary, indent=2))
    print(f"[v4_executor_compile_check] сохранено: {result_path}")


def _save(result: dict) -> None:
    result_path = Path(__file__).parent.parent / "data" / "task5_v4_executor_compile_check_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
