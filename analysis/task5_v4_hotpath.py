#!/usr/bin/env python3
"""Задача 5, живой пилот -- ГОРЯЧИЙ ПУТЬ (владелец, 2026-09-12):
"на каждый своп в пуле из живого маршрута -- пересчёт цикла в памяти,
подбор размера, и если плюс после газа -- отправка. Без повторного
поиска маршрута."

ЧЕСТНАЯ ОГОВОРКА ПО СКОРОСТИ (не скрываем): для V4 своп -- это вызов
`unlock(bytes)` синглтон-PoolManager с ПРОИЗВОЛЬНЫМ (не декодируемым
обобщённо) форматом данных каждого вызывающего контракта (см.
task5_v4_observation_hour.py, докстринг "ПОЧЕМУ НЕ ДЕКОДИРУЕМ ЧУЖИЕ
CALLDATA"). Поэтому триггер здесь -- РЕАЛЬНОЕ (уже подтверждённое)
`PoolManager.Swap`-событие (после исполнения), не предисполнительная
calldata из фида. Пересчёт САМ -- через реальный V4Quoter (eth_call),
не переизобретённая на месте формула AMM -- цена корректности важнее
секунд для реальных денег; это НЕ "в памяти без сети" в буквальном
смысле требования, честно отмечено как компромисс v1 (владелец,
2026-09-13, доп.: "Не расширяем задачу до локальной математики" --
переизобретение AMM-формул сознательно НЕ делается в этом фиксе,
только оценено по объёму отдельно).

ПРАВКА 2026-09-13 (внешнее ревью, пункты 3-5, 7 + доп. владельца):

  1. АРХИТЕКТУРА РАЗДЕЛЕНА НА ТРИ ПОТОКА, чтобы обнаружение/живучесть
     НЕ блокировали обработку торговых сигналов (владелец, доп.):
       - ДЕТЕКТОР (poll_once, вызывается из главного потока) -- только
         опрашивает НОВЫЕ Swap-логи отслеживаемых пулов и помечает
         затронутые маршруты в коалесцирующей очереди (_CoalescingRouteQueue).
         НЕ делает пересчёт/отправку сам -- быстрый, не блокируется.
       - ОЦЕНЩИК/ОТПРАВИТЕЛЬ (HotPath.evaluator_loop, отдельный поток) --
         разбирает очередь по ОДНОМУ маршруту за раз, ВСЕГДА по САМОМУ
         СВЕЖЕМУ известному блоку для него: "пока идёт расчёт, новые
         изменения одного маршрута объединяй в последнее доступное
         состояние" -- НЕ обрабатывает очередь устаревших состояний,
         очередь -- это словарь (route_id -> последний блок), не FIFO.
       - ФОН (BackgroundRegistryWorker, отдельный поток) -- непрерывное
         обнаружение новых пулов арбитражника + периодическая (раз в
         минуту) полная проверка живучести. Пишет в тот же реестр
         (RouteRegistry теперь потокобезопасен, RLock) -- новый маршрут
         передаётся детектору/оценщику СРАЗУ после появления его
         параметров (реестр читается заново на каждом опросе), без
         дополнительного перезапуска.
     Все три потока делят один и тот же RPC-троттлинг
     (alchemy_fallback._throttle, сделан потокобезопасным отдельной
     правкой) -- лимиты провайдера соблюдаются суммарно, не утраиваются.

  2. ЛОГ БЛОКА РАСЧЁТА, ВОЗРАСТА СОСТОЯНИЯ, ЗАДЕРЖКИ (владелец, доп.):
     AttemptTableRow.computed_at_block/state_age_blocks + печать в
     реальном времени. ФИНАЛЬНАЯ ПРОВЕРКА АКТУАЛЬНОЙ ИСПОЛНИМОСТИ --
     непосредственно перед отправкой пересчитываем на САМОМ СВЕЖЕМ
     блоке (не на том, что использовался для решения) -- если решение
     успело устареть (state_age_blocks > 0) и уже не прибыльно, НЕ
     отправляем.

  3. УЧЁТ БЮДЖЕТА (пункт ревью 3): резерв ПЕРЕД отправкой
     (gas_limit x maxFeePerGas, estimate_max_fee_per_gas_wei), факт --
     ИЗ РЕАЛЬНОГО РЕЦЕПТА (gasUsed x effectiveGasPrice, читаем сами --
     task5_bot_sender.py::SendResult не отдаёт effectiveGasPrice, эта
     сессия НЕ меняет sender.py, только ЧИТАЕТ уже опубликованный
     рецепт отдельным RPC-вызовом). Расход записывается СРАЗУ после
     получения рецепта, ДО чтения балансов/сверки учёта -- падение
     чего угодно ПОСЛЕ этого не теряет бухгалтерию уже потраченных
     реальных денег.

  4. НЕЗАВЕРШЁННЫЕ ТРАНЗАКЦИИ (пункт ревью 4): pending_tx_hash --
     персистентная часть PilotBudget (переживает рестарт). Таймаут
     ожидания рецепта САМИМ Sender'ом (RECEIPT_TIMEOUT_S=15с в
     task5_bot_sender.py) НЕ снимает in_flight -- если Sender сам не
     дождался, ждём и опрашиваем РЕАЛЬНЫЙ рецепт САМИ
     (wait_for_real_receipt), сколько потребуется. На старте --
     resolve_pending_tx_if_any() ПЕРЕД началом работы.

  5. ПРОВЕРКА КОШЕЛЬКА (пункт ревью 5): owner() контракта == адрес
     ключа Sender == --from-address (заявленный кошелёк пилота) --
     один адрес, иначе SystemExit при старте.

Учёт/лимиты -- task5_v4_pilot_accounting.py. Подпись/отправка --
ИСКЛЮЧИТЕЛЬНО task5_bot_sender.py::Sender.send_cycle (эта сессия эту
логику не пишет и не меняет, только вызывает с готовыми to/calldata/
gas_limit, и ЧИТАЕТ уже опубликованные рецепты read-only)."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _chunked_get_logs, _rpc_call, topic0  # noqa: E402
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402
from task5_v4_pilot_accounting import (  # noqa: E402
    BUDGET_STOP_USD, REASON_CALC_ERROR, REASON_NO_LIQUIDITY, REASON_NO_PROFITABLE_CYCLE,
    REASON_SIMULATION_FAILED, AttemptTable, AttemptTableRow, PilotBudget, ReasonLog,
    check_accounting_consistency, check_no_unexpected_token_spend,
)
from task5_v4_quote_replay import quote_exact_input_single  # noqa: E402
from task5_v4_route_registry import (  # noqa: E402
    RouteCycle, RouteRegistry, USDG, bootstrap_registry, check_route_liveness,
)

NATIVE = "0x0000000000000000000000000000000000000000"

# Реальный slot0() того же WETH/USDG V3-пула, что уже используется для
# живой цены в этом проекте (task5_true_arbitrageur_scan.py) -- нужен,
# чтобы честно перевести газ (платится в ETH) в USDG для маршрутов с
# exit_token=USDG, а не молчать про газ для них (см. _evaluate_and_maybe_send).
WETH_USDG_POOL_V3 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
WETH_DECIMALS, USDG_DECIMALS = 18, 6


def current_weth_usdg_price() -> float | None:
    """Та же реализация, что task5_true_arbitrageur_scan.py::current_weth_usdg_price
    -- реальная ТЕКУЩАЯ цена, не переиспользуем старую константу."""
    selector = "0x3850c7bd"  # slot0()
    try:
        result = _rpc_call("eth_call", [{"to": WETH_USDG_POOL_V3, "data": selector}, "latest"])
        sqrt_price_x96 = int(result[2:66], 16)
        raw_ratio = (sqrt_price_x96 / (2 ** 96)) ** 2
        return raw_ratio * (10 ** (WETH_DECIMALS - USDG_DECIMALS))
    except Exception:  # noqa: BLE001
        return None


SWAP_TOPIC0 = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
OWNER_SELECTOR = "0x8da5cb5b"  # keccak256("owner()")[:4] -- переиспользован, уже проверен этой сессией

POLL_INTERVAL_S = 0.5  # = alchemy_fallback._MIN_REQUEST_INTERVAL_S -- опрашивать чаще
                       # не даст выигрыша (eth_blockNumber всё равно упрётся в тот же
                       # самотроттлинг), реже -- добавляет задержку детекции сверх нужного.
LIVENESS_REFRESH_INTERVAL_S = 60.0  # владелец: "раз в минуту"
DISCOVERY_POLL_INTERVAL_S = 1.0  # фоновый поток -- не торговый цикл, может опрашивать чуть реже
PENDING_TX_RECEIPT_POLL_S = 2.0
PILOT_HOUR_S = 3600.0  # владелец, доп.: "Час пилота отсчитывай после завершения первоначального наполнения реестра"

# Собственный подбор размера (та же сетка, что task5_v4_observation_hour.py,
# см. её докстринг про то, что размер НЕ подставляется вслепую).
SIZE_GRID_BY_START_TOKEN = {
    USDG.lower(): [1_000_000, 3_000_000, 5_000_000, 7_000_000, 10_000_000, 15_000_000, 20_000_000, 30_000_000],
    NATIVE: [int(x * 1e18) for x in (0.02, 0.05, 0.08, 0.1, 0.15, 0.2, 0.3)],
}


def recompute_route(route: RouteCycle, block_number: int) -> dict:
    """Пересчёт цикла: свой перебор размера, возвращает лучший
    (amount_in, amount_out, profit_raw) ИЛИ причину отказа. Не
    подставляет заранее известный ответ -- пересчитывает КАЖДЫЙ раз из
    реального состояния пулов на blockNumber (событие свопа уже
    подтверждено -- это состояние РЕАЛЬНОЕ, не из будущего).

    НАЙДЕНА И ИСПРАВЛЕНА ПРИЧИНА "6 проверок за 90 секунд" (владелец,
    2026-09-13, "заведомо мимо, найти причину" -- диагноз по реальным
    данным task5_v4_observation_hour.py): узкое место -- НЕ таймер опроса
    (POLL_INTERVAL_S), а САМОТРОТТЛИНГ RPC (alchemy_fallback._throttle,
    _MIN_REQUEST_INTERVAL_S=0.5с МЕЖДУ ЛЮБЫМИ запросами, project-wide,
    сознательно введён после реальных инцидентов с 429 -- не трогаем)
    в сочетании со СПЛОШНЫМ перебором ВСЕЙ сетки размеров (до 8 точек x
    до 3 ног = до 24 последовательных eth_call) даже когда САМЫЙ МЕЛКИЙ
    размер уже падает с NotEnoughLiquidity: 24 x 0.5с = 12с на один
    пересчёт -- ровно наблюдавшийся порядок (90с / ~10-12с ~= 6-7
    проверок).
    Реальное, не выдуманное свойство V4-пулов с концентрированной
    ликвидностью, на котором строится исправление: если ДАЖЕ САМЫЙ
    МЕЛКИЙ размер в сетке (или мелкий размер на КАКОЙ-ЛИБО ноге цепочки)
    падает с NotEnoughLiquidity -- ликвидности в диапазоне текущей цены
    нет вообще, и ЛЮБОЙ БОЛЬШИЙ размер (которому нужно СТРОГО НЕ МЕНЬШЕ
    ликвидности) тоже упадёт тем же образом. Поэтому сетка перебирается
    по возрастанию, и при первом NotEnoughLiquidity -- немедленный
    останов (не перебор оставшихся точек): для мёртвого маршрута (наша
    текущая реальность -- оба сид-маршрута сейчас без ликвидности) это
    1 запрос вместо до 24, не до 12с, а меньше 1с на пересчёт."""
    start_token = route.legs[0].input_currency.lower()
    grid = SIZE_GRID_BY_START_TOKEN.get(start_token, SIZE_GRID_BY_START_TOKEN[USDG.lower()])
    best = None
    last_error = None
    for amount_in in grid:
        try:
            cur = amount_in
            for leg in route.legs:
                cur = quote_exact_input_single(leg.pool_key, leg.zero_for_one, cur, block_number)
            profit_raw = cur - amount_in
            if best is None or profit_raw > best["profit_raw"]:
                best = {"amount_in": amount_in, "amount_out": cur, "profit_raw": profit_raw}
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if "NotEnoughLiquidity" in last_error:
                # Больший размер строго не может пройти там, где не прошёл
                # меньший -- дальше по сетке идти бессмысленно и дорого
                # (см. докстринг выше). Если best уже найден на МЕНЬШЕМ
                # размере до этого сбоя -- он остаётся в силе, это не потеря.
                break
            continue
    if best is None:
        reason = REASON_NO_LIQUIDITY if last_error and "NotEnoughLiquidity" in last_error else REASON_CALC_ERROR
        return {"ok": False, "reason": reason, "detail": last_error or "неизвестная ошибка"}
    return {"ok": True, **best}


def estimate_gas(contract_address: str, calldata: bytes, from_address: str) -> dict:
    """eth_estimateGas -- РЕАЛЬНЫЙ read-only вызов (не отправка), точнее
    заранее известной оценки из fork-прогонов Этапа 2 (размер здесь
    подобран свежо, газ может отличаться от прежних измерений)."""
    try:
        raw = _rpc_call("eth_estimateGas", [{
            "from": from_address, "to": contract_address, "data": "0x" + calldata.hex(),
        }])
        return {"ok": True, "gas_estimate": int(raw, 16)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def estimate_max_fee_per_gas_wei() -> int:
    """Консервативная ОЦЕНКА maxFeePerGas ДЛЯ РЕЗЕРВИРОВАНИЯ БЮДЖЕТА
    ПЕРЕД отправкой (внешнее ревью, пункт 3) -- та же формула, что
    РЕАЛЬНО использует task5_bot_sender.py::Sender.send_cycle
    (base_fee*2 + priority fee): читаем сами base_fee ДО вызова
    send_cycle (эта сессия НЕ трогает/дублирует логику подписи -- это
    read-only оценка ДЛЯ РЕЗЕРВА, реальная transaction строится внутри
    Sender'а по факту в момент отправки, может отличаться на малую
    величину при движении base_fee между резервом и отправкой)."""
    priority_fee_wei_ref = int(1e8)  # см. task5_bot_sender.py::PRIORITY_FEE_WEI -- тот же факт, независимо прочитан
    try:
        block = _rpc_call("eth_getBlockByNumber", ["latest", False])
        base_fee = int(block["baseFeePerGas"], 16)
    except Exception:  # noqa: BLE001
        base_fee = int(_rpc_call("eth_gasPrice", []), 16)
    return base_fee * 2 + priority_fee_wei_ref


def _token_balance(token: str, account: str) -> int:
    """Баланс контракта по токену -- нативный ETH через eth_getBalance,
    ERC20 через balanceOf(address) (селектор 0x70a08231, keccak-проверен
    ранее в проекте). Используется ДО/ПОСЛЕ реальной попытки для
    check_no_unexpected_token_spend/check_accounting_consistency --
    честная проверка по факту, не по ожиданию."""
    if token.lower() == NATIVE:
        return int(_rpc_call("eth_getBalance", [account, "latest"]), 16)
    padded = account[2:].rjust(64, "0")
    raw = _rpc_call("eth_call", [{"to": token, "data": "0x70a08231" + padded}, "latest"])
    return int(raw, 16)


def fetch_real_receipt(tx_hash: str) -> dict | None:
    """Единичный НЕблокирующий read-only запрос -- None, если транзакция
    ещё не замайнена. Используется ВМЕСТО SendResult.gas_used
    (task5_bot_sender.py::SendResult не отдаёт effectiveGasPrice -- эта
    сессия НЕ меняет sender.py, только ЧИТАЕТ уже опубликованный рецепт
    отдельным RPC-вызовом для честного расчёта фактического расхода
    газа, см. внешнее ревью, пункт 3)."""
    try:
        return _rpc_call("eth_getTransactionReceipt", [tx_hash])
    except Exception:  # noqa: BLE001
        return None


def wait_for_real_receipt(tx_hash: str, poll_interval_s: float = PENDING_TX_RECEIPT_POLL_S,
                           log_every_s: float = 30.0) -> dict:
    """Блокирует, пока не появится РЕАЛЬНЫЙ рецепт -- владелец, внешнее
    ревью пункт 4: "таймаут рецепта не снимает in_flight... блокировка
    держится до подтверждённого результата". Логирует каждые
    log_every_s секунд -- "молчаливого ожидания быть не должно" касается
    и этого ожидания, не только решений "не отправляем"."""
    start = time.monotonic()
    last_log = 0.0
    while True:
        receipt = fetch_real_receipt(tx_hash)
        if receipt is not None:
            return receipt
        now = time.monotonic()
        if now - last_log >= log_every_s:
            print(f"[hotpath][pending] жду РЕАЛЬНЫЙ рецепт {tx_hash} уже {now - start:.0f}с "
                  f"(in_flight держится, таймаут Sender'а это не отменяет)...")
            last_log = now
        time.sleep(poll_interval_s)


def _receipt_gas_used_and_price(receipt: dict) -> tuple[int, int]:
    gas_used = receipt.get("gasUsed", 0)
    gas_used = int(gas_used, 16) if isinstance(gas_used, str) else gas_used
    egp = receipt.get("effectiveGasPrice")
    egp = int(egp, 16) if isinstance(egp, str) else (egp or 0)
    return gas_used, egp


def _receipt_status(receipt: dict) -> int:
    status = receipt.get("status", 0)
    return int(status, 16) if isinstance(status, str) else status


def fetch_contract_owner(contract_address: str) -> str:
    raw = _rpc_call("eth_call", [{"to": contract_address, "data": OWNER_SELECTOR}, "latest"])
    return "0x" + raw[-40:]


def verify_wallet_consistency(contract_address: str, declared_wallet: str, sender_address: str) -> str:
    """Владелец, внешнее ревью, пункт 5: "Перед деплоем зафиксировать:
    owner в деплое = ключ в Sender = кошелёк пилота. Один адрес,
    проверка при старте бота с остановкой при несовпадении."
    Возвращает единый адрес при совпадении; иначе -- SystemExit
    (остановка, не предупреждение)."""
    contract_owner = fetch_contract_owner(contract_address)
    addrs = {
        "owner() контракта": contract_owner.lower(),
        "адрес ключа Sender": sender_address.lower(),
        "заявленный кошелёк пилота (--from-address)": declared_wallet.lower(),
    }
    unique = set(addrs.values())
    if len(unique) != 1:
        detail = "; ".join(f"{label}={addr}" for label, addr in addrs.items())
        raise SystemExit(f"[hotpath] СТОП: owner=Sender-ключ=кошелёк пилота обязаны совпадать -- "
                          f"сейчас НЕ совпадают: {detail}")
    print(f"[hotpath] проверка кошелька: OK, все три адреса совпадают ({contract_owner})")
    return contract_owner


def resolve_pending_tx_if_any(budget: PilotBudget) -> None:
    """Владелец, внешнее ревью, пункт 4: "после перезапуска — сначала
    выяснить судьбу висящей транзакции, потом работать." Вызывается
    ПЕРВЫМ действием в main(), до бутстрапа реестра и до входа в
    основной цикл."""
    if not budget.tx_in_flight or not budget.pending_tx_hash:
        return
    tx_hash = budget.pending_tx_hash
    print(f"[hotpath][restart] обнаружена висящая транзакция с прошлого запуска: {tx_hash} -- "
          f"выясняю судьбу ПЕРЕД началом работы (in_flight держится, пока не решится)...")
    receipt = wait_for_real_receipt(tx_hash)
    gas_used, effective_gas_price = _receipt_gas_used_and_price(receipt)
    gas_cost_wei = gas_used * effective_gas_price
    price = current_weth_usdg_price()
    crossed = budget.record_gas_spend_wei(gas_cost_wei, price)
    status = _receipt_status(receipt)
    print(f"[hotpath][restart] висящая транзакция разрешена: status={status}, gasUsed={gas_used}, "
          f"газ учтён (${budget.cumulative_gas_loss_usd:.2f} накоплено суммарно).")
    for threshold in crossed:
        print(f"[hotpath][ПРОМЕЖУТОЧНЫЙ ОТЧЁТ] накопленные потери на газ достигли ${threshold:.0f} "
              f"(итого: ${budget.cumulative_gas_loss_usd:.2f})")
    budget.set_in_flight(False, None)


class _CoalescingRouteQueue:
    """Владелец, доп. 2026-09-13: "Не обрабатывай накопившуюся очередь
    устаревших состояний. Пока идёт расчёт, новые изменения одного
    маршрута объединяй в последнее доступное состояние." НЕ FIFO-очередь
    событий -- словарь route_id -> (последний_блок, время_получения),
    ПЕРЕЗАПИСЫВАЕМЫЙ при повторном касании того же маршрута, пока он ещё
    не забран оценщиком на обработку."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: dict[str, tuple[int, float]] = {}

    def mark(self, route_id: str, block_number: int, recv_t_monotonic: float) -> None:
        with self._cv:
            prev = self._pending.get(route_id)
            if prev is None or block_number >= prev[0]:
                self._pending[route_id] = (block_number, recv_t_monotonic)
                self._cv.notify()

    def pop_one(self, timeout_s: float = 0.5) -> tuple[str, int, float] | None:
        with self._cv:
            if not self._pending:
                self._cv.wait(timeout=timeout_s)
            if not self._pending:
                return None
            route_id = next(iter(self._pending))
            block_number, recv_t = self._pending.pop(route_id)
            return route_id, block_number, recv_t


class BackgroundRegistryWorker(threading.Thread):
    """Владелец, доп. 2026-09-13: "Обнаружение пулов и проверка
    живучести не должны блокировать обработку торговых сигналов...
    вынеси медленные запросы в фон." Отдельный поток: непрерывное
    обнаружение новых пулов известного арбитражника (инкрементально, по
    новым блокам) + периодическая (раз в минуту, владелец, исходная
    спецификация) полная проверка живучести ВСЕХ маршрутов. НИКОГДА не
    вызывается из горячего пути напрямую -- пишет в общий
    (потокобезопасный, RLock) реестр, который детектор/оценщик читают
    на каждом такте без ожидания этого потока."""

    def __init__(self, registry: RouteRegistry) -> None:
        super().__init__(name="registry-discovery-liveness", daemon=True)
        self.registry = registry
        self._stop_event = threading.Event()
        self._last_checked_block: int | None = None
        self._last_liveness_refresh_wall = time.monotonic()  # первая полная проверка уже была в bootstrap_registry()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                latest = int(_rpc_call("eth_blockNumber", []), 16)
                if self._last_checked_block is None:
                    self._last_checked_block = latest
                elif latest > self._last_checked_block:
                    from_block = self._last_checked_block + 1
                    new_routes = self.registry.discover_new_arbitrageur_routes(from_block, latest)
                    for route in new_routes:
                        res = check_route_liveness(route, latest)
                        res["checked_at_block"] = latest
                        res["checked_at_wall"] = time.time()
                        self.registry.set_liveness(route.route_id, res)
                        print(f"[registry-worker] новый маршрут от арбитражника (без перезапуска): "
                              f"{route.label} live={res['live']}")
                    self._last_checked_block = latest

                now = time.monotonic()
                if now - self._last_liveness_refresh_wall >= LIVENESS_REFRESH_INTERVAL_S:
                    results = self.registry.refresh_liveness_all(latest)
                    n_live = sum(1 for r in results.values() if r.get("live"))
                    print(f"[registry-worker] полная проверка живучести: {n_live}/{len(results)} живых "
                          f"(блок {latest})")
                    self._last_liveness_refresh_wall = now
            except Exception as exc:  # noqa: BLE001
                print(f"[registry-worker] ошибка фона (честно, не молчим): {exc}", file=sys.stderr)
            self._stop_event.wait(DISCOVERY_POLL_INTERVAL_S)


class HotPath:
    def __init__(self, registry: RouteRegistry, contract_address: str, from_address: str,
                 budget: PilotBudget, attempt_table: AttemptTable, reason_log: ReasonLog,
                 sender=None, dry_run: bool = True) -> None:
        self.registry = registry
        self.contract_address = contract_address
        self.from_address = from_address
        self.budget = budget
        self.attempt_table = attempt_table
        self.reason_log = reason_log
        self.sender = sender  # task5_bot_sender.Sender, только если не dry_run
        self.dry_run = dry_run
        self.last_checked_block: int | None = None
        self._queue = _CoalescingRouteQueue()
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    # --- ДЕТЕКТОР: быстрый, только опрос новых Swap-логов
    # отслеживаемых пулов -- НЕ пересчитывает/отправляет сам. ---
    def poll_once(self) -> None:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
        if self.last_checked_block is None:
            self.last_checked_block = latest - 1
        if latest <= self.last_checked_block:
            return
        from_block = self.last_checked_block + 1

        pool_ids = self.registry.snapshot_pool_ids()
        if not pool_ids:
            self.last_checked_block = latest
            return

        recv_t_monotonic = time.monotonic()
        logs = list(_chunked_get_logs(
            from_block, latest, topics=[SWAP_TOPIC0, pool_ids], address=POOL_MANAGER,
            chunk_size=latest - self.last_checked_block,
        ))
        self.last_checked_block = latest

        touched_pool_ids = {log["topics"][1] for log in logs}
        for pid in touched_pool_ids:
            for route in self.registry.routes_touched_by_pool(pid):
                self._queue.mark(route.route_id, latest, recv_t_monotonic)

    # --- ОЦЕНЩИК/ОТПРАВИТЕЛЬ: отдельный поток, разбирает очередь по
    # одному маршруту, всегда по САМОМУ СВЕЖЕМУ известному состоянию. ---
    def evaluator_loop(self) -> None:
        while not self._stop_event.is_set():
            item = self._queue.pop_one(timeout_s=0.5)
            if item is None:
                continue
            route_id, block_number, recv_t_monotonic = item
            route = self.registry.get_route(route_id)
            if route is None:
                continue
            if not self.registry.is_live(route_id):
                self.reason_log.log(route_id, route.label, REASON_NO_LIQUIDITY,
                                     "маршрут временно исключён (последняя проверка живучести)")
                continue
            try:
                self._evaluate_and_maybe_send(route, block_number, recv_t_monotonic)
            except Exception as exc:  # noqa: BLE001
                print(f"[hotpath] ошибка при оценке маршрута {route.label}: {exc}", file=sys.stderr)

    def _evaluate_and_maybe_send(self, route: RouteCycle, block_number: int, recv_t_monotonic: float) -> None:
        recompute = recompute_route(route, block_number)
        if not recompute["ok"]:
            self.reason_log.log(route.route_id, route.label, recompute["reason"], recompute["detail"])
            return

        if recompute["profit_raw"] <= 0:
            self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                 f"лучший размер {recompute['amount_in']}, профит(до газа)={recompute['profit_raw']} "
                                 f"(блок {block_number})")
            return

        first_amount_specified = -recompute["amount_in"]
        # Контракт теперь ТРЕБУЕТ minProfit>0 (внешнее ревью, пункт 1) --
        # 1 raw-единица exit_token как минимально ненулевой порог; сам
        # решение "отправлять ли" принимается ВЫШЕ по профиту ПОСЛЕ газа,
        # не по этому порогу контракта.
        calldata = build_execute_cycle_calldata(route, first_amount_specified, min_profit=1)

        gas_res = estimate_gas(self.contract_address, calldata, self.from_address)
        if not gas_res["ok"]:
            self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, gas_res["error"])
            return

        gas_price = int(_rpc_call("eth_gasPrice", []), 16)
        gas_cost_wei_est = gas_res["gas_estimate"] * gas_price
        gas_cost_eth_est = gas_cost_wei_est / 1e18
        exit_decimals = 6 if route.exit_token.lower() == USDG.lower() else 18
        profit_before_gas_in_exit_units = recompute["profit_raw"] / 10**exit_decimals

        weth_usdg_price = current_weth_usdg_price()
        if route.exit_token.lower() == NATIVE:
            profit_after_gas_in_exit_units = profit_before_gas_in_exit_units - gas_cost_eth_est
        else:
            # Газ платится в нативном ETH, прибыль -- в exit_token (USDG) --
            # переводим газ в USDG через РЕАЛЬНУЮ текущую цену, не гадаем
            # курс и не молчим про газ для этой ветки.
            if weth_usdg_price is None:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED,
                                     "не удалось получить живую цену WETH/USDG для перевода газа")
                return
            profit_after_gas_in_exit_units = profit_before_gas_in_exit_units - gas_cost_eth_est * weth_usdg_price

        if profit_after_gas_in_exit_units <= 0:
            self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                 f"профит до газа={profit_before_gas_in_exit_units:.6f}, газ~{gas_cost_eth_est:.6f} "
                                 f"ETH -- после газа {profit_after_gas_in_exit_units:.6f} <= 0 (блок {block_number})")
            return

        can_send, why = self.budget.can_send()
        if not can_send:
            self.reason_log.log(route.route_id, route.label, why, "")
            return

        if self.dry_run or self.sender is None:
            latency_s = time.monotonic() - recv_t_monotonic
            print(f"[hotpath][DRY-RUN] отправил бы: {route.label} размер={recompute['amount_in']} "
                  f"профит_после_газа~{profit_after_gas_in_exit_units:.6f} latency={latency_s*1000:.0f}мс "
                  f"блок={block_number}")
            row = AttemptTableRow(
                ts_wall=time.time(), route_label=route.label, route_id=route.route_id,
                size_in_raw=recompute["amount_in"], exit_token=route.exit_token,
                expected_profit_after_gas=profit_after_gas_in_exit_units,
                latency_recv_to_send_s=latency_s, tx_hash=None, result="dry_run_would_send",
                actual_gain_base_asset=None, gas_used=gas_res["gas_estimate"], gas_cost_native=gas_cost_eth_est,
                cumulative_gas_loss_usd=self.budget.cumulative_gas_loss_usd,
                cumulative_net_pnl_usd=self.budget.cumulative_net_pnl_usd,
                computed_at_block=block_number, state_age_blocks=0,
            )
            self.attempt_table.write(row)
            return

        # --- ФИНАЛЬНАЯ ПРОВЕРКА АКТУАЛЬНОЙ ИСПОЛНИМОСТИ (владелец,
        # доп.: "Перед отправкой сохраняй финальную проверку актуальной
        # исполнимости.") -- решение выше принято на block_number,
        # который к моменту готовности реальной отправки мог УЖЕ не
        # быть самым свежим (коалесцирование в очереди + время расчёта
        # газа/бюджета). Если состояние успело устареть -- пересчитываем
        # НА САМОМ СВЕЖЕМ блоке и отправляем ТОЛЬКО если всё ещё
        # прибыльно. ---
        fresh_latest = int(_rpc_call("eth_blockNumber", []), 16)
        state_age_blocks = max(0, fresh_latest - block_number)
        if fresh_latest > block_number:
            final_check = recompute_route(route, fresh_latest)
            if not final_check["ok"] or final_check["profit_raw"] <= 0:
                self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                     f"финальная проверка на блоке {fresh_latest} (решение было на {block_number}, "
                                     f"возраст состояния {state_age_blocks} блоков) -- уже не прибыльно, не отправляем")
                return
            # ВСЕГДА пересобираем calldata/газ/профит под РЕЗУЛЬТАТ финальной
            # проверки (не только когда amount_in изменился) -- она сделана
            # на более свежем блоке, её цифры авторитетнее исходных, даже
            # если лучший размер формально совпал.
            recompute = final_check
            first_amount_specified = -recompute["amount_in"]
            calldata = build_execute_cycle_calldata(route, first_amount_specified, min_profit=1)
            block_number = fresh_latest

            gas_res = estimate_gas(self.contract_address, calldata, self.from_address)
            if not gas_res["ok"]:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, gas_res["error"])
                return
            gas_price = int(_rpc_call("eth_gasPrice", []), 16)
            gas_cost_eth_est = gas_res["gas_estimate"] * gas_price / 1e18
            weth_usdg_price = current_weth_usdg_price()
            profit_before_gas_in_exit_units = recompute["profit_raw"] / 10**exit_decimals
            if route.exit_token.lower() == NATIVE:
                profit_after_gas_in_exit_units = profit_before_gas_in_exit_units - gas_cost_eth_est
            else:
                if weth_usdg_price is None:
                    self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED,
                                         "финальная проверка: не удалось получить живую цену WETH/USDG")
                    return
                profit_after_gas_in_exit_units = profit_before_gas_in_exit_units - gas_cost_eth_est * weth_usdg_price
            if profit_after_gas_in_exit_units <= 0:
                self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                     f"финальная проверка на блоке {fresh_latest}: после газа "
                                     f"{profit_after_gas_in_exit_units:.6f} <= 0 -- не отправляем")
                return

        # --- Резерв ПЕРЕД отправкой (внешнее ревью, пункт 3): "резервировать
        # максимальный расход следующей транзакции (gas_limit x
        # maxFeePerGas), не только считать потраченное". Использует АКТУАЛЬНУЮ
        # (пере)оценку газа для ИМЕННО ТОЙ calldata, что реально отправится. ---
        max_fee_per_gas_wei = estimate_max_fee_per_gas_wei()
        ok_reserve, why_reserve = self.budget.reserve_for_send(gas_res["gas_estimate"], max_fee_per_gas_wei,
                                                                weth_usdg_price)
        if not ok_reserve:
            self.reason_log.log(route.route_id, route.label, why_reserve, "")
            return

        # --- Реальная отправка -- ТОЛЬКО через уже существующий Sender ---
        route_tokens = sorted({leg.currency0.lower() for leg in route.legs} | {leg.currency1.lower() for leg in route.legs})
        pre_balances = {t: _token_balance(t, self.contract_address) for t in route_tokens}

        latency_s = time.monotonic() - recv_t_monotonic
        self.budget.set_in_flight(True, pending_tx_hash=None)
        result = self.sender.send_cycle(self.contract_address, calldata, gas_res["gas_estimate"])

        if not result.tx_hash:
            # Отправка не ушла в сеть вовсе -- ничего не потрачено.
            self.budget.set_in_flight(False, None)
            self.budget.release_reservation()
            self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED,
                                 f"send_cycle не вернул tx_hash: {result.error}")
            return

        # tx_hash есть -- персистим ЕГО СРАЗУ (переживает рестарт, пункт 4).
        self.budget.set_in_flight(True, pending_tx_hash=result.tx_hash)

        # --- Пункт 3: "Ошибка RPC после отправки — расход записать до
        # любых других действий." Получаем РЕАЛЬНЫЙ рецепт САМИ (Sender
        # не отдаёт effectiveGasPrice) -- если Sender сам уже дождался
        # (обычный путь), рецепт уже на цепи и вернётся мгновенно; если
        # Sender сам не дождался (таймаут внутри send_cycle) -- ждём и
        # опрашиваем САМИ, сколько потребуется (пункт 4: таймаут НЕ
        # снимает in_flight). ---
        receipt = wait_for_real_receipt(result.tx_hash)
        gas_used, effective_gas_price = _receipt_gas_used_and_price(receipt)
        gas_cost_wei = gas_used * effective_gas_price
        weth_usdg_price_for_budget = current_weth_usdg_price()
        crossed = self.budget.record_gas_spend_wei(gas_cost_wei, weth_usdg_price_for_budget)
        tx_status = _receipt_status(receipt)
        self.budget.set_in_flight(False, None)  # рецепт получен -- судьба известна, разблокируем
        for threshold in crossed:
            print(f"[hotpath][ПРОМЕЖУТОЧНЫЙ ОТЧЁТ] накопленные потери на газ достигли ${threshold:.0f} "
                  f"(итого: ${self.budget.cumulative_gas_loss_usd:.2f})")

        # --- Остальное (балансы токенов, сверка учёта, чистый PnL,
        # запись строки) -- В ОТДЕЛЬНОМ try/except: сбой здесь НЕ должен
        # потерять уже записанный расход выше. ---
        actual_gain_base_asset = None
        try:
            post_balances = {t: _token_balance(t, self.contract_address) for t in route_tokens}
            actual_gain_raw = post_balances[route.exit_token.lower()] - pre_balances[route.exit_token.lower()]
            actual_gain_base_asset = actual_gain_raw / 10**exit_decimals

            ok_tokens, why_tokens = check_no_unexpected_token_spend(pre_balances, post_balances, route.exit_token)
            if not ok_tokens:
                self.budget.halt(why_tokens)
                print(f"[hotpath] СТОП: {why_tokens}")

            if tx_status == 1:
                ok_acct, why_acct = check_accounting_consistency(recompute["profit_raw"], actual_gain_raw)
                if not ok_acct:
                    self.budget.halt(why_acct)
                    print(f"[hotpath] СТОП: {why_acct}")

                # Пункт 6 (мелочи ревью): накопленная ЧИСТАЯ прибыль
                # реально считается, не остаётся незаполненным полем.
                # Допущение этого проекта (см. Sender.py::record_outcome,
                # MAX_DAILY_LOSS_USD -- везде USDG трактуется как $1:1,
                # не изобретено здесь заново): USDG ~= USD напрямую.
                if route.exit_token.lower() == USDG.lower():
                    actual_gain_usd = actual_gain_base_asset
                elif weth_usdg_price_for_budget is not None:
                    actual_gain_usd = actual_gain_base_asset * weth_usdg_price_for_budget
                else:
                    actual_gain_usd = None
                gas_cost_usd = (gas_cost_wei / 1e18 * weth_usdg_price_for_budget
                                 if weth_usdg_price_for_budget is not None else None)
                if actual_gain_usd is not None and gas_cost_usd is not None:
                    self.budget.record_net_pnl_usd(actual_gain_usd - gas_cost_usd)
        except Exception as exc:  # noqa: BLE001
            print(f"[hotpath] пост-обработка после отправки упала (газ уже учтён выше): {exc}", file=sys.stderr)

        row = AttemptTableRow(
            ts_wall=time.time(), route_label=route.label, route_id=route.route_id,
            size_in_raw=recompute["amount_in"], exit_token=route.exit_token,
            expected_profit_after_gas=profit_after_gas_in_exit_units,
            latency_recv_to_send_s=latency_s, tx_hash=result.tx_hash,
            result="success" if tx_status == 1 else "reverted",
            actual_gain_base_asset=actual_gain_base_asset if tx_status == 1 else None,
            gas_used=gas_used, gas_cost_native=gas_cost_wei / 1e18,
            cumulative_gas_loss_usd=self.budget.cumulative_gas_loss_usd,
            cumulative_net_pnl_usd=self.budget.cumulative_net_pnl_usd,
            computed_at_block=block_number, state_age_blocks=state_age_blocks,
        )
        self.attempt_table.write(row)


def _print_status_report(label: str, registry: RouteRegistry, budget: PilotBudget, attempt_table: AttemptTable,
                          pilot_start_wall: float) -> None:
    """Владелец, доп. 2026-09-13: "Отчёты — на $5, $10 и по окончании
    часа, даже если отправок нет." -- снимок состояния, печатается
    БЕЗУСЛОВНО (не только при событии траты)."""
    elapsed_s = time.time() - pilot_start_wall
    print(f"[hotpath][ОТЧЁТ: {label}] прошло {elapsed_s/60:.1f} мин пилота | "
          f"маршрутов: {len(registry.routes)} (живых: {len(registry.live_routes())}) | "
          f"попыток отправки: {attempt_table.count} | "
          f"газ потрачено: ${budget.cumulative_gas_loss_usd:.2f} / ${BUDGET_STOP_USD:.0f} "
          f"({budget.cumulative_gas_loss_eth:.6f} ETH) | "
          f"чистый PnL: ${budget.cumulative_net_pnl_usd:.2f} | "
          f"в полёте: {budget.tx_in_flight} ({budget.pending_tx_hash or '-'}) | "
          f"остановлен: {budget.halted} ({budget.halt_reason or '-'})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contract-address", required=True, help="Адрес задеплоенного ClosedCycleExecutorV4")
    ap.add_argument("--from-address", required=True,
                     help="Заявленный адрес кошелька пилота -- ДОЛЖЕН совпасть с owner() контракта и "
                          "адресом ключа Sender (внешнее ревью, пункт 5), иначе стоп при старте")
    ap.add_argument("--confirm-mainnet", action="store_true", help="Без этого -- всегда dry-run")
    ap.add_argument("--duration-seconds", type=float, default=None)
    args = ap.parse_args()

    budget = PilotBudget()
    attempt_table = AttemptTable()
    reason_log = ReasonLog()

    # --- Пункт 4: "после перезапуска — сначала выяснить судьбу висящей
    # транзакции, потом работать" -- ПЕРВОЕ действие, до всего. ---
    resolve_pending_tx_if_any(budget)
    if budget.halted:
        print(f"[hotpath] ОСТАНОВЛЕН ещё до старта (см. состояние бюджета): {budget.halt_reason}")
        return

    sender = None
    if args.confirm_mainnet:
        import task5_bot_sender
        sender = task5_bot_sender.Sender()
        # --- Пункт 5: owner=Sender-ключ=кошелёк пилота, один адрес. ---
        verify_wallet_consistency(args.contract_address, args.from_address, sender.address)

    # Владелец, 2026-09-13: "подключай полный реестр (сид + обнаружение
    # из арбитражника) в task5_v4_hotpath.py::main(). Работать по всем
    # живым маршрутам, не по двум мёртвым сидам." -- bootstrap_registry()
    # это ТОТ ЖЕ код, что уже реально прогонялся диагностическим
    # запуском task5_v4_route_registry.py (166 маршрутов, 117 живых,
    # 88 новых пулов от арбитражника на 2026-09-13), не дублирован.
    print("[hotpath] бутстрап реестра (сид + обнаружение пулов арбитражника)...")
    registry, latest = bootstrap_registry()
    pilot_start_wall = time.time()  # владелец, доп.: час пилота -- ПОСЛЕ бутстрапа, не с момента старта процесса

    # --- Стартовый баннер (владелец, доп.): "При старте выводи
    # фактический адрес подписанта, owner контракта, число известных/
    # живых маршрутов, остаток общего бюджета $20 и наличие
    # незавершённой транзакции." ---
    try:
        contract_owner = fetch_contract_owner(args.contract_address)  # read-only -- показываем в ОБОИХ режимах
    except Exception as exc:  # noqa: BLE001
        contract_owner = f"(не удалось прочитать: {exc})"
    print(f"[hotpath] === СТАРТ ПИЛОТА ===")
    print(f"[hotpath]   режим: {'LIVE' if args.confirm_mainnet else 'DRY-RUN'}")
    print(f"[hotpath]   адрес подписанта (Sender): {sender.address if sender is not None else '(dry-run -- нет Sender)'}")
    print(f"[hotpath]   owner() контракта: {contract_owner}")
    print(f"[hotpath]   контракт: {args.contract_address}")
    print(f"[hotpath]   маршрутов известно: {len(registry.routes)}, живых: {len(registry.live_routes())}")
    print(f"[hotpath]   остаток бюджета газа: ${BUDGET_STOP_USD - budget.cumulative_gas_loss_usd:.2f} "
          f"из ${BUDGET_STOP_USD:.0f} (потрачено ${budget.cumulative_gas_loss_usd:.2f})")
    print(f"[hotpath]   незавершённая транзакция: {budget.tx_in_flight} ({budget.pending_tx_hash or '-'})")
    print(f"[hotpath] =====================")

    background_worker = BackgroundRegistryWorker(registry)
    background_worker.start()

    hotpath = HotPath(registry, args.contract_address, args.from_address, budget, attempt_table,
                       reason_log, sender=sender, dry_run=not args.confirm_mainnet)
    evaluator_thread = threading.Thread(target=hotpath.evaluator_loop, name="evaluator", daemon=True)
    evaluator_thread.start()

    hour_report_done = False
    last_periodic_report = time.monotonic()

    start = time.time()
    try:
        while True:
            can_send, why = budget.can_send()
            if not can_send and why not in ("уже есть неподтверждённая транзакция",):
                print(f"[hotpath] ОСТАНОВЛЕН: {why}")
                _print_status_report("СТОП", registry, budget, attempt_table, pilot_start_wall)
                break
            try:
                hotpath.poll_once()
            except Exception as exc:  # noqa: BLE001
                print(f"[hotpath] ошибка в цикле опроса: {exc}", file=sys.stderr)

            # Владелец, доп.: "Отчёты — на $5, $10 и по окончании часа,
            # даже если отправок нет." -- $5/$10 уже печатаются inline в
            # момент реального списания (см. record_gas_spend_wei); час
            # -- безусловный отчёт по таймеру, независимо от трат.
            elapsed_pilot_s = time.time() - pilot_start_wall
            if elapsed_pilot_s >= PILOT_HOUR_S and not hour_report_done:
                _print_status_report("ЧАС ПИЛОТА ЗАВЕРШЁН", registry, budget, attempt_table, pilot_start_wall)
                hour_report_done = True

            now_mono = time.monotonic()
            if now_mono - last_periodic_report >= 300.0:  # каждые 5 минут -- видимость живого процесса
                _print_status_report("периодический", registry, budget, attempt_table, pilot_start_wall)
                last_periodic_report = now_mono

            if args.duration_seconds is not None and time.time() - start >= args.duration_seconds:
                break
            time.sleep(POLL_INTERVAL_S)
    finally:
        background_worker.stop()
        hotpath.stop()


if __name__ == "__main__":
    main()
