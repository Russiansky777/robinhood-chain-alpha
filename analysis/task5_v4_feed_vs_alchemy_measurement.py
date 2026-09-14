#!/usr/bin/env python3
"""Задача 5 (владелец): ограниченный read-only замер источника данных.

ВТОРОЙ раунд правки -- владелец: "Данные пусты (Alchemy не успел
подключиться, пока жил фид)". Устраняет причины НЕВАЛИДНОГО измерения,
без нового пустого прогона. Пункты правки (нумерация -- по последнему
сообщению владельца):

1. Разбор уже сохранённых 733 сообщений -- ОТДЕЛЬНЫЙ скрипт
   (task5_v4_feed_backlog_diagnosis.py), без нового подключения.

2. Event loop НЕ блокируется тяжёлой обработкой в цикле приёма: приём
   (`ws.recv()` + фиксация t_monotonic ДО разбора) отделён от обработки
   (decode/ecrecover/JSON/запись файла) через asyncio.Queue и отдельную
   задачу-потребитель (_feed_message_processor). Реальный, локально
   измеренный (task5_v4_feed_eventloop_diagnosis.py, БЕЗ сети) вклад
   decode+ecrecover -- ~8.1мс/tx; при пакетах из многих транзакций в
   одном кадре это МОГЛО задерживать внутренний PING/PONG той же
   asyncio-петли. Ping timeout НЕ трактуется как доказательство
   неисправности сервера, heartbeat НЕ отключается/не ослабляется --
   вместо этого тяжёлая работа физически вынесена из горячего пути.

3. Alchemy проверена ОТДЕЛЬНО от фида (task5_v4_alchemy_ws_standalone_
   check.py) -- тот же URL, что отработал 600с в самой первой попытке.

4. Порядок общего запуска изменён: СНАЧАЛА Alchemy (handshake ->
   logs-подписка с фильтром Swap V4 PoolManager -> подтверждение
   получения РЕАЛЬНОГО Swap-лога, ограниченно по времени,
   ALCHEMY_PREP_TIMEOUT_S). Только ПОСЛЕ этого -- cooldown-гейт фида
   (обход -- ТОЛЬКО если cooldown ещё активен именно в этот момент,
   владелец разрешил однократно) и ОДНО соединение с фидом. Официальное
   600-секундное окно стартует только когда ОБА условия выполнены:
   успешное декодирование ХОТЯ БЫ одного сообщения И подтверждённый
   выход на актуальную (не-backlog) последовательность -- иначе, по
   истечении FEED_CATCHUP_TIMEOUT_S, честный останов с конкретным
   диагнозом (не открывает пробное соединение заранее). Alchemy уже
   открытое соединение из шага подготовки ПРОДОЛЖАЕТ работать -- не
   переоткрывается.

5. Сопоставление по blockHash -- см. task5_v4_feed_vs_alchemy_correlate.py.

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
# Ограниченный период подготовки Alchemy (владелец, п.3/4): один заход,
# handshake -> logs-подписка -> ожидание РЕАЛЬНОГО Swap-лога. Не
# бесконечно (не "ждать сколько понадобится").
ALCHEMY_PREP_TIMEOUT_S = 90.0
# Ограниченный период подготовки фида: декодирование первого сообщения +
# выход на актуальную (не-backlog) последовательность. Предыдущий реальный
# прогон прожил 64.7с и НИ РАЗУ не поймал не-backlog сообщение -- этот
# лимит сознательно шире (не гарантированно достаточен), чтобы дать
# исправлению п.2 (event loop не блокируется) реальный шанс быстрее
# вычитать бэклог, а не гадать точное число.
FEED_CATCHUP_TIMEOUT_S = 180.0
# Сообщение считается "стартовым бэклогом" (не живым), если его
# sequencer_timestamp (unix-секунды заголовка Nitro) отстаёт от текущего
# wall-time больше чем на это значение -- реальный, ранее уже
# наблюдавшийся факт (data/task5_feed_latency_measurement.json,
# 2026-09-12): подключение к публичному фиду выдаёт пачку СТАРЫХ
# сообщений с одинаковым t_wall получения. Помечается, НЕ отбрасывается
# молча -- см. "is_backlog" в записи.
BACKLOG_THRESHOLD_S = 10.0
POLL_INTERVAL_S = 2.0

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


def _pctl(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


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
    и есть реальный баг первой попытки: сервер прислал 'Server-Timing'
    дважды). НИКОГДА не бросает исключение наружу."""
    if headers is None:
        return {}
    try:
        return {k: v for k, v in headers.raw_items()}
    except Exception as exc:  # noqa: BLE001
        return {"_extraction_error": str(exc)}


def _safe_extract_response_headers(ws) -> dict:
    """Извлечение заголовков хендшейка -- структурно НЕ может бросить
    исключение наружу, ни на дубликате заголовка, ни на любой другой
    неожиданной поломке диагностики. Проверено локально, без сети:
    task5_v4_feed_header_fix_unit_test.py."""
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


def _decode_feed_payload(raw, t_mono: float, fh) -> tuple[int, int, bool]:
    """Разбирает один сырой WS-кадр фида, пишет по строке JSONL на каждую
    извлечённую транзакцию. Возвращает (n_tx_records_written,
    n_l2_batches_successfully_decoded, any_live_group) -- any_live_group
    True, если ХОТЯ БЫ одна группа (sequenceNumber) в этом кадре НЕ
    классифицирована как backlog (используется для критерия "выход на
    актуальную последовательность", владелец п.4)."""
    n_written = 0
    n_decoded_batches = 0
    any_live = False
    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        fh.write(json.dumps({"t_monotonic": t_mono, "parse_error": str(exc)}) + "\n")
        return 0, 0, False
    msgs = payload.get("messages") if isinstance(payload, dict) else None
    if not msgs:
        return 0, 0, False
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
        is_backlog = (seq_ts is not None and (time.time() - seq_ts) > BACKLOG_THRESHOLD_S)
        if not is_backlog:
            any_live = True
        tx_bytes_list: list[bytes] = []
        _walk_l2_batch_for_tx_bytes(raw_bytes, tx_bytes_list)
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
    return n_written, n_decoded_batches, any_live


async def _feed_message_processor(queue: asyncio.Queue, fh, diag: dict,
                                   window_deadline_box: dict, window_duration_s: float) -> None:
    """Тяжёлая обработка (JSON parse, RLP decode, ecrecover, запись файла)
    -- ОТДЕЛЬНО от цикла recv() (владелец, п.2): приём кладёт (raw, t_mono)
    в очередь и немедленно возвращается к следующему recv(), не дожидаясь
    разбора. t_mono фиксируется приёмом ДО постановки в очередь, то есть
    ДО любого разбора. Здесь же -- переход состояний "первое сообщение
    декодировано" / "выход на актуальную последовательность" (владелец,
    п.4), последнее -- задаёт window_deadline_box['deadline'], которое
    видит и run_alchemy (то же соединение Alchemy продолжает работать,
    не переоткрывается)."""
    processing_times_ms: list[float] = []
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break
        raw, t_mono = item
        t_proc_start = time.perf_counter()
        n_written, n_decoded_batches, any_live = _decode_feed_payload(raw, t_mono, fh)
        processing_times_ms.append((time.perf_counter() - t_proc_start) * 1000)
        diag["n_tx_records_written"] = diag.get("n_tx_records_written", 0) + n_written
        if n_decoded_batches > 0 and not diag.get("first_message_validated"):
            diag["first_message_validated"] = True
            diag["first_message_validated_at_monotonic"] = t_mono
        if any_live and not diag.get("caught_up_to_live"):
            diag["caught_up_to_live"] = True
            diag["caught_up_to_live_at_monotonic"] = t_mono
            diag["window_started_at_monotonic"] = t_mono
            window_deadline_box["deadline"] = t_mono + window_duration_s
        queue.task_done()
    if processing_times_ms:
        diag["processing_time_ms_stats"] = {
            "n": len(processing_times_ms),
            "mean": sum(processing_times_ms) / len(processing_times_ms),
            "max": max(processing_times_ms),
            "p95": _pctl(processing_times_ms, 0.95),
        }


def _feed_cooldown_gate(diag: dict, force_bypass: bool) -> None:
    """Владелец, п.4: обход cooldown разрешён ОДНОКРАТНО, но ТОЛЬКО если
    cooldown ещё реально активен В ЭТОТ МОМЕНТ (после уже подтверждённой
    готовности Alchemy) -- не безусловно. Основание cooldown -- честно:
    это НАСТРОЙКА НАШЕГО СКРИПТА (FEED_RECONNECT_COOLDOWN_S=1800s,
    task5_bot_feed_client.py), НЕ опубликованная политика провайдера."""
    diag["cooldown_basis"] = (
        f"FEED_RECONNECT_COOLDOWN_S={FEED_RECONNECT_COOLDOWN_S:.0f}s "
        "(task5_bot_feed_client.py) -- НАСТРОЙКА НАШЕГО СОБСТВЕННОГО СКРИПТА, "
        "НЕ опубликованная политика провайдера. Калибровка -- реальные прошлые "
        "HTTP 403 (Ohio дважды, NL один раз, 2026-09-13, см. docs/PROJECT_STATE.md)."
    )
    wait_s = seconds_until_feed_connect_allowed()
    if wait_s <= 0 and not force_bypass:
        require_feed_connect_allowed_now()
        diag["cooldown_bypass_used"] = False
        return
    diag["cooldown_bypass_used"] = True
    diag["cooldown_seconds_remaining_at_bypass"] = wait_s
    diag["cooldown_bypass_justification"] = (
        "Владелец разрешил ОДНОКРАТНЫЙ обход, ТОЛЬКО если cooldown ещё активен ПОСЛЕ "
        "уже подтверждённой готовности Alchemy (реальный Swap-лог получен) -- это НЕ "
        "цикл реконнектов, ровно одна попытка. Попытка всё равно фиксируется на диске "
        "(record_feed_connect_attempt) для честности будущего гейтинга."
    )
    record_feed_connect_attempt()


async def run_feed(out_path: Path, diag: dict, window_duration_s: float, catchup_timeout_s: float,
                    stop_event: asyncio.Event, window_deadline_box: dict) -> None:
    """ОДНО соединение (владелец: "не открывай пробное соединение feed
    перед основным"). Единый цикл приёма (recv + t_monotonic до разбора,
    п.2) с двумя состояниями перехода, отслеживаемыми в diag процессором:
    first_message_validated и caught_up_to_live. Официальное окно
    (window_deadline_box['deadline']) выставляется ТОЛЬКО когда оба
    условия достигнуты; иначе -- честный останов по catchup_timeout_s с
    конкретным диагнозом (владелец, п.4), stop_event останавливает и
    Alchemy."""
    diag["first_message_validated"] = False
    diag["caught_up_to_live"] = False
    diag["connect_attempted_at_monotonic"] = time.monotonic()
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
                diag["ping_pong_note"] = (
                    "ping_interval/ping_timeout НЕ переопределены -- дефолты установленной "
                    "версии websockets (см. task5_v4_feed_eventloop_diagnosis.py:: "
                    "websockets_ping_pong_defaults, реально 20s/20s на 17.1). Не изменены "
                    "здесь намеренно -- ping timeout не трактуется как доказательство "
                    "неисправности сервера, heartbeat не ослабляется как 'исправление'."
                )

                queue: asyncio.Queue = asyncio.Queue()
                diag["queue_max_size_seen"] = 0
                processor_task = asyncio.create_task(
                    _feed_message_processor(queue, fh, diag, window_deadline_box, window_duration_s))
                prep_deadline = time.monotonic() + catchup_timeout_s
                try:
                    while True:
                        if stop_event.is_set():
                            diag.setdefault("ended_reason", "stopped_by_other_source_failure")
                            break
                        dl = window_deadline_box["deadline"]
                        if dl is not None:
                            if time.monotonic() >= dl:
                                diag.setdefault("ended_reason", "deadline_reached")
                                break
                        elif time.monotonic() >= prep_deadline:
                            diag["ended_reason"] = "prep_catchup_timeout"
                            diag["prep_catchup_timeout_detail"] = (
                                f"first_message_validated={diag.get('first_message_validated', False)}, "
                                f"caught_up_to_live={diag.get('caught_up_to_live', False)} -- не достигнута "
                                f"актуальная (не-backlog) последовательность за {catchup_timeout_s:.0f}s подготовки"
                            )
                            stop_event.set()
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=POLL_INTERVAL_S)
                        except asyncio.TimeoutError:
                            continue
                        t_mono = time.monotonic()  # ФИКСАЦИЯ ДО разбора (владелец, п.2)
                        diag["n_messages"] = diag.get("n_messages", 0) + 1
                        queue.put_nowait((raw, t_mono))
                        diag["queue_max_size_seen"] = max(diag["queue_max_size_seen"], queue.qsize())
                finally:
                    try:
                        await asyncio.wait_for(queue.join(), timeout=5.0)
                    except asyncio.TimeoutError:
                        diag["queue_drain_timeout"] = True
                        diag["queue_size_at_drain_timeout"] = queue.qsize()
                    queue.put_nowait(None)
                    await processor_task
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
    """Сохраняет СЫРОЙ запрос подписки И сырой ответ (владелец, п.2 первого
    раунда) -- секретов в теле нет (ключ Alchemy -- в URL, не здесь). Три
    РАЗНЫХ, явно поименованных исхода: subscribed_ok / subscription_rejected
    / no_response_received_*."""
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
    return None


def _alchemy_ws_url() -> str | None:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url.replace("https://", "wss://", 1)
    if CONFIG.alchemy_api_key:
        return f"wss://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    return None


async def run_alchemy(out_path: Path, diag: dict, stop_event: asyncio.Event, ready_event: asyncio.Event,
                       window_deadline_box: dict, prep_timeout_s: float) -> None:
    """Владелец, п.3/4: подключается ПЕРВОЙ (до фида), логи -- ТОЛЬКО
    Swap V4 PoolManager. Готовность = РЕАЛЬНЫЙ полученный Swap-лог (не
    просто ack подписки), в пределах prep_timeout_s -- иначе честный отказ
    подготовки, конкретная стадия/ошибка, БЕЗ бесконечных retries. После
    готовности ЭТО ЖЕ соединение продолжает работать до window_deadline_box
    (заполняется фидом после его собственного catch-up) -- не
    переоткрывается."""
    url = _alchemy_ws_url()
    if not url:
        diag["stage"] = "no_url_configured"
        diag["error"] = "нет ALCHEMY_API_KEY/ALCHEMY_ROBINHOOD_RPC_URL -- Alchemy WS недоступен"
        stop_event.set()
        return
    diag["url_redacted"] = _redact_ws_url(url)
    diag["n_messages_pending"] = 0
    diag["n_messages_log"] = 0
    diag["stage"] = "connecting"
    with out_path.open("w") as fh:
        try:
            async with websockets.connect(url, open_timeout=15, close_timeout=5) as ws:
                diag["connected"] = True
                diag["connected_at_monotonic"] = time.monotonic()

                diag["stage"] = "subscribing_logs"
                sub_logs = await _subscribe(
                    ws, "logs", {"address": POOL_MANAGER, "topics": [V4_SWAP_TOPIC0]}, diag, "swap_log_sub")
                if not sub_logs:
                    diag["stage"] = "logs_subscription_failed"
                    diag["prep_failed_reason"] = f"logs subscription outcome: {diag.get('swap_log_sub_outcome')}"
                    stop_event.set()
                    return

                # newPendingTransactions -- ОТДЕЛЬНО, её отказ НЕ ломает logs-проверку
                # (владелец, п.2 первого раунда).
                diag["stage"] = "subscribing_pending_tx_separately"
                sub_pending = await _subscribe(ws, "newPendingTransactions", None, diag, "pending_tx_sub")

                diag["stage"] = "waiting_for_real_swap_log"
                prep_deadline = time.monotonic() + prep_timeout_s
                got_first_log = False
                while True:
                    if window_deadline_box["deadline"] is not None:
                        if time.monotonic() >= window_deadline_box["deadline"]:
                            diag.setdefault("ended_reason", "deadline_reached")
                            break
                    elif stop_event.is_set():
                        diag.setdefault("ended_reason", "stopped_by_other_source_failure")
                        break
                    elif not got_first_log and time.monotonic() >= prep_deadline:
                        diag["stage"] = "prep_timeout_no_real_swap_log"
                        diag["prep_failed_reason"] = f"не получен ни один реальный Swap-лог за {prep_timeout_s:.0f}s"
                        stop_event.set()
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=POLL_INTERVAL_S)
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
                        fh.flush()
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
                        if not got_first_log:
                            got_first_log = True
                            diag["stage"] = "ready_real_swap_log_received"
                            diag["ready_at_monotonic"] = t_mono
                            diag["first_real_swap_log_seconds_after_connect"] = (
                                t_mono - diag["connected_at_monotonic"])
                            ready_event.set()
                diag.setdefault("ended_reason", "loop_exit")
        except websockets.exceptions.InvalidStatus as exc:
            diag["stage"] = "handshake_rejected"
            diag["status_code"] = exc.response.status_code
            stop_event.set()
        except Exception as exc:  # noqa: BLE001
            diag["stage"] = f"failed_at_{diag.get('stage', 'unknown')}"
            diag["connected"] = diag.get("connected", False)
            diag["disconnect_error"] = f"{type(exc).__name__}: {exc}"
            stop_event.set()
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
    """Третий источник (владелец, подготовка) -- пропускается без токена
    (владелец: "BlockRazor без токена пропусти", "сейчас не трогать")."""
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
        help="Форсирует обход cooldown НЕЗАВИСИМО от его состояния (обычно не нужно -- "
             "_feed_cooldown_gate уже автоматически обходит cooldown, если он ещё активен "
             "именно в момент, когда Alchemy уже подтверждённо готова).")
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
    window_deadline_box: dict = {"deadline": None}
    t0_monotonic = time.monotonic()

    def _write_invalid_summary(reason: str) -> None:
        summary = {
            "comparison_valid": False, "invalidity_reason": reason,
            "t0_monotonic": t0_monotonic, "duration_s_requested": DURATION_S,
            "feed_diag": feed_diag, "alchemy_diag": alchemy_diag, "blockrazor_diag": blockrazor_diag,
            "alchemy_out": str(alchemy_out), "feed_out": str(feed_out),
        }
        print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
        (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))

    # --- Шаг 1 (владелец, п.4): Alchemy СНАЧАЛА, до какого-либо подключения к фиду ---
    alchemy_ready = asyncio.Event()
    alchemy_task = asyncio.create_task(
        run_alchemy(alchemy_out, alchemy_diag, stop_event, alchemy_ready, window_deadline_box,
                    ALCHEMY_PREP_TIMEOUT_S))
    ready_waiter = asyncio.create_task(alchemy_ready.wait())
    done, _pending = await asyncio.wait(
        {ready_waiter, alchemy_task}, timeout=ALCHEMY_PREP_TIMEOUT_S + 20.0, return_when=asyncio.FIRST_COMPLETED)
    if not alchemy_ready.is_set():
        ready_waiter.cancel()
        stop_event.set()
        try:
            await asyncio.wait_for(alchemy_task, timeout=10.0)
        except Exception:
            pass
        reason = ("Alchemy завершилась раньше готовности" if alchemy_task in done else
                  f"Alchemy не подтвердила получение реального Swap-лога за "
                  f"{ALCHEMY_PREP_TIMEOUT_S:.0f}s + запас")
        feed_diag["note"] = "фид НЕ подключался -- подготовка Alchemy не пройдена (владелец, п.4)"
        _write_invalid_summary(reason)
        return

    # --- Шаг 2: cooldown-гейт (обход -- только если ещё активен, только после готовности Alchemy) ---
    try:
        _feed_cooldown_gate(feed_diag, force_bypass=args.bypass_cooldown_once)
    except RuntimeError as exc:
        stop_event.set()
        try:
            await asyncio.wait_for(alchemy_task, timeout=10.0)
        except Exception:
            pass
        _write_invalid_summary(str(exc))
        return

    # --- Шаг 3: ОДНО соединение с фидом (Alchemy уже открыта и подтверждённо готова) ---
    feed_task = asyncio.create_task(
        run_feed(feed_out, feed_diag, DURATION_S, FEED_CATCHUP_TIMEOUT_S, stop_event, window_deadline_box))
    blockrazor_deadline = time.monotonic() + ALCHEMY_PREP_TIMEOUT_S + FEED_CATCHUP_TIMEOUT_S + DURATION_S + 30.0
    blockrazor_task = asyncio.create_task(run_blockrazor(blockrazor_deadline, blockrazor_out, blockrazor_diag, stop_event))

    await asyncio.gather(feed_task, alchemy_task, blockrazor_task, return_exceptions=True)

    comparison_valid = bool(feed_diag.get("caught_up_to_live"))
    summary = {
        "comparison_valid": comparison_valid,
        "invalidity_reason": None if comparison_valid else feed_diag.get(
            "prep_catchup_timeout_detail", feed_diag.get("disconnect_error", "фид не достиг готовности")),
        "t0_monotonic": t0_monotonic, "duration_s_requested": DURATION_S,
        "window_started_at_monotonic": feed_diag.get("window_started_at_monotonic"),
        "window_deadline_monotonic": window_deadline_box["deadline"],
        "stopped_early": stop_event.is_set(),
        "feed_diag": feed_diag, "alchemy_diag": alchemy_diag, "blockrazor_diag": blockrazor_diag,
        "feed_out": str(feed_out), "alchemy_out": str(alchemy_out), "blockrazor_out": str(blockrazor_out),
    }
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
