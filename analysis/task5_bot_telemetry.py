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
    revert_reason: str | None = None  # "price_moved" | "slippage" | "execution_error" | "unknown" -- владелец,
    # 2026-09-12: "основа решения про сервер через 2-3 дня" -- см. classify_revert_reason() в task5_bot_executor.py
    event: str = "detection"  # "detection" | "inclusion_update"


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
                                revert_reason: str | None = None, pnl_usd: float | None = None) -> None:
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
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
