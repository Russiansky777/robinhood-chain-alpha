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
import time
from dataclasses import dataclass

import websockets


@dataclass
class FeedMessage:
    t_wall: float
    sequence_number: int
    raw_l2_msg_hex: str | None  # None, если не удалось извлечь -- честно, не подделываем


class SequencerFeedClient:
    def __init__(self, feed_url: str) -> None:
        self.feed_url = feed_url
        self.diag: dict = {"n_messages_total": 0, "n_with_seq": 0, "n_unparsed": 0, "n_confirmation_only": 0}

    async def listen(self, on_message):
        """on_message(FeedMessage) вызывается синхронно для каждого
        реального сообщения фида с sequenceNumber -- вызывающий код
        (детектор) должен быть быстрым, это горячий путь без сети."""
        async with websockets.connect(self.feed_url, open_timeout=10, close_timeout=5) as ws:
            self.diag["connected_at_wall"] = time.time()
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
                    on_message(FeedMessage(t_wall=t_wall, sequence_number=seq, raw_l2_msg_hex=l2_msg_hex))

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
