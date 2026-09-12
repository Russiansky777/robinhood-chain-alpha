#!/usr/bin/env python3
"""Диагностика (ТОЛЬКО чтение, не подключается к фиду): проверить,
не активен ли уже cooldown реконнекта фида (task5_bot_feed_client.py,
FEED_RECONNECT_COOLDOWN_S=1800с) ПЕРЕД тем, как запускать час
наблюдения (Этап 3) -- если бы cooldown был активен, часть бюджета
времени наблюдения ушла бы на ожидание внутри самого SequencerFeedClient.
listen(), незаметно для вызывающего."""
import json
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from task5_bot_feed_client import FEED_STATE_FILE_DEFAULT, seconds_until_feed_connect_allowed

result = {"state_file": str(FEED_STATE_FILE_DEFAULT), "exists": FEED_STATE_FILE_DEFAULT.exists()}
if result["exists"]:
    try:
        data = json.loads(FEED_STATE_FILE_DEFAULT.read_text())
        result["last_connect_attempt_at"] = data.get("last_connect_attempt_at")
        result["seconds_since_last_attempt"] = (
            time.time() - data["last_connect_attempt_at"] if data.get("last_connect_attempt_at") else None
        )
    except Exception as exc:  # noqa: BLE001
        result["read_error"] = str(exc)
result["seconds_until_allowed_now"] = seconds_until_feed_connect_allowed()

print(json.dumps(result, indent=2))
out_path = Path(__file__).parent.parent / "data" / "task5_v4_diag_feed_cooldown_result.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, indent=2))
