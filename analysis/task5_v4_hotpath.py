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
смысле требования, честно отмечено как компромисс v1. Опрос блоков
(не WS-подписка) -- каждые POLL_INTERVAL_S, ~100мс блоктайм этой цепи
даёт задержку реакции порядка секунд, не миллисекунд, до отдельной
доработки на WS eth_subscribe (нужна реальная проверка на Ohio).

Учёт/лимиты -- task5_v4_pilot_accounting.py. Подпись/отправка --
ИСКЛЮЧИТЕЛЬНО task5_bot_sender.py::Sender.send_cycle (эта сессия эту
логику не пишет и не меняет, только вызывает с готовыми to/calldata/
gas_limit)."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _chunked_get_logs, _rpc_call, topic0  # noqa: E402
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402
from task5_v4_pilot_accounting import (  # noqa: E402
    REASON_CALC_ERROR, REASON_NO_LIQUIDITY, REASON_NO_PROFITABLE_CYCLE, REASON_SIMULATION_FAILED,
    AttemptTable, AttemptTableRow, PilotBudget, ReasonLog, check_accounting_consistency,
    check_no_unexpected_token_spend,
)
from task5_v4_quote_replay import quote_exact_input_single  # noqa: E402
from task5_v4_route_registry import RouteCycle, RouteRegistry, USDG, seed_routes  # noqa: E402

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

POLL_INTERVAL_S = 0.5  # = alchemy_fallback._MIN_REQUEST_INTERVAL_S -- опрашивать чаще
                       # не даст выигрыша (eth_blockNumber всё равно упрётся в тот же
                       # самотроттлинг), реже -- добавляет задержку детекции сверх нужного.
LIVENESS_REFRESH_INTERVAL_S = 60.0  # владелец: "раз в минуту"

# Собственный подбор размера (та же сетка, что task5_v4_observation_hour.py,
# см. её докстринг про то, что размер НЕ подставляется вслепую).
SIZE_GRID_BY_START_TOKEN = {
    USDG.lower(): [1_000_000, 3_000_000, 5_000_000, 7_000_000, 10_000_000, 15_000_000, 20_000_000, 30_000_000],
    "0x0000000000000000000000000000000000000000": [int(x * 1e18) for x in
                                                     (0.02, 0.05, 0.08, 0.1, 0.15, 0.2, 0.3)],
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


def _token_balance(token: str, account: str) -> int:
    """Баланс контракта по токену -- нативный ETH через eth_getBalance,
    ERC20 через balanceOf(address) (селектор 0x70a08231, keccak-проверен
    ранее в проекте). Используется ДО/ПОСЛЕ реальной попытки для
    check_no_unexpected_token_spend/check_accounting_consistency --
    честная проверка по факту, не по ожиданию."""
    if token.lower() == "0x0000000000000000000000000000000000000000":
        return int(_rpc_call("eth_getBalance", [account, "latest"]), 16)
    padded = account[2:].rjust(64, "0")
    raw = _rpc_call("eth_call", [{"to": token, "data": "0x70a08231" + padded}, "latest"])
    return int(raw, 16)


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
        self.last_liveness_refresh_wall = 0.0

    def poll_once(self) -> None:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
        if self.last_checked_block is None:
            self.last_checked_block = latest - 1

        now = time.monotonic()
        if now - self.last_liveness_refresh_wall >= LIVENESS_REFRESH_INTERVAL_S:
            self.registry.refresh_liveness_all(latest)
            self.last_liveness_refresh_wall = now

        if latest <= self.last_checked_block:
            return

        pool_ids = list(self.registry.pool_to_routes.keys())
        if not pool_ids:
            self.last_checked_block = latest
            return

        recv_t_monotonic = time.monotonic()
        logs = list(_chunked_get_logs(
            self.last_checked_block + 1, latest,
            topics=[SWAP_TOPIC0, pool_ids], address=POOL_MANAGER,
            chunk_size=latest - self.last_checked_block,
        ))
        self.last_checked_block = latest

        touched_pool_ids = {log["topics"][1] for log in logs}
        affected_route_ids: set[str] = set()
        for pid in touched_pool_ids:
            for route in self.registry.routes_touched_by_pool(pid):
                affected_route_ids.add(route.route_id)

        for route_id in affected_route_ids:
            route = self.registry.routes[route_id]
            if not self.registry.liveness.get(route_id, {}).get("live", True):
                self.reason_log.log(route_id, route.label, REASON_NO_LIQUIDITY,
                                     "маршрут временно исключён (последняя проверка живучести)")
                continue
            self._evaluate_and_maybe_send(route, latest, recv_t_monotonic)

    def _evaluate_and_maybe_send(self, route: RouteCycle, block_number: int, recv_t_monotonic: float) -> None:
        calc_start = time.monotonic()
        recompute = recompute_route(route, block_number)
        if not recompute["ok"]:
            self.reason_log.log(route.route_id, route.label, recompute["reason"], recompute["detail"])
            return

        if recompute["profit_raw"] <= 0:
            self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                 f"лучший размер {recompute['amount_in']}, профит(до газа)={recompute['profit_raw']}")
            return

        first_amount_specified = -recompute["amount_in"]
        calldata = build_execute_cycle_calldata(route, first_amount_specified, min_profit=0)

        gas_res = estimate_gas(self.contract_address, calldata, self.from_address)
        if not gas_res["ok"]:
            self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, gas_res["error"])
            return

        gas_price = int(_rpc_call("eth_gasPrice", []), 16)
        gas_cost_wei = gas_res["gas_estimate"] * gas_price
        gas_cost_eth = gas_cost_wei / 1e18
        exit_decimals = 6 if route.exit_token.lower() == USDG.lower() else 18
        profit_before_gas_in_exit_units = recompute["profit_raw"] / 10**exit_decimals

        if route.exit_token.lower() == "0x0000000000000000000000000000000000000000":
            profit_after_gas_in_exit_units = profit_before_gas_in_exit_units - gas_cost_eth
        else:
            # Газ платится в нативном ETH, прибыль -- в exit_token (USDG) --
            # переводим газ в USDG через РЕАЛЬНУЮ текущую цену (тот же
            # slot0()-механизм, что уже используется в проекте для живой
            # цены), не гадаем курс и не молчим про газ для этой ветки.
            weth_usdg_price = current_weth_usdg_price()
            if weth_usdg_price is None:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED,
                                     "не удалось получить живую цену WETH/USDG для перевода газа")
                return
            profit_after_gas_in_exit_units = profit_before_gas_in_exit_units - gas_cost_eth * weth_usdg_price

        if profit_after_gas_in_exit_units <= 0:
            self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                 f"профит до газа={profit_before_gas_in_exit_units:.6f}, газ~{gas_cost_eth:.6f} ETH -- "
                                 f"после газа {profit_after_gas_in_exit_units:.6f} <= 0")
            return

        can_send, why = self.budget.can_send()
        if not can_send:
            self.reason_log.log(route.route_id, route.label, why, "")
            return

        latency_s = time.monotonic() - recv_t_monotonic

        if self.dry_run or self.sender is None:
            print(f"[hotpath][DRY-RUN] отправил бы: {route.label} размер={recompute['amount_in']} "
                  f"профит_после_газа~{profit_after_gas_in_exit_units:.6f} latency={latency_s*1000:.0f}мс")
            row = AttemptTableRow(
                ts_wall=time.time(), route_label=route.label, route_id=route.route_id,
                size_in_raw=recompute["amount_in"], exit_token=route.exit_token,
                expected_profit_after_gas=profit_after_gas_in_exit_units,
                latency_recv_to_send_s=latency_s, tx_hash=None, result="dry_run_would_send",
                actual_gain_base_asset=None, gas_used=gas_res["gas_estimate"], gas_cost_native=gas_cost_eth,
                cumulative_gas_loss_usd=self.budget.cumulative_gas_loss_usd,
            )
            self.attempt_table.write(row)
            return

        # --- Реальная отправка -- ТОЛЬКО через уже существующий Sender ---
        route_tokens = sorted({leg.currency0.lower() for leg in route.legs} | {leg.currency1.lower() for leg in route.legs})
        pre_balances = {t: _token_balance(t, self.contract_address) for t in route_tokens}

        self.budget.set_in_flight(True)
        try:
            result = self.sender.send_cycle(self.contract_address, calldata, gas_res["gas_estimate"])
        finally:
            self.budget.set_in_flight(False)

        post_balances = {t: _token_balance(t, self.contract_address) for t in route_tokens}
        actual_gain_raw = post_balances[route.exit_token.lower()] - pre_balances[route.exit_token.lower()]
        actual_gain_base_asset = actual_gain_raw / 10**exit_decimals

        gas_cost_native_actual = ((result.gas_used or 0) * gas_price / 1e18) if result.gas_used else None
        weth_usdg_price_for_budget = current_weth_usdg_price()
        gas_cost_usd = (gas_cost_native_actual * weth_usdg_price_for_budget
                        if gas_cost_native_actual is not None and weth_usdg_price_for_budget is not None else None)
        if gas_cost_usd is not None:
            crossed = self.budget.record_gas_spend(gas_cost_usd)
            for threshold in crossed:
                print(f"[hotpath][ПРОМЕЖУТОЧНЫЙ ОТЧЁТ] накопленные потери на газ достигли ${threshold:.0f} "
                      f"(итого: ${self.budget.cumulative_gas_loss_usd:.2f})")

        # Остановка при непредвиденном расходе токена / нарушении учёта
        # (владелец, лимиты пилота) -- проверяем ПОСЛЕ каждой попытки,
        # успешной или нет.
        ok_tokens, why_tokens = check_no_unexpected_token_spend(pre_balances, post_balances, route.exit_token)
        if not ok_tokens:
            self.budget.halt(why_tokens)
            print(f"[hotpath] СТОП: {why_tokens}")

        if result.ok:
            ok_acct, why_acct = check_accounting_consistency(recompute["profit_raw"], actual_gain_raw)
            if not ok_acct:
                self.budget.halt(why_acct)
                print(f"[hotpath] СТОП: {why_acct}")

        row = AttemptTableRow(
            ts_wall=time.time(), route_label=route.label, route_id=route.route_id,
            size_in_raw=recompute["amount_in"], exit_token=route.exit_token,
            expected_profit_after_gas=profit_after_gas_in_exit_units,
            latency_recv_to_send_s=latency_s, tx_hash=result.tx_hash,
            result="success" if result.ok else ("reverted" if result.status == 0 else "send_error"),
            actual_gain_base_asset=actual_gain_base_asset if result.ok else None,
            gas_used=result.gas_used, gas_cost_native=gas_cost_native_actual,
            cumulative_gas_loss_usd=self.budget.cumulative_gas_loss_usd,
        )
        self.attempt_table.write(row)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contract-address", required=True, help="Адрес задеплоенного ClosedCycleExecutorV4")
    ap.add_argument("--from-address", required=True, help="Адрес кошелька пилота (для eth_estimateGas 'from')")
    ap.add_argument("--confirm-mainnet", action="store_true", help="Без этого -- всегда dry-run")
    ap.add_argument("--duration-seconds", type=float, default=None)
    args = ap.parse_args()

    registry = RouteRegistry()
    registry.add_routes(seed_routes())
    latest = int(_rpc_call("eth_blockNumber", []), 16)
    registry.refresh_liveness_all(latest)
    print(f"[hotpath] реестр: {len(registry.routes)} маршрутов, живых: {len(registry.live_routes())}")

    budget = PilotBudget()
    attempt_table = AttemptTable()
    reason_log = ReasonLog()

    sender = None
    if args.confirm_mainnet:
        import task5_bot_sender
        sender = task5_bot_sender.Sender()

    hotpath = HotPath(registry, args.contract_address, args.from_address, budget, attempt_table,
                       reason_log, sender=sender, dry_run=not args.confirm_mainnet)

    start = time.time()
    print(f"[hotpath] режим: {'LIVE' if args.confirm_mainnet else 'DRY-RUN'}")
    while True:
        can_send, why = budget.can_send()
        if not can_send and why != "уже есть неподтверждённая транзакция":
            print(f"[hotpath] ОСТАНОВЛЕН: {why}")
            break
        try:
            hotpath.poll_once()
        except Exception as exc:  # noqa: BLE001
            print(f"[hotpath] ошибка в цикле опроса: {exc}", file=sys.stderr)
        if args.duration_seconds is not None and time.time() - start >= args.duration_seconds:
            break
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
