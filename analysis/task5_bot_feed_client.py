#!/usr/bin/env python3
"""Задача 5, живой бот -- клиент фида секвенсера. Внешний JSON-конверт
подтверждён РЕАЛЬНО (analysis/seq_feed_latency_probe.py, реальный прогон
2026-09-11: сообщения с ключами {"messages": [{"sequenceNumber": N, ...}]}
и отдельно {"confirmedSequenceNumberMessage": ..., "version": ...} --
оба формата реально наблюдались на живом фиде).

ЧЕСТНАЯ ОГОВОРКА (главный реальный риск сроков, см. PROJECT_STATE.md):
декодирование СОДЕРЖИМОГО L2-сообщения (`messages[].message.l2Msg`) --
это Nitro `L1IncomingMessage` формат Arbitrum. `decode_l2_message()`
ниже реализует НАИБОЛЕЕ распространённый случай (`L2MessageType_signedTx`,
байт-константа 4 по публичной документации Nitro `arbos/l2message.go` --
НЕ проверено вживую на реальном фиде этой цепи, будет сверено в течение
первой недели dry-run) -- обычная RLP-транзакция, которую дальше можно
декодировать стандартным средством (eth_account/rlp). Другие типы
L2-сообщений (batch, deposit и т.д.) explicit НЕ обрабатываются --
пропускаются с пометкой в диагностике, не гадаются вслепую."""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import websockets

# Владелец, 2026-09-13, правило после повторных реальных 403 (структурная
# разведка прошла с 15+ минутным простоем IP, следующий 10-минутный
# dry-run с тем же IP через ~10-15 минут снова упал -- см. PROJECT_STATE.md,
# "честный пересмотр 403"): "одно соединение при старте, реконнект только
# при обрыве, с паузой 30+ минут, никаких повторных подключений в тестах."
# Это НЕ просто дисциплина оператора -- закреплено в коде как жёсткий
# гард, персистентный НА ДИСКЕ ХОСТА (не в эфемерном /tmp GH Actions
# раннера), так что даже отдельный, новый процесс/workflow-прогон не
# может подключиться раньше времени по ошибке/забывчивости.
FEED_RECONNECT_COOLDOWN_S = 1800.0  # 30 минут -- нижняя граница, не гарантированно достаточная
FEED_STATE_FILE_DEFAULT = Path(os.environ.get(
    "TASK5_FEED_STATE_FILE", "/home/bot/data/task5_feed_connect_state.json"
))


def seconds_until_feed_connect_allowed(state_file: Path = FEED_STATE_FILE_DEFAULT,
                                        cooldown_s: float = FEED_RECONNECT_COOLDOWN_S) -> float:
    """0.0, если подключаться можно прямо сейчас; иначе -- сколько секунд
    ещё ждать. НЕ записывает попытку сама -- см. record_feed_connect_attempt()
    (раздельно, чтобы можно было проверить не трогая состояние)."""
    if not state_file.exists():
        return 0.0
    try:
        last = json.loads(state_file.read_text()).get("last_connect_attempt_at")
    except Exception:
        return 0.0
    if last is None:
        return 0.0
    return max(0.0, cooldown_s - (time.time() - last))


def record_feed_connect_attempt(state_file: Path = FEED_STATE_FILE_DEFAULT) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_connect_attempt_at": time.time()}))


def require_feed_connect_allowed_now(state_file: Path = FEED_STATE_FILE_DEFAULT,
                                      cooldown_s: float = FEED_RECONNECT_COOLDOWN_S) -> None:
    """Для ОДНОРАЗОВЫХ диагностических скриптов (не живого бота) --
    владелец: "никаких повторных подключений в тестах". Останавливается
    (raise), НЕ ждёт молча -- одноразовый GH Actions job всё равно короче
    типичного cooldown, ждать внутри него бессмысленно, честнее отказать
    сразу с ясной причиной."""
    wait_s = seconds_until_feed_connect_allowed(state_file, cooldown_s)
    if wait_s > 0:
        raise RuntimeError(
            f"Feed reconnect cooldown активен -- последняя попытка подключения была "
            f"{cooldown_s - wait_s:.0f}с назад, нужно ещё {wait_s:.0f}с. Реальные повторные "
            f"HTTP 403 в этой сессии подтвердили cooldown по IP -- см. docs/PROJECT_STATE.md. "
            f"НЕ повторять эту попытку раньше."
        )
    record_feed_connect_attempt(state_file)

# Владелец, 2026-09-13: HTTP 403 на подключениях к фиду (Ohio дважды, NL
# один раз сразу после успешного) -- версия "rate-limit по IP" не
# объясняет NL 403 через 4 минуты после реального успеха с того же IP.
# Более вероятная гипотеза -- Cloudflare режет по отпечатку WS-клиента
# (голый `websockets` без браузерных заголовков), не по частоте.
# Браузерные заголовки -- реальный текущий Chrome UA (не выдуманный,
# актуальная стабильная версия на момент сессии), Origin -- домен
# продукта (chain.robinhood.com, не сам feed-хост -- так антибот обычно
# проверяет CORS-подобный Origin), Accept-Language -- типичный браузерный
# набор. ОДНА проверка (не серия) -- см. task5_bot_feed_structure_probe.py.
BROWSER_LIKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Origin": "https://chain.robinhood.com",
    "Accept-Language": "en-US,en;q=0.9",
}


def connect_with_headers(feed_url: str, headers: dict | None, **kwargs):
    """Обёртка над websockets.connect -- ОБЫЧНАЯ (не async) функция,
    как и сам websockets.connect (реальная сетевая работа происходит
    позже, на __aenter__/await самого возвращённого объекта, не здесь).

    ВАЖНО, реально проверено (не предположено): у websockets 17.x есть
    ОТДЕЛЬНЫЙ параметр `user_agent_header` (по умолчанию сам библиотека
    подставляет строку вида "Python/3.11 websockets/17.1" -- то самое
    "голое" значение, которое и могло триггерить блок по отпечатку) --
    он НЕ перекрывается простой передачей "User-Agent" в
    `additional_headers`, это два разных механизма. Поэтому "User-Agent"
    из `headers` вынимается и передаётся именно через `user_agent_header`
    (обнуляет библиотечное значение), остальные поля -- через
    `additional_headers`. Разные версии библиотеки называют этот параметр
    по-разному (`additional_headers` в новом asyncio-клиенте websockets
    13+, `extra_headers` в более старых) -- пробуем оба."""
    if not headers:
        return websockets.connect(feed_url, **kwargs)
    remaining = dict(headers)
    user_agent = remaining.pop("User-Agent", None)
    try:
        if user_agent is not None:
            return websockets.connect(feed_url, additional_headers=remaining, user_agent_header=user_agent, **kwargs)
        return websockets.connect(feed_url, additional_headers=remaining, **kwargs)
    except TypeError:
        # старая версия библиотеки без user_agent_header/additional_headers --
        # честный fallback: User-Agent просто в общий словарь extra_headers,
        # библиотечное значение может остаться (не гарантируем на старых версиях).
        if user_agent is not None:
            remaining = {**remaining, "User-Agent": user_agent}
        return websockets.connect(feed_url, extra_headers=remaining, **kwargs)


@dataclass
class FeedMessage:
    t_wall: float
    sequence_number: int
    raw_l2_msg_hex: str | None  # None, если не удалось извлечь -- честно, не подделываем
    sequencer_timestamp: int | None = None  # message.message.header.timestamp -- unix-секунды
    # (ЦЕЛЫЕ секунды, не мс -- реальная разведка структуры сообщения показала именно
    # такую гранулярность, см. task5_bot_feed_structure_probe_result.json), None если
    # заголовок отсутствует/не распознан -- не подставляем 0/угаданное значение.


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def _is_loopback_feed_url(feed_url: str) -> bool:
    """Владелец, 2026-09-12: наш собственный feed relay (Ohio,
    ws://127.0.0.1:9642) -- НЕ публичный Cloudflare-хост, cooldown-гард
    существует ИМЕННО для защиты публичного upstream'а от частых
    переподключений, не для локального сервиса на этой же машине.
    Подключение к loopback можно реконнектить без ограничений (владелец:
    "без ограничений на переподключения бота")."""
    try:
        host = urllib.parse.urlparse(feed_url).hostname
    except Exception:
        return False
    return host in _LOOPBACK_HOSTS


class SequencerFeedClient:
    def __init__(self, feed_url: str, headers: dict | None = BROWSER_LIKE_HEADERS,
                 state_file: Path = FEED_STATE_FILE_DEFAULT, cooldown_s: float = FEED_RECONNECT_COOLDOWN_S) -> None:
        self.feed_url = feed_url
        self.headers = headers
        self.state_file = state_file
        self.cooldown_s = cooldown_s
        self.is_loopback = _is_loopback_feed_url(feed_url)
        self.diag: dict = {"n_messages_total": 0, "n_with_seq": 0, "n_unparsed": 0, "n_confirmation_only": 0,
                            "n_connect_attempts": 0, "n_reconnects": 0}

    async def listen(self, on_message):
        """Владелец, 2026-09-13: "одно соединение при старте, реконнект
        только при обрыве, с паузой 30+ минут" -- реализовано как внешний
        цикл: КАЖДАЯ попытка подключения (включая самую первую) проверяет
        cooldown и ЖДЁТ (не отказывает -- это живой бот, не одноразовый
        скрипт) остаток паузы, если предыдущая попытка была недавно.
        on_message(FeedMessage) вызывается синхронно для каждого реального
        сообщения фида с sequenceNumber -- вызывающий код (детектор) должен
        быть быстрым, это горячий путь без сети.

        Владелец, 2026-09-12: для loopback (собственный feed relay) --
        cooldown НЕ применяется вообще, это локальный сервис, не
        публичный Cloudflare-хост."""
        while True:
            if not self.is_loopback:
                wait_s = seconds_until_feed_connect_allowed(self.state_file, self.cooldown_s)
                if wait_s > 0:
                    print(f"[feed] cooldown активен -- жду {wait_s:.0f}с перед подключением")
                    await asyncio.sleep(wait_s)
                record_feed_connect_attempt(self.state_file)
            self.diag["n_connect_attempts"] += 1
            try:
                # max_size=None -- владелец, 2026-09-12: реальный relay прислал сообщение
                # 3.57МБ, дефолтный лимит websockets (1МиБ) разорвал соединение ДО того,
                # как _consume() вообще увидел сообщение (n_messages_total осталось 0) --
                # реальный факт из первого прогона relay, не предположение. Нитро-батчи
                # (много L2-транзакций в одном сообщении фида) законно большие.
                async with connect_with_headers(self.feed_url, self.headers, open_timeout=10, close_timeout=5,
                                                 max_size=None) as ws:
                    self.diag["connected_at_wall"] = time.time()
                    await self._consume(ws, on_message)
            except websockets.exceptions.InvalidStatus as exc:
                # Владелец, 2026-09-12: "покажи полные заголовки последнего
                # 403-ответа" -- ЗА ВСЮ ИСТОРИЮ проекта это ни разу не было
                # захвачено (генерик except ниже отбрасывал structured
                # exc.response до строки). Честно фиксируем ВСЁ, что даёт
                # сама библиотека -- status_code, ПОЛНЫЕ заголовки (включая
                # cf-ray/cf-mitigated/server, если Cloudflare их вернул) и
                # тело, БЕЗ догадок о том, что "обычно" там бывает.
                resp = exc.response
                self.diag["last_disconnect_error"] = str(exc)
                self.diag["last_invalid_status_code"] = resp.status_code
                self.diag["last_invalid_status_headers"] = dict(resp.headers) if resp.headers else {}
                self.diag["last_invalid_status_body"] = (
                    resp.body.decode("utf-8", errors="replace") if resp.body else None
                )
            except Exception as exc:
                self.diag["last_disconnect_error"] = str(exc)
            # Соединение закрылось (штатно или с ошибкой) -- это и есть "обрыв". Реконнект
            # -- на следующей итерации цикла, но ТОЛЬКО после того же cooldown (проверка
            # в начале цикла), не сразу -- владелец: "с паузой 30+ минут".
            self.diag["n_reconnects"] += 1
            print(f"[feed] соединение прервано ({self.diag.get('last_disconnect_error', 'штатное закрытие')}) -- "
                  f"реконнект не раньше чем через {self.cooldown_s:.0f}с")

    async def _consume(self, ws, on_message) -> None:
        async for raw in ws:
            t_wall = time.time()
            self.diag["n_messages_total"] += 1
            try:
                payload = json.loads(raw)
            except Exception:
                self.diag["n_unparsed"] += 1
                continue
            msgs = payload.get("messages") if isinstance(payload, dict) else None
            if not msgs:
                if "confirmedSequenceNumberMessage" in (payload or {}):
                    self.diag["n_confirmation_only"] += 1
                continue
            for m in msgs:
                seq = m.get("sequenceNumber")
                if seq is None:
                    continue
                self.diag["n_with_seq"] += 1
                l2_msg_hex = self._extract_l2_msg_hex(m)
                seq_ts = self._extract_sequencer_timestamp(m)
                on_message(FeedMessage(t_wall=t_wall, sequence_number=seq, raw_l2_msg_hex=l2_msg_hex,
                                        sequencer_timestamp=seq_ts))

    @staticmethod
    def _extract_l2_msg_hex(m: dict) -> str | None:
        """Реальная форма поля `message.message.l2Msg` НЕ подтверждена
        байт-в-байт на живом фиде этой цепи (см. docstring модуля) --
        пробуем несколько правдоподобных путей защитно, как
        _peek_result_metadata в dune_client.py делает для Dune, а не
        гадаем один вариант вслепую."""
        candidates = [
            m.get("message", {}).get("message", {}).get("l2Msg") if isinstance(m.get("message"), dict) else None,
            m.get("l2Msg"),
        ]
        for c in candidates:
            if isinstance(c, str) and c:
                return c
        return None

    @staticmethod
    def _extract_sequencer_timestamp(m: dict) -> int | None:
        """`message.message.header.timestamp` -- РЕАЛЬНО подтверждено на
        живых сэмплах (task5_bot_feed_structure_probe_result.json,
        header={"kind":3,...,"timestamp":1789176677,...}) -- unix-секунды,
        не мс. Владелец, 2026-09-12: нужен для замера задержки чтения
        (таймстемп секвенсера против локального времени получения)."""
        header = None
        msg_msg = m.get("message")
        if isinstance(msg_msg, dict):
            inner = msg_msg.get("message")
            if isinstance(inner, dict):
                header = inner.get("header")
        if not isinstance(header, dict):
            return None
        ts = header.get("timestamp")
        return int(ts) if isinstance(ts, (int, float)) else None


def decode_l2_message(l2_msg_field: str) -> list[dict]:
    """Владелец, 2026-09-13: реальная разведка живого фида
    (`analysis/task5_bot_feed_structure_probe.py`, результат --
    `data/p3_guard_cache/task5_bot_feed_structure_probe_result.json`,
    3 реальных сообщения) нашла ДВЕ реальные причины прошлого 100%-ного
    провала декодера (2286/2286 `decode_returned_none`) -- **JSON-путь
    `message.message.l2Msg` был угадан ПРАВИЛЬНО с самого начала**,
    проблема была ниже:

    1. **`l2Msg` -- Base64, НЕ hex.** Старый код звал `bytes.fromhex(...)`
       на base64-строке -- гарантированный `ValueError` на КАЖДОМ реальном
       сообщении (base64-алфавит содержит символы вне `0-9a-f`).
    2. **Тип сообщения -- 3 (`L2MessageType_batch`), не 4.** Один элемент
       фида (= один L2-блок, `sequenceNumber`) несёт ЦЕЛЫЙ БЛОК
       транзакций ОДНИМ batch-сообщением: байт типа `0x03`, дальше
       повторяющиеся записи `[uint64 big-endian длина][под-сообщение]`
       до конца буфера. Реально пойманные под-сообщения (3 сэмпла,
       десятки под-сообщений) -- ВСЕ типа `4` (`L2MessageType_signedTx`),
       декодируются тем же RLP-путём, что и раньше (эта часть кода была
       верна с самого начала, просто до неё не доходило).

    Возвращает СПИСОК декодированных под-сообщений (обычно много -- один
    L2-блок содержит много транзакций, не одну): каждый элемент --
    `{"to":.., "data":.., "msg_type":.., "tx_kind":..}` (успех) |
    `{"unhandled_msg_type":..}` (под-сообщение не типа 4 -- честно, не
    гадаем) | `{"decode_error":..}`. Пустой список -- ничего не удалось
    декодировать вообще (base64 невалиден и т.п.), не путать с "0
    транзакций в блоке" (реально не наблюдалось ни разу за 3 сэмпла)."""
    raw = _decode_l2_msg_bytes(l2_msg_field)
    if raw is None:
        return []
    return _decode_message_bytes(raw)


def _decode_l2_msg_bytes(l2_msg_field: str) -> bytes | None:
    """Base64 -- реальный, подтверждённый формат (см. докстринг выше).
    Hex -- fallback НА СЛУЧАЙ будущего изменения формата бродкастер-
    протокола -- пробуем оба честно, не считаем один единственно
    возможным навсегда."""
    import base64
    try:
        return base64.b64decode(l2_msg_field, validate=True)
    except Exception:
        pass
    try:
        return bytes.fromhex(l2_msg_field[2:] if l2_msg_field.startswith("0x") else l2_msg_field)
    except ValueError:
        return None


def _decode_message_bytes(raw: bytes, _depth: int = 0) -> list[dict]:
    if not raw:
        return []
    msg_type = raw[0]
    if msg_type == 3:  # L2MessageType_batch -- рекурсивно разворачиваем под-сообщения
        if _depth > 4:  # честная защита от аномальной вложенности -- реально не наблюдалась
            return [{"decode_error": "batch nesting too deep (>4), aborting"}]
        out: list[dict] = []
        pos = 1
        while pos + 8 <= len(raw):
            size = int.from_bytes(raw[pos:pos + 8], "big")
            pos += 8
            if size < 0 or pos + size > len(raw):
                break  # честно останавливаемся на первом несогласованном размере, не гадаем остаток буфера
            out.extend(_decode_message_bytes(raw[pos:pos + size], _depth=_depth + 1))
            pos += size
        return out
    if msg_type != 4:
        return [{"unhandled_msg_type": msg_type}]
    return [_decode_signed_tx(raw[1:], msg_type)]


def _decode_signed_tx(tx_bytes: bytes, msg_type: int) -> dict:
    """RLP-декодер сигнатуры signedTx -- НЕ изменился, эта часть была
    верна с самого начала (см. докстринг `decode_l2_message`). Payload --
    сама подписанная транзакция ровно как отправлена в сеть (EIP-2718):
    первый байт >= 0xc0 -- legacy RLP-список напрямую (9 полей: nonce,
    gasPrice, gasLimit, to, value, data, v, r, s); первый байт в
    диапазоне типов (0x01-0x7f) -- typed-транзакция (для 0x02 EIP-1559:
    chainId, nonce, maxPriorityFeePerGas, maxFeePerGas, gasLimit, to,
    value, data, accessList, yParity, r, s -- to/data по индексам 5/7)."""
    if not tx_bytes:
        return {"decode_error": "empty tx_bytes after type-4 prefix"}
    try:
        import rlp
        first_byte = tx_bytes[0]
        if first_byte >= 0xC0:
            decoded = rlp.decode(tx_bytes)
            if len(decoded) < 6:
                return {"decode_error": "legacy tx: too few RLP fields"}
            to_field, data_field = decoded[3], decoded[5]
            tx_kind = "legacy"
        elif first_byte in (0x01, 0x02, 0x03):
            decoded = rlp.decode(tx_bytes[1:])
            if first_byte == 0x02:  # EIP-1559
                if len(decoded) < 8:
                    return {"decode_error": "0x02 tx: too few RLP fields"}
                to_field, data_field = decoded[5], decoded[7]
            elif first_byte == 0x01:  # EIP-2930
                if len(decoded) < 7:
                    return {"decode_error": "0x01 tx: too few RLP fields"}
                to_field, data_field = decoded[3], decoded[5]
            else:  # 0x03 EIP-4844 -- редкий случай, поля те же смещения, что 1559 + doc не гарантирована
                if len(decoded) < 8:
                    return {"decode_error": "0x03 tx: too few RLP fields"}
                to_field, data_field = decoded[5], decoded[7]
            tx_kind = f"typed_0x{first_byte:02x}"
        else:
            return {"unrecognized_first_byte": first_byte}

        to_hex = "0x" + to_field.hex() if isinstance(to_field, bytes) and to_field else None
        data_hex = "0x" + data_field.hex() if isinstance(data_field, bytes) else "0x"
        return {"to": to_hex, "data": data_hex, "msg_type": msg_type, "tx_kind": tx_kind}
    except Exception as exc:
        return {"decode_error": str(exc)}
