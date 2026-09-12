#!/usr/bin/env python3
"""Задача 5, feed relay -- ЖЁСТКАЯ проверка 12-часового окна перед
запуском деплоя relay (отдельно от общего кода-кулдауна 1800с в
FEED_RECONNECT_COOLDOWN_S, который защищает обычные переподключения
бота). Владелец, 2026-09-12: "запуск relay -- по истечении 12 часов
с последнего 403, одна попытка." Exit 0 -- можно запускать. Exit 1 --
рано, печатает сколько ещё ждать -- workflow должен остановиться, НЕ
запускать deploy_feed_relay.sh."""
from __future__ import annotations

import json
import sys

from task5_bot_feed_client import FEED_STATE_FILE_DEFAULT, seconds_until_feed_connect_allowed

REQUIRED_COOLDOWN_S = 12 * 3600.0


def main() -> int:
    wait_s = seconds_until_feed_connect_allowed(FEED_STATE_FILE_DEFAULT, REQUIRED_COOLDOWN_S)
    result = {
        "required_cooldown_s": REQUIRED_COOLDOWN_S,
        "seconds_until_allowed": wait_s,
        "ok_to_proceed": wait_s <= 0,
    }
    print(json.dumps(result, indent=2))
    return 0 if wait_s <= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
