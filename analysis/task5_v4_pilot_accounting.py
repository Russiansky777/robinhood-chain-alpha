#!/usr/bin/env python3
"""Задача 5, живой пилот -- бюджет/лимиты/учёт попыток. НЕ логика
подписи/отправки -- чистая бухгалтерия и решение "можно ли слать"
(gate), которое ГОРЯЧИЙ ПУТЬ проверяет ПЕРЕД вызовом
task5_bot_sender.py (prepare_transaction_fields/sign_prepared_transaction/
submit_prepared).

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

ПРАВКА 2026-09-13, ВТОРОЙ РАУНД ВНЕШНЕГО РЕВЬЮ -- ключевые изменения:

  ПУНКТ 1 (честный PnL): finalize_attempt() -- ЕДИНАЯ идемпотентная
  точка начисления газа+PnL. Успех: факт.прирост минус факт.газ.
  Откат: минус факт.газ. Неизвестный статус: НЕ финализируем вообще
  (резерв остаётся). Флаг pending["finalized"] защищает от повторного
  начисления при повторном receipt/рестарте -- см. докстринг
  finalize_attempt. Первичные суммы -- raw/wei
  (cumulative_gas_wei, cumulative_gross_profit_raw_by_token);
  cumulative_net_pnl_usd -- ЯВНО помеченная USD-проекция с допущением
  USDG=$1 (см. _raw_to_usd).

  ПУНКТ 3/4 (учёт бюджета): reserve_for_send() сохраняет курс И момент
  его получения. Отсутствующие обязательные поля рецепта (gasUsed/
  effectiveGasPrice) НЕ превращаются молча в 0 -- halt. Если ни текущий,
  ни последний известный курс недоступны -- USD-порог НЕ продвигается
  молча: halt (пункт 4: "Не допускать ситуации, когда газ записан в
  ETH, но долларовый лимит молча продолжает считаться без этого
  расхода" -- лучше остановиться, чем считать бюджет заведомо
  заниженным). Состояние сохраняется АТОМАРНО (temp-файл + os.replace)
  -- повреждённый файл (частичная запись при сбое) невозможен по
  конструкции, не только распознаётся постфактум.

  ПУНКТ 4 (незавершённые транзакции): self.pending -- ПОЛНЫЙ контекст
  попытки (не только хэш) -- маршрут, размер, ожидаемая прибыль,
  резерв, курс на момент резерва -- достаточно, чтобы восстановить
  строку отчёта и PnL после рестарта БЕЗ обращения к состоянию,
  которое могло исчезнуть (см. begin_attempt/to_context).

  ПУНКТ 5 (часовой пилот): pilot_started_at/pilot_completed --
  персистентны. Рестарт ВНУТРИ часа продолжает с ТОГО ЖЕ начала
  (не перезапускает таймер); после pilot_completed=True -- новый
  час НЕ начинается автоматически (см. task5_v4_hotpath.py::main)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
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
REASON_PILOT_COMPLETED = "пилот уже завершён -- новый час не начинается автоматически"

USDG_ADDRESS_LOWER = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


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
    result: str  # "sent_pending" | "success" | "reverted" | "unresolved" | "send_error"
    actual_gain_base_asset: float | None  # фактический прирост exit_token, заполняется по рецепту
    gas_used: int | None
    gas_cost_native: float | None
    cumulative_gas_loss_usd: float  # накопленный итог по газу+откатам (best-effort USD)
    cumulative_net_pnl_usd: float | None = None
    computed_at_block: int | None = None
    state_age_blocks: int | None = None


class PilotBudget:
    def __init__(self, state_path: str = "/home/bot/data/task5_v4_pilot_budget_state.json") -> None:
        self.state_path = Path(state_path)
        # Первичные суммы -- ТОЧНЫЕ, raw/wei (пункт 1: "храни первичные
        # суммы в целых raw/wei").
        self.cumulative_gas_wei = 0
        self.cumulative_gross_profit_raw_by_token: dict[str, int] = {}  # ТОЛЬКО успешные попытки
        # Производные/best-effort USD-величины (для порогов $5/$10/$20 --
        # владелец задал лимит именно в USD, курс ETH/USD неизбежно
        # приближение).
        self.cumulative_gas_loss_usd = 0.0
        self.cumulative_gas_loss_eth = 0.0
        self.cumulative_net_pnl_usd = 0.0
        self.last_known_eth_usd_price: float | None = None
        self.last_known_eth_usd_price_at: float | None = None
        self.reserved_wei = 0
        self.reserved_usd = 0.0
        self.reserved_price_used: float | None = None
        self.reserved_price_used_at: float | None = None
        self.reported_thresholds: set[float] = set()
        self.halted = False
        self.halt_reason: str | None = None
        # Полный контекст НЕЗАВЕРШЁННОЙ попытки (пункт 4) -- не просто
        # хэш+булев флаг. None, если ничего не в полёте.
        self.pending: dict | None = None
        # Часовой пилот (пункт 5) -- персистентны, рестарт НЕ обнуляет.
        self.pilot_started_at: float | None = None
        self.pilot_completed = False
        self.pilot_completed_reason: str | None = None
        self._load()

    # ---------- персистентность ----------

    def _load(self) -> None:
        if not self.state_path.exists():
            return  # законный чистый старт -- не ошибка, не halt
        try:
            data = json.loads(self.state_path.read_text())
        except Exception as exc:  # noqa: BLE001
            # Файл ЕСТЬ, но не читается -- СТОП, не продолжение с нулём
            # (иначе можно незаметно превысить реальный накопленный
            # лимит газа). Атомарная запись (см. _save) делает
            # "повреждён посередине записи" практически невозможным --
            # эта ветка теперь ловит только внешнюю порчу файла.
            self.halted = True
            self.halt_reason = (f"файл состояния бюджета {self.state_path} существует, но не читается "
                                 f"({exc}) -- честный стоп, не продолжаем с нулевым накоплением")
            return
        self.cumulative_gas_wei = data.get("cumulative_gas_wei", 0)
        self.cumulative_gross_profit_raw_by_token = data.get("cumulative_gross_profit_raw_by_token", {})
        self.cumulative_gas_loss_usd = data.get("cumulative_gas_loss_usd", 0.0)
        self.cumulative_gas_loss_eth = data.get("cumulative_gas_loss_eth", 0.0)
        self.cumulative_net_pnl_usd = data.get("cumulative_net_pnl_usd", 0.0)
        self.last_known_eth_usd_price = data.get("last_known_eth_usd_price")
        self.last_known_eth_usd_price_at = data.get("last_known_eth_usd_price_at")
        self.reserved_wei = data.get("reserved_wei", 0)
        self.reserved_usd = data.get("reserved_usd", 0.0)
        self.reserved_price_used = data.get("reserved_price_used")
        self.reserved_price_used_at = data.get("reserved_price_used_at")
        self.reported_thresholds = set(data.get("reported_thresholds", []))
        self.halted = data.get("halted", False)
        self.halt_reason = data.get("halt_reason")
        self.pending = data.get("pending")
        self.pilot_started_at = data.get("pilot_started_at")
        self.pilot_completed = data.get("pilot_completed", False)
        self.pilot_completed_reason = data.get("pilot_completed_reason")

    def _save(self) -> None:
        """АТОМАРНАЯ запись (пункт 4: "Состояние сохранять атомарно.
        Повреждённый файл не считать чистым стартом") -- пишем во
        временный файл В ТОЙ ЖЕ директории (гарантия того же
        файлового устройства для os.replace) и переименовываем;
        os.replace на POSIX -- атомарная операция, читатель никогда не
        увидит частично записанный файл, даже при падении процесса
        посередине записи."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cumulative_gas_wei": self.cumulative_gas_wei,
            "cumulative_gross_profit_raw_by_token": self.cumulative_gross_profit_raw_by_token,
            "cumulative_gas_loss_usd": self.cumulative_gas_loss_usd,
            "cumulative_gas_loss_eth": self.cumulative_gas_loss_eth,
            "cumulative_net_pnl_usd": self.cumulative_net_pnl_usd,
            "last_known_eth_usd_price": self.last_known_eth_usd_price,
            "last_known_eth_usd_price_at": self.last_known_eth_usd_price_at,
            "reserved_wei": self.reserved_wei,
            "reserved_usd": self.reserved_usd,
            "reserved_price_used": self.reserved_price_used,
            "reserved_price_used_at": self.reserved_price_used_at,
            "reported_thresholds": sorted(self.reported_thresholds),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "pending": self.pending,
            "pilot_started_at": self.pilot_started_at,
            "pilot_completed": self.pilot_completed,
            "pilot_completed_reason": self.pilot_completed_reason,
        }
        fd, tmp_path = tempfile.mkstemp(dir=str(self.state_path.parent), prefix=".budget_tmp_")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---------- гейт ----------

    def can_send(self) -> tuple[bool, str]:
        if self.pilot_completed:
            return False, f"{REASON_PILOT_COMPLETED}: {self.pilot_completed_reason or ''}"
        if self.halted:
            return False, f"{REASON_HALTED}: {self.halt_reason}"
        if self.pending is not None:
            return False, REASON_TX_IN_FLIGHT
        if self.cumulative_gas_loss_usd >= BUDGET_STOP_USD:
            return False, REASON_GAS_LIMIT
        return True, ""

    # ---------- часовой пилот ----------

    def ensure_pilot_started(self) -> float:
        """Пункт 5: "Час пилота отсчитывай после завершения
        первоначального наполнения реестра" + "Перезапуск не должен...
        начинать новый час торговли после завершённого пилота." Первый
        вызов за ВСЮ жизнь состояния фиксирует pilot_started_at
        НАВСЕГДА; рестарт внутри часа возвращает ТОТ ЖЕ момент, не
        новый. Вызывать ПОСЛЕ bootstrap_registry(), ДО основного цикла."""
        if self.pilot_started_at is None:
            self.pilot_started_at = time.time()
            self._save()
        return self.pilot_started_at

    def complete_pilot(self, reason: str) -> None:
        self.pilot_completed = True
        self.pilot_completed_reason = reason
        self._save()

    # ---------- резерв ПЕРЕД отправкой ----------

    def reserve_for_send(self, gas_limit: int, max_fee_per_gas_wei: int,
                          eth_usd_price: float | None) -> tuple[bool, str]:
        """"Резервировать максимальный расход следующей транзакции
        (gas_limit x maxFeePerGas), не только считать потраченное" --
        отказ ЗАРАНЕЕ, если даже ХУДШИЙ случай пробил бы лимит. Курс И
        МОМЕНТ ЕГО ПОЛУЧЕНИЯ сохраняются -- используются позже при
        finalize_attempt как fallback, если курс на момент реального
        расхода недоступен (честное, заранее объявленное правило: "тот
        же курс, что был при резервировании этой же попытки", не любой
        произвольно старый last_known)."""
        max_cost_wei = gas_limit * max_fee_per_gas_wei
        price = eth_usd_price if eth_usd_price is not None else self.last_known_eth_usd_price
        if price is None:
            return False, REASON_NO_PRICE_FOR_RESERVE
        max_cost_usd = (max_cost_wei / 1e18) * price
        if self.cumulative_gas_loss_usd + max_cost_usd > BUDGET_STOP_USD:
            return False, REASON_GAS_LIMIT
        self.reserved_wei = max_cost_wei
        self.reserved_usd = max_cost_usd
        self.reserved_price_used = price
        self.reserved_price_used_at = time.time()
        self._save()
        return True, ""

    def release_reservation(self) -> None:
        """Освобождает резерв БЕЗ финализации попытки -- только для
        случая, когда попытка вообще НЕ была начата (см. begin_attempt
        ниже не вызывался) -- например, финальная проверка перед
        подписью не прошла. НЕ вызывать после begin_attempt -- та
        попытка либо финализируется, либо остаётся pending до
        разрешения (пункт 3: "не отправлять следующую независимую
        сделку, пока предыдущая не разрешена")."""
        self.reserved_wei = 0
        self.reserved_usd = 0.0
        self.reserved_price_used = None
        self.reserved_price_used_at = None
        self._save()

    # ---------- незавершённая попытка (пункт 4) ----------

    def begin_attempt(self, context: dict) -> None:
        """Сохраняет ПОЛНЫЙ контекст попытки НА ДИСК ДО первого
        сетевого обращения (send) -- пункт 3: "сохранить... данные
        попытки до первого сетевого обращения". context должен
        включать как минимум: tx_hash, nonce, route_id, route_label,
        size_in_raw, exit_token, expected_profit_after_gas,
        computed_at_block, latency (на момент отправки),
        gas_limit/max_fee_per_gas -- этого достаточно для
        восстановления PnL и строки отчёта после рестарта, даже если
        вся остальная память процесса потеряна."""
        self.pending = {**context, "gas_recorded": False, "finalized": False}
        self.reserved_wei = 0
        self.reserved_usd = 0.0
        self.reserved_price_used = None
        self.reserved_price_used_at = None
        self._save()

    def finalize_gas(self, tx_status: int | None, gas_used: int | None, effective_gas_price: int | None,
                      eth_usd_price: float | None) -> dict:
        """ПЕРВАЯ (обязательная, ВСЕГДА выполнимая при известном
        статусе) половина финализации -- вызывается СРАЗУ после
        получения tx_status, ДО чтения балансов/сверки учёта (пункт 3:
        "ошибка RPC после отправки — расход записать до любых других
        действий"). Идемпотентна через pending["gas_recorded"] --
        повторный вызов (повторный receipt/рестарт) НИЧЕГО не
        начисляет повторно.

        - tx_status is None (судьба ГЕНУИННО ещё не известна) -- НЕ
          трогаем ничего, {"resolved": False} -- вызывающий код обязан
          продолжать выяснять судьбу (пункт 3: "не отправлять следующую
          независимую сделку, пока предыдущая не разрешена").
        - gas_used/effective_gas_price отсутствуют -- НЕ превращаем в 0
          молча: halt, {"resolved": False, "halted_missing_fields": True}
          (пункт 4: "отсутствующие обязательные поля не превращать
          молча в нули").
        - tx_status == 0 (откат): net PnL -= факт.газ, попытка
          закрывается целиком ЗДЕСЬ (прибыли не будет по определению).
        - tx_status == 1 (успех): net PnL -= факт.газ СЕЙЧАС (газ --
          гарантированный расход), прибыльная сторона добавляется
          ПОЗЖЕ через finalize_profit_and_close -- попытка остаётся
          pending (gas_recorded=True, finalized=False) до тех пор."""
        if self.pending is None:
            return {"resolved": True, "no_pending": True}
        if self.pending.get("gas_recorded"):
            return {"resolved": True, "already_recorded": True, "tx_status": self.pending.get("tx_status")}
        if tx_status is None:
            return {"resolved": False}
        if gas_used is None or effective_gas_price is None:
            self.halt(f"рецепт для {self.pending.get('tx_hash')} не содержит gasUsed/effectiveGasPrice -- "
                      f"не могу достоверно посчитать фактический газ, СТОП (не подставляем 0)")
            return {"resolved": False, "halted_missing_fields": True}

        gas_cost_wei = int(gas_used) * int(effective_gas_price)
        self.cumulative_gas_wei += gas_cost_wei
        gas_cost_eth = gas_cost_wei / 1e18
        self.cumulative_gas_loss_eth += gas_cost_eth

        # Правило выбора курса (честно объявлено, не произвольно):
        # 1) курс, полученный ПРЯМО СЕЙЧАС (eth_usd_price, свежий);
        # 2) иначе -- курс, зафиксированный ПРИ РЕЗЕРВЕ этой же попытки
        #    (pending["reserved_price_used"]) -- тот же контекст решения,
        #    не произвольно старый; 3) иначе -- последний известный курс
        #    ВООБЩЕ (last_known). Если НИ ОДИН недоступен -- USD-итог НЕ
        #    продвигается молча: halt (пункт 4: "не допускать ситуации,
        #    когда газ записан в ETH, но долларовый лимит молча
        #    продолжает считаться без этого расхода").
        price = (eth_usd_price if eth_usd_price is not None
                 else self.pending.get("reserved_price_used")
                 if self.pending.get("reserved_price_used") is not None
                 else self.last_known_eth_usd_price)
        newly_crossed: list[float] = []
        gas_cost_usd = None
        if price is not None:
            self.last_known_eth_usd_price = price
            self.last_known_eth_usd_price_at = time.time()
            gas_cost_usd = gas_cost_eth * price
            self.cumulative_gas_loss_usd += gas_cost_usd
            self.cumulative_net_pnl_usd -= gas_cost_usd  # газ -- гарантированный расход, сразу
            for threshold in BUDGET_REPORT_THRESHOLDS_USD:
                if self.cumulative_gas_loss_usd >= threshold and threshold not in self.reported_thresholds:
                    self.reported_thresholds.add(threshold)
                    newly_crossed.append(threshold)
            if self.cumulative_gas_loss_usd >= BUDGET_STOP_USD and not self.halted:
                self.halted = True
                self.halt_reason = f"бюджет газа исчерпан (${self.cumulative_gas_loss_usd:.2f} >= ${BUDGET_STOP_USD})"

        self.pending["gas_recorded"] = True
        self.pending["tx_status"] = tx_status
        self.pending["gas_cost_wei"] = gas_cost_wei
        self.pending["gas_cost_usd"] = gas_cost_usd
        # ПРАВКА (третий раунд ревью, пункт 3): сохраняем raw gas_used,
        # чтобы _ensure_pending_row_written (task5_v4_hotpath.py) могло
        # восстановить полную, точную строку попытки ТОЛЬКО из pending
        # на диске, без опоры на память рухнувшего процесса.
        self.pending["gas_used"] = int(gas_used)
        self._save()

        if price is None:
            self.halt("курс ETH/USD недоступен ни сейчас, ни на момент резерва этой попытки, ни ранее -- "
                      "USD-бюджет/PnL не может быть достоверно продвинут, СТОП (газ в ETH уже учтён выше, "
                      "raw-сумма не потеряна)")
            return {"resolved": False, "halted_no_price": True, "gas_cost_wei": gas_cost_wei}

        if tx_status == 0:
            # Откат -- вклад в PnL уже полный (только газ), финансово
            # попытка закрыта ЗДЕСЬ (finalize_profit_and_close для
            # отката не вызывается). ПРАВКА (третий раунд ревью, пункт
            # 3): pending НЕ обнуляется сразу -- остаётся с
            # finalized=True, пока вызывающий код (task5_v4_hotpath.py)
            # не запишет итоговую строку попытки и не вызовет
            # clear_finalized_pending(). Так восстановление после
            # рестарта (случай "finalized=True, но не очищено") может
            # безопасно дописать пропущенную строку без повторного
            # начисления газа/прибыли.
            self.pending["finalized"] = True
            self._save()
            return {"resolved": True, "gas_cost_wei": gas_cost_wei, "gas_cost_usd": gas_cost_usd,
                    "newly_crossed": newly_crossed, "tx_status": 0}

        return {"resolved": True, "awaiting_profit": True, "gas_cost_wei": gas_cost_wei,
                "gas_cost_usd": gas_cost_usd, "newly_crossed": newly_crossed, "tx_status": 1}

    def finalize_profit_and_close(self, actual_gain_raw: int | None) -> dict:
        """ВТОРАЯ половина -- ТОЛЬКО для успешных транзакций
        (tx_status==1), вызывается ПОСЛЕ finalize_gas, после (попытки)
        чтения балансов. Идемпотентна через pending["finalized"].
        actual_gain_raw=None (баланс прочитать не удалось) -- газ уже
        учтён в finalize_gas, прибыльный вклад в net PnL честно
        пропускается (не гадаем), попытка ВСЁ РАВНО закрывается
        (вызывающий код -- task5_v4_hotpath.py -- обязан отдельно
        перевести пилот в halt при сбое чтения баланса, ДО вызова этого
        метода с actual_gain_raw=None по этой причине)."""
        if self.pending is None:
            return {"resolved": True, "no_pending": True}
        if self.pending.get("finalized"):
            return {"resolved": True, "already_finalized": True}
        if not self.pending.get("gas_recorded"):
            raise RuntimeError("finalize_profit_and_close вызван до finalize_gas -- нарушение порядка вызовов")

        exit_token = self.pending.get("exit_token")
        actual_gain_usd = None
        if actual_gain_raw is not None and exit_token:
            self.cumulative_gross_profit_raw_by_token[exit_token] = (
                self.cumulative_gross_profit_raw_by_token.get(exit_token, 0) + int(actual_gain_raw))
            actual_gain_usd = _raw_to_usd(exit_token, actual_gain_raw, self.last_known_eth_usd_price)
            if actual_gain_usd is not None:
                self.cumulative_net_pnl_usd += actual_gain_usd

        # ПРАВКА (третий раунд ревью, пункт 3): сохраняем фактическую
        # raw-прибыль этой попытки, чтобы _ensure_pending_row_written
        # (task5_v4_hotpath.py) могло восстановить строку попытки
        # ТОЛЬКО из pending на диске (без памяти рухнувшего процесса).
        self.pending["actual_gain_raw"] = actual_gain_raw
        # pending НЕ обнуляется сразу -- см. finalize_gas (ветка
        # tx_status==0) для того же обоснования: остаётся
        # finalized=True до записи итоговой строки попытки, затем
        # clear_finalized_pending().
        self.pending["finalized"] = True
        self._save()
        return {"resolved": True, "actual_gain_usd": actual_gain_usd}

    def mark_pending_row_written(self) -> None:
        """Вызывать СРАЗУ после успешной записи итоговой строки
        попытки (AttemptTable.write) -- отдельный флаг (не просто
        "finalized"), чтобы восстановление после рестарта могло
        отличить "прибыль/газ учтены, строка ещё НЕ записана" от
        "всё сделано, просто pending не успел очиститься" -- и не
        писать строку повторно (пункт 3: "обеспечивать наличие
        итоговой строки попытки без дубликатов")."""
        if self.pending is not None:
            self.pending["row_written"] = True
            self._save()

    def clear_finalized_pending(self) -> dict:
        """Финально очищает pending -- вызывать ТОЛЬКО после того, как
        итоговая строка попытки надёжно записана (mark_pending_row_written).
        Идемпотентно: если pending уже None или ещё не finalized,
        честно отказывается (вызывающий код ошибся в порядке)."""
        if self.pending is None:
            return {"cleared": True, "no_pending": True}
        if not self.pending.get("finalized"):
            return {"cleared": False, "not_finalized": True}
        self.pending = None
        self._save()
        return {"cleared": True}

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self._save()


def _raw_to_usd(token: str, raw_amount: int, weth_usdg_price: float | None) -> float | None:
    """ДОПУЩЕНИЕ ЭТОГО ПРОЕКТА, ЯВНО ОБОЗНАЧЕНО (не изобретено здесь
    заново -- см. task5_bot_sender.py::record_outcome/MAX_DAILY_LOSS_USD,
    где USDG-суммы везде трактуются как доллары напрямую): USDG == $1.
    Для нативного ETH -- перевод по РЕАЛЬНОЙ текущей цене WETH/USDG
    (weth_usdg_price), не гадаем курс."""
    if token.lower() == USDG_ADDRESS_LOWER:
        return raw_amount / 10**6  # USDG assumed == $1
    if weth_usdg_price is None:
        return None
    return (raw_amount / 10**18) * weth_usdg_price


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
        self._lock = threading.Lock()
        # Счётчик попыток за ЭТОТ запуск процесса -- для статусных
        # отчётов (владелец, доп.: отчёты на $5/$10/по часу должны
        # показывать реальную картину, включая "попыток было 0").
        self.count = 0

    def write(self, row: AttemptTableRow) -> None:
        with self._lock:
            with self.path.open("a") as fh:
                fh.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
            self.count += 1


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
