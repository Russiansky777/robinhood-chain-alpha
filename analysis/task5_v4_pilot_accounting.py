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
    check_accounting_consistency), при исчерпании бюджета газа."""
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


class PilotBudget:
    def __init__(self, state_path: str = "/home/bot/data/task5_v4_pilot_budget_state.json") -> None:
        self.state_path = Path(state_path)
        self.cumulative_gas_loss_usd = 0.0
        self.reported_thresholds: set[float] = set()
        self.halted = False
        self.halt_reason: str | None = None
        self.tx_in_flight = False
        self._load()

    def _load(self) -> None:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                self.cumulative_gas_loss_usd = data.get("cumulative_gas_loss_usd", 0.0)
                self.reported_thresholds = set(data.get("reported_thresholds", []))
                self.halted = data.get("halted", False)
                self.halt_reason = data.get("halt_reason")
            except Exception:  # noqa: BLE001
                pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "cumulative_gas_loss_usd": self.cumulative_gas_loss_usd,
            "reported_thresholds": sorted(self.reported_thresholds),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }))

    def can_send(self) -> tuple[bool, str]:
        if self.halted:
            return False, f"{REASON_HALTED}: {self.halt_reason}"
        if self.tx_in_flight:
            return False, REASON_TX_IN_FLIGHT
        if self.cumulative_gas_loss_usd >= BUDGET_STOP_USD:
            return False, REASON_GAS_LIMIT
        return True, ""

    def record_gas_spend(self, cost_usd: float) -> list[str]:
        """Каждая попытка (успешная ИЛИ ревертнутая) добавляет газ в
        бюджет -- владелец: 'включая откаты и успешные'. Возвращает
        список НОВЫХ достигнутых промежуточных порогов (для отчёта)."""
        self.cumulative_gas_loss_usd += cost_usd
        newly_crossed = []
        for threshold in BUDGET_REPORT_THRESHOLDS_USD:
            if self.cumulative_gas_loss_usd >= threshold and threshold not in self.reported_thresholds:
                self.reported_thresholds.add(threshold)
                newly_crossed.append(threshold)
        if self.cumulative_gas_loss_usd >= BUDGET_STOP_USD and not self.halted:
            self.halted = True
            self.halt_reason = f"бюджет газа исчерпан (${self.cumulative_gas_loss_usd:.2f} >= ${BUDGET_STOP_USD})"
        self._save()
        return newly_crossed

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self._save()

    def set_in_flight(self, value: bool) -> None:
        self.tx_in_flight = value
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
