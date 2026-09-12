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
        # Владелец, 2026-09-13: task5_bot_sender.py -- владелец написал и
        # закоммитил сам (подпись/отправка/nonce/стоп-условия), эта сессия
        # только подключает его сюда (calldata + вызов), не пишет сама
        # логику подписи/отправки -- см. PROJECT_STATE.md. Sender создаётся
        # ЛЕНИВО (только при первой реальной попытке, не в __init__) --
        # тот же принцип, что get_private_key() ниже: без --confirm-mainnet
        # ни это, ни PRIVATE_KEY_NOX вообще не читаются.
        self._sender = None
        # Обновляется task5_bot_run.py на каждом сообщении фида (sequenceNumber
        # == номер блока) -- нужно ТОЛЬКО для block_before_send в телеметрии
        # реальной попытки (см. docs/TASK5_WRITEPATH_CLEAN_SPEC.md).
        self.last_seen_block_number: int | None = None
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
            # Владелец, 2026-09-12 ('добавка', Путь А): `opp.trigger_kind` различает
            # "divergence" (Swap-цена уже в реестре) от "router_calldata" (декодер
            # роутера, до исполнения) -- честнее, чем всегда "unknown".
            catalyst_type=opp.trigger_kind,
            mode="live" if self.confirm_mainnet else "dry_run",
            size_usd=size_usd,
        )

        if not self.confirm_mainnet:
            record.result = "would_enter"
            self.telemetry.write(record)
            if opp.trigger_kind == "router_calldata":
                print(f"[dry-run][Путь А] вот здесь я бы вошёл: router={opp.router_to} "
                      f"function={opp.router_function_label} touched_pool={opp.touched_pool} "
                      f"zeroForOne={opp.touched_zero_for_one} amount_in~{opp.touched_amount_in_human:.6f} "
                      f"(~${opp.touched_amount_in_usd_approx:.2f}) pool_a={opp.pool_a} pool_b={opp.pool_b}")
            else:
                print(f"[dry-run] вот здесь я бы вошёл: pool_a={opp.pool_a} pool_b={opp.pool_b} "
                      f"ожидаемый_захват=${opp.expected_capture_after_gas_and_reverts_usd:.2f} "
                      f"возраст_расхождения={opp.divergence_age_blocks} блоков")
            return

        # Реальная отправка -- ТОЛЬКО после --confirm-mainnet.
        try:
            tx_hash = self._build_sign_send(opp, size_usd, attempt_id)
            record.result = "sent"
            record.tx_hash = tx_hash
        except Exception as exc:
            record.result = "error"
            record.error = str(exc)
        self.telemetry.write(record)

    def _build_sign_send(self, opp: Opportunity, size_usd: float, attempt_id: str) -> str:
        """Владелец, 2026-09-13: `analysis/task5_bot_sender.py` написан и
        закоммичен владельцем самим (подпись/`eth_sendRawTransaction`/nonce/
        стоп-условия -- вся сигнинг-логика, эта сессия её НЕ писала и не
        коммитила). Здесь -- только сборка calldata (ABI-энкодинг
        `CycleParams`, ноль подписи) и вызов уже готового `Sender.send_cycle()`,
        плюс запись богатой телеметрии по реальному исходу (см.
        docs/TASK5_BOT_EXECUTOR_SPEC.md, шаги 1-2 -- та же логика, что там
        описана текстом, теперь реальный код)."""
        from eth_abi import encode

        from task5_bot_config import USDG, WETH, WETH_DECIMALS, USDG_DECIMALS, WETH_USDG_POOL
        from task5_bot_route_precompute import fill_cycle_params

        if self.route_table is None or self.registry is None:
            raise RuntimeError("route_table/registry не переданы в Executor -- нужны для сборки CycleParams")

        skeleton = self.route_table.get(opp.pool_a, opp.pool_b)
        if skeleton is None:
            raise RuntimeError(f"нет предрасчитанного маршрута для {opp.pool_a}/{opp.pool_b}")

        cheap = self.registry.by_address[opp.pool_a.lower()]
        expensive = self.registry.by_address[opp.pool_b.lower()]
        price_usd = self._current_weth_usd_price(WETH_USDG_POOL, WETH, WETH_DECIMALS, USDG_DECIMALS)
        notional_wei = int(size_usd / max(price_usd, 1e-9) * 1e18)

        params = fill_cycle_params(skeleton, cheap, expensive, notional_wei,
                                    opp.expected_capture_after_gas_and_reverts_usd, price_usd)
        encoded = encode(
            ["(address,address,bool,int256,address,uint256,uint160,uint160)"],
            [(params["poolA"], params["poolB"], params["zeroForOneA"], params["amountSpecifiedA"],
              params["exitToken"], params["minProfit"], params["sqrtPriceLimitA"], params["sqrtPriceLimitB"])],
        )
        calldata = bytes.fromhex(params["execute_cycle_selector"][2:]) + encoded

        if self._sender is None:
            from task5_bot_sender import Sender  # импортируется здесь, лениво -- см. __init__
            self._sender = Sender()

        # Газ -- НЕ выдумываем фиксированное число (контракт не аудирован,
        # реальный расход не измерен, см. docs/TASK5_BOT_EXECUTOR_SPEC.md) --
        # eth_estimateGas на лету + запас 20%, честная оценка, не константа.
        gas_est = self._sender.rpc.eth.estimate_gas({
            "to": self.contract_address, "data": "0x" + calldata.hex(), "from": self._sender.address,
        })
        gas_limit = int(gas_est * 1.2)

        block_before_send = self.last_seen_block_number
        result = self._sender.send_cycle(self.contract_address, calldata, gas_limit=gas_limit)

        self.telemetry.write_inclusion_update(
            attempt_id=attempt_id,
            inclusion_sequence_number=result.block_number,
            result="success" if result.ok else ("reverted" if result.status == 0 else "error"),
            tx_hash=result.tx_hash,
            error=result.error or None,
            revert_reason=result.revert_reason or None,
            submit_endpoint=result.submit_endpoint or None,
            block_before_send=block_before_send,
        )
        if not result.ok:
            raise RuntimeError(
                f"send_cycle: endpoint={result.submit_endpoint} status={result.status} "
                f"revert_reason={result.revert_reason} error={result.error}"
            )
        return result.tx_hash

    def _current_weth_usd_price(self, weth_usdg_pool: str, weth: str, weth_decimals: int, usdg_decimals: int) -> float:
        """Реальная цена WETH в USDG(~USD) из референсного пула, уже в
        реестре (тот же пул, что используется во всех измерениях этой
        сессии) -- НЕ выдумывается: если пул ещё без цены, падаем явно,
        не подставляем заглушку."""
        if self.registry is None:
            raise RuntimeError("registry не передан -- нужен для цены WETH/USDG")
        ref_pool = self.registry.by_address.get(weth_usdg_pool.lower())
        if ref_pool is None or ref_pool.sqrt_price_x96 is None:
            raise RuntimeError("референсный пул WETH_USDG_POOL без цены -- нельзя оценить размер позиции")
        raw = ref_pool.implied_price_token1_per_token0()
        if ref_pool.token0.lower() == weth.lower():
            return raw * (10 ** (weth_decimals - usdg_decimals))
        return (1.0 / raw) * (10 ** (weth_decimals - usdg_decimals)) if raw else 0.0


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
    "0xb95380e9": "RepayShortfall",       # RepayShortfall(uint256,uint256) -- ревизия владельца
    # 2026-09-12 после код-ревью (фикс UnexpectedCallback на внутреннем свопе pool B и
    # перепутанного направления zeroForOne/долга). Селектор перепроверен независимо
    # (keccak256("RepayShortfall(uint256,uint256)")[:4] = 0xb95380e9).
}
_KNOWN_CONTRACT_ERRORS = {
    "InsufficientProfit": "price_moved_or_slippage",  # минимальная прибыль не набралась -- цена
    # успела сдвинуться между детекцией и включением, либо оценка minProfit была завышена
    "UnexpectedCallback": "execution_error",           # колбэк пришёл не от ожидаемого pool A/B -- баг
    # в адресации пула, не рыночное явление
    "ReentrantCall": "execution_error",                # inCycle уже true -- параллельная попытка
    "NotOwner": "execution_error",                     # msg.sender != owner -- баг в подписанте/адресе
    "RepayShortfall": "price_moved_or_slippage",        # закрывающий своп в pool B не вернул
    # достаточно для погашения долга перед pool A -- то же явление, что InsufficientProfit, но
    # на более раннем шаге (не хватило даже на возврат долга, не только на профит). Заменяет
    # прежний require-string "insufficient funds to repay poolA" из добаговой версии контракта.
}
_KNOWN_REQUIRE_STRINGS: dict[str, str] = {
    # Прежний require-string "insufficient funds to repay poolA" (добаговая версия
    # контракта) заменён явным custom error RepayShortfall(uint256,uint256) -- см.
    # CUSTOM_ERROR_SELECTORS/_KNOWN_CONTRACT_ERRORS выше, здесь больше не встречается.
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
