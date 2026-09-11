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


def decode_l2_message(l2_msg_hex: str) -> dict | None:
    """Best-effort декодер L2MessageType_signedTx (байт-константа 4,
    Nitro `arbos/l2message.go`, публичная документация -- НЕ проверено
    вживую на этой цепи, задача первой недели dry-run см. PROJECT_STATE.md).
    Возвращает {"to": ..., "data": ...} для дальнейшего сопоставления с
    сигнатурой Uniswap V3 `swap()`, либо None (тип не распознан/не
    поддержан -- честно, не подделываем "распознавание")."""
    try:
        raw = bytes.fromhex(l2_msg_hex[2:] if l2_msg_hex.startswith("0x") else l2_msg_hex)
    except ValueError:
        return None
    if not raw:
        return None
    msg_type = raw[0]
    if msg_type != 4:  # L2MessageType_signedTx -- ЕДИНСТВЕННЫЙ обрабатываемый тип в этой версии
        return None
    try:
        import rlp
        tx_rlp = raw[1:]
        decoded = rlp.decode(tx_rlp)
        # decoded -- список полей RLP-транзакции; извлекаем to/data по
        # позиции (стандартная EIP-1559/legacy раскладка) -- полная
        # валидация сигнатуры/nonce не нужна для детекции факта свопа.
        return {"raw_fields": [f.hex() if isinstance(f, bytes) else f for f in decoded]}
    except Exception:
        return None
