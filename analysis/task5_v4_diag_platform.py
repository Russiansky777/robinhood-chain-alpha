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

print(json.dumps(result, indent=2))
out_path = Path(__file__).parent.parent / "data" / "task5_v4_diag_platform_result.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, indent=2))
