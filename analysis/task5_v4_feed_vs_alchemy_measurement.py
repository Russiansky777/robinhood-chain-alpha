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
арендует серверы, не открывает вторых подключений к фиду.

ПРАВКА (владелец, третий источник): подготовлена (не подключена -- нет
токена, см. run_blockrazor) возможность добавить BlockRazor Sequencer
Feed Ultra третьим источником. Добавлены: пометка "стартового бэклога"
(is_backlog, по sequencer_timestamp против wall-time, п.3 требований),
blockHash в записях Alchemy logs (НЕ header.blockNumber Nitro-сообщений
-- он может быть номером L1, п.5 требований) -- корректор сопоставляет
по blockHash/tx_hash, не по blockNumber/sequenceNumber."""
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
# Сообщение считается "стартовым бэклогом" (не живым), если его
# sequencer_timestamp (unix-секунды заголовка Nitro) отстаёт от текущего
# wall-time больше чем на это значение -- реальный, ранее уже
# наблюдавшийся факт (data/task5_feed_latency_measurement.json,
# 2026-09-12): подключение к публичному фиду выдаёт пачку СТАРЫХ
# сообщений с одинаковым t_wall получения. Помечается, НЕ отбрасывается
# молча -- см. "is_backlog" в записи.
BACKLOG_THRESHOLD_S = 10.0

# --- Третий источник (владелец, подготовка): BlockRazor Sequencer Feed
# Ultra, https://docs.blockrazor.io/streams/node-stream/robinhood-chain/
# sequencer-feed-ultra -- РЕАЛЬНО найденные на этой странице URL (read-only
# HTTPS GET, data/task5_v4_blockrazor_docs_and_creds_check_result.json):
#   wss://us.robinhood-feeder.blockrazor.io/ws/{authToken}
#   wss://us.robinhood-feeder.blockrazor.io/ws/ultra/{authToken}
#   wss://jp.robinhood-feeder.blockrazor.io/ws/{ultra/}{authToken}
# Страница также цитирует пример конфигурации Nitro-ноды:
#   "--node.feed.input.url=wss://us.robinhood-feeder.blockrazor.io/ws/{authToken}"
# -- это ТОТ ЖЕ CLI-флаг, которым официальная Nitro-нода настраивает
# ВХОДНОЙ фид секвенсера, а не отдельный, самопальный протокол. Отсюда
# рабочая (не подтверждённая живым подключением -- токена нет) гипотеза:
# формат сообщений СОВМЕСТИМ с тем же {"messages":[...]} JSON-конвертом,
# что и официальный фид -- можно переиспользовать ТОТ ЖЕ парсер
# (decode_l2_message/_walk_l2_batch_for_tx_bytes), просто с другим URL.
# НИКАКОГО реального токена в этом окружении НЕТ (проверено на Ohio,
# data/task5_v4_blockrazor_docs_and_creds_check_result.json ->
# any_candidate_env_var_present=false) -- подключение НЕ выполняется,
# только подготовлена возможность (см. run_blockrazor ниже).
BLOCKRAZOR_TOKEN_ENV_VARS = (
    "BLOCKRAZOR_API_KEY", "BLOCKRAZOR_TOKEN", "BLOCKRAZOR_ACCESS_TOKEN",
    "BLOCKRAZOR_AUTH_TOKEN", "BLOCKRAZOR_API_TOKEN", "BLOCKRAZOR_KEY",
)
BLOCKRAZOR_URL_TEMPLATE = "wss://us.robinhood-feeder.blockrazor.io/ws/ultra/{token}"

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
                # РЕАЛЬНЫЙ БАГ, найденный этим же прогоном (dict() на
                # websockets.Headers падает с MultipleValuesError, если
                # сервер прислал ОДИН и тот же заголовок несколько раз --
                # реально произошло с 'server-timing' на этом фиде,
                # оборвав соединение почти сразу после подключения, ~5с
                # жизни вместо всего окна замера). Теперь -- безопасное
                # извлечение через .raw_items() (допускает дубликаты,
                # НЕ падает), последнее значение каждого имени сохраняется
                # (для Sec-WebSocket-Extensions этого достаточно).
                resp_headers: dict[str, str] = {}
                if getattr(ws, "response", None) is not None:
                    try:
                        for k, v in ws.response.headers.raw_items():
                            resp_headers[k] = v
                    except Exception as exc:  # noqa: BLE001
                        resp_headers = {"_extraction_error": str(exc)}
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
                        # ПРАВКА (владелец, п.3): "исключить стартовую выдачу старых
                        # сообщений" -- реальный, ранее наблюдавшийся факт (backlog
                        # burst на подключении). is_backlog помечается, НЕ отбрасывается
                        # молча -- корректор фильтрует по этому полю явно.
                        is_backlog = (seq_ts is not None and (time.time() - seq_ts) > BACKLOG_THRESHOLD_S)
                        for tx_bytes in tx_bytes_list:
                            tx_hash = keccak256(tx_bytes)
                            decoded = _decode_signed_tx(tx_bytes, 4)
                            fh.write(json.dumps({
                                "t_monotonic": t_mono, "sequence_number": seq, "sequencer_timestamp": seq_ts,
                                "is_backlog": is_backlog,
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
                            # ПРАВКА (владелец, п.5): blockHash сохраняется отдельно
                            # от blockNumber -- ключ для сопоставления между
                            # источниками, КОГДА оно делается на уровне блока (не по
                            # tx_hash), должен быть blockHash, не blockNumber
                            # (Nitro-заголовок header.blockNumber в сообщениях фида
                            # может быть номером L1, не L2 -- сюда это НЕ подставляется).
                            fh.write(json.dumps({
                                "t_monotonic": t_mono, "kind": "log",
                                "tx_hash": result.get("transactionHash"),
                                "block_hash": result.get("blockHash"),
                                "block_number": result.get("blockNumber"),
                                "log_index": result.get("logIndex"),
                            }) + "\n")
                        fh.flush()
            except Exception as exc:  # noqa: BLE001
                diag["n_reconnects"] += 1
                diag["last_error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.0)


def _blockrazor_token() -> str | None:
    for name in BLOCKRAZOR_TOKEN_ENV_VARS:
        val = os.environ.get(name)
        if val:
            return val
    return None


async def run_blockrazor(deadline: float, out_path: Path, diag: dict) -> None:
    """Третий источник (владелец, подготовка) -- BlockRazor Sequencer
    Feed Ultra. НЕ подключается, если токена нет (проверено на этом же
    хосте: data/task5_v4_blockrazor_docs_and_creds_check_result.json,
    any_candidate_env_var_present=false на момент подготовки) -- владелец:
    "если токена доступа нет -- укажи это и закончи подготовку, не уходи
    в обходные поиски". Если токен ПОЯВИТСЯ в окружении -- код готов
    переиспользовать ТОТ ЖЕ парсер, что официальный фид (гипотеза по
    документации: формат сообщений совместим, см. константы выше), НЕ
    подтверждено живым подключением."""
    token = _blockrazor_token()
    if not token:
        diag["skipped"] = True
        diag["reason"] = (
            "нет ни одной из переменных окружения " + ", ".join(BLOCKRAZOR_TOKEN_ENV_VARS) +
            " -- токен доступа BlockRazor отсутствует, подключение не выполняется"
        )
        return
    url = BLOCKRAZOR_URL_TEMPLATE.format(token=token)
    diag["connect_attempted_at_monotonic"] = time.monotonic()
    with out_path.open("w") as fh:
        try:
            # Гипотеза (см. докстринг run_blockrazor): тот же JSON-конверт,
            # что официальный фид -- переиспользуем connect_with_headers +
            # decode_l2_message путь БЕЗ cooldown-гарда официального фида
            # (это другой хост, другой механизм доступа по токену, а не
            # по IP/фингерпринту).
            async with connect_with_headers(url, BROWSER_LIKE_HEADERS, open_timeout=10,
                                             close_timeout=5, max_size=None) as ws:
                diag["connected"] = True
                diag["connected_at_monotonic"] = time.monotonic()
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    t_mono = time.monotonic()
                    diag["n_messages"] = diag.get("n_messages", 0) + 1
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    msgs = payload.get("messages") if isinstance(payload, dict) else None
                    if not msgs:
                        continue
                    for m in msgs:
                        seq = m.get("sequenceNumber")
                        l2_field = SequencerFeedClient._extract_l2_msg_hex(m)
                        seq_ts = SequencerFeedClient._extract_sequencer_timestamp(m)
                        if not l2_field:
                            continue
                        raw_bytes = _decode_l2_msg_bytes(l2_field)
                        if raw_bytes is None:
                            continue
                        tx_bytes_list: list[bytes] = []
                        _walk_l2_batch_for_tx_bytes(raw_bytes, tx_bytes_list)
                        is_backlog = (seq_ts is not None and (time.time() - seq_ts) > BACKLOG_THRESHOLD_S)
                        for tx_bytes in tx_bytes_list:
                            fh.write(json.dumps({
                                "t_monotonic": t_mono, "sequence_number": seq, "sequencer_timestamp": seq_ts,
                                "is_backlog": is_backlog, "tx_hash": keccak256(tx_bytes),
                            }) + "\n")
                    fh.flush()
        except Exception as exc:  # noqa: BLE001
            diag["connected"] = diag.get("connected", False)
            diag["disconnect_error"] = f"{type(exc).__name__}: {exc}"
        diag["ended_at_monotonic"] = time.monotonic()


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
    blockrazor_diag: dict = {}
    feed_out = OUT_DIR / "feed_messages.jsonl"
    alchemy_out = OUT_DIR / "alchemy_messages.jsonl"
    blockrazor_out = OUT_DIR / "blockrazor_messages.jsonl"

    tasks = [run_feed(deadline, feed_out, feed_diag), run_alchemy(deadline, alchemy_out, alchemy_diag)]
    # Третий источник -- ТОЛЬКО если токен реально есть в окружении (см.
    # run_blockrazor). Если нет -- diag честно фиксирует причину без
    # попытки подключения, задача 1/2 (официальный фид/Alchemy) не ждёт.
    tasks.append(run_blockrazor(deadline, blockrazor_out, blockrazor_diag))
    await asyncio.gather(*tasks)

    summary = {
        "t0_monotonic": t0_monotonic, "duration_s_requested": DURATION_S,
        "feed_diag": feed_diag, "alchemy_diag": alchemy_diag, "blockrazor_diag": blockrazor_diag,
        "feed_out": str(feed_out), "alchemy_out": str(alchemy_out), "blockrazor_out": str(blockrazor_out),
    }
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
