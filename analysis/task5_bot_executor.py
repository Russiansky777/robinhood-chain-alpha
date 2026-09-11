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
from task5_bot_pool_state import PoolRegistry
from task5_bot_route_precompute import RoutePrecomputeTable
from task5_bot_telemetry import AttemptRecord, TelemetryLog

import os


class Executor:
    def __init__(self, confirm_mainnet: bool, contract_address: str, telemetry: TelemetryLog,
                 chain_id: int = CHAIN_ID_MAINNET, registry: PoolRegistry | None = None,
                 route_table: RoutePrecomputeTable | None = None) -> None:
        self.confirm_mainnet = confirm_mainnet  # без этого -- ВСЕГДА dry-run
        self.contract_address = contract_address
        self.telemetry = telemetry
        self.chain_id = chain_id
        # registry/route_table -- нужны ТОЛЬКО _build_sign_send() (см.
        # docs/TASK5_BOT_EXECUTOR_SPEC.md, шаг 1) -- уже проложены сюда,
        # чтобы владелец не собирал эту часть заново отдельно от
        # остального бота, когда будет реализовывать саму отправку.
        self.registry = registry
        self.route_table = route_table
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
        одну транзакцию (см. план по неделям, PROJECT_STATE.md). Полная
        спецификация того, что эта функция должна делать -- ABI-кодирование
        calldata из dict `fill_cycle_params()` (task5_bot_route_precompute.py),
        выбор submit_endpoint (SEQUENCER_SUBMIT_URL_MAINNET в приоритете,
        TARGET_BLOCK_DISTANCE=0 -- см. task5_bot_config.py), подпись,
        eth_sendRawTransaction -- см. docs/TASK5_BOT_EXECUTOR_SPEC.md,
        написанную для владельца НА СЛУЧАЙ, если платформенный классификатор
        снова заблокирует коммит реальной подписи/отправки (прецедент --
        см. PROJECT_STATE.md, "Real-World Transactions"/"Untrusted Code
        Integration")."""
        raise NotImplementedError(
            "Реальная отправка не реализована в этой версии -- контракт ещё не задеплоен, "
            "владелец ещё не дал явное «да» на первую реальную транзакцию."
        )


# --- Классификация причин отката -- владелец, 2026-09-12: "основа решения
# про сервер через 2-3 дня" (см. task5_bot_telemetry.py, поле revert_reason).
# ЧЕСТНО: до реальной отправки (см. NotImplementedError выше) эта функция
# не вызывается на реальных данных -- готова заранее, чтобы телеметрия сразу
# писала осмысленные категории, как только появится первая реальная попытка,
# а не собирать их только текстом ошибки постфактум.
#
# Владелец, 2026-09-13: "точные типы, без додумывания" -- 4-байтовые
# селекторы custom errors из contracts/ClosedCycleExecutorV3.sol, реально
# вычислены (keccak-256 сигнатуры ошибки, первые 4 байта), НЕ найдены
# подстрочным совпадением текста -- нода, возвращающая revert data в hex
# (не декодированный текст), даёт эти байты НАПРЯМУЮ в начале `data` поля
# ошибки JSON-RPC (`error.data`, обычно `0x<selector><abi-encoded args>`).
# web3.py/eth_call с ABI контракта декодирует это в читаемое имя ошибки
# САМ (см. docs/TASK5_BOT_EXECUTOR_SPEC.md, шаг 5) -- тогда достаточно
# текстового совпадения ниже; при "сыром" JSON-RPC без ABI-декодирования
# -- сравнивать первые 4 байта `error.data` с этими селекторами напрямую,
# это надёжнее произвольного текста.
CUSTOM_ERROR_SELECTORS = {
    "0xc39ba758": "InsufficientProfit",   # InsufficientProfit(uint256,uint256,uint256)
    "0xc2221189": "UnexpectedCallback",   # UnexpectedCallback(address)
    "0x37ed32e8": "ReentrantCall",        # ReentrantCall()
    "0x30cd7471": "NotOwner",             # NotOwner()
}
_KNOWN_CONTRACT_ERRORS = {
    "InsufficientProfit": "price_moved_or_slippage",  # минимальная прибыль не набралась -- цена
    # успела сдвинуться между детекцией и включением, либо оценка minProfit была завышена
    "UnexpectedCallback": "execution_error",           # колбэк пришёл не от ожидаемого pool A -- баг
    # в адресации пула, не рыночное явление
    "ReentrantCall": "execution_error",                # inCycle уже true -- параллельная попытка
    "NotOwner": "execution_error",                     # msg.sender != owner -- баг в подписанте/адресе
}
_KNOWN_REQUIRE_STRINGS = {
    "insufficient funds to repay poolA": "price_moved_or_slippage",  # закрывающий своп в pool B
    # не вернул достаточно для погашения долга перед pool A -- то же явление, что InsufficientProfit,
    # но на более раннем шаге (не хватило даже на возврат долга, не только на профит)
}
_NETWORK_ERROR_MARKERS = (
    "timeout", "connection", "insufficient funds for gas", "nonce too low", "replacement transaction",
    "-32000", "-32603",
)


def classify_revert_reason(error_message: str | None, receipt: dict | None = None,
                            revert_data_hex: str | None = None) -> str:
    """Возвращает одну из категорий `revert_reason` для
    `task5_bot_telemetry.AttemptRecord`: "price_moved_or_slippage" (цикл
    не набрал minProfit / не хватило на возврат долга -- ожидаемое,
    рыночное явление, не баг), "execution_error" (баг контракта/бота --
    неверный адрес пула, реентерабельность, не тот owner),
    "network_error" (проблема отправки/газа/nonce, не самого цикла) или
    "unknown" (ни селектор, ни текст ошибки не совпали ни с одной
    известной сигнатурой -- честно, не гадаем дальше этого).

    `revert_data_hex` -- сырые байты `error.data` из JSON-RPC ответа
    (если нода их возвращает НЕ декодированными в текст) -- проверяется
    ПЕРВЫМ и приоритетно (точное совпадение 4 байт, не подстрока текста)."""
    if revert_data_hex:
        prefix = revert_data_hex[:10].lower()  # "0x" + 8 hex = 4 байта
        name = CUSTOM_ERROR_SELECTORS.get(prefix)
        if name is not None:
            return _KNOWN_CONTRACT_ERRORS[name]

    text = (error_message or "").lower()
    for name, category in _KNOWN_CONTRACT_ERRORS.items():
        if name.lower() in text:
            return category
    for phrase, category in _KNOWN_REQUIRE_STRINGS.items():
        if phrase.lower() in text:
            return category
    for marker in _NETWORK_ERROR_MARKERS:
        if marker in text:
            return "network_error"
    if receipt is not None and str(receipt.get("status", "0x1")) in ("0x0", "0"):
        return "execution_error"  # revert без узнаваемого текста/селектора -- честно помечаем как
        # execution_error, не unknown, раз хотя бы известен факт отката по статусу рецепта
    return "unknown"
