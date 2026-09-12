#!/usr/bin/env python3
"""Задача 5, живой пилот -- бюджет/лимиты/учёт попыток. НЕ логика
подписи/отправки -- чистая бухгалтерия и решение "можно ли слать"
(gate), которое ГОРЯЧИЙ ПУТЬ проверяет ПЕРЕД вызовом
task5_bot_sender.py::Sender.send_cycle.

Лимиты пилота (владелец, 2026-09-12, с поправкой):
  - отдельный кошелёк, весь инвентарь $150 ему не передавать (внешний
    по отношению к этому коду факт -- контролируется тем, СКОЛЬКО
    владелец реально перевёл на кошелёк/контракт, не проверяется
    здесь программно).
  - до $20 совокупных потерь на газ, ВКЛЮЧАЯ откаты и успешные --
    поправка от исходных $5. Промежуточные отчёты на $5 и $10 --
    чтобы не ждать исчерпания лимита, если картина ясна раньше.
  - одна неподтверждённая транзакция одновременно.
  - остановка при расходе любого нашего токена помимо газа (см.
    check_no_unexpected_token_spend), при нарушении учёта (см.
    check_accounting_consistency), при исчерпании бюджета газа.

ПРАВКА 2026-09-13 (внешнее ревью, пункт 3 -- "Учёт бюджета"):
  - "Перед отправкой резервировать максимальный расход следующей
    транзакции (gas_limit x maxFeePerGas), не только считать
    потраченное" -- см. reserve_for_send()/release_reservation().
  - "Фактический расход — из рецепта (gasUsed x effectiveGasPrice), не
    из старого eth_gasPrice" -- вызывающий код (task5_v4_hotpath.py)
    теперь читает РЕАЛЬНЫЙ рецепт и передаёт сюда уже готовый
    gas_cost_wei, эта функция сама больше не решает, ЧТО было ценой
    газа.
  - "Если курс не получен — расход всё равно записывать в ETH" --
    cumulative_gas_loss_eth обновляется ВСЕГДА, независимо от того,
    удалось ли получить курс ETH/USD в этот момент; last_known_eth_usd_price
    используется как честный fallback, если текущий курс недоступен.
  - "Ошибка чтения файла бюджета — стоп, не продолжение с нулём" --
    _load() различает "файла ещё нет" (законный чистый старт) и "файл
    есть, но не читается" (halt).
  - "Ошибка RPC после отправки — расход записать до любых других
    действий" -- обеспечивается ПОРЯДКОМ вызовов в hotpath.py
    (record_gas_spend_wei вызывается сразу после получения рецепта, ДО
    чтения балансов/сверки учёта), не этим модулем напрямую, но
    reserve_for_send/record_gas_spend_wei спроектированы как ОДИН
    дешёвый вызов без сетевых операций внутри -- не могут сами упасть
    на RPC.

ПРАВКА 2026-09-13 (внешнее ревью, пункт 4 -- "Незавершённые
транзакции"): PilotBudget.tx_in_flight и pending_tx_hash теперь ЧАСТЬ
персистентного состояния (переживают рестарт процесса) -- таймаут
ожидания рецепта САМИМ Sender'ом (RECEIPT_TIMEOUT_S=15с в
task5_bot_sender.py) НЕ снимает in_flight здесь; это делает ТОЛЬКО
получение РЕАЛЬНОГО рецепта (см. task5_v4_hotpath.py::
resolve_pending_tx_if_any/wait_for_real_receipt)."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

BUDGET_REPORT_THRESHOLDS_USD = (5.0, 10.0)
BUDGET_STOP_USD = 20.0

# Причины "не отправляем" -- ровно словарь владельца, чтобы таблица
# читалась без перевода: "нет прибыльного цикла / нет ликвидности /
# ошибка расчёта / симуляция не прошла / лимит газа".
REASON_NO_PROFITABLE_CYCLE = "нет прибыльного цикла"
REASON_NO_LIQUIDITY = "нет ликвидности"
REASON_CALC_ERROR = "ошибка расчёта"
REASON_SIMULATION_FAILED = "симуляция не прошла"
REASON_GAS_LIMIT = "лимит газа"
REASON_TX_IN_FLIGHT = "уже есть неподтверждённая транзакция"
REASON_HALTED = "пилот остановлен (см. halt_reason)"
REASON_NO_PRICE_FOR_RESERVE = "нет курса ETH/USD для безопасного резервирования бюджета"


@dataclass
class AttemptTableRow:
    """Ровно поля, которые перечислил владелец: "время, маршрут,
    размер, ожидаемая прибыль после газа, задержка от получения данных
    до отправки, хэш, результат, фактический прирост базового актива,
    газ, накопленный итог"."""
    ts_wall: float
    route_label: str
    route_id: str
    size_in_raw: int
    exit_token: str
    expected_profit_after_gas: float  # в единицах exit_token (human-readable)
    latency_recv_to_send_s: float  # от получения данных (своп-событие/блок) до отправки
    tx_hash: str | None
    result: str  # "sent_pending" | "success" | "reverted" | "send_error"
    actual_gain_base_asset: float | None  # фактический прирост exit_token, заполняется по рецепту
    gas_used: int | None
    gas_cost_native: float | None
    cumulative_gas_loss_usd: float  # накопленный итог по газу+откатам
    cumulative_net_pnl_usd: float | None = None
    # НОВОЕ (диагностика "не обрабатывай очередь устаревших состояний" --
    # владелец, 2026-09-13, доп.): блок, на котором СЧИТАЛСЯ цикл, и
    # возраст этого состояния к моменту отправки -- честно видно,
    # насколько "свежим" было решение.
    computed_at_block: int | None = None
    state_age_blocks: int | None = None


class PilotBudget:
    def __init__(self, state_path: str = "/home/bot/data/task5_v4_pilot_budget_state.json") -> None:
        self.state_path = Path(state_path)
        self.cumulative_gas_loss_usd = 0.0
        self.cumulative_gas_loss_eth = 0.0
        self.cumulative_net_pnl_usd = 0.0
        self.last_known_eth_usd_price: float | None = None
        self.reserved_wei = 0
        self.reserved_usd = 0.0
        self.reported_thresholds: set[float] = set()
        self.halted = False
        self.halt_reason: str | None = None
        self.tx_in_flight = False
        self.pending_tx_hash: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return  # законный чистый старт -- не ошибка, не halt
        try:
            data = json.loads(self.state_path.read_text())
        except Exception as exc:  # noqa: BLE001
            # ПРАВКА (внешнее ревью, пункт 3): файл ЕСТЬ, но не читается
            # -- СТОП, не продолжение с нулём (иначе можно незаметно
            # превысить реальный накопленный лимит газа).
            self.halted = True
            self.halt_reason = (f"файл состояния бюджета {self.state_path} существует, но не читается "
                                 f"({exc}) -- честный стоп, не продолжаем с нулевым накоплением")
            return
        self.cumulative_gas_loss_usd = data.get("cumulative_gas_loss_usd", 0.0)
        self.cumulative_gas_loss_eth = data.get("cumulative_gas_loss_eth", 0.0)
        self.cumulative_net_pnl_usd = data.get("cumulative_net_pnl_usd", 0.0)
        self.last_known_eth_usd_price = data.get("last_known_eth_usd_price")
        self.reserved_wei = data.get("reserved_wei", 0)
        self.reserved_usd = data.get("reserved_usd", 0.0)
        self.reported_thresholds = set(data.get("reported_thresholds", []))
        self.halted = data.get("halted", False)
        self.halt_reason = data.get("halt_reason")
        self.tx_in_flight = data.get("tx_in_flight", False)
        self.pending_tx_hash = data.get("pending_tx_hash")

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "cumulative_gas_loss_usd": self.cumulative_gas_loss_usd,
            "cumulative_gas_loss_eth": self.cumulative_gas_loss_eth,
            "cumulative_net_pnl_usd": self.cumulative_net_pnl_usd,
            "last_known_eth_usd_price": self.last_known_eth_usd_price,
            "reserved_wei": self.reserved_wei,
            "reserved_usd": self.reserved_usd,
            "reported_thresholds": sorted(self.reported_thresholds),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "tx_in_flight": self.tx_in_flight,
            "pending_tx_hash": self.pending_tx_hash,
        }))

    def can_send(self) -> tuple[bool, str]:
        if self.halted:
            return False, f"{REASON_HALTED}: {self.halt_reason}"
        if self.tx_in_flight:
            return False, REASON_TX_IN_FLIGHT
        if self.cumulative_gas_loss_usd >= BUDGET_STOP_USD:
            return False, REASON_GAS_LIMIT
        return True, ""

    def reserve_for_send(self, gas_limit: int, max_fee_per_gas_wei: int,
                          eth_usd_price: float | None) -> tuple[bool, str]:
        """ПРАВКА (внешнее ревью, пункт 3): "резервировать максимальный
        расход следующей транзакции (gas_limit x maxFeePerGas), не
        только считать потраченное" -- отказ ЗАРАНЕЕ, если даже
        ХУДШИЙ случай (весь gas_limit по максимальной цене) пробил бы
        лимит, а не только постфактум по факту. Курс нужен, чтобы
        честно оценить лимит в USD -- при полном отсутствии курса
        (ни текущего, ни последнего известного) отказываем: рисковать
        реальными деньгами без ЛЮБОЙ оценки в USD нельзя."""
        max_cost_wei = gas_limit * max_fee_per_gas_wei
        price = eth_usd_price if eth_usd_price is not None else self.last_known_eth_usd_price
        if price is None:
            return False, REASON_NO_PRICE_FOR_RESERVE
        max_cost_usd = (max_cost_wei / 1e18) * price
        if self.cumulative_gas_loss_usd + max_cost_usd > BUDGET_STOP_USD:
            return False, REASON_GAS_LIMIT
        self.reserved_wei = max_cost_wei
        self.reserved_usd = max_cost_usd
        self._save()
        return True, ""

    def release_reservation(self) -> None:
        self.reserved_wei = 0
        self.reserved_usd = 0.0
        self._save()

    def record_gas_spend_wei(self, gas_cost_wei: int, eth_usd_price: float | None) -> list[float]:
        """Каждая попытка (успешная ИЛИ ревертнутая) добавляет газ в
        бюджет -- владелец: 'включая откаты и успешные'. gas_cost_wei
        -- ФАКТИЧЕСКИЙ расход (gasUsed x effectiveGasPrice ИЗ РЕЦЕПТА,
        см. task5_v4_hotpath.py), не оценка. Возвращает список НОВЫХ
        достигнутых промежуточных порогов (для отчёта).

        Курс ETH/USD может быть недоступен в момент вызова -- расход в
        ETH пишется ВСЕГДА (владелец: "если курс не получен — расход
        всё равно записывать в ETH"); USD-итог продвигается только
        если есть текущий ИЛИ последний известный курс, иначе честно
        предупреждаем, что USD-накопление в этом шаге не обновлено
        (риск недооценки лимита в долларах, не скрываем)."""
        gas_cost_eth = gas_cost_wei / 1e18
        self.cumulative_gas_loss_eth += gas_cost_eth
        price = eth_usd_price if eth_usd_price is not None else self.last_known_eth_usd_price
        newly_crossed: list[float] = []
        if price is not None:
            self.last_known_eth_usd_price = price
            self.cumulative_gas_loss_usd += gas_cost_eth * price
            for threshold in BUDGET_REPORT_THRESHOLDS_USD:
                if self.cumulative_gas_loss_usd >= threshold and threshold not in self.reported_thresholds:
                    self.reported_thresholds.add(threshold)
                    newly_crossed.append(threshold)
            if self.cumulative_gas_loss_usd >= BUDGET_STOP_USD and not self.halted:
                self.halted = True
                self.halt_reason = f"бюджет газа исчерпан (${self.cumulative_gas_loss_usd:.2f} >= ${BUDGET_STOP_USD})"
        else:
            print(f"[pilot][ВНИМАНИЕ] курс ETH/USD недоступен (ни текущий, ни последний известный) -- "
                  f"расход {gas_cost_eth:.8f} ETH записан в cumulative_gas_loss_eth, но USD-итог "
                  f"НЕ обновлён в этом шаге (честно: возможна недооценка бюджета в USD)")
        self.reserved_wei = 0
        self.reserved_usd = 0.0
        self._save()
        return newly_crossed

    def record_net_pnl_usd(self, pnl_usd: float) -> None:
        """Пункт ревью 6 -- накопленная ЧИСТАЯ прибыль (прибыль минус
        газ, в USD) реально считается и сохраняется, не остаётся
        незаполненным полем."""
        self.cumulative_net_pnl_usd += pnl_usd
        self._save()

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self._save()

    def set_in_flight(self, value: bool, pending_tx_hash: str | None = None) -> None:
        """ПРАВКА (внешнее ревью, пункт 4): pending_tx_hash -- ЧАСТЬ
        персистентного состояния (переживает рестарт). Снимать
        in_flight (value=False) следует ТОЛЬКО когда судьба транзакции
        РЕАЛЬНО известна (получен рецепт) -- эта функция сама не
        проверяет это, дисциплина -- на вызывающем коде
        (task5_v4_hotpath.py), см. докстринг модуля."""
        self.tx_in_flight = value
        self.pending_tx_hash = pending_tx_hash if value else None
        self._save()


def check_no_unexpected_token_spend(contract_pre: dict[str, int], contract_post: dict[str, int],
                                     exit_token: str) -> tuple[bool, str]:
    """Владелец: 'остановка при расходе любого нашего токена помимо
    газа'. Контракт после успешного цикла должен быть net>=0 по ВСЕМ
    токенам маршрута, кроме, возможно, exit_token (который растёт) --
    любое СНИЖЕНИЕ баланса контракта по НЕ-exit_token токену -- реальный
    признак утечки инвентаря (баг/непредвиденный skim), не газа (газ
    платит EOA, не контракт)."""
    for token, pre_balance in contract_pre.items():
        if token.lower() == exit_token.lower():
            continue
        post_balance = contract_post.get(token, pre_balance)
        if post_balance < pre_balance:
            return False, (f"токен {token}: баланс контракта упал с {pre_balance} до {post_balance} "
                            f"(не exit_token, газ тут ни при чём) -- СТОП")
    return True, ""


def check_accounting_consistency(expected_profit_raw: int, actual_profit_raw: int | None,
                                  tolerance_fraction: float = 0.5) -> tuple[bool, str]:
    """Владелец: 'остановка... при нарушении учёта'. Если фактическая
    прибыль СИЛЬНО (>tolerance_fraction) расходится с ожидаемой ПОСЛЕ
    успешного исполнения -- это не проскальзывание (для успешной tx
    ожидание уже строилось на симуляции того же блока), а сбой в нашем
    учёте -- честно останавливаемся, не продолжаем считать вслепую."""
    if actual_profit_raw is None:
        return True, ""  # tx не подтвердилась ещё/не успешна -- нечего сверять
    if expected_profit_raw <= 0:
        return True, ""
    diff = abs(actual_profit_raw - expected_profit_raw) / expected_profit_raw
    if diff > tolerance_fraction:
        return False, (f"ожидали прибыль {expected_profit_raw}, реально {actual_profit_raw} "
                        f"(расхождение {diff:.0%} > {tolerance_fraction:.0%}) -- нарушение учёта, СТОП")
    return True, ""


class AttemptTable:
    def __init__(self, path: str = "/home/bot/data/task5_v4_pilot_attempts.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, row: AttemptTableRow) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


class ReasonLog:
    """'Молчаливого ожидания быть не должно' -- каждое решение "не
    отправляем" пишется с причиной, отдельно от таблицы попыток (та --
    только для реальных отправок)."""

    def __init__(self, path: str = "/home/bot/data/task5_v4_pilot_no_send_log.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, route_id: str, route_label: str, reason: str, detail: str = "") -> None:
        rec = {"ts_wall": time.time(), "route_id": route_id, "route_label": route_label,
               "reason": reason, "detail": detail}
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[pilot][не отправлено] {route_label}: {reason} ({detail})")
