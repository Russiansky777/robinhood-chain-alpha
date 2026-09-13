#!/usr/bin/env python3
"""Задача 5, живой пилот -- ГОРЯЧИЙ ПУТЬ (владелец, 2026-09-12):
"на каждый своп в пуле из живого маршрута -- пересчёт цикла в памяти,
подбор размера, и если плюс после газа -- отправка. Без повторного
поиска маршрута."

ЧЕСТНАЯ ОГОВОРКА ПО СКОРОСТИ (не скрываем): для V4 своп -- это вызов
`unlock(bytes)` синглтон-PoolManager с ПРОИЗВОЛЬНЫМ (не декодируемым
обобщённо) форматом данных каждого вызывающего контракта (см.
task5_v4_observation_hour.py, докстринг "ПОЧЕМУ НЕ ДЕКОДИРУЕМ ЧУЖИЕ
CALLDATA"). Триггер здесь -- РЕАЛЬНОЕ (уже подтверждённое)
`PoolManager.Swap`-событие ЛЮБОГО участника в отслеживаемом пуле (после
исполнения), не предисполнительная calldata. Пересчёт -- через реальный
V4Quoter (eth_call), не переизобретённая формула AMM (владелец,
2026-09-13, доп.: "Не расширяем задачу до локальной математики" --
сознательно не делаем, только оценено по объёму отдельно).

ЧЕСТНО О ФАКТИЧЕСКОМ ИСТОЧНИКЕ СИГНАЛОВ (внешнее ревью, второй раунд,
2026-09-13: "Не называй RPC-опрос логов Swap-событиями из sequencer
feed"): это ПЕРИОДИЧЕСКИЙ RPC-ОПРОС `eth_getLogs`
(alchemy_fallback._chunked_get_logs, раз в POLL_INTERVAL_S), НЕ
WebSocket-подписка (`eth_subscribe`) и НЕ клиент sequencer-фида
(task5_bot_feed_client.py -- не импортируется здесь). Задержка детекции
= время до следующего опроса (<=POLL_INTERVAL_S) + время самого
RPC-запроса; не путать с "latency" в таблице попыток -- см. её
определение в _evaluate_and_maybe_send.

ПРАВКА 2026-09-13, ПЕРВЫЙ РАУНД внешнего ревью (архитектура, бюджет-v1,
in-flight-v1, кошелёк, immutable-верификатор, живучесть в фон) --
см. git-историю; не переделывается здесь без причины.

ПРАВКА 2026-09-13, ВТОРОЙ РАУНД внешнего ревью:

  1. ЧЕСТНЫЙ PnL: PilotBudget.finalize_gas()/finalize_profit_and_close()
     (task5_v4_pilot_accounting.py) -- газ учитывается ВСЕГДА и СРАЗУ
     (успех и откат), прибыль -- только для успеха, отдельным
     идемпотентным шагом. Никаких повторных начислений при повторном
     receipt/рестарте (флаги gas_recorded/finalized в pending).

  2/3. ПОДГОТОВКА/РЕЗЕРВ/ОТПРАВКА СОГЛАСОВАНЫ: task5_bot_sender.py
     теперь даёт prepare_transaction_fields -> [ЗДЕСЬ проверяем бюджет
     по ЕЁ maxFeePerGas/gas] -> sign_prepared_transaction (tx_hash
     ЛОКАЛЬНО, ДО сети) -> begin_attempt (сохраняем контекст ДО первого
     сетевого обращения) -> submit_prepared. "Нет tx_hash -- значит не
     ушла" убрано полностью -- хэш известен всегда, submit_prepared
     всегда ждёт РЕАЛЬНЫЙ рецепт по нему, unresolved значит "неизвестно",
     не "провалилось".

  4. БЮДЖЕТ: атомарные записи, обязательные поля рецепта не в 0 молча,
     курс+момент его получения сохраняются при резерве, halt при
     невозможности достоверно продвинуть USD-бюджет или восстановить
     факт после исполнения (не просто warning).

  5. ЧАСОВОЙ ПИЛОТ: PilotBudget.ensure_pilot_started()/complete_pilot()
     -- персистентны, рестарт не обнуляет и не начинает новый час после
     завершения. Плавная остановка по --duration-seconds: новые
     кандидаты не берутся, текущая попытка (если есть) доводится до
     конца, поток не обрывается. Однопоточный лок на кошелёк
     (single-instance).

  6. ФИНАЛЬНАЯ ПРОВЕРКА -- ТОЛЬКО выбранный размер (quote_route_at_size),
     БЕЗ повторного полного перебора сетки. Явный MIN_PROFIT_FLOOR_RAW
     с честным описанием (не "покрытие газа отката").

Учёт/лимиты -- task5_v4_pilot_accounting.py. Подпись/отправка --
ИСКЛЮЧИТЕЛЬНО task5_bot_sender.py (эта сессия не обходит его внешними
приблизительными расчётами; изменения В САМОМ sender.py сделаны с явного
разрешения владельца для согласования резерва/отправки, см. его
докстринг)."""
from __future__ import annotations

import argparse
import fcntl
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
# exit_token=USDG, а не молчать про газ для них.
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

# = alchemy_fallback._MIN_REQUEST_INTERVAL_S -- ЕДИНСТВЕННЫЙ троттлинг
# RPC во всём проекте (project-wide, потокобезопасный лок), который
# реально ограничивает частоту запросов; POLL_INTERVAL_S здесь
# опрашивать чаще этого порога не даст выигрыша (eth_blockNumber всё
# равно упрётся в тот же _throttle), реже -- добавляет задержку детекции
# сверх нужного.
POLL_INTERVAL_S = 0.5
LIVENESS_REFRESH_INTERVAL_S = 60.0  # владелец: "раз в минуту"
DISCOVERY_POLL_INTERVAL_S = 1.0  # фоновый поток -- не торговый цикл, может опрашивать чуть реже
PENDING_TX_RECEIPT_POLL_S = 2.0
# Владелец, доп.: "торговые проверки должны получать приоритет" при
# общем троттле -- фон (BackgroundRegistryWorker) пропускает СВОЙ такт,
# если торговый путь делал РЕАЛЬНЫЙ RPC-запрос в течение последних
# TRADING_PRIORITY_WINDOW_S секунд (см. _RpcPriorityHint ниже). Это не
# отдельная очередь приоритетов у самого _throttle (не меняем
# alchemy_fallback.py второй раз без нужды) -- фон просто честно
# уступает дорогу, когда видит недавнюю торговую активность.
TRADING_PRIORITY_WINDOW_S = 2.0

# Владелец, доп.: "минимально ненулевой порог на контракте -- НЕ
# покрытие газа отката, обещать это нельзя" (внешнее ревью, второй
# раунд, пункт 6). Контракт требует minProfit>0 (см. ClosedCycleExecutorV4.sol,
# require(p.minProfit > 0)) -- это ТОЛЬКО санитарный пол ("хоть что-то
# положительное"), а НЕ экономический порог. Реальное решение
# "отправлять ли" принимается ВЫШЕ, в Python, по profit_after_gas > 0
# (после РЕАЛЬНОГО учёта газа) -- этот порог его НЕ подменяет и газ
# отката не компенсирует (revert не платит profit вообще).
MIN_PROFIT_FLOOR_RAW = 1

# Владелец, доп.: "как остановить бота" -- ТОТ ЖЕ файл-флаг, что уже
# понимает task5_bot_sender.py::STOP_FILE ("touch этот файл — бот
# перестаёт отправлять"). Здесь просто ЧИТАЕМ его существование (не
# импортируем sender.py в dry-run режиме без нужды) -- при обнаружении
# запускается ТА ЖЕ плавная остановка, что по --duration-seconds.
STOP_FILE_PATH = Path("/etc/bot/STOP")

# Собственный подбор размера (та же сетка, что task5_v4_observation_hour.py,
# см. её докстринг про то, что размер НЕ подставляется вслепую).
SIZE_GRID_BY_START_TOKEN = {
    USDG.lower(): [1_000_000, 3_000_000, 5_000_000, 7_000_000, 10_000_000, 15_000_000, 20_000_000, 30_000_000],
    NATIVE: [int(x * 1e18) for x in (0.02, 0.05, 0.08, 0.1, 0.15, 0.2, 0.3)],
}


def quote_route_at_size(route: RouteCycle, amount_in: int, block_number: int) -> dict:
    """ОДНА цепочка котировок (до len(route.legs) реальных eth_call,
    НЕ вся сетка) для КОНКРЕТНОГО amount_in -- переиспользуется и
    первоначальным подбором размера (recompute_route, полный перебор
    сетки), и финальной проверкой перед отправкой (ТОЛЬКО этот один
    размер -- внешнее ревью, второй раунд, пункт 6: "не запускать
    второй полный перебор внутри финальной проверки")."""
    try:
        cur = amount_in
        for leg in route.legs:
            cur = quote_exact_input_single(leg.pool_key, leg.zero_for_one, cur, block_number)
        return {"ok": True, "amount_in": amount_in, "amount_out": cur, "profit_raw": cur - amount_in}
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        reason = REASON_NO_LIQUIDITY if "NotEnoughLiquidity" in detail else REASON_CALC_ERROR
        return {"ok": False, "reason": reason, "detail": detail}


def recompute_route(route: RouteCycle, block_number: int) -> dict:
    """ПЕРВОНАЧАЛЬНЫЙ подбор размера: свой перебор ВСЕЙ сетки, возвращает
    лучший (amount_in, amount_out, profit_raw) ИЛИ причину отказа. Не
    подставляет заранее известный ответ -- пересчитывает КАЖДЫЙ раз из
    реального состояния пулов на blockNumber.

    НАЙДЕНА И ИСПРАВЛЕНА ПРИЧИНА "6 проверок за 90 секунд" (первый
    раунд ревью): узкое место -- НЕ таймер опроса, а САМОТРОТТЛИНГ RPC
    (_MIN_REQUEST_INTERVAL_S=0.5с МЕЖДУ ЛЮБЫМИ запросами, project-wide)
    в сочетании со СПЛОШНЫМ перебором ВСЕЙ сетки размеров даже когда
    самый мелкий размер уже падает с NotEnoughLiquidity. Реальное
    свойство V4-пулов с концентрированной ликвидностью: больший размер
    строго не может пройти там, где не прошёл меньший -- сетка
    перебирается по возрастанию, при первом NotEnoughLiquidity --
    немедленный останов."""
    start_token = route.legs[0].input_currency.lower()
    grid = SIZE_GRID_BY_START_TOKEN.get(start_token, SIZE_GRID_BY_START_TOKEN[USDG.lower()])
    best = None
    last_reason = None
    last_detail = None
    for amount_in in grid:
        res = quote_route_at_size(route, amount_in, block_number)
        if res["ok"]:
            if best is None or res["profit_raw"] > best["profit_raw"]:
                best = res
        else:
            last_reason, last_detail = res["reason"], res["detail"]
            if res["reason"] == REASON_NO_LIQUIDITY:
                break  # больший размер тоже упадёт -- см. докстринг
    if best is None:
        return {"ok": False, "reason": last_reason or REASON_CALC_ERROR, "detail": last_detail or "неизвестная ошибка"}
    return {"ok": True, **best}


def estimate_gas(contract_address: str, calldata: bytes, from_address: str) -> dict:
    """eth_estimateGas -- РЕАЛЬНЫЙ read-only вызов (не отправка)."""
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
    ранее в проекте)."""
    if token.lower() == NATIVE:
        return int(_rpc_call("eth_getBalance", [account, "latest"]), 16)
    padded = account[2:].rjust(64, "0")
    raw = _rpc_call("eth_call", [{"to": token, "data": "0x70a08231" + padded}, "latest"])
    return int(raw, 16)


def fetch_real_receipt(tx_hash: str) -> dict | None:
    """Единичный НЕблокирующий read-only запрос -- None, если транзакция
    ещё не замайнена."""
    try:
        return _rpc_call("eth_getTransactionReceipt", [tx_hash])
    except Exception:  # noqa: BLE001
        return None


def wait_for_real_receipt(tx_hash: str, poll_interval_s: float = PENDING_TX_RECEIPT_POLL_S,
                           log_every_s: float = 30.0) -> dict:
    """Блокирует, пока не появится РЕАЛЬНЫЙ рецепт -- "таймаут рецепта
    не снимает in_flight... блокировка держится до подтверждённого
    результата". Логирует каждые log_every_s секунд -- "молчаливого
    ожидания быть не должно" касается и этого ожидания."""
    start = time.monotonic()
    last_log = 0.0
    while True:
        receipt = fetch_real_receipt(tx_hash)
        if receipt is not None:
            return receipt
        now = time.monotonic()
        if now - last_log >= log_every_s:
            print(f"[hotpath][pending] жду РЕАЛЬНЫЙ рецепт {tx_hash} уже {now - start:.0f}с "
                  f"(in_flight держится, таймаут отдельного вызова это не отменяет)...")
            last_log = now
        time.sleep(poll_interval_s)


def _receipt_gas_used_and_price(receipt: dict) -> tuple[int | None, int | None]:
    """Возвращает (None, None), если поле реально отсутствует -- НЕ
    подставляет 0 молча (внешнее ревью, пункт 4: "отсутствующие
    обязательные поля не превращать молча в нули"); вызывающий код
    (PilotBudget.finalize_gas) сам решает, что делать (halt)."""
    gas_used = receipt.get("gasUsed")
    if gas_used is None:
        return None, None
    gas_used = int(gas_used, 16) if isinstance(gas_used, str) else gas_used
    egp = receipt.get("effectiveGasPrice")
    if egp is None:
        return gas_used, None
    egp = int(egp, 16) if isinstance(egp, str) else egp
    return gas_used, egp


def _receipt_status(receipt: dict) -> int:
    status = receipt.get("status", 0)
    return int(status, 16) if isinstance(status, str) else status


def fetch_contract_owner(contract_address: str) -> str:
    raw = _rpc_call("eth_call", [{"to": contract_address, "data": OWNER_SELECTOR}, "latest"])
    return "0x" + raw[-40:]


def verify_wallet_consistency(contract_address: str, declared_wallet: str, sender_address: str) -> str:
    """owner() контракта == адрес ключа Sender == --from-address, иначе
    SystemExit (остановка, не предупреждение). ВАЖНО (внешнее ревью,
    второй раунд): совпадение этих трёх адресов подтверждает ТОЛЬКО
    внутреннюю согласованность конфигурации (мы шлём тем ключом,
    которым задеплоен контракт) -- оно НЕ доказывает, что это
    "отдельный кошелёк" с ограниченным финансированием (см. итоговый
    отчёт: реальный адрес + факт того, СКОЛЬКО на него переведено, --
    внешние по отношению к этой проверке факты, подтверждаются
    отдельно)."""
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


class _SingleInstanceLock:
    """Владелец, доп. (второй раунд): "невозможно случайно запустить
    два торгующих экземпляра на одном кошельке". Файловый advisory-лок
    (flock, POSIX) на путь, зависящий от адреса кошелька -- разные
    кошельки могут работать независимо, один и тот же -- нет. Держится
    на ВСЁ время жизни процесса (дескриптор не закрывается до выхода)."""

    def __init__(self, wallet_address: str, lock_dir: str = "/home/bot/data") -> None:
        Path(lock_dir).mkdir(parents=True, exist_ok=True)
        self.path = Path(lock_dir) / f"task5_v4_hotpath_{wallet_address.lower()}.lock"
        self._fh = None

    def acquire(self) -> None:
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            raise SystemExit(f"[hotpath] СТОП: другой экземпляр уже держит лок {self.path} для этого кошелька -- "
                              f"два торгующих процесса на одном кошельке не допускаются")
        self._fh.write(str(os.getpid()))
        self._fh.flush()

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
            except OSError:
                pass


class _RpcPriorityHint:
    """Владелец, доп. (второй раунд): "При общем RPC-троттле торговые
    проверки должны получать приоритет." Не отдельная очередь приоритетов
    внутри самого _throttle (не меняем alchemy_fallback.py второй раз
    без нужды) -- лёгкий, отдельный механизм: фон явно уступает,
    пропуская СВОЙ такт, если видит недавнюю торговую активность."""

    def __init__(self) -> None:
        self._last_trading_rpc_at = 0.0
        self._lock = threading.Lock()

    def mark_trading_active(self) -> None:
        with self._lock:
            self._last_trading_rpc_at = time.monotonic()

    def should_background_yield(self) -> bool:
        with self._lock:
            return (time.monotonic() - self._last_trading_rpc_at) < TRADING_PRIORITY_WINDOW_S


def resolve_pending_tx_if_any(budget: PilotBudget) -> None:
    """"После перезапуска — сначала выяснить судьбу висящей попытки,
    потом работать." Использует ТЕ ЖЕ идемпотентные методы, что и
    обычный путь (finalize_gas/finalize_profit_and_close) -- если
    процесс упал ПОСЛЕ finalize_gas, но ДО finalize_profit_and_close,
    здесь довершается ИМЕННО прибыльная половина, газ повторно НЕ
    учитывается (флаг gas_recorded)."""
    if budget.pending is None:
        return
    p = budget.pending
    tx_hash = p.get("tx_hash")
    if not tx_hash:
        budget.halt("обнаружена pending-попытка без tx_hash -- противоречивое состояние, "
                    "не должно происходить по конструкции (begin_attempt вызывается только с готовым tx_hash), "
                    "ручной разбор")
        return

    print(f"[hotpath][restart] обнаружена незавершённая попытка с прошлого запуска: {tx_hash} "
          f"(маршрут {p.get('route_label')}) -- выясняю судьбу ПЕРЕД началом работы...")

    if not p.get("gas_recorded"):
        receipt = wait_for_real_receipt(tx_hash)
        gas_used, effective_gas_price = _receipt_gas_used_and_price(receipt)
        tx_status = _receipt_status(receipt)
        price = current_weth_usdg_price()
        gas_res = budget.finalize_gas(tx_status, gas_used, effective_gas_price, price)
        for threshold in gas_res.get("newly_crossed", []):
            print(f"[hotpath][ПРОМЕЖУТОЧНЫЙ ОТЧЁТ] накопленные потери на газ достигли ${threshold:.0f} "
                  f"(итого: ${budget.cumulative_gas_loss_usd:.2f})")
        if not gas_res.get("resolved"):
            print(f"[hotpath][restart] СТОП при разрешении газа висящей попытки: {budget.halt_reason}")
            return
        if tx_status == 0:
            print(f"[hotpath][restart] висящая попытка -- ОТКАТ, газ учтён, попытка закрыта.")
            return

    # tx_status==1 (успех) -- вторая половина (прибыль) ещё не закрыта.
    if budget.pending is not None and not budget.pending.get("finalized"):
        p = budget.pending
        try:
            exit_token = p["exit_token"]
            contract_address = p["contract_address"]
            route_tokens = p["route_tokens"]
            pre_balances = p["pre_balances"]
            post_balances = {t: _token_balance(t, contract_address) for t in route_tokens}
            actual_gain_raw = post_balances[exit_token.lower()] - pre_balances[exit_token.lower()]
            ok_tokens, why_tokens = check_no_unexpected_token_spend(pre_balances, post_balances, exit_token)
            if not ok_tokens:
                budget.halt(why_tokens)
                print(f"[hotpath][restart] СТОП: {why_tokens}")
                return
            budget.finalize_profit_and_close(actual_gain_raw)
            print(f"[hotpath][restart] висящая попытка -- УСПЕХ, факт. прирост восстановлен и учтён.")
        except Exception as exc:  # noqa: BLE001
            budget.halt(f"не удалось восстановить факт прибыли висящей (успешной) попытки после рестарта: {exc} "
                        f"-- газ уже учтён, прибыль -- нет, ручной разбор")
            print(f"[hotpath][restart] СТОП: {budget.halt_reason}")


class _CoalescingRouteQueue:
    """"Не обрабатывай накопившуюся очередь устаревших состояний. Пока
    идёт расчёт, новые изменения одного маршрута объединяй в последнее
    доступное состояние." НЕ FIFO-очередь событий -- словарь
    route_id -> (последний_блок, время_получения), перезаписываемый при
    повторном касании того же маршрута, пока он ещё не забран."""

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
    """Отдельный поток: непрерывное обнаружение новых пулов известного
    арбитражника (инкрементально, по новым блокам) + периодическая (раз
    в минуту) полная проверка живучести ВСЕХ маршрутов. НЕ в горячем
    пути -- пишет в общий (потокобезопасный, RLock) реестр."""

    def __init__(self, registry: RouteRegistry, priority_hint: _RpcPriorityHint) -> None:
        super().__init__(name="registry-discovery-liveness", daemon=True)
        self.registry = registry
        self.priority_hint = priority_hint
        self._stop_event = threading.Event()
        self._last_checked_block: int | None = None
        self._last_liveness_refresh_wall = time.monotonic()  # первая полная проверка уже была в bootstrap_registry()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self.priority_hint.should_background_yield():
                # Торговый путь только что делал реальный RPC-запрос --
                # уступаем дорогу (владелец, доп.: "торговые проверки
                # должны получать приоритет" при общем троттле).
                self._stop_event.wait(DISCOVERY_POLL_INTERVAL_S)
                continue
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
                 priority_hint: _RpcPriorityHint, sender=None, dry_run: bool = True) -> None:
        self.registry = registry
        self.contract_address = contract_address
        self.from_address = from_address
        self.budget = budget
        self.attempt_table = attempt_table
        self.reason_log = reason_log
        self.priority_hint = priority_hint
        self.sender = sender  # task5_bot_sender.Sender, только если не dry_run
        self.dry_run = dry_run
        self.last_checked_block: int | None = None
        self._queue = _CoalescingRouteQueue()
        self._stop_event = threading.Event()
        self._no_new_candidates = threading.Event()
        self.busy = False  # True строго внутри _evaluate_and_maybe_send -- для плавной остановки по --duration-seconds

    def stop(self) -> None:
        self._stop_event.set()

    def request_stop_new_candidates(self) -> None:
        """Владелец, доп.: "По истечении часа: запретить новые
        отправки, включая уже рассчитываемых кандидатов... не обрывать
        поток посреди отправки." Новые элементы очереди больше НЕ
        забираются на оценку; уже НАЧАТАЯ оценка (self.busy) доводится
        до конца естественным образом."""
        self._no_new_candidates.set()

    def is_idle(self) -> bool:
        return not self.busy and self.budget.pending is None

    # --- ДЕТЕКТОР: быстрый, только опрос новых Swap-логов
    # отслеживаемых пулов -- НЕ пересчитывает/отправляет сам. ---
    def poll_once(self) -> None:
        latest = int(_rpc_call("eth_blockNumber", []), 16)
        self.priority_hint.mark_trading_active()
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
            if self._no_new_candidates.is_set():
                route_id = item[0]
                route = self.registry.get_route(route_id)
                if route is not None:
                    self.reason_log.log(route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                         "пилот завершает работу (--duration-seconds истёк) -- новые кандидаты не берутся")
                continue
            route_id, block_number, recv_t_monotonic = item
            route = self.registry.get_route(route_id)
            if route is None:
                continue
            if not self.registry.is_live(route_id):
                self.reason_log.log(route_id, route.label, REASON_NO_LIQUIDITY,
                                     "маршрут временно исключён (последняя проверка живучести)")
                continue
            self.busy = True
            try:
                self._evaluate_and_maybe_send(route, block_number, recv_t_monotonic)
            except Exception as exc:  # noqa: BLE001
                print(f"[hotpath] ошибка при оценке маршрута {route.label}: {exc}", file=sys.stderr)
            finally:
                self.busy = False

    def _evaluate_and_maybe_send(self, route: RouteCycle, block_number: int, recv_t_monotonic: float) -> None:
        self.priority_hint.mark_trading_active()
        # --- Первоначальный подбор размера (полный перебор сетки --
        # ЭТО оставляем, см. внешнее ревью п.6: "оставь первоначальный
        # подбор размера"). ---
        recompute = recompute_route(route, block_number)
        if not recompute["ok"]:
            self.reason_log.log(route.route_id, route.label, recompute["reason"], recompute["detail"])
            return
        if recompute["profit_raw"] <= 0:
            self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                 f"лучший размер {recompute['amount_in']}, профит(до газа)={recompute['profit_raw']} "
                                 f"(блок {block_number})")
            return

        exit_decimals = 6 if route.exit_token.lower() == USDG.lower() else 18

        def _profit_after_gas(profit_raw: int, gas_estimate: int, gas_price_wei: int,
                               weth_price: float | None) -> tuple[float, str] | tuple[None, None]:
            gas_cost_eth = gas_estimate * gas_price_wei / 1e18
            profit_before_gas = profit_raw / 10**exit_decimals
            if route.exit_token.lower() == NATIVE:
                return profit_before_gas - gas_cost_eth, ""
            if weth_price is None:
                return None, "не удалось получить живую цену WETH/USDG для перевода газа"
            return profit_before_gas - gas_cost_eth * weth_price, ""

        first_amount_specified = -recompute["amount_in"]
        calldata = build_execute_cycle_calldata(route, first_amount_specified, min_profit=MIN_PROFIT_FLOOR_RAW)

        gas_res = estimate_gas(self.contract_address, calldata, self.from_address)
        if not gas_res["ok"]:
            self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, gas_res["error"])
            return
        gas_price = int(_rpc_call("eth_gasPrice", []), 16)
        weth_usdg_price = current_weth_usdg_price()
        profit_after_gas, err = _profit_after_gas(recompute["profit_raw"], gas_res["gas_estimate"], gas_price,
                                                   weth_usdg_price)
        if profit_after_gas is None:
            self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, err)
            return
        if profit_after_gas <= 0:
            self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                 f"профит после газа {profit_after_gas:.6f} <= 0 (блок {block_number})")
            return

        can_send, why = self.budget.can_send()
        if not can_send:
            self.reason_log.log(route.route_id, route.label, why, "")
            return

        if self.dry_run or self.sender is None:
            latency_s = time.monotonic() - recv_t_monotonic
            print(f"[hotpath][DRY-RUN] отправил бы: {route.label} размер={recompute['amount_in']} "
                  f"профит_после_газа~{profit_after_gas:.6f} latency={latency_s*1000:.0f}мс блок={block_number}")
            row = AttemptTableRow(
                ts_wall=time.time(), route_label=route.label, route_id=route.route_id,
                size_in_raw=recompute["amount_in"], exit_token=route.exit_token,
                expected_profit_after_gas=profit_after_gas,
                latency_recv_to_send_s=latency_s, tx_hash=None, result="dry_run_would_send",
                actual_gain_base_asset=None, gas_used=gas_res["gas_estimate"],
                gas_cost_native=gas_res["gas_estimate"] * gas_price / 1e18,
                cumulative_gas_loss_usd=self.budget.cumulative_gas_loss_usd,
                cumulative_net_pnl_usd=self.budget.cumulative_net_pnl_usd,
                computed_at_block=block_number, state_age_blocks=0,
            )
            self.attempt_table.write(row)
            return

        # --- ФИНАЛЬНАЯ ПРОВЕРКА -- ТОЛЬКО выбранный размер, ОДНА
        # цепочка котировок (quote_route_at_size), НЕ вся сетка заново
        # (внешнее ревью, второй раунд, пункт 6). Если уже не подходит
        # -- пропускаем этот кандидат целиком, следующий сигнал придёт
        # своим чередом из очереди (не ищем альтернативный размер здесь). ---
        fresh_latest = int(_rpc_call("eth_blockNumber", []), 16)
        state_age_blocks = max(0, fresh_latest - block_number)
        final_amount_in = recompute["amount_in"]
        final_profit_raw = recompute["profit_raw"]
        if fresh_latest > block_number:
            final_quote = quote_route_at_size(route, final_amount_in, fresh_latest)
            if not final_quote["ok"] or final_quote["profit_raw"] <= 0:
                self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                     f"финальная проверка размера {final_amount_in} на блоке {fresh_latest} "
                                     f"(решение было на {block_number}, возраст {state_age_blocks} блоков) -- "
                                     f"уже не подходит, пропускаем кандидата")
                return
            final_profit_raw = final_quote["profit_raw"]
            block_number = fresh_latest
            # calldata НЕ меняется -- amount_in/minProfit/маршрут те же,
            # calldata не зависит от состояния блока; пересчитываем
            # только оценку газа (могла измениться) и итоговый профит.
            gas_res = estimate_gas(self.contract_address, calldata, self.from_address)
            if not gas_res["ok"]:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, gas_res["error"])
                return
            gas_price = int(_rpc_call("eth_gasPrice", []), 16)
            weth_usdg_price = current_weth_usdg_price()
            profit_after_gas, err = _profit_after_gas(final_profit_raw, gas_res["gas_estimate"], gas_price,
                                                       weth_usdg_price)
            if profit_after_gas is None:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, err)
                return
            if profit_after_gas <= 0:
                self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                     f"финальная проверка: после газа {profit_after_gas:.6f} <= 0 -- не отправляем")
                return

        # --- Подготовка -> проверка бюджета -> подпись -> сохранение
        # контекста -> отправка (внешнее ревью, второй раунд, пункт 2:
        # ИМЕННО в этом порядке, согласовано с Sender). ---
        try:
            tx_fields = self.sender.prepare_transaction_fields(self.contract_address, calldata,
                                                                 gas_res["gas_estimate"])
        except RuntimeError as exc:
            self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED,
                                 f"prepare_transaction_fields отказал: {exc}")
            return

        ok_reserve, why_reserve = self.budget.reserve_for_send(tx_fields["gas"], tx_fields["maxFeePerGas"],
                                                                weth_usdg_price)
        if not ok_reserve:
            self.reason_log.log(route.route_id, route.label, why_reserve, "")
            return

        prepared = self.sender.sign_prepared_transaction(tx_fields)  # tx_hash ЛОКАЛЬНО, ДО сети; резервирует nonce

        route_tokens = sorted({leg.currency0.lower() for leg in route.legs} | {leg.currency1.lower() for leg in route.legs})
        pre_balances = {t: _token_balance(t, self.contract_address) for t in route_tokens}

        latency_s = time.monotonic() - recv_t_monotonic
        # "Сохранить... данные попытки до первого сетевого обращения" --
        # begin_attempt ПЕРЕД submit_prepared (сам submit -- первое
        # сетевое обращение НА ОТПРАВКУ; prepare/sign выше уже включали
        # ОДИН read-only вызов на base_fee, не отправку).
        self.budget.begin_attempt({
            **prepared.to_context(),
            "route_id": route.route_id, "route_label": route.label, "exit_token": route.exit_token,
            "size_in_raw": final_amount_in, "expected_profit_after_gas": profit_after_gas,
            "latency_recv_to_send_s": latency_s, "computed_at_block": block_number,
            "state_age_blocks": state_age_blocks, "contract_address": self.contract_address,
            "route_tokens": route_tokens, "pre_balances": pre_balances,
            "reserved_price_used": weth_usdg_price,
        })

        result = self.sender.submit_prepared(prepared)
        # tx_hash ИЗВЕСТЕН ВСЕГДА (см. PreparedTx) -- "неопределённая
        # отправка" теперь означает result.unresolved=True, НЕ "не ушла".
        if result.unresolved:
            print(f"[hotpath][pending] отправка {prepared.tx_hash} не подтверждена за стандартный таймаут -- "
                  f"доразрешаю сама (одна tx одновременно, следующая независимая сделка не начнётся)...")
            receipt = wait_for_real_receipt(prepared.tx_hash)
            tx_status = _receipt_status(receipt)
            gas_used, effective_gas_price = _receipt_gas_used_and_price(receipt)
        else:
            tx_status = result.status
            gas_used, effective_gas_price = result.gas_used, None
            if gas_used is not None:
                # SendResult (обычный путь Sender'а) не отдаёт
                # effectiveGasPrice -- добираем САМИ отдельным read-only
                # RPC-вызовом (не трогаем sender.py второй раз без нужды
                # для этого поля, оно и так честно есть в рецепте).
                receipt = fetch_real_receipt(prepared.tx_hash) or {}
                _, effective_gas_price = _receipt_gas_used_and_price(receipt)

        # --- Пункт 3/4: газ -- СРАЗУ, ДО любых других действий. ---
        weth_usdg_price_for_budget = current_weth_usdg_price()
        gas_res_fin = self.budget.finalize_gas(tx_status, gas_used, effective_gas_price, weth_usdg_price_for_budget)
        for threshold in gas_res_fin.get("newly_crossed", []):
            print(f"[hotpath][ПРОМЕЖУТОЧНЫЙ ОТЧЁТ] накопленные потери на газ достигли ${threshold:.0f} "
                  f"(итого: ${self.budget.cumulative_gas_loss_usd:.2f})")
        if not gas_res_fin.get("resolved"):
            print(f"[hotpath] СТОП: {self.budget.halt_reason}")
            row = AttemptTableRow(
                ts_wall=time.time(), route_label=route.label, route_id=route.route_id,
                size_in_raw=final_amount_in, exit_token=route.exit_token,
                expected_profit_after_gas=profit_after_gas, latency_recv_to_send_s=latency_s,
                tx_hash=prepared.tx_hash, result="unresolved",
                actual_gain_base_asset=None, gas_used=gas_used, gas_cost_native=None,
                cumulative_gas_loss_usd=self.budget.cumulative_gas_loss_usd,
                cumulative_net_pnl_usd=self.budget.cumulative_net_pnl_usd,
                computed_at_block=block_number, state_age_blocks=state_age_blocks,
            )
            self.attempt_table.write(row)
            return

        gas_cost_native = (gas_res_fin.get("gas_cost_wei", 0) or 0) / 1e18

        # --- Остальное (балансы, сверка учёта, прибыльная половина PnL)
        # -- сбой здесь переводит пилот в ОСТАНОВКУ (не просто печатает
        # предупреждение и продолжает торговать -- внешнее ревью, пункт
        # 4: "сбой чтения балансов... должен переводить пилот в
        # остановку/восстановление учёта"). Газ уже учтён выше в любом
        # случае. ---
        actual_gain_base_asset = None
        try:
            if tx_status == 1:
                post_balances = {t: _token_balance(t, self.contract_address) for t in route_tokens}
                actual_gain_raw = post_balances[route.exit_token.lower()] - pre_balances[route.exit_token.lower()]
                actual_gain_base_asset = actual_gain_raw / 10**exit_decimals

                ok_tokens, why_tokens = check_no_unexpected_token_spend(pre_balances, post_balances, route.exit_token)
                if not ok_tokens:
                    self.budget.halt(why_tokens)
                    print(f"[hotpath] СТОП: {why_tokens}")

                ok_acct, why_acct = check_accounting_consistency(final_profit_raw, actual_gain_raw)
                if not ok_acct:
                    self.budget.halt(why_acct)
                    print(f"[hotpath] СТОП: {why_acct}")

                self.budget.finalize_profit_and_close(actual_gain_raw)
            # tx_status == 0 (откат) -- finalize_gas уже закрыл попытку
            # целиком (см. её докстринг), здесь ничего дополнительно
            # закрывать не нужно.
        except Exception as exc:  # noqa: BLE001
            self.budget.halt(f"пост-обработка после отправки (баланс/PnL) упала: {exc} -- "
                              f"газ уже учтён, прибыль/закрытие попытки -- НЕТ, требуется разбор")
            print(f"[hotpath] СТОП (пост-обработка): {self.budget.halt_reason}", file=sys.stderr)

        row = AttemptTableRow(
            ts_wall=time.time(), route_label=route.label, route_id=route.route_id,
            size_in_raw=final_amount_in, exit_token=route.exit_token,
            expected_profit_after_gas=profit_after_gas,
            latency_recv_to_send_s=latency_s, tx_hash=prepared.tx_hash,
            result="success" if tx_status == 1 else "reverted",
            actual_gain_base_asset=actual_gain_base_asset if tx_status == 1 else None,
            gas_used=gas_used, gas_cost_native=gas_cost_native,
            cumulative_gas_loss_usd=self.budget.cumulative_gas_loss_usd,
            cumulative_net_pnl_usd=self.budget.cumulative_net_pnl_usd,
            computed_at_block=block_number, state_age_blocks=state_age_blocks,
        )
        self.attempt_table.write(row)


def _print_status_report(label: str, registry: RouteRegistry, budget: PilotBudget, attempt_table: AttemptTable,
                          pilot_start_wall: float) -> None:
    """"Отчёты — на $5, $10 и по окончании часа, даже если отправок
    нет." -- снимок состояния, печатается БЕЗУСЛОВНО."""
    elapsed_s = time.time() - pilot_start_wall
    gross_by_token = budget.cumulative_gross_profit_raw_by_token
    print(f"[hotpath][ОТЧЁТ: {label}] прошло {elapsed_s/60:.1f} мин пилота | "
          f"маршрутов: {len(registry.routes)} (живых: {len(registry.live_routes())}) | "
          f"попыток отправки: {attempt_table.count} | "
          f"газ (валовой, ВСЕ попытки): {budget.cumulative_gas_wei} wei "
          f"(${budget.cumulative_gas_loss_usd:.2f} / ${BUDGET_STOP_USD:.0f}) | "
          f"валовая прибыль по активам (raw, USDG допущено ==$1): {gross_by_token} | "
          f"чистый PnL (USD, прибыль минус газ, все разрешённые попытки): ${budget.cumulative_net_pnl_usd:.2f} | "
          f"в полёте: {budget.pending is not None} ({(budget.pending or {}).get('tx_hash', '-')}) | "
          f"остановлен: {budget.halted} ({budget.halt_reason or '-'})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contract-address", required=True, help="Адрес задеплоенного ClosedCycleExecutorV4")
    ap.add_argument("--from-address", required=True,
                     help="Заявленный адрес кошелька пилота -- ДОЛЖЕН совпасть с owner() контракта и "
                          "адресом ключа Sender, иначе стоп при старте")
    ap.add_argument("--confirm-mainnet", action="store_true", help="Без этого -- всегда dry-run")
    ap.add_argument("--duration-seconds", type=float, default=None,
                     help="Владелец: часовой пилот -- 3600. Отсчёт от завершения bootstrap реестра, "
                          "переживает рестарт (PilotBudget.pilot_started_at)")
    args = ap.parse_args()

    lock = _SingleInstanceLock(args.from_address)
    lock.acquire()
    try:
        _main(args)
    finally:
        lock.release()


def _main(args) -> None:
    budget = PilotBudget()
    attempt_table = AttemptTable()
    reason_log = ReasonLog()

    if budget.pilot_completed:
        print(f"[hotpath] ПИЛОТ УЖЕ ЗАВЕРШЁН ({budget.pilot_completed_reason}) -- новый час НЕ начинается "
              f"автоматически. Для нового пилота -- явное решение владельца (напр. новое состояние budget).")
        return

    # "После перезапуска — сначала выяснить судьбу висящей попытки,
    # потом работать" -- ПЕРВОЕ действие.
    resolve_pending_tx_if_any(budget)
    if budget.halted:
        print(f"[hotpath] ОСТАНОВЛЕН ещё до старта (см. состояние бюджета): {budget.halt_reason}")
        return

    sender = None
    if args.confirm_mainnet:
        import task5_bot_sender
        sender = task5_bot_sender.Sender()
        verify_wallet_consistency(args.contract_address, args.from_address, sender.address)

    print("[hotpath] бутстрап реестра (сид + обнаружение пулов арбитражника)...")
    registry, latest = bootstrap_registry()
    # Владелец: "Час пилота отсчитывай после завершения первоначального
    # наполнения реестра" + "Перезапуск не должен... начинать новый час
    # после завершённого пилота" -- persisted, НЕ time.time() каждый раз.
    pilot_start_wall = budget.ensure_pilot_started()

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
    print(f"[hotpath]   незавершённая транзакция: {budget.pending is not None}")
    print(f"[hotpath]   час пилота начат: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(pilot_start_wall))} "
          f"(прошло {(time.time()-pilot_start_wall)/60:.1f} мин)")
    print(f"[hotpath] =====================")

    priority_hint = _RpcPriorityHint()
    background_worker = BackgroundRegistryWorker(registry, priority_hint)
    background_worker.start()

    hotpath = HotPath(registry, args.contract_address, args.from_address, budget, attempt_table,
                       reason_log, priority_hint, sender=sender, dry_run=not args.confirm_mainnet)
    evaluator_thread = threading.Thread(target=hotpath.evaluator_loop, name="evaluator", daemon=True)
    evaluator_thread.start()

    hour_report_done = False
    stopping = False
    last_periodic_report = time.monotonic()

    try:
        while True:
            can_send, why = budget.can_send()
            # tx_in_flight -- не повод остановить ДЕТЕКТОР (тот не
            # шлёт сам); повод остановить ВЕСЬ пилот -- только halted/
            # бюджет исчерпан/пилот завершён.
            if budget.halted or budget.pilot_completed or budget.cumulative_gas_loss_usd >= BUDGET_STOP_USD:
                if not stopping:
                    print(f"[hotpath] ОСТАНОВЛЕН: {why}")
                stopping = True

            # Ручная остановка (владелец, доп. отчёта: "как остановить
            # бота") -- ТОТ ЖЕ файл, что уже понимает task5_bot_sender.py
            # (STOP_FILE=/etc/bot/STOP, "touch этот файл -- бот
            # перестаёт отправлять"). Здесь -- та же ПЛАВНАЯ остановка,
            # что по --duration-seconds: новые кандидаты не берутся,
            # текущая попытка (если есть) доводится до конца, итог
            # печатается -- не голое убийство процесса.
            if not stopping and STOP_FILE_PATH.exists():
                print(f"[hotpath] обнаружен {STOP_FILE_PATH} -- останавливаюсь ПЛАВНО (новые кандидаты не "
                      f"берутся, дожидаюсь завершения текущей попытки, если есть)...")
                hotpath.request_stop_new_candidates()
                stopping = True

            if args.duration_seconds is not None and not stopping:
                elapsed_pilot_s = time.time() - pilot_start_wall
                if elapsed_pilot_s >= args.duration_seconds:
                    print(f"[hotpath] час пилота истёк ({elapsed_pilot_s/60:.1f} мин) -- новые кандидаты больше "
                          f"не берутся, дожидаюсь завершения текущей попытки (если есть)...")
                    hotpath.request_stop_new_candidates()
                    stopping = True

            if stopping:
                if hotpath.is_idle():
                    reason = why if (budget.halted or budget.pilot_completed) else "истёк --duration-seconds"
                    budget.complete_pilot(reason)
                    unresolved = budget.pending is not None
                    print(f"[hotpath] === ИТОГ ПИЛОТА ===")
                    print(f"[hotpath]   причина завершения: {reason}")
                    print(f"[hotpath]   неизвестные (неразрешённые) результаты остались: {unresolved}")
                    _print_status_report("ИТОГ", registry, budget, attempt_table, pilot_start_wall)
                    print(f"[hotpath] ====================")
                    break
                else:
                    print(f"[hotpath] ...жду завершения текущей попытки перед остановкой "
                          f"(busy={hotpath.busy}, pending={budget.pending is not None})...")
                    time.sleep(POLL_INTERVAL_S)
                    continue

            try:
                hotpath.poll_once()
            except Exception as exc:  # noqa: BLE001
                print(f"[hotpath] ошибка в цикле опроса: {exc}", file=sys.stderr)

            elapsed_pilot_s = time.time() - pilot_start_wall
            if args.duration_seconds is not None and elapsed_pilot_s >= 3600.0 and not hour_report_done:
                _print_status_report("ЧАС ПИЛОТА", registry, budget, attempt_table, pilot_start_wall)
                hour_report_done = True

            now_mono = time.monotonic()
            if now_mono - last_periodic_report >= 300.0:  # каждые 5 минут -- видимость живого процесса
                _print_status_report("периодический", registry, budget, attempt_table, pilot_start_wall)
                last_periodic_report = now_mono

            time.sleep(POLL_INTERVAL_S)
    finally:
        background_worker.stop()
        hotpath.stop()


if __name__ == "__main__":
    main()
