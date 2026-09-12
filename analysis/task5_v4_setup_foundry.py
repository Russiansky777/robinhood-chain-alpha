#!/usr/bin/env python3
"""Установка Foundry (anvil/forge/cast) на Ohio VPS для локальной
fork-симуляции ClosedCycleExecutorV4 (Этап 2, владелец 2026-09-12).

Обычная, безопасная установка dev-тулчейна -- НЕ затрагивает реальные
транзакции, ключи или цепочку. foundryup публикует бинарники под
несколько архитектур, включая linux-aarch64 (в отличие от
binaries.soliditylang.org, только linux-amd64) -- поэтому этот путь,
в отличие от solc, реально может сработать на этом VPS (aarch64,
подтверждено в task5_v4_diag_platform_result.json)."""
import json
import os
import subprocess
from pathlib import Path

HOME = Path.home()
FOUNDRY_BIN = HOME / ".foundry" / "bin"

result = {}

# 1) Установка foundryup (если ещё не стоит).
foundryup_path = FOUNDRY_BIN / "foundryup"
if not foundryup_path.exists():
    try:
        install_script = subprocess.run(["curl", "-fsSL", "https://foundry.paradigm.xyz"],
                                         capture_output=True, text=True, timeout=30).stdout
        proc = subprocess.run(["bash"], input=install_script, capture_output=True, text=True, timeout=60)
        result["install_foundryup_stdout"] = proc.stdout.strip()[-2000:]
        result["install_foundryup_stderr"] = proc.stderr.strip()[-2000:]
        result["install_foundryup_returncode"] = proc.returncode
    except Exception as exc:  # noqa: BLE001
        result["install_foundryup_error"] = str(exc)

result["foundryup_exists_after_install"] = foundryup_path.exists()

# 2) foundryup -- ставит/обновляет anvil, forge, cast в ~/.foundry/bin.
if foundryup_path.exists():
    try:
        env = dict(os.environ)
        proc = subprocess.run([str(foundryup_path)], capture_output=True, text=True, timeout=300, env=env)
        result["foundryup_run_stdout"] = proc.stdout.strip()[-3000:]
        result["foundryup_run_stderr"] = proc.stderr.strip()[-3000:]
        result["foundryup_run_returncode"] = proc.returncode
    except Exception as exc:  # noqa: BLE001
        result["foundryup_run_error"] = str(exc)

# 3) Проверка результата -- версии установленных бинарников.
for name in ("anvil", "forge", "cast"):
    binp = FOUNDRY_BIN / name
    result[f"{name}_exists"] = binp.exists()
    if binp.exists():
        try:
            proc = subprocess.run([str(binp), "--version"], capture_output=True, text=True, timeout=15)
            result[f"{name}_version_stdout"] = proc.stdout.strip()
            result[f"{name}_version_returncode"] = proc.returncode
        except Exception as exc:  # noqa: BLE001
            result[f"{name}_version_error"] = str(exc)

print(json.dumps(result, indent=2))
out_path = Path(__file__).parent.parent / "data" / "task5_v4_setup_foundry_result.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, indent=2))
