#!/usr/bin/env python3
"""Задача 5, живой бот -- разведка РЕАЛЬНОЙ формы сообщений фида
секвенсера, ПЕРЕД тем, как решать между (а) починкой собственного
JSON-пути в `_extract_l2_msg_hex()` (task5_bot_feed_client.py) и (б)
подключением стороннего `rhfeed`. Владелец, 2026-09-13: детекция не
работает, потому что декодер реально не извлекает поля свопа (0/2286 в
прошлом прогоне) -- эта разведка нужна, чтобы понять, ПОЧЕМУ, вместо
того чтобы гадать дальше или сразу тянуть стороннюю зависимость.

Пассивное чтение, read-only, БЕЗ отправки чего-либо -- сохраняет ПОЛНУЮ
(не усечённую) структуру первых N реальных сообщений с `sequenceNumber`
в файл, чтобы можно было реально увидеть путь к `l2Msg` (или его
отсутствие/другое имя) вместо предположений."""
from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets

FEED_URL = "wss://feed.mainnet.chain.robinhood.com"


async def probe(n_samples: int, timeout_s: float) -> dict:
    samples = []
    diag = {"n_messages_total": 0, "n_with_seq": 0, "n_confirmation_only": 0, "n_unparsed": 0}
    deadline = time.monotonic() + timeout_s
    async with websockets.connect(FEED_URL, open_timeout=10, close_timeout=5) as ws:
        while time.monotonic() < deadline and len(samples) < n_samples:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            diag["n_messages_total"] += 1
            try:
                payload = json.loads(raw)
            except Exception as exc:
                diag["n_unparsed"] += 1
                samples.append({"parse_error": str(exc), "raw_prefix": raw[:500]})
                continue
            msgs = payload.get("messages") if isinstance(payload, dict) else None
            if not msgs:
                if isinstance(payload, dict) and "confirmedSequenceNumberMessage" in payload:
                    diag["n_confirmation_only"] += 1
                if "top_level_keys_seen" not in diag:
                    diag["top_level_keys_seen"] = sorted(payload.keys()) if isinstance(payload, dict) else str(type(payload))
                continue
            for m in msgs:
                if m.get("sequenceNumber") is not None:
                    diag["n_with_seq"] += 1
                    if len(samples) < n_samples:
                        samples.append(m)  # ПОЛНАЯ структура, без усечения -- нужно реально увидеть форму
    return {"samples": samples, "diag": diag}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    timeout_s = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    result = asyncio.run(probe(n, timeout_s))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
