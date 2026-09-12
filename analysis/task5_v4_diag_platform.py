#!/usr/bin/env python3
"""Диагностика: solcx.install_solc('0.8.24') упал с 'Exec format error'
на Ohio VPS (см. data/p3_guard_cache/task5_bot_known_answer_test_log.txt,
прогон 2026-09-12T20:42). Проверяем архитектуру/ОС хоста и что реально
скачалось, чтобы не гадать о причине."""
import json
import platform
import subprocess
from pathlib import Path

result = {
    "platform_machine": platform.machine(),
    "platform_system": platform.system(),
    "platform_release": platform.release(),
    "platform_platform": platform.platform(),
}

solc_path = Path.home() / ".solcx" / "solc-v0.8.24"
result["solc_binary_exists"] = solc_path.exists()
if solc_path.exists():
    result["solc_binary_size_bytes"] = solc_path.stat().st_size
    try:
        result["file_command_output"] = subprocess.run(["file", str(solc_path)], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        result["file_command_error"] = str(exc)

# solc официально публикует бинарники только для linux-amd64
# (binaries.soliditylang.org) -- на aarch64 (см. platform_machine выше)
# solcx.install_solc() не может дать рабочий бинарник. Проверяем
# (ТОЛЬКО чтение, ничего не ставим) есть ли нативный пакет solc в apt
# для этой архитектуры -- Debian/Ubuntu собирают solc из исходников на
# каждую архитектуру отдельно, в отличие от апстрим-бинарников.
for cmd in (["apt-cache", "policy", "solc"], ["which", "solc"], ["lsb_release", "-a"]):
    key = "_".join(cmd[:2]).replace("-", "_")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        result[f"cmd_{key}_stdout"] = proc.stdout.strip()
        result[f"cmd_{key}_stderr"] = proc.stderr.strip()
        result[f"cmd_{key}_returncode"] = proc.returncode
    except Exception as exc:  # noqa: BLE001
        result[f"cmd_{key}_error"] = str(exc)

# apt-cache policy solc вернул ПУСТО (returncode 0) -- пакет неизвестен
# apt целиком (не просто не установлен), вероятно нет в архиве Ubuntu
# 26.04 ("resolute") под этим именем. Проверяем альтернативный путь:
# официальный Docker-образ ethereum/solc публикует multi-arch манифесты
# (включая linux/arm64) в отличие от голых бинарников
# binaries.soliditylang.org -- это ЧИТАЕМАЯ проверка (which docker +
# один versioned run), ничего не меняет в системе необратимо.
for cmd in (["which", "docker"], ["docker", "--version"]):
    key = "_".join(cmd).replace("-", "_").replace(".", "_")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        result[f"cmd_{key}_stdout"] = proc.stdout.strip()
        result[f"cmd_{key}_stderr"] = proc.stderr.strip()
        result[f"cmd_{key}_returncode"] = proc.returncode
    except Exception as exc:  # noqa: BLE001
        result[f"cmd_{key}_error"] = str(exc)

if result.get("cmd_docker___version_returncode") == 0:
    try:
        proc = subprocess.run(["docker", "run", "--rm", "ethereum/solc:0.8.24", "--version"],
                               capture_output=True, text=True, timeout=120)
        result["docker_solc_0_8_24_stdout"] = proc.stdout.strip()
        result["docker_solc_0_8_24_stderr"] = proc.stderr.strip()[-2000:]
        result["docker_solc_0_8_24_returncode"] = proc.returncode
    except Exception as exc:  # noqa: BLE001
        result["docker_solc_0_8_24_error"] = str(exc)

print(json.dumps(result, indent=2))
out_path = Path(__file__).parent.parent / "data" / "task5_v4_diag_platform_result.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, indent=2))
