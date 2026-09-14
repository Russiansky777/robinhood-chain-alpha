#!/usr/bin/env python3
"""Задача 5 (владелец, замер источника фида секвенсера): ТОЛЬКО чтение
состояния cooldown-гарда (task5_bot_feed_client.py::seconds_until_feed_
connect_allowed) -- НЕ вызывает require_feed_connect_allowed_now() и НЕ
записывает попытку подключения, никакого сетевого вызова к фиду здесь
нет вообще. Нужно узнать, можно ли вообще пробовать подключаться, ДО
того как тратить единственную попытку в рамках 30-минутного гарда."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_feed_client import FEED_RECONNECT_COOLDOWN_S, FEED_STATE_FILE_DEFAULT, seconds_until_feed_connect_allowed  # noqa: E402


def main() -> None:
    wait_s = seconds_until_feed_connect_allowed()
    result = {
        "state_file": str(FEED_STATE_FILE_DEFAULT),
        "cooldown_s_configured": FEED_RECONNECT_COOLDOWN_S,
        "seconds_until_allowed_now": wait_s,
        "allowed_now": wait_s <= 0.0,
        "checked_at_unix": time.time(),
    }
    if FEED_STATE_FILE_DEFAULT.exists():
        try:
            result["state_file_content"] = json.loads(FEED_STATE_FILE_DEFAULT.read_text())
        except Exception as exc:  # noqa: BLE001
            result["state_file_read_error"] = str(exc)
    else:
        result["state_file_exists"] = False
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
