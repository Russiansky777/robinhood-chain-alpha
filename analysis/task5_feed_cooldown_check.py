#!/usr/bin/env python3
"""Задача 5, живой бот -- дешёвая read-only проверка остатка cooldown
на подключение к фиду секвенсера, БЕЗ попытки подключения. Нужна перед
запуском полного dry-run (`task5_bot_run.py --duration-seconds ...`):
`SequencerFeedClient.listen()` не отказывает мгновенно при активном
cooldown -- он ЖДЁТ остаток паузы внутри самого прогона (владелец,
2026-09-13: "живой бот, не одноразовый скрипт"). Если cooldown больше
окна dry-run, весь прогон уйдёт в ожидание и не задетектирует ничего --
эта проверка позволяет узнать остаток ЗАРАНЕЕ, не тратя окно прогона."""
from __future__ import annotations

import json
import time

from task5_bot_feed_client import FEED_STATE_FILE_DEFAULT, seconds_until_feed_connect_allowed


def main() -> None:
    wait_s = seconds_until_feed_connect_allowed()
    state = json.load(open(FEED_STATE_FILE_DEFAULT)) if FEED_STATE_FILE_DEFAULT.exists() else None
    print(json.dumps({
        "seconds_until_allowed": wait_s,
        "checked_at_unix": time.time(),
        "state_file_content": state,
    }, indent=2))


if __name__ == "__main__":
    main()
