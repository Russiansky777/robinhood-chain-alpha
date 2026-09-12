#!/usr/bin/env python3
"""Диагностика: есть ли на Ohio VPS Foundry (anvil/forge/cast) для
локальной fork-симуляции ClosedCycleExecutorV4 -- Этап 2 (владелец,
2026-09-12: "локальная fork-симуляция нашего исполнителя на
контрольных транзакциях... пройдёт ли наш контракт маршрут USDG ->
MOSIAI -> USDG на состоянии блока 61248736, и с какой прибылью").

ТОЛЬКО чтение/диагностика -- ничего не ставит, ничего не запускает
на реальной цепочке."""
import json
import subprocess
from pathlib import Path

result = {}
for cmd in (["which", "anvil"], ["which", "forge"], ["which", "cast"],
            ["anvil", "--version"], ["forge", "--version"], ["cast", "--version"]):
    key = "_".join(cmd).replace("-", "_")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        result[f"cmd_{key}_stdout"] = proc.stdout.strip()
        result[f"cmd_{key}_stderr"] = proc.stderr.strip()
        result[f"cmd_{key}_returncode"] = proc.returncode
    except Exception as exc:  # noqa: BLE001
        result[f"cmd_{key}_error"] = str(exc)

print(json.dumps(result, indent=2))
out_path = Path(__file__).parent.parent / "data" / "task5_v4_diag_foundry_result.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, indent=2))
