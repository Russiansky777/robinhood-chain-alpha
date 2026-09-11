#!/usr/bin/env python3
"""Задача 5, живой бот -- подпись и отправка. Владелец, 2026-09-11/12:
"подпись и отправка -- сразу после детекции, без промежуточных проверок
через сеть" + "Секреты и ключи -- по протоколу проекта: dry-run, потом
явное «да» на первую реальную транзакцию." -- ТОТ ЖЕ протокол, что
sc1_launcher.py: без --confirm-mainnet отправка НИКОГДА не происходит,
независимо от наличия ключа в окружении."""
from __future__ import annotations

import time

from eth_account import Account

from task5_bot_config import (
    CHAIN_ID_MAINNET,
    EXPECTED_WALLET_ENV_VAR,
    get_private_key,
)
from task5_bot_detector import Opportunity
from task5_bot_telemetry import AttemptRecord, TelemetryLog

import os


class Executor:
    def __init__(self, confirm_mainnet: bool, contract_address: str, telemetry: TelemetryLog,
                 chain_id: int = CHAIN_ID_MAINNET) -> None:
        self.confirm_mainnet = confirm_mainnet  # без этого -- ВСЕГДА dry-run
        self.contract_address = contract_address
        self.telemetry = telemetry
        self.chain_id = chain_id
        self.account = None
        if confirm_mainnet:
            priv = get_private_key()
            self.account = Account.from_key(priv)
            expected = os.environ.get(EXPECTED_WALLET_ENV_VAR, "")
            if expected and self.account.address.lower() != expected.lower():
                raise RuntimeError(
                    f"Приватный ключ даёт адрес {self.account.address}, ожидался {expected} -- "
                    "СТОП, не отправляем с неожиданного адреса."
                )

    def handle_opportunity(self, opp: Opportunity, size_usd: float) -> None:
        """Горячий путь: НИКАКИХ сетевых вызовов ПЕРЕД подписью/отправкой
        (владелец: "без промежуточных проверок через сеть") -- все данные
        для транзакции уже есть в Opportunity + локальном реестре."""
        attempt_id = self.telemetry.new_attempt_id()
        record = AttemptRecord(
            attempt_id=attempt_id,
            ts_wall=time.time(),
            detection_sequence_number=opp.detection_sequence_number,
            pool_a=opp.pool_a,
            pool_b=opp.pool_b,
            expected_capture_usd=opp.expected_capture_usd,
            expected_capture_after_gas_and_reverts_usd=opp.expected_capture_after_gas_and_reverts_usd,
            divergence_age_blocks=opp.divergence_age_blocks,
            catalyst_sequence_number=opp.catalyst_sequence_number,
            catalyst_type="unknown",  # честно: этот бот-скелет пока не классифицирует тип катализатора детально
            mode="live" if self.confirm_mainnet else "dry_run",
            size_usd=size_usd,
        )

        if not self.confirm_mainnet:
            record.result = "would_enter"
            self.telemetry.write(record)
            print(f"[dry-run] вот здесь я бы вошёл: pool_a={opp.pool_a} pool_b={opp.pool_b} "
                  f"ожидаемый_захват=${opp.expected_capture_after_gas_and_reverts_usd:.2f} "
                  f"возраст_расхождения={opp.divergence_age_blocks} блоков")
            return

        # Реальная отправка -- ТОЛЬКО после --confirm-mainnet.
        try:
            tx_hash = self._build_sign_send(opp, size_usd)
            record.result = "sent"
            record.tx_hash = tx_hash
        except Exception as exc:
            record.result = "error"
            record.error = str(exc)
        self.telemetry.write(record)

    def _build_sign_send(self, opp: Opportunity, size_usd: float) -> str:
        """ЗАГЛУШКА до реального деплоя ClosedCycleExecutorV3 и калибровки
        ABI-энкодинга CycleParams -- владелец получит явный запрос на
        подтверждение ПЕРЕД тем, как эта функция реально отправит хоть
        одну транзакцию (см. план по неделям, PROJECT_STATE.md)."""
        raise NotImplementedError(
            "Реальная отправка не реализована в этой версии -- контракт ещё не задеплоен, "
            "владелец ещё не дал явное «да» на первую реальную транзакцию."
        )
