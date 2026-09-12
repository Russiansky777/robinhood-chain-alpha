#!/usr/bin/env python3
"""Задача 5, feed relay -- ВНЕШНЯЯ валидация того, что локальный relay
(`ws://127.0.0.1:9642`) реально получает сообщения от апстрима. НЕ
парсит логи контейнера (формат/маркеры успеха-в-логах самого relay не
подтверждены вживую этой сессией, не гадаем) -- вместо этого делает то
же самое, что уже проверенный код проекта: подключается (loopback --
без cooldown-гарда, см. task5_bot_feed_client.py::_is_loopback_feed_url)
и честно считает, пришло ли хотя бы одно реальное сообщение с
sequenceNumber за timeout секунд.

Exit code 0 -- relay реально работает (получены сообщения). Exit code 1
-- НЕ работает (0 сообщений/обрыв/таймаут) -- вызывающий скрипт
(deploy_feed_relay.sh) должен ОСТАНОВИТЬ контейнер, НЕ включать
restart-policy, НЕ оставлять его ретраить апстрим самостоятельно."""
from __future__ import annotations

import asyncio
import json
import sys
import time

from task5_bot_config import SEQUENCER_FEED_URL_LOCAL_RELAY
from task5_bot_feed_client import SequencerFeedClient


async def _validate(timeout_s: float, n_required: int) -> dict:
    client = SequencerFeedClient(SEQUENCER_FEED_URL_LOCAL_RELAY)
    assert client.is_loopback, "ожидался loopback URL -- cooldown-гард не должен применяться здесь"
    samples: list[dict] = []

    def on_message(msg) -> None:
        if len(samples) < n_required:
            samples.append({"sequence_number": msg.sequence_number, "t_wall": msg.t_wall})

    try:
        await asyncio.wait_for(client.listen(on_message), timeout=timeout_s)
    except asyncio.TimeoutError:
        pass  # штатно -- ждём ровно timeout_s, не ошибка

    return {
        "relay_url": SEQUENCER_FEED_URL_LOCAL_RELAY,
        "n_messages_received": len(samples),
        "n_required": n_required,
        "samples": samples,
        "diag": client.diag,
        "ok": len(samples) >= n_required,
    }


def main() -> int:
    timeout_s = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
    n_required = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    result = asyncio.run(_validate(timeout_s, n_required))
    result["checked_at_unix"] = time.time()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
