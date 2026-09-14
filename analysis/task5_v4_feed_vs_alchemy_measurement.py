#!/usr/bin/env python3
"""Задача 5 (владелец): ограниченный read-only замер источника данных.

ЭТА ВЕРСИЯ -- исправление реального сбоя первой попытки (владелец: "Текущая
попытка не является сравнением: feed упал до первого сообщения"). Все шесть
пунктов правки:

1. Диагностика заголовков хендшейка вынесена в отдельную, локально
   протестированную (task5_v4_feed_header_fix_unit_test.py, без сети)
   функцию _safe_extract_response_headers() -- НИКОГДА не бросает исключение
   наружу, ни на дубликате заголовка (реальный баг прошлой попытки,
   MultipleValuesError: 'server-timing'), ни на любой другой неожиданной
   поломке диагностики. Ошибка этой диагностики НЕ может закрыть соединение
   или прервать чтение -- она физически не может выйти за пределы try/except
   этой функции.

2. Alchemy newPendingTransactions: сырой запрос подписки И сырой ответ
   сохраняются в diag (секреты не пишутся -- ключ в URL, не в теле запроса;
   URL логируется в редактированном виде). Три РАЗНЫХ, явно поименованных
   исхода: "subscribed_ok" (успех, событий может быть 0 -- это НЕ то же
   самое, что провал подписки), "subscription_rejected" (в ответе есть
   "error"), "no_response_received" (не увидели ответ до таймаута/обрыва).

3. Cooldown -- честно указано основание: FEED_RECONNECT_COOLDOWN_S=1800s
   (task5_bot_feed_client.py) -- НАСТРОЙКА НАШЕГО СОБСТВЕННОГО СКРИПТА,
   калиброванная по реальным прошлым HTTP 403 (Ohio дважды, NL один раз,
   2026-09-13, docs/PROJECT_STATE.md), а НЕ опубликованная политика
   провайдера. Владелец явно авторизовал ОДИН контролируемый повтор после
   исправления бага (--bypass-cooldown-once) -- НЕ цикл реконнектов, ровно
   одна попытка за запуск, попытка всё равно фиксируется на диске
   (record_feed_connect_attempt) для честности будущего гейтинга. При
   РЕАЛЬНОМ HTTP 403 в этой попытке -- немедленный останов, ответ
   (headers/body/Retry-After) сохраняется в отдельный файл, никакого
   повторного подключения в этом запуске.

4. Порядок: подключение к фиду -> ожидание и успешное декодирование ПЕРВОГО
   содержательного сообщения В ТОМ ЖЕ соединении -> ТОЛЬКО ПОСЛЕ этого
   начинается общее измерительное окно и запускается Alchemy. Никакого
   отдельного тестового подключения к фиду до основного. Если фид не
   провалидировался за validation-таймаут (или упал раньше) -- сравнение
   немедленно помечается невалидным, Alchemy вообще не подключается, 600с
   молчаливого измерения одного источника не происходит. Если после
   валидации ЛЮБОЙ из источников падает -- общий stop_event останавливает
   оба, а не даёт другому доработать окно в одиночку.

5. Сопоставление -- по blockHash: Alchemy logs подписка фильтруется по
   topic0=Swap(...)(V4 PoolManager) -- считаются только блоки, где реально
   есть Swap-событие. Метрика называется ЯВНО: "приход блока в feed ->
   приход первого Swap-лога этого блока через Alchemy" -- это разные стадии
   обработки, не чистое сравнение сетевых каналов (см. корректор,
   task5_v4_feed_vs_alchemy_correlate.py).

6. Итоговый отчёт формируется корректором отдельно (без сети, можно
   перезапускать) -- см. task5_v4_feed_vs_alchemy_correlate.py.

Не трогает торговый код, contract/Sender, не запускает LIVE, не арендует
серверы."""
from __future__ import annotations

import argparse
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

from alchemy_fallback import UNISWAP_V4_SWAP_SIG, topic0  # noqa: E402
from config import CONFIG  # noqa: E402
from task5_bot_feed_client import (  # noqa: E402
    BROWSER_LIKE_HEADERS,
    FEED_RECONNECT_COOLDOWN_S,
    SequencerFeedClient,
    _decode_l2_msg_bytes,
    _decode_signed_tx,
    connect_with_headers,
    record_feed_connect_attempt,
    require_feed_connect_allowed_now,
    seconds_until_feed_connect_allowed,
)

FEED_URL = "wss://feed.mainnet.chain.robinhood.com"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_SWAP_TOPIC0 = topic0(UNISWAP_V4_SWAP_SIG)
DURATION_S = 600.0
# Максимум ожидания ПЕРВОГО содержательного сообщения фида ДО начала общего
# окна (владелец, п.4) -- не бесконечно (не "ждать 600с молча"), но и не
# слишком коротко ("confirmedSequenceNumberMessage" -- легитимный, но
# бессодержательный тип сообщения, может прийти первым).
FEED_FIRST_MESSAGE_VALIDATION_TIMEOUT_S = 90.0
# Сообщение считается "стартовым бэклогом" (не живым), если его
# sequencer_timestamp (unix-секунды заголовка Nitro) отстаёт от текущего
# wall-time больше чем на это значение -- реальный, ранее уже
# наблюдавшийся факт (data/task5_feed_latency_measurement.json,
# 2026-09-12): подключение к публичному фиду выдаёт пачку СТАРЫХ
# сообщений с одинаковым t_wall получения. Помечается, НЕ отбрасывается
# молча -- см. "is_backlog" в записи.
BACKLOG_THRESHOLD_S = 10.0
# Как часто проверять stop_event/окончание общего окна, пока recv()
# блокирован в ожидании следующего сообщения -- иначе один источник может
# не заметить сбой другого до собственного таймаута.
POLL_INTERVAL_S = 2.0

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
# что и официальный фид. НИКАКОГО реального токена в этом окружении НЕТ
# (проверено на Ohio, any_candidate_env_var_present=false) -- подключение
# НЕ выполняется, только подготовлена возможность (см. run_blockrazor).
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


def _redact_ws_url(url: str) -> str:
    """Убирает API-ключ/токен из URL для логов -- ключ обычно последний
    path-сегмент (Alchemy /v2/<key>, BlockRazor /ws/ultra/<token>)."""
    if not url:
        return url
    parts = url.rsplit("/", 1)
    if len(parts) == 2 and len(parts[1]) > 8:
        return parts[0] + "/<redacted>"
    return url


def _safe_headers_to_dict(headers) -> dict:
    """.raw_items() допускает повторяющиеся имена заголовков (в отличие от
    dict(headers), который на дубликате бросает MultipleValuesError -- это
    и есть реальный баг прошлой попытки: сервер фида прислал 'Server-Timing'
    дважды). НИКОГДА не бросает исключение наружу."""
    if headers is None:
        return {}
    try:
        return {k: v for k, v in headers.raw_items()}
    except Exception as exc:  # noqa: BLE001
        return {"_extraction_error": str(exc)}


def _safe_extract_response_headers(ws) -> dict:
    """Извлечение заголовков хендшейка (владелец, п.1): "повторяющийся
    server-timing не вызывает исключение. Ошибка необязательной диагностики
    заголовков не должна закрывать рабочее соединение или прекращать
    чтение." Структурно НЕ может бросить исключение наружу -- любая
    неожиданная поломка (не только дубликат заголовка) ловится и честно
    помечается в результате, никогда не пробрасывается вызывающему коду.
    Проверено локально, без сети: task5_v4_feed_header_fix_unit_test.py."""
    try:
        resp = getattr(ws, "response", None)
        if resp is None:
            return {}
        return _safe_headers_to_dict(getattr(resp, "headers", None))
    except Exception as exc:  # noqa: BLE001
        return {"_extraction_error": f"unexpected outer failure: {exc}"}


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


def _decode_feed_payload(raw, t_mono: float, fh) -> tuple[int, int]:
    """Разбирает один сырой WS-фрейм фида, пишет по строке JSONL на каждую
    извлечённую транзакцию. Возвращает (n_tx_records_written,
    n_l2_batches_successfully_decoded) -- второе используется ТОЛЬКО для
    решения "первое сообщение успешно декодировано" (п.4), не влияет на
    сами записи."""
    n_written = 0
    n_decoded_batches = 0
    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        fh.write(json.dumps({"t_monotonic": t_mono, "parse_error": str(exc)}) + "\n")
        return 0, 0
    msgs = payload.get("messages") if isinstance(payload, dict) else None
    if not msgs:
        # Легитимный, но бессодержательный тип (напр. confirmedSequenceNumberMessage) --
        # это НЕ ошибка декодирования, но и не содержательное сообщение для
        # целей валидации "первого сообщения".
        return 0, 0
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
        n_decoded_batches += 1
        tx_bytes_list: list[bytes] = []
        _walk_l2_batch_for_tx_bytes(raw_bytes, tx_bytes_list)
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
            n_written += 1
        fh.flush()
    return n_written, n_decoded_batches


async def run_feed(out_path: Path, diag: dict, window_duration_s: float,
                    validated_event: asyncio.Event, stop_event: asyncio.Event,
                    bypass_cooldown_once: bool) -> None:
    """Одно соединение, ДВЕ фазы (владелец, п.4): (1) ждём и декодируем
    первое содержательное сообщение -- это "валидация"; (2) только после
    неё начинаем отсчёт window_duration_s. Обе фазы -- ОДНО и то же
    соединение, никакого отдельного тестового подключения до основного."""
    diag["cooldown_basis"] = (
        f"FEED_RECONNECT_COOLDOWN_S={FEED_RECONNECT_COOLDOWN_S:.0f}s "
        "(task5_bot_feed_client.py) -- НАСТРОЙКА НАШЕГО СОБСТВЕННОГО СКРИПТА, "
        "НЕ опубликованная политика провайдера. Калибровка -- реальные прошлые "
        "HTTP 403 (Ohio дважды, NL один раз, 2026-09-13, см. docs/PROJECT_STATE.md)."
    )
    if bypass_cooldown_once:
        wait_s = seconds_until_feed_connect_allowed()
        diag["cooldown_bypass_used"] = True
        diag["cooldown_seconds_remaining_at_bypass"] = wait_s
        diag["cooldown_bypass_justification"] = (
            "Владелец явно авторизовал ОДИН контролируемый повтор после исправления "
            "бага заголовков хендшейка (MultipleValuesError) -- предыдущая попытка "
            "была потрачена этим багом ДО получения полезных данных, реального 403 "
            "в ней не было. Это НЕ цикл реконнектов: одна попытка за запуск. Попытка "
            "всё равно фиксируется на диске (record_feed_connect_attempt) для "
            "честности будущего гейтинга."
        )
        record_feed_connect_attempt()
    else:
        require_feed_connect_allowed_now()
    diag["connect_attempted_at_monotonic"] = time.monotonic()
    diag["first_message_validated"] = False
    with out_path.open("w") as fh:
        try:
            async with connect_with_headers(FEED_URL, BROWSER_LIKE_HEADERS, open_timeout=10,
                                             close_timeout=5, max_size=None) as ws:
                diag["connected"] = True
                diag["connected_at_monotonic"] = time.monotonic()
                diag["handshake_response_headers"] = _safe_extract_response_headers(ws)
                ext = diag["handshake_response_headers"].get("Sec-WebSocket-Extensions", "")
                diag["permessage_deflate_negotiated"] = ("permessage-deflate" in ext
                                                          if isinstance(ext, str) else None)

                # --- Фаза 1: валидация первого содержательного сообщения ---
                validation_deadline = time.monotonic() + FEED_FIRST_MESSAGE_VALIDATION_TIMEOUT_S
                n_frames_seen_before_validation = 0
                while time.monotonic() < validation_deadline:
                    remaining = validation_deadline - time.monotonic()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, POLL_INTERVAL_S))
                    except asyncio.TimeoutError:
                        continue
                    t_mono = time.monotonic()
                    n_frames_seen_before_validation += 1
                    if "first_message_at_monotonic" not in diag:
                        diag["first_message_at_monotonic"] = t_mono
                        diag["first_message_is_text"] = isinstance(raw, str)
                        diag["first_message_len_bytes"] = len(raw) if isinstance(raw, (str, bytes)) else None
                    n_written, n_decoded_batches = _decode_feed_payload(raw, t_mono, fh)
                    diag["n_messages"] = diag.get("n_messages", 0) + 1
                    if n_decoded_batches > 0:
                        diag["first_message_validated"] = True
                        diag["first_message_validated_at_monotonic"] = t_mono
                        diag["n_frames_before_validation"] = n_frames_seen_before_validation
                        break
                if not diag["first_message_validated"]:
                    diag["ended_reason"] = "first_message_validation_timeout"
                    diag["n_frames_seen_before_validation_timeout"] = n_frames_seen_before_validation
                    stop_event.set()
                    diag["ended_at_monotonic"] = time.monotonic()
                    return
                validated_event.set()

                # --- Фаза 2: общее измерительное окно (уже провалидированное соединение) ---
                window_deadline = diag["first_message_validated_at_monotonic"] + window_duration_s
                while time.monotonic() < window_deadline:
                    if stop_event.is_set() and diag.get("ended_reason") is None:
                        diag["ended_reason"] = "stopped_by_other_source_failure"
                        break
                    remaining = window_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, POLL_INTERVAL_S))
                    except asyncio.TimeoutError:
                        continue
                    t_mono = time.monotonic()
                    diag["n_messages"] = diag.get("n_messages", 0) + 1
                    _decode_feed_payload(raw, t_mono, fh)
                diag.setdefault("ended_reason", "deadline_reached")
        except websockets.exceptions.InvalidStatus as exc:
            resp = exc.response
            diag["connected"] = False
            diag["status_code"] = resp.status_code
            resp_headers = _safe_headers_to_dict(getattr(resp, "headers", None))
            diag["response_headers"] = resp_headers
            diag["response_body"] = resp.body.decode("utf-8", errors="replace") if resp.body else None
            if resp.status_code == 403:
                diag["real_403_this_attempt"] = True
                diag["retry_after_header"] = resp_headers.get("Retry-After")
                diag["stop_reason"] = (
                    "Реальный HTTP 403 подтверждён В ЭТОЙ попытке -- немедленный останов, "
                    "никакого повторного подключения в этом запуске (владелец, п.3)."
                )
                (OUT_DIR / "feed_403_response.json").write_text(json.dumps({
                    "status_code": resp.status_code, "headers": resp_headers,
                    "body": diag["response_body"], "retry_after": resp_headers.get("Retry-After"),
                    "at_monotonic": time.monotonic(), "at_wall_time": time.time(),
                }, indent=2, ensure_ascii=False))
            stop_event.set()
        except Exception as exc:  # noqa: BLE001
            diag["connected"] = diag.get("connected", False)
            diag["disconnect_error"] = f"{type(exc).__name__}: {exc}"
            stop_event.set()
        diag["ended_at_monotonic"] = time.monotonic()


async def _subscribe(ws, name: str, params: dict | None, diag: dict, diag_key: str) -> str | None:
    """Сохраняет СЫРОЙ запрос подписки И сырой ответ (владелец, п.2) --
    секретов в теле нет (ключ Alchemy -- в URL, не здесь). Три РАЗНЫХ,
    явно поименованных исхода: subscribed_ok / subscription_rejected /
    no_response_received -- нулевые последующие события НЕ равны отказу
    подписки, это отдельные, независимые факты."""
    req = {"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": [name] + ([params] if params else [])}
    diag[f"{diag_key}_request"] = req
    await ws.send(json.dumps(req))
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    except asyncio.TimeoutError:
        diag[f"{diag_key}_response"] = None
        diag[f"{diag_key}_outcome"] = "no_response_received_timeout"
        return None
    except Exception as exc:  # noqa: BLE001
        diag[f"{diag_key}_response"] = None
        diag[f"{diag_key}_outcome"] = f"no_response_received_error: {type(exc).__name__}: {exc}"
        return None
    try:
        body = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        diag[f"{diag_key}_response"] = {"_parse_error": str(exc)}
        diag[f"{diag_key}_outcome"] = "no_response_received_unparseable"
        return None
    diag[f"{diag_key}_response"] = body
    if isinstance(body, dict) and "error" in body:
        diag[f"{diag_key}_outcome"] = "subscription_rejected"
        return None
    if isinstance(body, dict) and body.get("result"):
        diag[f"{diag_key}_outcome"] = "subscribed_ok"
        return body.get("result")
    diag[f"{diag_key}_outcome"] = "unexpected_response_shape"
    return body.get("result") if isinstance(body, dict) else None


def _alchemy_ws_url() -> str | None:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url.replace("https://", "wss://", 1)
    if CONFIG.alchemy_api_key:
        return f"wss://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    return None


async def run_alchemy(deadline: float, out_path: Path, diag: dict, stop_event: asyncio.Event) -> None:
    url = _alchemy_ws_url()
    if not url:
        diag["error"] = "нет ALCHEMY_API_KEY/ALCHEMY_ROBINHOOD_RPC_URL -- Alchemy WS недоступен"
        stop_event.set()
        return
    diag["url_redacted"] = _redact_ws_url(url)
    diag["n_messages_pending"] = 0
    diag["n_messages_log"] = 0
    diag["n_reconnects"] = 0
    with out_path.open("w") as fh:
        while time.monotonic() < deadline and not stop_event.is_set():
            try:
                async with websockets.connect(url, open_timeout=10, close_timeout=5) as ws:
                    diag["connected"] = True
                    # ПРАВКА (владелец, п.5): logs-подписка фильтруется по
                    # topic0=Swap(...) V4 PoolManager -- "учитывай только блоки
                    # с нужными Swap-событиями", а не все логи PoolManager.
                    sub_pending = await _subscribe(ws, "newPendingTransactions", None, diag, "pending_tx_sub")
                    sub_logs = await _subscribe(
                        ws, "logs", {"address": POOL_MANAGER, "topics": [V4_SWAP_TOPIC0]}, diag, "swap_log_sub")
                    diag["sub_id_pending"] = sub_pending
                    diag["sub_id_logs"] = sub_logs
                    while time.monotonic() < deadline:
                        if stop_event.is_set():
                            diag["ended_reason"] = "stopped_by_other_source_failure"
                            return
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, POLL_INTERVAL_S))
                        except asyncio.TimeoutError:
                            continue
                        t_mono = time.monotonic()
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            continue
                        params = payload.get("params") or {}
                        sub_id = params.get("subscription")
                        result = params.get("result")
                        if sub_id == sub_pending and isinstance(result, str):
                            diag["n_messages_pending"] += 1
                            fh.write(json.dumps({"t_monotonic": t_mono, "kind": "pending_tx", "tx_hash": result}) + "\n")
                        elif sub_id == sub_logs and isinstance(result, dict):
                            diag["n_messages_log"] += 1
                            fh.write(json.dumps({
                                "t_monotonic": t_mono, "kind": "log",
                                "tx_hash": result.get("transactionHash"),
                                "block_hash": result.get("blockHash"),
                                "block_number": result.get("blockNumber"),
                                "log_index": result.get("logIndex"),
                                "topic0": (result.get("topics") or [None])[0],
                            }) + "\n")
                        fh.flush()
                    diag.setdefault("ended_reason", "deadline_reached")
            except Exception as exc:  # noqa: BLE001
                diag["n_reconnects"] += 1
                diag["last_error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.0)
    # ПРАВКА (владелец, п.2): успешная подписка с нулём событий -- ЧЕСТНО
    # отдельная пометка, не вывод "подписки нет" по факту нуля уведомлений.
    diag["pending_tx_zero_events_note"] = (
        "subscribed_ok, но n_messages_pending==0 -- это НЕ означает 'подписки не было', "
        "см. pending_tx_sub_outcome/_request/_response для реального исхода подписки."
        if diag.get("pending_tx_sub_outcome") == "subscribed_ok" and diag["n_messages_pending"] == 0
        else None
    )


def _blockrazor_token() -> str | None:
    for name in BLOCKRAZOR_TOKEN_ENV_VARS:
        val = os.environ.get(name)
        if val:
            return val
    return None


async def run_blockrazor(deadline: float, out_path: Path, diag: dict, stop_event: asyncio.Event) -> None:
    """Третий источник (владелец, подготовка) -- BlockRazor Sequencer Feed
    Ultra. НЕ подключается, если токена нет (проверено на этом же хосте:
    any_candidate_env_var_present=false) -- владелец: "BlockRazor без
    токена пропусти"."""
    token = _blockrazor_token()
    if not token:
        diag["skipped"] = True
        diag["reason"] = (
            "нет ни одной из переменных окружения " + ", ".join(BLOCKRAZOR_TOKEN_ENV_VARS) +
            " -- токен доступа BlockRazor отсутствует, подключение не выполняется (владелец: пропустить)"
        )
        return
    url = BLOCKRAZOR_URL_TEMPLATE.format(token=token)
    diag["url_redacted"] = _redact_ws_url(url)
    diag["connect_attempted_at_monotonic"] = time.monotonic()
    with out_path.open("w") as fh:
        try:
            async with connect_with_headers(url, BROWSER_LIKE_HEADERS, open_timeout=10,
                                             close_timeout=5, max_size=None) as ws:
                diag["connected"] = True
                diag["connected_at_monotonic"] = time.monotonic()
                while time.monotonic() < deadline:
                    if stop_event.is_set():
                        diag["ended_reason"] = "stopped_by_other_source_failure"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, POLL_INTERVAL_S))
                    except asyncio.TimeoutError:
                        continue
                    t_mono = time.monotonic()
                    diag["n_messages"] = diag.get("n_messages", 0) + 1
                    _decode_feed_payload(raw, t_mono, fh)
                diag.setdefault("ended_reason", "deadline_reached")
        except Exception as exc:  # noqa: BLE001
            diag["connected"] = diag.get("connected", False)
            diag["disconnect_error"] = f"{type(exc).__name__}: {exc}"
        diag["ended_at_monotonic"] = time.monotonic()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--bypass-cooldown-once", action="store_true",
        help="Явно авторизованный ОДИН обход локального cooldown-гарда фида (владелец, п.3). "
             "НЕ цикл реконнектов -- одна попытка. Использовать только когда предыдущая попытка "
             "была потрачена багом ДО получения данных, без реального 403.")
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    feed_diag: dict = {}
    alchemy_diag: dict = {}
    blockrazor_diag: dict = {}
    feed_out = OUT_DIR / "feed_messages.jsonl"
    alchemy_out = OUT_DIR / "alchemy_messages.jsonl"
    blockrazor_out = OUT_DIR / "blockrazor_messages.jsonl"

    stop_event = asyncio.Event()
    validated_event = asyncio.Event()

    t0_monotonic = time.monotonic()
    feed_task = asyncio.create_task(
        run_feed(feed_out, feed_diag, DURATION_S, validated_event, stop_event, args.bypass_cooldown_once))

    # ПРАВКА (владелец, п.4): ждём подтверждения ПЕРВОГО содержательного
    # сообщения фида (в РАМКАХ ТОГО ЖЕ соединения) ДО начала общего окна и
    # ДО подключения Alchemy -- никакого отдельного тестового подключения.
    validated_waiter = asyncio.create_task(validated_event.wait())
    done, _pending = await asyncio.wait(
        {validated_waiter, feed_task}, timeout=FEED_FIRST_MESSAGE_VALIDATION_TIMEOUT_S + 15.0,
        return_when=asyncio.FIRST_COMPLETED)

    comparison_valid = True
    invalidity_reason = None
    if feed_task in done and not validated_event.is_set():
        comparison_valid = False
        invalidity_reason = "feed завершился (ошибка/останов) ДО валидации первого сообщения -- см. feed_diag"
        validated_waiter.cancel()
    elif validated_waiter not in done:
        comparison_valid = False
        invalidity_reason = (
            f"первое сообщение фида не подтверждено за отведённое время "
            f"({FEED_FIRST_MESSAGE_VALIDATION_TIMEOUT_S:.0f}s + запас) -- см. feed_diag"
        )
        validated_waiter.cancel()
        stop_event.set()

    if not comparison_valid:
        # ПРАВКА (владелец, п.4): "если один источник упал -- сразу
        # зафиксируй невалидность сравнения; не продолжай десять минут
        # молча измерять только второй." Alchemy вообще не запускается.
        try:
            await asyncio.wait_for(feed_task, timeout=10.0)
        except Exception:
            pass
        summary = {
            "comparison_valid": False, "invalidity_reason": invalidity_reason,
            "t0_monotonic": t0_monotonic, "duration_s_requested": DURATION_S,
            "feed_diag": feed_diag, "alchemy_diag": {"note": "не запускался -- фид не провалидирован"},
            "blockrazor_diag": {"note": "не запускался -- фид не провалидирован"},
            "feed_out": str(feed_out),
        }
        print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
        (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
        return

    # Фид провалидирован -- окно уже официально идёт с
    # feed_diag["first_message_validated_at_monotonic"]. Alchemy стартует
    # ТЕПЕРЬ, с тем же дедлайном.
    window_deadline = feed_diag["first_message_validated_at_monotonic"] + DURATION_S
    alchemy_task = asyncio.create_task(run_alchemy(window_deadline, alchemy_out, alchemy_diag, stop_event))
    blockrazor_task = asyncio.create_task(run_blockrazor(window_deadline, blockrazor_out, blockrazor_diag, stop_event))

    await asyncio.gather(feed_task, alchemy_task, blockrazor_task, return_exceptions=True)

    summary = {
        "comparison_valid": True,
        "t0_monotonic": t0_monotonic, "duration_s_requested": DURATION_S,
        "window_started_at_monotonic": feed_diag["first_message_validated_at_monotonic"],
        "window_deadline_monotonic": window_deadline,
        "stopped_early": stop_event.is_set() and time.monotonic() < window_deadline,
        "feed_diag": feed_diag, "alchemy_diag": alchemy_diag, "blockrazor_diag": blockrazor_diag,
        "feed_out": str(feed_out), "alchemy_out": str(alchemy_out), "blockrazor_out": str(blockrazor_out),
    }
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
