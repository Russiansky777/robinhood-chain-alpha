#!/usr/bin/env python3
"""Задача 5 (владелец): ограниченный read-only замер источника данных.

1) Подключение к официальному фиду секвенсера (wss://feed.mainnet.chain.
   robinhood.com) -- ОДНА попытка (cooldown-гард task5_bot_feed_client.py
   уже проверен ОТДЕЛЬНЫМ read-only скриптом перед этим запуском, здесь
   вызывается require_feed_connect_allowed_now() -- фиксирует попытку
   на диске, как и раньше).

2) Если подключение работает -- до 600с одновременного наблюдения:
   фид секвенсера + Alchemy WebSocket (newPendingTransactions -- момент
   "видим транзакцию", logs на PoolManager -- момент "готовы логи").
   Сопоставление -- ТОЛЬКО по реальному хэшу транзакции (keccak256
   сырых подписанных байт, извлечённых из L2-сообщения, тот же RLP-путь,
   что task5_bot_feed_client.py::decode_l2_message), НЕ по предположению
   sequenceNumber==номер L2-блока.

ЧЕСТНАЯ ОГОВОРКА (реальный, уже задокументированный факт этого проекта,
task5_bot_feed_client.py::SequencerFeedClient.listen(), решение
владельца 2026-09-13): при разрыве соединения с публичным фидом
реконнект НЕ происходит раньше 30 минут (тот же cooldown, что и для
первого подключения) -- это НАМЕРЕННАЯ защита от повторных HTTP 403.
Поэтому этот скрипт НЕ реконнектится к фиду сам в течение 600с -- одна
попытка живёт РОВНО столько, сколько сервер сам её держит (ранее
наблюдалось ~85с без close-фрейма). Alchemy WebSocket -- ОБЫЧНЫЙ
реконнект при обрыве (для него такого ограничения не задокументировано).
Это НЕ "10 минут непрерывного фида" -- честно указано отдельно, сколько
секунд фид реально был жив.

Не трогает торговый код, contract/Sender, не запускает LIVE, не
арендует серверы, не открывает вторых подключений к фиду."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import websockets  # noqa: E402
from Crypto.Hash import keccak  # noqa: E402

from config import CONFIG  # noqa: E402
from task5_bot_feed_client import (  # noqa: E402
    BROWSER_LIKE_HEADERS,
    SequencerFeedClient,
    _decode_l2_msg_bytes,
    _decode_signed_tx,
    connect_with_headers,
    require_feed_connect_allowed_now,
)

FEED_URL = "wss://feed.mainnet.chain.robinhood.com"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
DURATION_S = 600.0

OUT_DIR = Path(__file__).parent.parent / "data" / "task5_v4_feed_vs_alchemy_measurement"


def keccak256(b: bytes) -> str:
    h = keccak.new(digest_bits=256)
    h.update(b)
    return "0x" + h.hexdigest()


def _alchemy_ws_url() -> str | None:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url.replace("https://", "wss://", 1)
    if CONFIG.alchemy_api_key:
        return f"wss://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    return None


def _walk_l2_batch_for_tx_bytes(raw: bytes, out: list[bytes], depth: int = 0) -> None:
    """Та же логика, что task5_bot_feed_client.py::_decode_message_bytes,
    но здесь нужны СЫРЫЕ tx_bytes (для keccak256), а не только to/data --
    не модифицирует общий файл, отдельная копия только для этого замера."""
    if not raw or depth > 4:
        return
    msg_type = raw[0]
    if msg_type == 3:
        pos = 1
        while pos + 8 <= len(raw):
            size = int.from_bytes(raw[pos:pos + 8], "big")
            pos += 8
            if size < 0 or pos + size > len(raw):
                break
            _walk_l2_batch_for_tx_bytes(raw[pos:pos + size], out, depth + 1)
            pos += size
        return
    if msg_type == 4:
        out.append(raw[1:])


async def run_feed(deadline: float, out_path: Path, diag: dict) -> None:
    require_feed_connect_allowed_now()  # ОДНА попытка, фиксирует attempt на диске
    diag["connect_attempted_at_monotonic"] = time.monotonic()
    with out_path.open("w") as fh:
        try:
            async with connect_with_headers(FEED_URL, BROWSER_LIKE_HEADERS, open_timeout=10,
                                             close_timeout=5, max_size=None) as ws:
                diag["connected"] = True
                diag["connected_at_monotonic"] = time.monotonic()
                # Реальные заголовки хендшейка -- формат/сжатие НЕ гадаем.
                resp_headers = dict(ws.response.headers) if getattr(ws, "response", None) else {}
                diag["handshake_response_headers"] = resp_headers
                diag["permessage_deflate_negotiated"] = "permessage-deflate" in resp_headers.get(
                    "Sec-WebSocket-Extensions", "")
                first_message_seen = False
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        diag["ended_reason"] = "deadline_reached"
                        break
                    t_mono = time.monotonic()
                    if not first_message_seen:
                        first_message_seen = True
                        diag["first_message_at_monotonic"] = t_mono
                        diag["first_message_is_text"] = isinstance(raw, str)
                        diag["first_message_len_bytes"] = len(raw) if isinstance(raw, (str, bytes)) else None
                    diag["n_messages"] = diag.get("n_messages", 0) + 1
                    try:
                        payload = json.loads(raw)
                    except Exception as exc:  # noqa: BLE001
                        diag["n_unparsed"] = diag.get("n_unparsed", 0) + 1
                        fh.write(json.dumps({"t_monotonic": t_mono, "parse_error": str(exc)}) + "\n")
                        continue
                    msgs = payload.get("messages") if isinstance(payload, dict) else None
                    if not msgs:
                        continue
                    for m in msgs:
                        seq = m.get("sequenceNumber")
                        if seq is None:
                            continue
                        l2_field = SequencerFeedClient._extract_l2_msg_hex(m)
                        seq_ts = SequencerFeedClient._extract_sequencer_timestamp(m)
                        if not l2_field:
                            continue
                        raw_bytes = _decode_l2_msg_bytes(l2_field)
                        if raw_bytes is None:
                            continue
                        tx_bytes_list: list[bytes] = []
                        _walk_l2_batch_for_tx_bytes(raw_bytes, tx_bytes_list)
                        for tx_bytes in tx_bytes_list:
                            tx_hash = keccak256(tx_bytes)
                            decoded = _decode_signed_tx(tx_bytes, 4)
                            fh.write(json.dumps({
                                "t_monotonic": t_mono, "sequence_number": seq, "sequencer_timestamp": seq_ts,
                                "tx_hash": tx_hash, "to": decoded.get("to"), "tx_kind": decoded.get("tx_kind"),
                                "decode_error": decoded.get("decode_error"),
                            }) + "\n")
                    fh.flush()
                diag.setdefault("ended_reason", "loop_exit")
        except websockets.exceptions.InvalidStatus as exc:
            resp = exc.response
            diag["connected"] = False
            diag["status_code"] = resp.status_code
            diag["response_headers"] = dict(resp.headers) if resp.headers else {}
            diag["response_body"] = resp.body.decode("utf-8", errors="replace") if resp.body else None
        except Exception as exc:  # noqa: BLE001
            diag["connected"] = diag.get("connected", False)
            diag["disconnect_error"] = f"{type(exc).__name__}: {exc}"
        diag["ended_at_monotonic"] = time.monotonic()


async def run_alchemy(deadline: float, out_path: Path, diag: dict) -> None:
    url = _alchemy_ws_url()
    if not url:
        diag["error"] = "нет ALCHEMY_API_KEY/ALCHEMY_ROBINHOOD_RPC_URL -- Alchemy WS недоступен"
        return
    diag["n_messages"] = 0
    diag["n_reconnects"] = 0
    with out_path.open("w") as fh:
        while time.monotonic() < deadline:
            try:
                async with websockets.connect(url, open_timeout=10, close_timeout=5) as ws:
                    diag["connected"] = True
                    sub_pending = await _subscribe(ws, "newPendingTransactions")
                    sub_logs = await _subscribe(ws, "logs", {"address": POOL_MANAGER})
                    diag["sub_id_pending"] = sub_pending
                    diag["sub_id_logs"] = sub_logs
                    while time.monotonic() < deadline:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        except asyncio.TimeoutError:
                            break
                        t_mono = time.monotonic()
                        diag["n_messages"] += 1
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            continue
                        params = payload.get("params") or {}
                        sub_id = params.get("subscription")
                        result = params.get("result")
                        if sub_id == sub_pending and isinstance(result, str):
                            fh.write(json.dumps({"t_monotonic": t_mono, "kind": "pending_tx", "tx_hash": result}) + "\n")
                        elif sub_id == sub_logs and isinstance(result, dict):
                            fh.write(json.dumps({
                                "t_monotonic": t_mono, "kind": "log",
                                "tx_hash": result.get("transactionHash"),
                                "block_number": result.get("blockNumber"),
                                "log_index": result.get("logIndex"),
                            }) + "\n")
                        fh.flush()
            except Exception as exc:  # noqa: BLE001
                diag["n_reconnects"] += 1
                diag["last_error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.0)


async def _subscribe(ws, name: str, params: dict | None = None) -> str | None:
    req = {"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": [name] + ([params] if params else [])}
    await ws.send(json.dumps(req))
    raw = await asyncio.wait_for(ws.recv(), timeout=15)
    body = json.loads(raw)
    return body.get("result")


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0_monotonic = time.monotonic()
    deadline = t0_monotonic + DURATION_S

    feed_diag: dict = {}
    alchemy_diag: dict = {}
    feed_out = OUT_DIR / "feed_messages.jsonl"
    alchemy_out = OUT_DIR / "alchemy_messages.jsonl"

    await asyncio.gather(
        run_feed(deadline, feed_out, feed_diag),
        run_alchemy(deadline, alchemy_out, alchemy_diag),
    )

    summary = {
        "t0_monotonic": t0_monotonic, "duration_s_requested": DURATION_S,
        "feed_diag": feed_diag, "alchemy_diag": alchemy_diag,
        "feed_out": str(feed_out), "alchemy_out": str(alchemy_out),
    }
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
