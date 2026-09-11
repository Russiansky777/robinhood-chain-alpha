#!/usr/bin/env python3
"""Задача 5, живой бот -- телеметрия. Владелец, 2026-09-11/12:
"Телеметрия -- обязательная часть, не довесок: по каждой попытке --
блок обнаружения, блок включения, результат, размер, был ли катализатор
и какой. Это ответит на вопрос про «none» без Dune."

Каждая строка JSONL -- одна попытка (в т.ч. dry-run), НИКОГДА не
перезаписывается задним числом -- если появляется блок включения позже
(подтверждение через фид), пишется ОТДЕЛЬНАЯ строка-обновление с тем же
attempt_id, а не мутация предыдущей (append-only, тот же принцип, что
p5_fee_accrual.jsonl/live_divergence.jsonl в этом проекте)."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from task5_bot_config import TELEMETRY_JSONL_PATH


@dataclass
class AttemptRecord:
    attempt_id: str
    ts_wall: float
    detection_sequence_number: int  # "блок обнаружения" -- sequenceNumber фида
    pool_a: str
    pool_b: str
    expected_capture_usd: float
    expected_capture_after_gas_and_reverts_usd: float
    divergence_age_blocks: int
    catalyst_sequence_number: int | None
    catalyst_type: str  # "large_swap" | "unknown" | "none" -- честно, не гадаем без сигнала
    mode: str  # "dry_run" | "live"
    size_usd: float
    inclusion_sequence_number: int | None = None  # заполняется отдельной строкой-обновлением
    result: str = "pending"  # "would_enter" (dry-run) | "sent" | "success" | "reverted" | "halted" | "error"
    tx_hash: str | None = None
    error: str | None = None
    revert_reason: str | None = None  # "price_moved_or_slippage" | "execution_error" | "network_error" |
    # "unknown" -- владелец, 2026-09-12: "основа решения про сервер через 2-3 дня" -- см.
    # classify_revert_reason() в task5_bot_executor.py (категории и обоснование там же)
    event: str = "detection"  # "detection" | "inclusion_update"
    submit_endpoint: str | None = None  # "rpc_mainnet" | "sequencer_mainnet_guess" -- владелец, 2026-09-13:
    # третий VPS (Огайо) целится в sequencer.mainnet напрямую -- см. SEQUENCER_SUBMIT_URL_MAINNET,
    # task5_bot_config.py; поле пишется executor'ом при реальной отправке, None в dry-run
    block_before_send: int | None = None  # последний известный на фиде номер блока непосредственно перед
    # отправкой -- та же метрика, что docs/TASK5_WRITEPATH_CLEAN_SPEC.md ("блок на момент отправки")
    block_number_included: int | None = None  # заполняется строкой-обновлением, из рецепта (единственный
    # RPC-вызов ПОСЛЕ отправки -- см. спецификацию, "нужен факт, не момент")
    blocks_to_inclusion: int | None = None  # block_number_included - block_before_send, ЦЕЛОЕ число блоков,
    # без интерполяции -- прямой аналог метрики из task5_writepath_block_recompute.py, но без её оговорок
    # (там она оценивалась по секундным block_timestamp постфактум, здесь -- известна напрямую с фида)


class TelemetryLog:
    def __init__(self, path: str = TELEMETRY_JSONL_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def new_attempt_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def write(self, record: AttemptRecord) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def write_inclusion_update(self, attempt_id: str, inclusion_sequence_number: int, result: str,
                                tx_hash: str | None = None, error: str | None = None,
                                revert_reason: str | None = None, pnl_usd: float | None = None,
                                submit_endpoint: str | None = None, block_before_send: int | None = None) -> None:
        # sequenceNumber фида == номер L2-блока на этой цепи (см.
        # docs/TASK5_WRITEPATH_CLEAN_SPEC.md, честная оговорка про источник
        # этого утверждения) -- inclusion_sequence_number и есть
        # block_number_included, blocks_to_inclusion считается здесь же, не
        # оценивается постфактум интерполяцией (в отличие от
        # task5_writepath_block_recompute.py, у которого этого поля не было).
        blocks_to_inclusion = (
            inclusion_sequence_number - block_before_send if block_before_send is not None else None
        )
        rec = {
            "attempt_id": attempt_id,
            "ts_wall": time.time(),
            "event": "inclusion_update",
            "inclusion_sequence_number": inclusion_sequence_number,
            "result": result,
            "tx_hash": tx_hash,
            "error": error,
            "revert_reason": revert_reason,
            "pnl_usd": pnl_usd,
            "submit_endpoint": submit_endpoint,
            "block_before_send": block_before_send,
            "block_number_included": inclusion_sequence_number,
            "blocks_to_inclusion": blocks_to_inclusion,
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
