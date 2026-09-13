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
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import alchemy_fallback  # noqa: E402
from alchemy_fallback import _chunked_get_logs, _rpc_call, topic0  # noqa: E402
from alchemy_fallback import rpc_call_trading_path as _uncounted_rpc_call_trading_path  # noqa: E402

# ПРАВКА (седьмой раунд, разбор владельца, пункт 6): "минимальная
# инструментация для следующего пилота -- ... количество RPC-вызовов".
# Тонкая обёртка ПОВЕРХ реального rpc_call_trading_path -- считает
# КАЖДЫЙ вызов (котировка, estimateGas, gasPrice, blockNumber -- всё,
# что реально идёт через торговый путь), НЕ меняя ни сигнатуру, ни
# поведение самого вызова. _evaluate_and_maybe_send сбрасывает счётчик
# в начале оценки ОДНОГО кандидата и читает его там, где уже логирует
# результат -- однопоточный evaluator (см. _CoalescingRouteQueue.pop_one,
# ОДИН кандидат за раз), поэтому глобальный счётчик безопасен без
# дополнительной изоляции по кандидату; Lock -- всё равно, на случай
# параллельного вызова из фона (простая защита, не архитектурное
# решение).
_rpc_call_count_lock = threading.Lock()
_rpc_call_count = 0


def rpc_call_trading_path(method: str, params: list) -> dict:
    global _rpc_call_count
    with _rpc_call_count_lock:
        _rpc_call_count += 1
    return _uncounted_rpc_call_trading_path(method, params)


def _reset_rpc_call_count() -> None:
    global _rpc_call_count
    with _rpc_call_count_lock:
        _rpc_call_count = 0


def _read_rpc_call_count() -> int:
    with _rpc_call_count_lock:
        return _rpc_call_count
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402
from task5_v4_pilot_accounting import (  # noqa: E402
    BUDGET_STOP_USD, REASON_CALC_ERROR, REASON_NO_LIQUIDITY, REASON_NO_PROFITABLE_CYCLE,
    REASON_SIMULATION_FAILED, AttemptTable, AttemptTableRow, PilotBudget, ReasonLog,
    check_accounting_consistency, check_no_unexpected_token_spend,
)
from task5_v4_quote_replay import quote_exact_input_single, V4_QUOTER  # noqa: E402
from task5_v4_pool_math import decode_quote_result, quote_exact_input_multihop_calldata  # noqa: E402
from task5_v4_route_registry import (  # noqa: E402
    RouteCycle, RouteRegistry, USDG, bootstrap_registry, check_route_liveness, save_registry_state,
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

# ПРАВКА (шестой раунд, пункт 5A/5F): poll_once() (детектор) теперь
# использует alchemy_fallback.rpc_call_trading_path -- ОТДЕЛЬНЫЙ,
# более быстрый троттлинг (_ALCHEMY_MIN_REQUEST_INTERVAL_S=0.1с, ~10
# req/с, если Alchemy настроен -- измерено этой сессией: реальный
# бёрст дал 14.95 req/с 0 ошибок), НЕ общий alchemy_fallback._
# MIN_REQUEST_INTERVAL_S=0.5с (тот делит бюджет с фоном discover_new_
# arbitrageur_routes/liveness -- см. докстринг rpc_call_trading_path).
# POLL_INTERVAL_S ниже согласован с ЭТИМ более быстрым порогом -- если
# Alchemy НЕ настроен, rpc_call_trading_path прозрачно падает на
# _rpc_call (0.5с) и САМ этот троттлинг всё равно не даст опрашивать
# чаще (лишние пробуждения безвредны, просто не дают выигрыша).
POLL_INTERVAL_S = 0.1
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

# Пункт 2 (внешнее ревью, четвёртый раунд): "обычный опрос блоков не
# должен бессрочно запрещать discovery -- у сборщика должна быть
# гарантированная возможность продвигаться в пределах лимитов RPC".
# Фон гарантированно получает ход не реже раза в это время, НЕЗАВИСИМО
# от того, насколько активен торговый путь (см. _RpcPriorityHint).
# Один eth_blockNumber фона раз в ~5с тривиально укладывается в общий
# троттлинг (alchemy_fallback._MIN_REQUEST_INTERVAL_S=0.5с) -- нагрузка
# на провайдера НЕ повышается.
MAX_BACKGROUND_STARVATION_S = 5.0

# Пункт 2, доп.: "полная проверка живучести не должна занимать фон
# неделимым проходом на сотни запросов -- обрабатывай небольшими
# порциями, между которыми возможно обнаружение новых пулов". Раньше
# refresh_liveness_all() гоняла ВСЕ маршруты (десятки-сотни, до
# len(route.legs) RPC-вызовов каждый) ОДНИМ неделимым проходом внутри
# ОДНОЙ итерации фонового цикла -- discover_new_arbitrageur_routes на
# это время не вызывался вообще. Полный проход теперь разбит на
# порции по LIVENESS_BATCH_SIZE маршрутов -- discovery проверяется на
# КАЖДОЙ итерации цикла, независимо от того, идёт ли ещё проход
# живучести.
LIVENESS_BATCH_SIZE = 10

# Владелец, доп.: "минимально ненулевой порог на контракте -- НЕ
# покрытие газа отката, обещать это нельзя" (внешнее ревью, второй
# раунд, пункт 6). Контракт требует minProfit>0 (см. ClosedCycleExecutorV4.sol,
# require(p.minProfit > 0)) -- это САНИТАРНЫЙ пол, НЕ подменяющий
# основное решение "отправлять ли" (то принимается ВЫШЕ, в Python, по
# profit_after_gas > 0 после РЕАЛЬНОГО учёта газа). Используется как
# СЕМЯ для первой (предварительной) оценки газа -- ниже, calldata
# пересобирается с СОГЛАСОВАННЫМ порогом (см. compute_min_profit_raw)
# ПЕРЕД реальной отправкой (внешнее ревью, третий раунд, пункт 5).
MIN_PROFIT_FLOOR_RAW = 1

# Пункт 5 (внешнее ревью, третий раунд): небольшой положительный запас
# СВЕРХ стоимости газа согласованной транзакции -- не голый ноль (если
# minProfit == стоимости газа ровно, округление/дрожание цены газа
# между резервом и отправкой может дать contract-revert на самой
# грани). НЕ обещаем, что это компенсирует газ ОТКАТОВ -- revert НЕ
# платит profit вообще, откат остаётся расходом общего бюджета пилота.
MIN_PROFIT_MARGIN_FRACTION = 0.05

# Пункт 6 (внешнее ревью, четвёртый раунд): "ограниченно пересобрать и
# проверить, либо пропустить кандидата -- не делать бесконечный подбор".
# Между ДВУМЯ подготовками транзакции (каждая читает СВЕЖИЙ baseFee
# отдельным RPC-вызовом) комиссия может измениться настолько, что
# minProfit, закодированный по ПЕРВОЙ подготовке, перестаёт покрывать
# газ ВТОРОЙ -- реально воспроизведено владельцем (требовалось 788 raw,
# в calldata осталось 263). Ниже -- ЦИКЛ "подготовить -> сверить
# закодированный порог с требуемым ПО ЭТОЙ ЖЕ подготовке -> если не
# сходится, пересобрать ещё раз", ограниченный этим числом
# ДОПОЛНИТЕЛЬНЫХ кругов сверх первого.
MAX_MIN_PROFIT_RECONCILE_ROUNDS = 2

# Пункт 2/4 (внешнее ревью, третий раунд): "не превращать неизвестный
# результат в бесконечное блокирующее ожидание без возможности штатно
# завершить процесс". Ограниченное, но щедрое (реальная confirmation-
# задержка сети может быть заметной) время ожидания рецепта, ПОСЛЕ
# которого решение "результат неизвестен" фиксируется (halt, pending
# сохраняется) и процесс штатно завершается -- НЕ ждём буквально вечно.
UNRESOLVED_RECOVERY_TIMEOUT_S = 300.0

# Пункт 8 (внешнее ревью, четвёртый раунд): "убрать исторические ретраи
# из торгового пути -- внешнее ожидание receipt на 300с не является
# реальной верхней границей, если ОДИН внутренний RPC-вызов способен
# ждать до 900с НА КАЖДОМ из нескольких эндпоинтов (в сумме -- до ~45
# минут на один _rpc_call)". Устанавливается ОДИН раз в _main(), ДО
# любого RPC-вызова этого процесса (см. alchemy_fallback.
# set_rate_limit_wait_budget_s) -- модульный глобал МУТИРУЕТСЯ только
# внутри ЭТОГО процесса, поведение остальных (исторических) задач,
# запускаемых СВОИМИ отдельными процессами, не меняется НИКАК. Меньше
# UNRESOLVED_RECOVERY_TIMEOUT_S -- иначе внешний "ограниченный" таймаут
# сам по себе перестаёт быть реальной верхней границей.
LIVE_RPC_RETRY_BUDGET_S = 20.0

# CycleExecuted(address exitToken, uint256 profit, uint256 nLegs) --
# реальное событие ClosedCycleExecutorV4.sol (проверено НЕЗАВИСИМО
# keccak256 через pycryptodome И eth_utils, оба дают одно и то же;
# ЭМПИРИЧЕСКИ подтверждено этой же сессией на реальном исполнении на
# форке -- data/task5_v4_verify_calldata_crosscheck_result.json: лог с
# этим topics[0], data слово 1 == 0xc4a06 == 805382 == ИЗВЕСТНАЯ
# реальная прибыль того прогона, побайтово). Пункт 3 (внешнее ревью,
# третий раунд): "для прибыли предпочитай данные конкретного
# исполнения... а не изменение баланса до неопределённого latest после
# долгого простоя" -- profit ЭТОЙ КОНКРЕТНОЙ транзакции, посчитанный
# НА ЧЕЙНЕ в НЕЙ ЖЕ, не подвержен контаминации другими транзакциями,
# трогавшими баланс контракта за время между отправкой и восстановлением.
CYCLE_EXECUTED_TOPIC0 = topic0("CycleExecuted(address,uint256,uint256)")


def parse_cycle_executed_profit(receipt: dict, contract_address: str, expected_exit_token: str | None = None) -> int | None:
    """Ищет CycleExecuted ИМЕННО от contract_address в логах рецепта,
    возвращает поле profit (raw, в единицах exitToken). None, если лог
    не найден/не распознан/структура не соответствует ожидаемой --
    ПРАВКА (внешнее ревью, пятый раунд, пункт 2): вызывающий код НЕ
    ДОЛЖЕН откатываться на разницу балансов как замену профиту (за
    время простоя/восстановления контракт мог получить/потратить
    средства ДРУГИМИ транзакциями -- balance-diff до latest НЕ
    доказывает принадлежность прироста ИМЕННО этой попытке) -- прибыль
    остаётся неустановленной, пилот останавливается с явной причиной
    (см. HotPath._evaluate_and_maybe_send / _recover_profit_half).

    ПРАВКА (внешнее ревью, четвёртый раунд, пункт 7): "парсер должен
    проверять адрес контракта, exitToken и корректность структуры
    события" -- раньше проверялись ТОЛЬКО address+topic0 и БЕРЁТСЯ
    слово[1] без проверки числа слов/декодирования exitToken. Теперь:
    ровно ТРИ 32-байтных слова (address exitToken, uint256 profit,
    uint256 nLegs -- ровно столько нон-индексированных полей в событии,
    ни больше, ни меньше), верхние 12 байт слова exitToken -- нули (это
    и есть корректно ABI-закодированный address, не произвольный
    мусор), и, если expected_exit_token передан -- он ДОЛЖЕН совпасть с
    декодированным exitToken (иначе это лог совсем другого маршрута/
    другой попытки, случайно совпавший по topic0 -- не наш профит)."""
    try:
        for log in receipt.get("logs", []):
            if log.get("address", "").lower() != contract_address.lower():
                continue
            topics = log.get("topics") or []
            if not topics or topics[0].lower() != CYCLE_EXECUTED_TOPIC0.lower():
                continue
            data = log.get("data", "")
            data = data[2:] if data.startswith("0x") else data
            words = [data[i:i + 64] for i in range(0, len(data), 64)]
            if len(words) != 3:  # address exitToken, uint256 profit, uint256 nLegs -- ровно 3, не "хотя бы 2"
                continue
            exit_token_word = words[0]
            if exit_token_word[:24] != "0" * 24:  # верхние 12 байт address-слова обязаны быть нулевыми
                continue
            decoded_exit_token = "0x" + exit_token_word[24:]
            if expected_exit_token is not None and decoded_exit_token.lower() != expected_exit_token.lower():
                continue  # лог другого exitToken -- не наша попытка, честно НЕ подставляем чужой profit
            n_legs = int(words[2], 16)
            if n_legs not in (2, 3):  # поддерживаемые в этом пилоте длины цикла -- см. build_cycles_from_pools
                continue
            return int(words[1], 16)
    except Exception:  # noqa: BLE001
        return None
    return None


def compute_min_profit_raw(gas_limit: int, max_fee_per_gas_wei: int, exit_token: str,
                            weth_usdg_price: float | None) -> int | None:
    """Пункт 5 (внешнее ревью, третий раунд): "рассчитывай minProfit в
    raw-единицах базового актива из стоимости газа СОГЛАСОВАННОЙ
    транзакции и небольшого положительного остатка". Худший случай
    стоимости газа -- gas_limit x maxFeePerGas -- ТА ЖЕ величина, что
    используется для резерва бюджета (см. reserve_for_send), не отдельная
    оценка. НЕ утверждаем, что это компенсирует газ ОТКАТОВ -- revert
    вообще не платит profit, откат остаётся расходом общего бюджета
    пилота (PilotBudget.finalize_gas). None, если курс нужен, но
    недоступен -- честно отказываемся считать порог, не гадаем
    (вызывающий код должен пропустить кандидата).

    ПРАВКА (внешнее ревью, четвёртый раунд, пункт 6): "для арифметики
    wei и округления вверх использовать целые числа; для конвертации
    курса -- Decimal ... вместо промежуточного float". gas_limit и
    max_fee_per_gas_wei -- УЖЕ целые (их произведение точное); курс
    (weth_usdg_price) -- float ТОЛЬКО потому, что так его отдаёт
    current_weth_usdg_price() (реальная цена из slot0(), сама по себе
    внутренне float-производная -- отдельная, не решаемая здесь задача),
    но ЗДЕСЬ он конвертируется в Decimal ЧЕРЕЗ str() (то самое
    десятичное представление, что и печатается/логируется -- не
    протаскиваем двоичную неточность float дальше произвольно), и ВСЯ
    последующая арифметика (включая финальное округление вверх) --
    Decimal/int, без единого промежуточного float."""
    max_gas_cost_wei = gas_limit * max_fee_per_gas_wei  # int * int -- точно
    margin = Decimal(1) + Decimal(str(MIN_PROFIT_MARGIN_FRACTION))
    if exit_token.lower() == USDG.lower():
        # USDG (6 decimals) допущено ==$1 -- тот же честный курс, что
        # везде в проекте (см. _raw_to_usd в task5_v4_pilot_accounting.py).
        if weth_usdg_price is None:
            return None
        price_dec = Decimal(str(weth_usdg_price))
        max_gas_cost_raw_dec = (Decimal(max_gas_cost_wei) / (Decimal(10) ** WETH_DECIMALS)) * price_dec * (Decimal(10) ** USDG_DECIMALS)
    else:
        # exit_token == NATIVE -- та же валюта, что и газ (wei ETH),
        # конвертация курса не нужна -- ровно целое число wei.
        max_gas_cost_raw_dec = Decimal(max_gas_cost_wei)
    threshold_dec = (max_gas_cost_raw_dec * margin).to_integral_value(rounding=ROUND_CEILING)
    return int(threshold_dec)

# Владелец, доп.: "как остановить бота" -- ТОТ ЖЕ файл-флаг, что уже
# понимает task5_bot_sender.py::STOP_FILE ("touch этот файл — бот
# перестаёт отправлять"). Здесь просто ЧИТАЕМ его существование (не
# импортируем sender.py в dry-run режиме без нужды) -- при обнаружении
# запускается ТА ЖЕ плавная остановка, что по --duration-seconds.
STOP_FILE_PATH = Path("/etc/bot/STOP")

# Собственный подбор размера (та же сетка, что task5_v4_observation_hour.py,
# см. её докстринг про то, что размер НЕ подставляется вслепую).
# Пункт 3 (внешнее ревью, пятый раунд): расширение КОНЕЧНОЙ пробной
# сетки USDG -- 100/300/1000 USDG (100_000_000/300_000_000/
# 1_000_000_000 raw, 6 decimals) добавлены СВЕРХ всех прежних меньших
# размеров (НЕ заменяют их). ETH-сетка НЕ трогается. Это НЕ поиск
# математического оптимума -- та же конечная сетка, только шире; новые
# размеры НЕ обязывают отправлять крупную сделку -- recompute_route
# (ниже) выбирает ЛУЧШИЙ размер из ВСЕХ прошедших котировку и проверку
# прибыли, каким бы он ни оказался.
SIZE_GRID_BY_START_TOKEN = {
    USDG.lower(): [1_000_000, 3_000_000, 5_000_000, 7_000_000, 10_000_000, 15_000_000, 20_000_000, 30_000_000,
                   100_000_000, 300_000_000, 1_000_000_000],
    NATIVE: [int(x * 1e18) for x in (0.02, 0.05, 0.08, 0.1, 0.15, 0.2, 0.3)],
}


def quote_route_at_size(route: RouteCycle, amount_in: int, block_number: int) -> dict:
    """ОДНА цепочка котировок (до len(route.legs) реальных eth_call,
    НЕ вся сетка) для КОНКРЕТНОГО amount_in -- переиспользуется и
    первоначальным подбором размера (recompute_route, полный перебор
    сетки), и финальной проверкой перед отправкой (ТОЛЬКО этот один
    размер -- внешнее ревью, второй раунд, пункт 6: "не запускать
    второй полный перебор внутри финальной проверки").

    ПРАВКА (шестой раунд, пункт 5B): если ВСЕ плечи маршрута БЕЗ hooks
    -- ОДИН eth_call (quoteExactInput, весь маршрут разом) вместо
    len(route.legs) последовательных quoteExactInputSingle. Реально
    провалидировано (task5_v4_control_case_investigation.py, часть C,
    реальный живой hookless-маршрут на Ohio): результат ИДЕНТИЧЕН
    последовательной котировке (matches_sequential=true), см.
    data/task5_v4_control_case_investigation_result.json.

    Маршруты С hooks (в реестре пилота их большинство содержит хотя бы
    одно такое плечо -- НЕ исключаются "для простоты": идут ПРЕЖНИМ,
    полностью проверенным последовательным путём -- многоходовая
    котировка для hook-маршрута НЕ провалидирована (PathKey.hooks
    потребовал бы РЕАЛЬНОГО адреса хука каждого плеча, а не NONE, что
    отдельно не проверено против настоящего hook-пула -- ложноположи-
    тельное совпадение на хукless-случае ничего не говорит о hook-
    случае)."""
    if all(leg.hooks.lower() == NATIVE for leg in route.legs):
        try:
            path = [(leg.output_currency, leg.fee, leg.tick_spacing, leg.hooks) for leg in route.legs]
            calldata = quote_exact_input_multihop_calldata(route.legs[0].input_currency, path, amount_in)
            raw = rpc_call_trading_path("eth_call", [{"to": V4_QUOTER, "data": calldata}, hex(block_number)])
            amount_out, _gas_estimate = decode_quote_result(raw)
            return {"ok": True, "amount_in": amount_in, "amount_out": amount_out, "profit_raw": amount_out - amount_in}
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            reason = REASON_NO_LIQUIDITY if "NotEnoughLiquidity" in detail else REASON_CALC_ERROR
            return {"ok": False, "reason": reason, "detail": detail}

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
    немедленный останов.

    Пункт 3 (внешнее ревью, пятый раунд): при расширении сетки крупными
    размерами (100/300/1000 USDG) `best` НИКОГДА не обнуляется и не
    заменяется провалом -- если 100 USDG уже не котируется (или
    котируется хуже уже найденного), УЖЕ найденный лучший МЕНЬШИЙ
    размер остаётся в `best` и именно он возвращается; при этом более
    крупный размер, если он и правда котируется ВЫГОДНЕЕ, законно его
    заменит (`res["profit_raw"] > best["profit_raw"]`) -- выбирается
    лучший из ВСЕХ реально прошедших котировку, а не обязательно самый
    крупный."""
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


def estimate_gas(contract_address: str, calldata: bytes, from_address: str,
                  block_number: int | None = None) -> dict:
    """eth_estimateGas -- РЕАЛЬНЫЙ read-only вызов (не отправка).

    ПРАВКА (шестой раунд, пункт 5A): rpc_call_trading_path (не голый
    _rpc_call) -- отдельная, более быстрая полоса для торгового пути
    (Alchemy напрямую, ~10 req/с троттлинг, измерено; см. её докстринг
    в alchemy_fallback.py), НЕ делящая бюджет с фоновым обнаружением/
    живучестью (те продолжают идти через _rpc_call/_chunked_get_logs,
    0.5с, не изменены).

    ПРАВКА (седьмой раунд, разбор владельца, пункт 2): необязательный
    block_number -- ВТОРОЙ параметр eth_estimateGas (blockParameter),
    ЕСЛИ вызывающий код хочет оценку НА ТОМ ЖЕ блоке, что и котировка
    (см. _quote_and_estimate_gas_consistent ниже). None (по умолчанию)
    -- прежнее поведение (latest/pending), НЕ меняет существующих
    вызывающих (recompute_route и т.д. по-прежнему получают gas на
    latest неявно, если явно не передан блок)."""
    try:
        params = [{"from": from_address, "to": contract_address, "data": "0x" + calldata.hex()}]
        if block_number is not None:
            params.append(hex(block_number))
        raw = rpc_call_trading_path("eth_estimateGas", params)
        return {"ok": True, "gas_estimate": int(raw, 16)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_BLOCK_PARAM_UNSUPPORTED_MARKERS = (
    "unsupported", "not supported", "invalid block", "block parameter", "method not found",
    "-32601", "-32602", "invalid argument",
)


def _looks_like_block_param_unsupported(detail: str) -> bool:
    """Отличаем "этот RPC не принимает второй параметр eth_estimateGas"
    (транспортная/протокольная несовместимость -- повод для честного
    fallback) от ОБЫЧНОГО отката исполнения (revert -- НЕ повод менять
    режим, кандидат просто отклоняется как обычно)."""
    d = detail.lower()
    return any(m in d for m in _BLOCK_PARAM_UNSUPPORTED_MARKERS)


def _quote_and_estimate_gas_consistent(route: RouteCycle, amount_in: int, contract_address: str,
                                        calldata: bytes, from_address: str) -> dict:
    """Пункт 2 (седьмой раунд, разбор владельца): "estimateGas(latest)
    -> blockNumber -> quote(blockNumber) не гарантирует одинаковое
    состояние". Получаем КОНКРЕТНЫЙ блок B ПЕРВЫМ (один eth_blockNumber),
    затем котировку И оценку газа -- НА ЭТОМ B (estimate_gas с явным
    параметром блока, если RPC его поддерживает -- реально проверено:
    этот RPC принимает исторический блок для eth_estimateGas, см.
    task5_v4_control_case_investigation.py, часть D). Если параметр
    блока НЕ поддержан этим RPC (честно определяется по тексту ошибки,
    см. _looks_like_block_param_unsupported) -- fallback: фиксируем
    НОМЕРА блока ДО и ПОСЛЕ пары запросов, НЕ заявляем "одно состояние
    доказано". При сдвиге блока между ДО/ПОСЛЕ -- РОВНО ОДНА повторная
    попытка; при повторном сдвиге -- честный отказ (устаревший
    кандидат), НЕ бесконечный подбор.

    Возвращает {"ok": True, "block": B, "profit_raw": ..., "gas_estimate": ...,
    "mode": "block_param"|"fallback_stable"|"fallback_retried_stable",
    "block_before": ..., "block_after": ...} либо {"ok": False, "reason": ...,
    "detail": ..., "mode": ..., блоки для честного лога}."""
    block_b = int(rpc_call_trading_path("eth_blockNumber", []), 16)
    quote_res = quote_route_at_size(route, amount_in, block_b)
    if not quote_res["ok"]:
        return {"ok": False, "mode": "block_param", "block": block_b, "block_before": block_b,
                "block_after": block_b, "reason": quote_res["reason"], "detail": quote_res["detail"]}
    gas_res = estimate_gas(contract_address, calldata, from_address, block_number=block_b)
    if gas_res["ok"]:
        return {"ok": True, "mode": "block_param", "block": block_b, "block_before": block_b,
                "block_after": block_b, "profit_raw": quote_res["profit_raw"],
                "gas_estimate": gas_res["gas_estimate"]}
    if not _looks_like_block_param_unsupported(gas_res["error"]):
        # Обычная ошибка исполнения (revert и т.п.) -- НЕ вопрос
        # согласованности состояний, пробрасываем как есть.
        return {"ok": False, "mode": "block_param", "block": block_b, "block_before": block_b,
                "block_after": block_b, "reason": REASON_SIMULATION_FAILED, "detail": gas_res["error"]}

    # --- Fallback: этот RPC не принял явный параметр блока у eth_estimateGas ---
    for attempt in range(2):  # РОВНО одна повторная попытка при сдвиге блока, не более
        block_before = int(rpc_call_trading_path("eth_blockNumber", []), 16)
        quote_res = quote_route_at_size(route, amount_in, block_before)
        if not quote_res["ok"]:
            return {"ok": False, "mode": "fallback", "block": block_before, "block_before": block_before,
                     "block_after": block_before, "reason": quote_res["reason"], "detail": quote_res["detail"]}
        gas_res = estimate_gas(contract_address, calldata, from_address)  # БЕЗ блока -- параметр не поддержан
        if not gas_res["ok"]:
            return {"ok": False, "mode": "fallback", "block": block_before, "block_before": block_before,
                     "block_after": block_before, "reason": REASON_SIMULATION_FAILED, "detail": gas_res["error"]}
        block_after = int(rpc_call_trading_path("eth_blockNumber", []), 16)
        if block_after == block_before:
            mode = "fallback_stable" if attempt == 0 else "fallback_retried_stable"
            return {"ok": True, "mode": mode, "block": block_before, "block_before": block_before,
                     "block_after": block_after, "profit_raw": quote_res["profit_raw"],
                     "gas_estimate": gas_res["gas_estimate"]}
        if attempt == 0:
            continue  # блок сдвинулся -- ровно одна повторная попытка
        return {"ok": False, "mode": "fallback_unstable", "block": block_after, "block_before": block_before,
                 "block_after": block_after, "reason": REASON_NO_PROFITABLE_CYCLE,
                 "detail": f"блок сдвинулся ДВАЖДЫ подряд между котировкой и оценкой газа "
                            f"({block_before}->{block_after}) -- параметр блока не поддержан этим RPC, "
                            f"состояние НЕ зафиксировано как одно, кандидат устарел, пропускаем "
                            f"(не бесконечный подбор)"}
    raise AssertionError("недостижимо")  # цикл всегда либо return, либо continue один раз


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


def wait_for_receipt_bounded(tx_hash: str, timeout_s: float = UNRESOLVED_RECOVERY_TIMEOUT_S,
                              poll_interval_s: float = PENDING_TX_RECEIPT_POLL_S,
                              log_every_s: float = 30.0) -> dict | None:
    """Ждёт РЕАЛЬНЫЙ рецепт, но ОГРАНИЧЕННО (внешнее ревью, третий
    раунд, пункт 2/4: "не превращать неизвестный результат в
    бесконечное блокирующее ожидание без возможности штатно завершить
    процесс") -- возвращает None по истечении timeout_s, НЕ ждёт вечно.
    "Таймаут отдельного вызова не снимает in_flight" по-прежнему верно
    -- None здесь означает "пока неизвестно", вызывающий код обязан
    сохранить pending и остановиться (halt), а НЕ считать это отказом
    или освобождать резерв. Логирует каждые log_every_s секунд --
    "молчаливого ожидания быть не должно" касается и этого ожидания."""
    start = time.monotonic()
    last_log = 0.0
    while time.monotonic() - start < timeout_s:
        receipt = fetch_real_receipt(tx_hash)
        if receipt is not None:
            return receipt
        now = time.monotonic()
        if now - last_log >= log_every_s:
            print(f"[hotpath][pending] жду РЕАЛЬНЫЙ рецепт {tx_hash} уже {now - start:.0f}с из {timeout_s:.0f}с "
                  f"(in_flight держится, таймаут отдельного вызова это не отменяет)...")
            last_log = now
        time.sleep(poll_interval_s)
    return None


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


def _new_send_forbidden_now(budget: PilotBudget) -> str | None:
    """Пункт 5 (внешнее ревью, четвёртый раунд): ЕДИНАЯ причина, почему
    новая (или повторная, после краха ДО отправки) отправка СЕЙЧАС
    запрещена -- используется И при восстановлении ДО сети
    (_recover_before_broadcast, ДО того как появляется HotPath), И
    внутри HotPath._gate_blocks_new_send (там же ДОПОЛНИТЕЛЬНО
    проверяется _no_new_candidates/can_send() -- см. её докстринг).
    Проверяет STOP-файл НАПРЯМУЮ (не полагается на то, что что-то уже
    его заметило и защёлкнуло Event) -- это самый дешёвый, всегда
    актуальный сигнал."""
    if STOP_FILE_PATH.exists():
        return "обнаружен STOP-файл"
    if budget.pilot_completed:
        return f"пилот уже завершён ({budget.pilot_completed_reason})"
    if budget.halted:
        return f"пилот остановлен ({budget.halt_reason})"
    return None


def verify_nonce_consistency(sender, budget: PilotBudget) -> None:
    """Пункт 1 (третий раунд) + пункт 4 (четвёртый раунд, "довести
    согласование nonce на старте и при восстановлении"): read-only
    сравнение локального счётчика с ончейн, и, КОГДА ЭТО БЕЗОПАСНО --
    ЯВНАЯ резинхронизация (не просто предупреждение, как раньше).

    Резинхронизация выполняется, ТОЛЬКО когда ОБА условия выполнены:
      1. У НАС нет pending-попытки (resolve_pending_tx_if_any уже
         отработал ДО этой функции, см. main()) -- расхождение НЕ
         объясняется нашей же неразрешённой попыткой;
      2. Ончейн pending-nonce == ончейн latest-nonce -- цепь САМА С
         СОБОЙ согласована: НЕТ НИКАКОЙ неподтверждённой транзакции с
         этого адреса в мемпуле (ни нашей -- см. п.1, ни чужой).
         Владелец подтвердил остановку других отправителей с этого
         кошелька на время пилота (пункт 1) -- это НЕЗАВИСИМАЯ
         проверка того же факта по факту состояния цепи, а не просто
         доверие декларации.
    Если ончейн pending != latest, А у нас pending нет -- значит
    неподтверждённая транзакция НЕ наша (другой процесс с этого же
    кошелька, вопреки требованию его остановить) -- ЭТО И ЕСТЬ
    "необъяснимое расхождение": halt, НЕ отправляем заведомо
    некорректную транзакцию, НЕ резинхронизируем вслепую."""
    try:
        info = sender.describe_nonce_state()
    except Exception as exc:  # noqa: BLE001
        # ПРАВКА (пятый раунд ревью, пункт 1): раньше -- print + return,
        # пилот мог начать торговать со СТАРЫМ, ни разу не проверенным
        # nonce. Ончейн-nonce -- обязательное условие безопасной
        # отправки (иначе можем сжечь/повторить чужой nonce) -- сбой
        # ЭТОЙ проверки ТАК ЖЕ фатален, как сбой describe_nonce_state
        # внутри самой отправки: honest halt, не тихое продолжение.
        budget.halt(f"не удалось проверить актуальность nonce при старте ({exc}) -- НЕ отправляем со "
                    f"старым, ни разу не подтверждённым nonce, СТОП")
        print(f"[hotpath][nonce][СТОП] {budget.halt_reason}", file=sys.stderr)
        return
    pending_nonce = (budget.pending or {}).get("nonce")
    print(f"[hotpath][nonce] локальный={info['local_nonce']}, ончейн(pending)={info['onchain_nonce_pending']}, "
          f"ончейн(latest)={info['onchain_nonce_latest']}"
          + (f", nonce висящей попытки={pending_nonce}" if pending_nonce is not None else ""))
    if info["matches_pending"] and info["matches_latest"]:
        return

    if budget.pending is not None:
        # Ожидаемое расхождение, пока НАША висящая попытка не разрешена.
        print(f"[hotpath][nonce] расхождение объяснимо: висящая (неразрешённая) попытка держит nonce "
              f"{pending_nonce}, ончейн ещё не продвинулся дальше -- ожидаемо, резинхронизация НЕ выполняется.")
        return

    if info["onchain_nonce_pending"] != info["onchain_nonce_latest"]:
        budget.halt(f"nonce: у нас НЕТ pending-попытки, но ончейн pending ({info['onchain_nonce_pending']}) != "
                    f"latest ({info['onchain_nonce_latest']}) -- на этом адресе есть НЕ НАША неподтверждённая "
                    f"транзакция (другой отправитель с этого кошелька, возможно, НЕ остановлен -- см. пункт 1) "
                    f"-- НЕ резинхронизируем и НЕ торгуем, пока это не разъяснится вручную")
        print(f"[hotpath][nonce][СТОП] {budget.halt_reason}")
        return

    # Безопасно: у нас нет pending, цепь согласована сама с собой
    # (pending == latest) -- явная резинхронизация. Реальная причина,
    # найденная владельцем: деплой с ЭТОГО ЖЕ кошелька тратит nonce ВНЕ
    # Sender -- локальный счётчик после деплоя отстаёт от ончейн.
    old_local = info["local_nonce"]
    new_local = sender.resync_nonce_to_chain("pending")
    print(f"[hotpath][nonce] РЕЗИНХРОНИЗИРОВАНО: локальный nonce {old_local} -> {new_local} "
          f"(ончейн pending=latest={info['onchain_nonce_latest']}, pending-попыток нет -- безопасно, "
          f"напр. могло быть вызвано деплоем с этого же кошелька ВНЕ Sender).")


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
    пропуская СВОЙ такт, если видит недавнюю торговую активность.

    ПРАВКА (внешнее ревью, четвёртый раунд, пункт 2): "обычный опрос
    блоков не должен БЕССРОЧНО запрещать discovery -- у сборщика должна
    быть ГАРАНТИРОВАННАЯ возможность продвигаться". РАНЬШЕ
    poll_once() (детектор, каждые POLL_INTERVAL_S=0.5с) отмечал
    ЛЮБОЙ реальный RPC-вызов как "торговую активность" -- при
    0.5с-опросе против TRADING_PRIORITY_WINDOW_S=2.0с окна фон видел
    "недавнюю активность" ПОСТОЯННО и уступал НАВСЕГДА (воспроизведено
    владельцем: 20 отметок раз в 1.5с -> фон уступил 20 из 20). Теперь
    should_background_yield() ГАРАНТИРУЕТ фону ход не реже, чем раз в
    MAX_BACKGROUND_STARVATION_S, НЕЗАВИСИМО от торговой активности --
    один вызов eth_blockNumber фона раз в ~MAX_BACKGROUND_STARVATION_S
    секунд тривиально укладывается в общий троттлинг провайдера
    (_MIN_REQUEST_INTERVAL_S=0.5с -- см. alchemy_fallback.py), не
    "повышает нагрузку сверх лимитов провайдера" (сам троттлинг НЕ
    меняется)."""

    def __init__(self) -> None:
        self._last_trading_rpc_at = 0.0
        self._last_background_progress_at = time.monotonic()
        self._lock = threading.Lock()

    def mark_trading_active(self) -> None:
        with self._lock:
            self._last_trading_rpc_at = time.monotonic()

    def mark_background_progressed(self) -> None:
        """Фон вызывает ПОСЛЕ того, как реально сделал свой такт работы
        (не после холостого should_background_yield()==True такта) --
        сбрасывает таймер гарантии прогресса."""
        with self._lock:
            self._last_background_progress_at = time.monotonic()

    def should_background_yield(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now - self._last_background_progress_at >= MAX_BACKGROUND_STARVATION_S:
                return False  # гарантированное продвижение -- НЕЗАВИСИМО от торговой активности
            return (now - self._last_trading_rpc_at) < TRADING_PRIORITY_WINDOW_S


def _tx_fields_to_storable(tx_fields: dict) -> dict:
    """Пункт 2 (третий раунд): сериализуемая (JSON) форма НЕподписанных
    полей транзакции -- разрешённый владельцем вариант "полный контекст с
    проверкой по хэшу" (НЕ храним готовую ПОДПИСАННУЮ raw-транзакцию на
    диске -- при восстановлении подписываем ЗАНОВО тем же ключом и
    сверяем результирующий хэш с уже сохранённым tx_hash побайтово, см.
    _recover_before_broadcast). Приватный ключ и подписанные raw-байты
    сюда НЕ попадают ни при каких условиях."""
    stored = dict(tx_fields)
    data = stored.get("data")
    if isinstance(data, (bytes, bytearray)):
        stored["data"] = "0x" + bytes(data).hex()
    return stored


def _tx_fields_from_storable(stored: dict) -> dict:
    tx = dict(stored)
    data = tx.get("data")
    if isinstance(data, str):
        tx["data"] = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    return tx


def _recover_before_broadcast(pending: dict, sender, budget: PilotBudget) -> dict | None:
    """Пункт 2 (третий раунд): крах МЕЖДУ begin_attempt() и
    submit_prepared() -- транзакция МОГЛА никогда не попасть в сеть.
    Восстанавливает СТРОГО ТУ ЖЕ подписанную транзакцию: пересобирает
    исходные НЕподписанные поля (tx_fields_for_recovery), переподписывает
    ТЕМ ЖЕ ключом (детерминированная ECDSA-подпись eth_account/RFC6979 --
    те же поля дают тот же tx_hash гарантированно) и СВЕРЯЕТ хэш ПЕРЕД
    отправкой -- никогда не гадаем. Если nonce уже занят ЧЕМ-ТО ДРУГИМ (не
    нашей транзакцией по её же хэшу) -- неразрешимое противоречие, halt,
    ручной разбор (пункт 2: "отсутствие транзакции на одном RPC -- не
    доказательство того, что она не принята нигде"). Возвращает рецепт
    (dict), если судьба разрешилась, иначе None (budget уже halted)."""
    tx_hash = pending["tx_hash"]

    receipt = fetch_real_receipt(tx_hash)
    if receipt is not None:
        return receipt

    stored_fields = pending.get("tx_fields_for_recovery")
    if not stored_fields:
        budget.halt(f"pending-попытка {tx_hash} не содержит сохранённых полей для восстановления "
                    f"(tx_fields_for_recovery) -- вероятно, создана ДО этой правки -- ручной разбор "
                    f"обязателен, НЕ гадаем и не начинаем новую независимую сделку")
        return None

    try:
        info = sender.describe_nonce_state()
    except Exception as exc:  # noqa: BLE001
        budget.halt(f"не удалось проверить ончейн-nonce для восстановления {tx_hash}: {exc} -- "
                    f"ручной разбор, pending сохранён")
        return None

    our_nonce = pending.get("nonce")
    if info["onchain_nonce_latest"] > our_nonce:
        # Nonce нашей попытки уже занят ЧЕМ-ТО на цепи, но рецепта по
        # НАШЕМУ хэшу нет -- неразрешимое противоречие (при
        # детерминированной подписи и неизменных полях наша ЖЕ
        # транзакция не может занять этот nonce под другим хэшем).
        # Никогда не угадываем результат -- halt.
        budget.halt(f"nonce {our_nonce} висящей попытки {tx_hash} уже израсходован на цепи (ончейн "
                    f"latest={info['onchain_nonce_latest']}), но рецепт ИМЕННО по этому хэшу не найден -- "
                    f"неразрешимое противоречие, ручной разбор (отсутствие на одном RPC не доказывает "
                    f"отсутствие в сети вообще)")
        return None

    # Пункт 5 (четвёртый раунд): "на рестарте после STOP/завершённого
    # часа разрешено читать receipt и завершать учёт -- автоматическую
    # повторную отправку РАНЕЕ подготовленной транзакции НЕ выполнять в
    # обход действующего запрета отправок". Рецепт мы УЖЕ проверили
    # выше (fetch_real_receipt в начале функции) -- его нет, значит
    # транзакция ГЕНУИННО ни разу не уходила в сеть. Если запрет
    # действует ПРЯМО СЕЙЧАС -- не отправляем автоматически, даже если
    # nonce свободен и мы могли бы это сделать безопасно технически.
    stop_reason = _new_send_forbidden_now(budget)
    if stop_reason is not None:
        budget.halt(f"pending {tx_hash} ни разу не отправлялась (рецепта нет), nonce свободен -- технически "
                    f"можно было бы безопасно переотправить ТУ ЖЕ транзакцию, но {stop_reason} -- "
                    f"автоматическая отправка ЗАПРЕЩЕНА (пункт 5: не в обход действующего запрета), pending "
                    f"СОХРАНЯЕТСЯ, требуется явное решение владельца")
        return None

    # Nonce ещё свободен -- безопасно пересобрать, переподписать (тем же
    # ключом) и СВЕРИТЬ хэш ПЕРЕД повторной отправкой.
    tx_fields = _tx_fields_from_storable(stored_fields)
    prepared = sender.sign_prepared_transaction(tx_fields)
    if prepared.tx_hash.lower() != tx_hash.lower():
        budget.halt(f"восстановление {tx_hash}: пересобранная транзакция дала ДРУГОЙ хэш "
                    f"({prepared.tx_hash}) -- сохранённые поля не соответствуют исходной подписи, "
                    f"НЕ отправляем, ручной разбор")
        return None

    print(f"[hotpath][recover] nonce {our_nonce} ещё свободен, хэш пересобранной транзакции совпал -- "
          f"повторно отправляю ТУ ЖЕ транзакцию {tx_hash}...")
    result = sender.submit_prepared(prepared)
    if not result.unresolved:
        return fetch_real_receipt(tx_hash) or {"status": result.status, "blockNumber": result.block_number,
                                                "gasUsed": result.gas_used}
    return wait_for_receipt_bounded(tx_hash)


def _ensure_pending_row_written(pending: dict, attempt_table: AttemptTable, budget: PilotBudget) -> None:
    """Пункт 3 (третий раунд): "восстановление обязано гарантировать
    наличие итоговой строки попытки без дубликатов" -- строит
    AttemptTableRow ИСКЛЮЧИТЕЛЬНО из полей, персистентно сохранённых в
    pending (никакой опоры на память рухнувшего процесса), пишет её
    РОВНО один раз (row_written -- идемпотентный флаг, см.
    PilotBudget.mark_pending_row_written)."""
    if pending.get("row_written"):
        return
    tx_status = pending.get("tx_status")
    exit_token = pending.get("exit_token", "?")
    exit_decimals = 6 if exit_token.lower() == USDG.lower() else 18
    actual_gain_raw = pending.get("actual_gain_raw")
    gas_cost_wei = pending.get("gas_cost_wei")
    row = AttemptTableRow(
        ts_wall=time.time(), route_label=pending.get("route_label", "?"), route_id=pending.get("route_id", "?"),
        size_in_raw=pending.get("size_in_raw", 0), exit_token=exit_token,
        expected_profit_after_gas=pending.get("expected_profit_after_gas", 0.0),
        latency_recv_to_send_s=pending.get("latency_recv_to_send_s", 0.0),
        tx_hash=pending.get("tx_hash"),
        result="success" if tx_status == 1 else "reverted" if tx_status == 0 else "unresolved",
        actual_gain_base_asset=(actual_gain_raw / 10**exit_decimals) if (tx_status == 1 and actual_gain_raw is not None) else None,
        gas_used=pending.get("gas_used"),
        gas_cost_native=(gas_cost_wei / 1e18) if gas_cost_wei is not None else None,
        cumulative_gas_loss_usd=budget.cumulative_gas_loss_usd, cumulative_net_pnl_usd=budget.cumulative_net_pnl_usd,
        computed_at_block=pending.get("computed_at_block"), state_age_blocks=pending.get("state_age_blocks"),
    )
    attempt_table.write(row)


def _recover_profit_half(budget: PilotBudget, sender, attempt_table: AttemptTable, p: dict,
                          receipt: dict | None = None) -> None:
    """Ветка C: газ уже учтён (gas_recorded=True), tx_status==1,
    finalized==False -- восстанавливаем ТОЛЬКО прибыльную половину.

    ПРАВКА (внешнее ревью, пятый раунд, пункт 2): "нельзя записывать
    разницу между старым балансом и latest как прибыль конкретной
    сделки, если ожидаемое CycleExecuted отсутствует/некорректно." За
    время между отправкой и восстановлением (может быть ДОЛГИМ --
    рестарт после сбоя, дни простоя) контракт МОГ получить/потратить
    средства ДРУГИМИ транзакциями (другой маршрут, другая попытка) --
    balance-diff до неопределённого latest НЕ доказывает, что весь
    прирост принадлежит ИМЕННО этой попытке. Раньше это использовалось
    как "честный fallback" -- теперь: событие отсутствует -> прибыль
    ОСТАЁТСЯ НЕУСТАНОВЛЕННОЙ, pending СОХРАНЯЕТСЯ (finalize_profit_
    and_close НЕ вызывается), пилот останавливается с явной причиной.
    Газ УЖЕ учтён (finalize_gas, до этой функции) -- это НЕ теряется.
    Проверка балансов (check_no_unexpected_token_spend) ОСТАЁТСЯ -- но
    ТОЛЬКО как независимый сигнал "утечки" токенов, НЕ как источник
    числа для прибыли."""
    tx_hash = p.get("tx_hash")
    contract_address = p.get("contract_address")
    exit_token = p.get("exit_token")
    # Пункт 4 (четвёртый раунд): gas_recorded=True здесь ЗНАЧИТ рецепт
    # УЖЕ был получен (см. finalize_gas) -- nonce РЕАЛЬНО израсходован
    # на цепи; подтверждаем его Sender'у ЗДЕСЬ (идемпотентно), на
    # случай если предыдущий процесс упал ПОСЛЕ finalize_gas, но ДО
    # того, как успел вызвать confirm_nonce_used сам.
    if sender is not None and p.get("nonce") is not None:
        sender.confirm_nonce_used(p["nonce"])
    try:
        if receipt is None:
            receipt = fetch_real_receipt(tx_hash)
        actual_gain_raw = parse_cycle_executed_profit(receipt, contract_address, exit_token) if receipt else None
        if actual_gain_raw is None:
            route_tokens = p.get("route_tokens")
            pre_balances = p.get("pre_balances")
            if route_tokens and pre_balances:
                # НЕ как источник профита -- ТОЛЬКО как независимая
                # проверка "не утекли ли другие токены маршрута".
                try:
                    post_balances = {t: _token_balance(t, contract_address) for t in route_tokens}
                    ok_tokens, why_tokens = check_no_unexpected_token_spend(pre_balances, post_balances, exit_token)
                    if not ok_tokens:
                        budget.halt(why_tokens)
                        print(f"[hotpath][restart] СТОП: {why_tokens}")
                        return
                except Exception as exc:  # noqa: BLE001
                    print(f"[hotpath][restart][ВНИМАНИЕ] проверка расхода токенов не удалась: {exc} "
                          f"(не меняет решение ниже -- прибыль и так не подтверждена событием)")
            budget.halt(f"не удалось подтвердить прибыль по событию CycleExecuted для {tx_hash} -- газ уже "
                        f"учтён, прибыль ОСТАЁТСЯ НЕУСТАНОВЛЕННОЙ (разница балансов до неопределённого "
                        f"latest НЕ используется как замена), pending СОХРАНЯЕТСЯ, требуется ручной разбор")
            print(f"[hotpath][restart] СТОП: {budget.halt_reason}")
            return
        budget.finalize_profit_and_close(actual_gain_raw)
        print(f"[hotpath][restart] висящая попытка -- УСПЕХ, факт. прирост подтверждён событием CycleExecuted "
              f"и учтён.")
        _ensure_pending_row_written(budget.pending, attempt_table, budget)
        budget.mark_pending_row_written()
        budget.clear_finalized_pending()
    except Exception as exc:  # noqa: BLE001
        budget.halt(f"не удалось восстановить факт прибыли висящей (успешной) попытки после рестарта: {exc} "
                    f"-- газ уже учтён, прибыль -- нет, ручной разбор")
        print(f"[hotpath][restart] СТОП: {budget.halt_reason}")


def resolve_pending_tx_if_any(budget: PilotBudget, sender, attempt_table: AttemptTable) -> None:
    """"После перезапуска — сначала выяснить судьбу висящей попытки,
    потом работать." ПРАВКА (третий раунд ревью, пункт 3): переписано на
    ЧЕТЫРЕ явных, взаимоисключающих ветки pending-состояния (внешняя
    проверка владельца локально воспроизвела ДВА реальных бага в
    предыдущей версии этой функции):

      A/B) pending["finalized"] == True -- УСПЕХ с уже учтённой
           прибылью, ИЛИ ОТКАТ с уже учтённым газом (ОБА варианта, по
           конструкции finalize_gas/finalize_profit_and_close,
           оставляют finalized=True БЕЗ очистки pending) -- финансово
           уже всё сделано, ничего не пересчитываем и не начисляем
           повторно (это и есть найденный внешней проверкой баг А:
           "recovery пропускает finalized=True, can_send() заблокирован
           навсегда" -- здесь дописываем недостающую строку и очищаем
           pending explicit-но).
      (защитная ветка) gas_recorded=True, tx_status==0, НО finalized НЕ
           True -- НЕ должна случаться по конструкции (finalize_gas сам
           закрывает откат), но если состояние диска этому противоречит
           -- закрываем как откат явно, НЕ проваливаемся в прибыльную
           ветку (это и есть найденный внешней проверкой баг Б).
      C) gas_recorded=True, tx_status==1, finalized=False -- ТОЛЬКО
           прибыльная половина не закрыта -- см. _recover_profit_half.
      D) gas_recorded=False -- судьба ГЕНУИННО не известна: ограниченное
           (не бесконечное) ожидание рецепта по уже известному хэшу,
           затем, если не нашёлся, _recover_before_broadcast (ончейн-
           nonce -> пересборка/переподпись/сверка хэша -> повторная
           отправка ТОЙ ЖЕ транзакции -> ещё раз ограниченное ожидание).
           Если и после этого неизвестно -- halt, pending СОХРАНЯЕТСЯ,
           процесс завершается штатно (никогда не бесконечное ожидание,
           никогда не начинаем новую независимую сделку раньше)."""
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

    # --- Ветки A/B: финансово уже всё сделано -- дописать строку (если
    # ещё не записана) и очистить pending. ---
    if p.get("finalized"):
        print(f"[hotpath][restart] висящая попытка {tx_hash} уже финансово закрыта (finalized=True) с "
              f"прошлого запуска -- дописываю строку попытки (если ещё не записана) и очищаю pending, "
              f"БЕЗ повторного начисления газа/прибыли.")
        # Пункт 4 (четвёртый раунд): "согласуй nonce во всех ветках с
        # подтверждённым результатом, включая случай сбоя после
        # финансового учёта, но до обновления Sender" -- gas_recorded/
        # finalized уже True здесь ЗНАЧИТ рецепт БЫЛ, nonce РЕАЛЬНО
        # израсходован; если процесс упал ПОСЛЕ finalize_* но ДО
        # confirm_nonce_used (тот вызывается ПОЗЖЕ, в самом
        # _evaluate_and_maybe_send) -- Sender об этом мог не узнать.
        # confirm_nonce_used идемпотентен -- безопасно вызывать всегда.
        if sender is not None and p.get("nonce") is not None:
            sender.confirm_nonce_used(p["nonce"])
        _ensure_pending_row_written(p, attempt_table, budget)
        budget.mark_pending_row_written()
        res = budget.clear_finalized_pending()
        if not res.get("cleared"):
            budget.halt(f"pending {tx_hash} помечена finalized, но clear_finalized_pending отказал: {res} -- "
                        f"противоречивое состояние, ручной разбор")
        return

    # --- Защитная ветка: откат, зафиксированный как gas_recorded, но БЕЗ
    # finalized -- по конструкции не должно случаться (см. докстринг),
    # закрываем явно, НЕ проваливаясь в прибыльную ветку ниже (найденный
    # владельцем баг Б). ---
    if p.get("gas_recorded") and p.get("tx_status") == 0:
        print(f"[hotpath][restart] висящая попытка {tx_hash} -- ОТКАТ, газ учтён, но finalized не был "
              f"выставлен (неожиданно) -- закрываю явно как откат, БЕЗ расчёта прибыли.")
        if sender is not None and p.get("nonce") is not None:
            sender.confirm_nonce_used(p["nonce"])
        budget.pending["finalized"] = True
        budget._save()
        _ensure_pending_row_written(budget.pending, attempt_table, budget)
        budget.mark_pending_row_written()
        budget.clear_finalized_pending()
        return

    # --- Ветка C: успех, газ уже учтён, ждём только прибыльную половину. ---
    if p.get("gas_recorded") and p.get("tx_status") == 1:
        print(f"[hotpath][restart] висящая попытка {tx_hash} -- УСПЕХ (газ уже учтён), прибыльная "
              f"половина ещё не закрыта -- восстанавливаю...")
        _recover_profit_half(budget, sender, attempt_table, p)
        return

    # --- Ветка D: газ ещё не учтён -- судьба ГЕНУИННО не известна. ---
    print(f"[hotpath][restart] висящая попытка {tx_hash} (маршрут {p.get('route_label')}) -- газ ещё не "
          f"учтён, судьба не известна -- выясняю ПЕРЕД началом работы (ограниченное ожидание, затем, при "
          f"необходимости, безопасное восстановление ДО сети)...")
    receipt = wait_for_receipt_bounded(tx_hash, timeout_s=UNRESOLVED_RECOVERY_TIMEOUT_S)
    if receipt is None:
        if sender is None:
            budget.halt(f"pending {tx_hash} без рецепта, а восстановление ДО сети требует --confirm-mainnet "
                        f"(dry-run не может подписывать) -- pending СОХРАНЯЕТСЯ, ручной разбор")
            print(f"[hotpath][restart] СТОП: {budget.halt_reason}")
            return
        receipt = _recover_before_broadcast(p, sender, budget)
    if receipt is None:
        if not budget.halted:
            budget.halt(f"судьба висящей попытки {tx_hash} осталась неизвестной после ограниченного "
                        f"ожидания и попытки восстановления -- pending СОХРАНЯЕТСЯ (резерв НЕ снимается, "
                        f"бюджет НЕ обнуляется), процесс завершается штатно, НЕ бесконечное ожидание")
        print(f"[hotpath][restart] СТОП: {budget.halt_reason}")
        return

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
    if sender is not None:
        sender.confirm_nonce_used(p["nonce"])

    if tx_status == 0:
        print(f"[hotpath][restart] висящая попытка -- ОТКАТ, газ учтён, попытка закрыта.")
        _ensure_pending_row_written(budget.pending, attempt_table, budget)
        budget.mark_pending_row_written()
        budget.clear_finalized_pending()
        return

    _recover_profit_half(budget, sender, attempt_table, budget.pending, receipt=receipt)


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
    в минуту) проверка живучести ВСЕХ маршрутов, теперь ПОРЦИЯМИ (пункт
    2, четвёртый раунд -- см. LIVENESS_BATCH_SIZE). НЕ в горячем пути --
    пишет в общий (потокобезопасный, RLock) реестр."""

    def __init__(self, registry: RouteRegistry, priority_hint: _RpcPriorityHint, start_from_block: int,
                 state_path: str | None = None) -> None:
        """start_from_block -- ОБЯЗАТЕЛЬНЫЙ (пункт 3, четвёртый раунд:
        "фоновый курсор должен продолжать с последнего обработанного
        bootstrap-блока -- сейчас при первом запуске фона он
        устанавливается в НОВЫЙ latest, пропуская промежуток длительного
        bootstrap"). Раньше self._last_checked_block инициализировался
        None и на ПЕРВОЙ итерации ЭТОГО потока (которая может произойти
        через десятки секунд-минуты ПОСЛЕ того, как bootstrap_registry()
        уже просканировал блоки ДО СВОЕГО latest) устанавливался в
        latest НА ТОТ МОМЕНТ -- блоки МЕЖДУ concом bootstrap и первым
        реальным тактом фона никогда никем не сканировались. Теперь
        вызывающий код (main()) передаёт СЮДА тот самый latest, на
        котором bootstrap_registry() закончил работу -- разрыва нет
        независимо от того, сколько фон "спал" перед первым тактом."""
        super().__init__(name="registry-discovery-liveness", daemon=True)
        self.registry = registry
        self.priority_hint = priority_hint
        self._stop_event = threading.Event()
        self._last_checked_block: int = start_from_block
        self._last_liveness_refresh_wall = time.monotonic()  # первая полная проверка уже была в bootstrap_registry()
        # Порционный проход живучести -- None, когда проход не идёт.
        self._liveness_pass_route_ids: list[str] | None = None
        self._liveness_pass_cursor = 0
        # Пункт 3 (четвёртый раунд): периодически сохраняем реестр +
        # курсор (не на КАЖДЫЙ такт -- ограничено по времени, чтобы не
        # превратиться в "большой сканер"/лишнюю нагрузку на диск).
        self.state_path = state_path
        self._last_state_save_wall = 0.0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self.priority_hint.should_background_yield():
                # Торговый путь только что делал реальный RPC-запрос --
                # уступаем дорогу, НО НЕ БЕССРОЧНО (см. докстринг
                # _RpcPriorityHint/MAX_BACKGROUND_STARVATION_S).
                self._stop_event.wait(DISCOVERY_POLL_INTERVAL_S)
                continue
            try:
                latest = int(_rpc_call("eth_blockNumber", []), 16)
                if latest > self._last_checked_block:
                    from_block = self._last_checked_block + 1
                    new_routes = self.registry.discover_new_arbitrageur_routes(from_block, latest)
                    for route in new_routes:
                        res = check_route_liveness(route, latest)
                        res["checked_at_block"] = latest
                        res["checked_at_wall"] = time.time()
                        if res.get("live") is not None:  # None -- RPC-ошибка, не путать с "не живой" (пункт 3)
                            self.registry.set_liveness(route.route_id, res)
                        print(f"[registry-worker] новый маршрут от арбитражника (без перезапуска): "
                              f"{route.label} live={res.get('live')} ({res.get('error_kind') or 'ok'})")
                    self._last_checked_block = latest
                    self.priority_hint.mark_background_progressed()

                now = time.monotonic()
                # Пункт 2: "обрабатывай небольшими порциями, между
                # которыми возможно обнаружение новых пулов" -- НАЧАТЬ
                # новый проход, только если предыдущий уже завершён (не
                # идёт) И интервал истёк; ПРОДВИНУТЬ текущий проход (если
                # идёт) -- на КАЖДОЙ итерации, независимо от интервала.
                # discover_new_arbitrageur_routes выше уже отработал
                # ДО этого -- т.е. discovery ГАРАНТИРОВАННО не блокируется
                # проходом живучести, даже если тот занимает много тактов.
                if self._liveness_pass_route_ids is None and now - self._last_liveness_refresh_wall >= LIVENESS_REFRESH_INTERVAL_S:
                    with self.registry._lock:
                        self._liveness_pass_route_ids = list(self.registry.routes.keys())
                    self._liveness_pass_cursor = 0
                    print(f"[registry-worker] начинаю порционную проверку живучести "
                          f"({len(self._liveness_pass_route_ids)} маршрутов, по {LIVENESS_BATCH_SIZE} за такт)...")

                if self._liveness_pass_route_ids is not None:
                    batch = self._liveness_pass_route_ids[
                        self._liveness_pass_cursor:self._liveness_pass_cursor + LIVENESS_BATCH_SIZE]
                    if batch:
                        self.registry.refresh_liveness_batch(latest, batch)
                        self._liveness_pass_cursor += len(batch)
                        self.priority_hint.mark_background_progressed()
                    if self._liveness_pass_cursor >= len(self._liveness_pass_route_ids):
                        n_live = sum(1 for rid in self._liveness_pass_route_ids if self.registry.is_live(rid))
                        print(f"[registry-worker] порционная проверка живучести завершена: "
                              f"{n_live}/{len(self._liveness_pass_route_ids)} живых (последний блок {latest})")
                        self._liveness_pass_route_ids = None
                        self._last_liveness_refresh_wall = now

                # Пункт 3: периодическая персистентность (не на КАЖДЫЙ
                # такт) -- следующий рестарт сможет догнать инкрементально
                # от cursor_block=self._last_checked_block, а не заново
                # полным lookback-сканом.
                if self.state_path and (time.monotonic() - self._last_state_save_wall >= 30.0):
                    try:
                        save_registry_state(self.registry, self._last_checked_block, self.state_path)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[registry-worker] не удалось сохранить состояние реестра (не критично): {exc}",
                              file=sys.stderr)
                    self._last_state_save_wall = time.monotonic()
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

    def evaluator_finished_current_work(self) -> bool:
        """Пункт 5 (внешнее ревью, четвёртый раунд): ЧЕСТНО означает
        ТОЛЬКО "поток-оценщик закончил текущую единицу работы" (не
        занят прямо сейчас) -- И НИЧЕГО про то, разрешилась ли pending-
        попытка. РАНЬШЕ этот метод (тогда назывался is_idle) требовал
        ЕЩЁ и self.budget.pending is None -- если после ограниченного
        ожидания рецепта попытка остаётся ГЕНУИННО неизвестной (halt,
        pending СОХРАНЁН -- намеренно, см. wait_for_receipt_bounded),
        self.busy становится False, НО pending остаётся НЕ-None
        НАВСЕГДА (ничто больше его не тронет -- новые кандидаты не
        берутся) -- старое условие НИКОГДА не становилось True, главный
        цикл бесконечно печатал "жду завершения" (реально
        воспроизведено владельцем). "Поток закончил работу" и
        "результат транзакции известен" -- РАЗНЫЕ вопросы (см. main()::
        решение "неизвестный результат" -- отдельная, явная терминальная
        ветка завершения пилота, не блокирующее ожидание)."""
        return not self.busy

    def _gate_blocks_new_send(self) -> str | None:
        """Пункт 4 (внешнее ревью, третий раунд): "остановка обязана
        запрещать отправку уже СЧИТАЮЩЕГОСЯ кандидата тоже" -- ЕДИНЫЙ
        гейт (час/STOP + бюджет/halt/pending), проверяемый ЗДЕСЬ,
        НЕПОСРЕДСТВЕННО перед сетевой отправкой -- не только при заборе
        кандидата из очереди (evaluator_loop уже это делает, но кандидат,
        начатый ДО дедлайна, иначе проскочил бы). None -- отправка
        разрешена; иначе -- причина (не отправляем, кандидат пропущен)."""
        if self._no_new_candidates.is_set():
            return "остановка запрошена (истёк --duration-seconds или обнаружен STOP-файл) во время расчёта этого кандидата"
        # _new_send_forbidden_now проверяет STOP-файл НАПРЯМУЮ (не
        # только через уже защёлкнутый _no_new_candidates) -- страхует
        # от гонки "файл появился, а главный цикл ещё не успел дойти до
        # своей следующей проверки и вызвать request_stop_new_candidates".
        forbidden = _new_send_forbidden_now(self.budget)
        if forbidden is not None:
            return forbidden
        can_send, why = self.budget.can_send()
        if not can_send:
            return why
        return None

    # --- ДЕТЕКТОР: быстрый, только опрос новых Swap-логов
    # отслеживаемых пулов -- НЕ пересчитывает/отправляет сам. ---
    def poll_once(self) -> None:
        # ПРАВКА (шестой раунд, пункт 5A/5F): rpc_call_trading_path --
        # отдельная, быстрая полоса для детектора (не делит бюджет с
        # фоном); напрямую снижает задержку "новый блок -> детектор
        # заметил" (пункт 5F).
        latest = int(rpc_call_trading_path("eth_blockNumber", []), 16)
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
                # Пункт 3 (внешнее ревью, четвёртый раунд): "исключённый
                # маршрут перепроверять при изменениях его пулов" -- этот
                # самый сигнал (block_number/recv_t_monotonic из очереди)
                # ЗНАЧИТ, что один из пулов маршрута ТОЛЬКО ЧТО тронулся
                # (см. poll_once -> _queue.mark) -- РЕАЛЬНАЯ причина
                # перепроверить СЕЙЧАС, а не ждать до минутного батча
                # фона. RPC-ошибку (error_kind == "rpc") ОТЛИЧАЕМ от
                # подтверждённого отсутствия ликвидности -- инконклюзивный
                # результат НЕ обновляет статус и НЕ считается "снова не
                # живой", маршрут остаётся с прежним статусом.
                recheck = check_route_liveness(route, block_number)
                if recheck.get("live") is not None:
                    recheck["checked_at_block"] = block_number
                    recheck["checked_at_wall"] = time.time()
                    self.registry.set_liveness(route_id, recheck)
                if recheck.get("live") is True:
                    print(f"[hotpath] маршрут {route.label} ОЖИЛ по свежему сигналу -- оцениваю немедленно.")
                elif recheck.get("live") is False:
                    self.reason_log.log(route_id, route.label, REASON_NO_LIQUIDITY,
                                         f"маршрут исключён, перепроверка ПО СВЕЖЕМУ сигналу (не по расписанию) "
                                         f"подтвердила: {recheck.get('error')}")
                    continue
                else:  # None -- RPC/транспортная ошибка, НЕ подтверждённое отсутствие ликвидности
                    self.reason_log.log(route_id, route.label, REASON_CALC_ERROR,
                                         f"проверка живучести по свежему сигналу не удалась (RPC): "
                                         f"{recheck.get('error')} -- статус маршрута НЕ изменён, пропускаю "
                                         f"этот сигнал")
                    continue
            self.busy = True
            try:
                self._evaluate_and_maybe_send(route, block_number, recv_t_monotonic)
            except Exception as exc:  # noqa: BLE001
                print(f"[hotpath] ошибка при оценке маршрута {route.label}: {exc}", file=sys.stderr)
            finally:
                self.busy = False

    def _evaluate_and_maybe_send(self, route: RouteCycle, block_number: int, recv_t_monotonic: float) -> None:
        # Пункт 6 (седьмой раунд, разбор владельца): "минимальная
        # инструментация -- ... количество RPC-вызовов" -- сброс В НАЧАЛЕ
        # оценки ЭТОГО кандидата (см. докстринг rpc_call_trading_path
        # выше -- однопоточный evaluator, безопасно).
        _reset_rpc_call_count()
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

        # ПРАВКА (седьмой раунд, разбор владельца, пункт 2): раньше здесь
        # было estimateGas(latest) -> blockNumber -> quote(blockNumber) --
        # ТРИ отдельных вызова НЕ гарантируют одно и то же состояние
        # (latest МОГ измениться МЕЖДУ первым eth_estimateGas и
        # последующим eth_blockNumber). Теперь -- ОДНА функция
        # (_quote_and_estimate_gas_consistent), которая либо получает
        # блок B ПЕРВЫМ и котирует+оценивает газ ИМЕННО на нём (явный
        # параметр блока у eth_estimateGas, реально поддержан этим RPC),
        # либо -- честный fallback с зафиксированными block_before/
        # block_after и одной повторной попыткой при сдвиге (см. её
        # докстринг). Это и есть причина категории "профит(до газа) был
        # бы положительным, профит(после газа) отрицательный" из
        # no_send_log пилота (7 записей, блоки 61755138 и др.).
        first_check = _quote_and_estimate_gas_consistent(route, recompute["amount_in"], self.contract_address,
                                                           calldata, self.from_address)
        if not first_check["ok"]:
            self.reason_log.log(route.route_id, route.label, first_check["reason"],
                                 f"{first_check['detail']} (режим={first_check['mode']}, "
                                 f"блок_до={first_check['block_before']}, блок_после={first_check['block_after']}, "
                                 f"сигнал был на {block_number})",
                                 size_in_raw=recompute["amount_in"], quote_block=first_check.get("block"),
                                 calldata_hex="0x" + calldata.hex(), rpc_call_count=_read_rpc_call_count())
            return
        effective_block = first_check["block"]
        effective_profit_raw = first_check["profit_raw"]
        gas_res = {"ok": True, "gas_estimate": first_check["gas_estimate"]}

        gas_price = int(rpc_call_trading_path("eth_gasPrice", []), 16)
        weth_usdg_price = current_weth_usdg_price()
        profit_after_gas, err = _profit_after_gas(effective_profit_raw, gas_res["gas_estimate"], gas_price,
                                                   weth_usdg_price)
        if profit_after_gas is None:
            self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, err,
                                 size_in_raw=recompute["amount_in"], quote_block=effective_block,
                                 calldata_hex="0x" + calldata.hex(), rpc_call_count=_read_rpc_call_count())
            return
        if profit_after_gas <= 0:
            # ПРАВКА (седьмой раунд): для mode="block_param" состояние
            # ДЕЙСТВИТЕЛЬНО одно (явный блок у обоих вызовов) -- честно
            # так и пишем. Для fallback-режимов НЕ заявляем "одно
            # состояние доказано" -- называем режим и фактические номера
            # блоков ДО/ПОСЛЕ, как они были получены.
            state_note = (f"согласованный блок {effective_block} (явный параметр блока у estimateGas)"
                          if first_check["mode"] == "block_param" else
                          f"режим={first_check['mode']}, блок_до={first_check['block_before']}, "
                          f"блок_после={first_check['block_after']}")
            # Пункт 6: этот кандидат был ПРИБЫЛЕН ДО газа (см. гейт
            # recompute["profit_raw"]<=0 выше, уже прошёл) -- ЭТО и есть
            # "прибыльный кандидат", отклонённый позже (после газа) --
            # сохраняем размер/calldata/блок котировки/число RPC-вызовов.
            self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                 f"профит после газа {profit_after_gas:.6f} <= 0 на {state_note} "
                                 f"(сигнал был на {block_number})",
                                 size_in_raw=recompute["amount_in"], quote_block=effective_block,
                                 calldata_hex="0x" + calldata.hex(), rpc_call_count=_read_rpc_call_count())
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
                rpc_call_count=_read_rpc_call_count(),
            )
            self.attempt_table.write(row)
            return

        # --- ФИНАЛЬНАЯ ПРОВЕРКА -- ТОЛЬКО выбранный размер, ОДНА
        # цепочка котировок (quote_route_at_size), НЕ вся сетка заново
        # (внешнее ревью, второй раунд, пункт 6). Если уже не подходит
        # -- пропускаем этот кандидат целиком, следующий сигнал придёт
        # своим чередом из очереди (не ищем альтернативный размер здесь).
        #
        # ПРАВКА (четвёртый раунд, пункт 9): block_number (исходный блок
        # ДЕТЕКЦИИ) НЕ подменяется здесь -- свежий блок ре-котировки
        # хранится ОТДЕЛЬНО (final_quote_block).
        #
        # ПРАВКА (седьмой раунд, разбор владельца, пункт 2): та же
        # _quote_and_estimate_gas_consistent, что и в первой проверке --
        # НЕ отдельные eth_blockNumber/quote_route_at_size/estimate_gas
        # по очереди (тот же класс несогласованности, что уже исправлен
        # выше). Перезапускаем ТОЛЬКО если reset действительно нужен
        # (текущий блок ушёл вперёд относительно effective_block) --
        # иначе оставляем уже согласованную пару без лишнего вызова. ---
        final_quote_block = effective_block
        final_amount_in = recompute["amount_in"]
        final_profit_raw = effective_profit_raw
        fresh_latest_final = int(rpc_call_trading_path("eth_blockNumber", []), 16)
        if fresh_latest_final > effective_block:
            final_check = _quote_and_estimate_gas_consistent(route, final_amount_in, self.contract_address,
                                                               calldata, self.from_address)
            if not final_check["ok"] or final_check["profit_raw"] <= 0:
                detail = final_check.get("detail", "") if not final_check["ok"] else (
                    f"профит {final_check['profit_raw']} <= 0")
                self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                     f"финальная проверка размера {final_amount_in} (режим="
                                     f"{final_check.get('mode')}, блок_до={final_check.get('block_before')}, "
                                     f"блок_после={final_check.get('block_after')}) не прошла: {detail} "
                                     f"(согласованное решение было на {effective_block}, сигнал -- на "
                                     f"{block_number}) -- уже не подходит, пропускаем кандидата")
                return
            final_profit_raw = final_check["profit_raw"]
            final_quote_block = final_check["block"]
            gas_res = {"ok": True, "gas_estimate": final_check["gas_estimate"]}
            gas_price = int(rpc_call_trading_path("eth_gasPrice", []), 16)
            weth_usdg_price = current_weth_usdg_price()
            profit_after_gas, err = _profit_after_gas(final_profit_raw, gas_res["gas_estimate"], gas_price,
                                                       weth_usdg_price)
            if profit_after_gas is None:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, err)
                return
            if profit_after_gas <= 0:
                self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                     f"финальная проверка: после газа {profit_after_gas:.6f} <= 0 на согласованном "
                                     f"блоке {final_quote_block} -- не отправляем")
                return

        # --- Пункт 4 (третий раунд): единый гейт "новых отправок" --
        # проверяется ЗДЕСЬ, непосредственно ПЕРЕД началом необратимой
        # части (подготовка -> резерв -> подпись -> сохранение ->
        # отправка) -- уже НАЧАТАЯ (эта самая) оценка обязана быть
        # остановлена, если дедлайн/STOP/бюджет/halt наступили, ПОКА она
        # считалась (не только при заборе из очереди). ---
        gate_reason = self._gate_blocks_new_send()
        if gate_reason is not None:
            self.reason_log.log(route.route_id, route.label, gate_reason,
                                 "единый гейт отправки сработал ПОСЛЕ расчёта, ДО подготовки к отправке (пункт 4)")
            return

        # --- Пункт 1 (третий раунд): ВСЕ предварительные RPC-чтения
        # (включая балансы контракта) -- ДО подготовки/резерва/подписи,
        # чтобы их сбой НЕ оставлял ни резерва, ни подписанной
        # транзакции без единой сохранённой (begin_attempt) попытки. ---
        route_tokens = sorted({leg.currency0.lower() for leg in route.legs} | {leg.currency1.lower() for leg in route.legs})
        pre_balances = {t: _token_balance(t, self.contract_address) for t in route_tokens}

        # --- Пункт 5/6 (третий/четвёртый раунд): СОГЛАСОВАННЫЙ minProfit
        # -- цикл "подготовить -> сверить закодированный порог с
        # требуемым ПО ЭТОЙ ЖЕ подготовленной транзакции -> при
        # несовпадении пересобрать" (см. докстринг
        # MAX_MIN_PROFIT_RECONCILE_ROUNDS про то, ПОЧЕМУ одного круга
        # недостаточно -- комиссия может измениться МЕЖДУ двумя
        # чтениями baseFee). Ограниченная последовательность -- НЕ
        # бесконечный подбор: после MAX_MIN_PROFIT_RECONCILE_ROUNDS
        # дополнительных кругов без совпадения -- честно пропускаем
        # кандидата. ---
        calldata_final = calldata  # семя (MIN_PROFIT_FLOOR_RAW=1) -- заведомо не пройдёт первую же сверку, запустит круг 0
        encoded_min_profit = MIN_PROFIT_FLOOR_RAW
        tx_fields: dict | None = None
        gas_res_final: dict | None = None
        for round_i in range(MAX_MIN_PROFIT_RECONCILE_ROUNDS + 1):
            gas_res_final = estimate_gas(self.contract_address, calldata_final, self.from_address)
            if not gas_res_final["ok"]:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, gas_res_final["error"])
                return
            try:
                tx_fields = self.sender.prepare_transaction_fields(self.contract_address, calldata_final,
                                                                     gas_res_final["gas_estimate"])
            except RuntimeError as exc:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED,
                                     f"prepare_transaction_fields (круг {round_i}) отказал: {exc}")
                return
            required_min_profit = compute_min_profit_raw(tx_fields["gas"], tx_fields["maxFeePerGas"],
                                                           route.exit_token, weth_usdg_price)
            if required_min_profit is None:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED,
                                     "не удалось согласовать minProfit со стоимостью газа (курс WETH/USDG "
                                     "недоступен) -- честно пропускаем кандидата, не гадаем")
                return
            if encoded_min_profit >= required_min_profit:
                break  # закодированный порог УЖЕ покрывает требуемый ПО ЭТОЙ ЖЕ подготовленной транзакции
            if round_i == MAX_MIN_PROFIT_RECONCILE_ROUNDS:
                self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED,
                                     f"minProfit не удалось согласовать за {MAX_MIN_PROFIT_RECONCILE_ROUNDS} "
                                     f"доп. круга (закодировано {encoded_min_profit} raw, требуется "
                                     f"{required_min_profit} raw) -- комиссия колеблется быстрее, чем успеваем "
                                     f"пересобрать, пропускаем кандидата (ограниченный, не бесконечный подбор)")
                return
            calldata_final = build_execute_cycle_calldata(route, first_amount_specified, min_profit=required_min_profit)
            encoded_min_profit = required_min_profit

        # ПРАВКА (седьмой раунд, разбор владельца, пункт 2): "после
        # окончательного изменения calldata пересчёт комиссии и minProfit
        # должен относиться к ОКОНЧАТЕЛЬНОЙ транзакции". final_profit_raw
        # до этой точки посчитан НА БОЛЕЕ РАННЕМ блоке (до цикла
        # согласования minProfit выше), а каждый круг того цикла заново
        # зовёт estimate_gas БЕЗ явного блока -- gas_res_final (итоговый,
        # использованный для tx_fields, который реально уйдёт в сеть)
        # мог резолвиться на более позднем состоянии. Пере-котируем
        # final_amount_in ПРЯМО СЕЙЧАС (протокол "блок до/после, ровно
        # одна повторная попытка" -- тот же, что в
        # _quote_and_estimate_gas_consistent) -- НЕ вызываем estimate_gas
        # заново (это запустило бы ЕЩЁ один круг prepare/sign, ровно тот
        # "бесконечный перебор", которого просили избегать); используем
        # УЖЕ зафиксированный gas_res_final/tx_fields (то, что реально
        # будет отправлено).
        for _pf_attempt in range(2):
            block_before_pf = int(rpc_call_trading_path("eth_blockNumber", []), 16)
            requote_pf = quote_route_at_size(route, final_amount_in, block_before_pf)
            if not requote_pf["ok"]:
                self.reason_log.log(route.route_id, route.label, requote_pf["reason"],
                                     f"пере-котировка перед итоговой проверкой (после согласования minProfit) "
                                     f"не прошла: {requote_pf['detail']} (блок {block_before_pf})")
                return
            block_after_pf = int(rpc_call_trading_path("eth_blockNumber", []), 16)
            if block_after_pf == block_before_pf:
                final_profit_raw = requote_pf["profit_raw"]
                final_quote_block = block_before_pf
                break
            if _pf_attempt == 1:
                self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                     f"блок сдвинулся ДВАЖДЫ подряд перед итоговой проверкой ({block_before_pf}"
                                     f"->{block_after_pf}) -- кандидат устарел, пропускаем (не бесконечный подбор)")
                return

        # Консервативная проверка ПОСЛЕ согласования (пункт 5: "избегать
        # бесконечного перебора... консервативная проверка") -- худший
        # случай стоимости газа (maxFeePerGas) ИТОГОВОЙ (после цикла)
        # подготовленной транзакции. НЕ утверждаем, что порог покрывает
        # газ ИМЕННО ОТКАТА -- revert profit не платит вообще.
        profit_after_gas_final, err_final = _profit_after_gas(final_profit_raw, gas_res_final["gas_estimate"],
                                                                tx_fields["maxFeePerGas"], weth_usdg_price)
        if profit_after_gas_final is None:
            self.reason_log.log(route.route_id, route.label, REASON_SIMULATION_FAILED, err_final)
            return
        if profit_after_gas_final <= 0:
            self.reason_log.log(route.route_id, route.label, REASON_NO_PROFITABLE_CYCLE,
                                 f"после согласования minProfit с газом ({encoded_min_profit} raw) профит "
                                 f"после газа {profit_after_gas_final:.6f} <= 0 -- не отправляем")
            return
        profit_after_gas = profit_after_gas_final

        ok_reserve, why_reserve = self.budget.reserve_for_send(tx_fields["gas"], tx_fields["maxFeePerGas"],
                                                                weth_usdg_price)
        if not ok_reserve:
            self.reason_log.log(route.route_id, route.label, why_reserve, "")
            return

        prepared = self.sender.sign_prepared_transaction(tx_fields)  # tx_hash ЛОКАЛЬНО, ДО сети; nonce НЕ продвигается (см. sender.py, пункт 1)

        # Пункт 9 (четвёртый раунд): "записывать согласованные значения"
        # -- пересчитываем state_age_blocks ЗДЕСЬ, ЕЩЁ РАЗ, относительно
        # final_quote_block (а не старого fresh_latest выше) -- честная
        # мера "насколько устарел computed_at_block ПРЯМО СЕЙЧАС, в
        # момент коммита попытки", без двойного смысла одного поля.
        commit_latest = int(rpc_call_trading_path("eth_blockNumber", []), 16)
        state_age_blocks = max(0, commit_latest - final_quote_block)

        latency_recv_to_commit_s = time.monotonic() - recv_t_monotonic
        # "Сохранить... данные попытки до первого сетевого обращения" --
        # begin_attempt ПЕРЕД submit_prepared. tx_fields_for_recovery
        # (пункт 2, третий раунд): НЕподписанные поля в сериализуемой
        # форме -- НЕ подписанная raw-транзакция -- позволяют
        # переподписать ТУ ЖЕ транзакцию (детерминированная подпись
        # eth_account/RFC6979) и сверить хэш побайтово при восстановлении
        # после краха ДО отправки, без хранения на диске готовой к
        # немедленной отправке подписи (см. _recover_before_broadcast).
        # latency_recv_to_send_s ЗДЕСЬ -- ТОЛЬКО оценка на момент коммита
        # (для восстановления после краха ДО отправки); честная,
        # ИЗМЕРЕННАЯ В МОМЕНТ ФАКТИЧЕСКОЙ ОТПРАВКИ величина считается
        # НИЖЕ (пункт 9: "время до отправки -- от фактической передачи
        # транзакции Sender'ом") и используется в ИТОГОВОЙ строке.
        self.budget.begin_attempt({
            **prepared.to_context(),
            "route_id": route.route_id, "route_label": route.label, "exit_token": route.exit_token,
            "size_in_raw": final_amount_in, "expected_profit_after_gas": profit_after_gas,
            "latency_recv_to_send_s": latency_recv_to_commit_s, "computed_at_block": final_quote_block,
            "state_age_blocks": state_age_blocks, "contract_address": self.contract_address,
            "route_tokens": route_tokens, "pre_balances": pre_balances,
            "reserved_price_used": weth_usdg_price,
            "tx_fields_for_recovery": _tx_fields_to_storable(tx_fields),
        })

        # --- Пункт 5 (четвёртый раунд): "проверять стоп непосредственно
        # перед отправкой -- если ещё не отправили, не отправлять после
        # остановки". Между гейтом выше и ЭТОЙ строкой прошли ещё RPC-
        # обращения (предчтения баланса, согласование minProfit,
        # резерв, подпись, begin_attempt) -- STOP/дедлайн МОГЛИ наступить
        # ИМЕННО в этом окне (воспроизведено владельцем: STOP выставлен
        # при чтении баланса, submit_prepared всё равно вызывался). Мы
        # ТОЧНО знаем, что submit_prepared ЕЩЁ НЕ вызывался -- ничего не
        # уходило в сеть -- поэтому НЕ гадаем и не пытаемся определить
        # состояние: просто НЕ отправляем, halt, pending СОХРАНЁН (без
        # gas_recorded) для следующего запуска (resolve_pending_tx_if_any,
        # ветка D, которая САМА откажется автоматически переотправлять
        # ТУ ЖЕ транзакцию, пока действует запрет -- см. её докстринг). ---
        # ВАЖНО: НЕ переиспользуем _gate_blocks_new_send() целиком здесь
        # -- та включает budget.can_send(), а can_send() блокирует по
        # "уже есть неподтверждённая транзакция", ЧТО ИМЕННО ТОЛЬКО ЧТО
        # СТАЛО ИСТИНОЙ строкой begin_attempt() выше (наша ЖЕ попытка) --
        # такой гейт срабатывал бы ВСЕГДА и блокировал КАЖДУЮ реальную
        # отправку. Здесь нужны ТОЛЬКО STOP/завершённый пилот/halted --
        # см. _new_send_forbidden_now (она НЕ проверяет pending).
        final_gate_reason = ("остановка запрошена (--duration-seconds/STOP) во время подготовки" if
                              self._no_new_candidates.is_set() else _new_send_forbidden_now(self.budget))
        if final_gate_reason is not None:
            self.budget.halt(f"{final_gate_reason} -- обнаружено МЕЖДУ подготовкой (begin_attempt) и "
                             f"сетевой отправкой (submit_prepared); транзакция {prepared.tx_hash} ТОЧНО НЕ "
                             f"отправлялась в этом процессе -- НЕ отправляем после остановки, pending "
                             f"сохранён для явного разбора (следующий запуск НЕ отправит её автоматически, "
                             f"см. _recover_before_broadcast)")
            print(f"[hotpath] СТОП: {self.budget.halt_reason}")
            return

        # Пункт 9 (четвёртый раунд): "время до отправки -- от
        # ФАКТИЧЕСКОЙ передачи транзакции Sender'ом, отдельно от
        # времени получения логов/ожидания очереди/расчёта" -- честная
        # задержка меряется ЗДЕСЬ, непосредственно перед фактическим
        # сетевым вызовом (не раньше, когда ещё шли prepare/reserve/
        # sign/begin_attempt -- это отдельная величина, см.
        # latency_recv_to_commit_s выше, используется ТОЛЬКО для
        # восстановления после краха ДО отправки).
        actual_send_latency_s = time.monotonic() - recv_t_monotonic
        result = self.sender.submit_prepared(prepared)
        # tx_hash ИЗВЕСТЕН ВСЕГДА (см. PreparedTx) -- "неопределённая
        # отправка" теперь означает result.unresolved=True, НЕ "не ушла".
        receipt = None
        if result.unresolved:
            print(f"[hotpath][pending] отправка {prepared.tx_hash} не подтверждена за стандартный таймаут -- "
                  f"доразрешаю сама, ОГРАНИЧЕННО (одна tx одновременно, следующая независимая сделка не "
                  f"начнётся)...")
            receipt = wait_for_receipt_bounded(prepared.tx_hash)
            if receipt is None:
                # Пункт 2/4 (третий раунд): "неизвестный результат не
                # должен превращаться в бесконечное блокирующее ожидание
                # без возможности штатно завершить процесс" -- честно
                # останавливаемся ЗДЕСЬ, pending СОХРАНЁН как есть
                # (следующий запуск разрешит его через
                # resolve_pending_tx_if_any, ветка D), резерв/бюджет НЕ
                # обнуляются, НЕ гадаем и НЕ ждём вечно.
                self.budget.halt(f"результат {prepared.tx_hash} остался неизвестным после ограниченного "
                                 f"ожидания ({UNRESOLVED_RECOVERY_TIMEOUT_S:.0f}с) -- pending СОХРАНЯЕТСЯ, "
                                 f"резерв/бюджет НЕ обнуляются, штатная остановка (не бесконечное ожидание)")
                print(f"[hotpath] СТОП: {self.budget.halt_reason}")
                return
            tx_status = _receipt_status(receipt)
            gas_used, effective_gas_price = _receipt_gas_used_and_price(receipt)
        else:
            tx_status = result.status
            gas_used, effective_gas_price = result.gas_used, None

        if receipt is None:
            # Добираем полный рецепт (нужен целиком -- логи -- для
            # CycleExecuted ниже, не только gasUsed/effectiveGasPrice).
            receipt = fetch_real_receipt(prepared.tx_hash)
        if receipt is not None and effective_gas_price is None:
            _, effective_gas_price = _receipt_gas_used_and_price(receipt)

        # Nonce подтверждён РЕАЛЬНЫМ рецептом (success ИЛИ revert -- оба
        # тратят nonce) -- ТЕПЕРЬ, и только теперь, продвигаем локальный
        # счётчик (пункт 1, третий раунд).
        self.sender.confirm_nonce_used(prepared.nonce)

        # --- Пункт 3/4: газ -- СРАЗУ, ДО любых других действий. ---
        weth_usdg_price_for_budget = current_weth_usdg_price()
        gas_res_fin = self.budget.finalize_gas(tx_status, gas_used, effective_gas_price, weth_usdg_price_for_budget)
        for threshold in gas_res_fin.get("newly_crossed", []):
            print(f"[hotpath][ПРОМЕЖУТОЧНЫЙ ОТЧЁТ] накопленные потери на газ достигли ${threshold:.0f} "
                  f"(итого: ${self.budget.cumulative_gas_loss_usd:.2f})")
        if not gas_res_fin.get("resolved"):
            # halted_missing_fields -- pending ОСТАЁТСЯ с gas_recorded
            # всё ещё False; строка попытки НЕ пишется здесь -- следующий
            # рестарт разберёт её через resolve_pending_tx_if_any (ветка
            # D) и допишет РОВНО одну строку там (без дубликата).
            print(f"[hotpath] СТОП: {self.budget.halt_reason}")
            return

        gas_cost_native = (gas_res_fin.get("gas_cost_wei", 0) or 0) / 1e18

        # --- Остальное (балансы, сверка учёта, прибыльная половина PnL)
        # -- сбой здесь переводит пилот в ОСТАНОВКУ. Газ уже учтён выше в
        # любом случае. ПРАВКА (пятый раунд, пункт 2): прибыль
        # начисляется ИСКЛЮЧИТЕЛЬНО из CycleExecuted ЭТОЙ конкретной
        # транзакции -- разница балансов ДО latest БОЛЬШЕ НЕ
        # используется как замена отсутствующему/некорректному событию
        # (за время между отправкой и обработкой контракт МОГ получить/
        # потратить средства другими транзакциями -- balance-diff не
        # доказывает принадлежность прироста ИМЕННО этой попытке).
        # Проверка балансов (check_no_unexpected_token_spend) ОСТАЁТСЯ
        # -- как НЕЗАВИСИМЫЙ сигнал утечки токенов, НЕ как источник
        # числа для профита. ---
        actual_gain_base_asset = None
        if tx_status == 1:
            try:
                actual_gain_raw = parse_cycle_executed_profit(receipt, self.contract_address, route.exit_token) if receipt else None
                post_balances = {t: _token_balance(t, self.contract_address) for t in route_tokens}
                ok_tokens, why_tokens = check_no_unexpected_token_spend(pre_balances, post_balances, route.exit_token)
                if not ok_tokens:
                    self.budget.halt(why_tokens)
                    print(f"[hotpath] СТОП: {why_tokens}")

                if actual_gain_raw is None:
                    self.budget.halt(f"не удалось подтвердить прибыль по событию CycleExecuted для "
                                     f"{prepared.tx_hash} -- газ уже учтён, прибыль ОСТАЁТСЯ "
                                     f"НЕУСТАНОВЛЕННОЙ (разница балансов до latest НЕ используется как "
                                     f"замена), pending СОХРАНЯЕТСЯ, требуется ручной разбор")
                    print(f"[hotpath] СТОП: {self.budget.halt_reason}")
                else:
                    actual_gain_base_asset = actual_gain_raw / 10**exit_decimals
                    ok_acct, why_acct = check_accounting_consistency(final_profit_raw, actual_gain_raw)
                    if not ok_acct:
                        self.budget.halt(why_acct)
                        print(f"[hotpath] СТОП: {why_acct}")
                    self.budget.finalize_profit_and_close(actual_gain_raw)
            except Exception as exc:  # noqa: BLE001
                self.budget.halt(f"пост-обработка после отправки (баланс/PnL) упала: {exc} -- "
                                  f"газ уже учтён, прибыль/закрытие попытки -- НЕТ, требуется разбор")
                print(f"[hotpath] СТОП (пост-обработка): {self.budget.halt_reason}", file=sys.stderr)
        # tx_status == 0 (откат) -- finalize_gas уже закрыл попытку
        # целиком (см. её докстринг), здесь ничего дополнительно
        # закрывать не нужно.

        # Пункт 1/3 (третий раунд): строка попытки пишется ТОЛЬКО когда
        # pending действительно финализирован (иначе -- пост-обработка
        # выше упала и halt уже сработал) -- иначе при более позднем
        # ручном/рестартовом восстановлении получился бы ДУБЛИКАТ строки
        # (см. _ensure_pending_row_written). mark_pending_row_written +
        # clear_finalized_pending -- РОВНО после того, как строка
        # надёжно записана.
        if self.budget.pending is not None and self.budget.pending.get("finalized"):
            row = AttemptTableRow(
                ts_wall=time.time(), route_label=route.label, route_id=route.route_id,
                size_in_raw=final_amount_in, exit_token=route.exit_token,
                expected_profit_after_gas=profit_after_gas,
                latency_recv_to_send_s=actual_send_latency_s, tx_hash=prepared.tx_hash,
                result="success" if tx_status == 1 else "reverted",
                actual_gain_base_asset=actual_gain_base_asset if tx_status == 1 else None,
                gas_used=gas_used, gas_cost_native=gas_cost_native,
                cumulative_gas_loss_usd=self.budget.cumulative_gas_loss_usd,
                cumulative_net_pnl_usd=self.budget.cumulative_net_pnl_usd,
                computed_at_block=final_quote_block, state_age_blocks=state_age_blocks,
                rpc_call_count=_read_rpc_call_count(),
            )
            self.attempt_table.write(row)
            self.budget.mark_pending_row_written()
            self.budget.clear_finalized_pending()
        else:
            print(f"[hotpath] попытка {prepared.tx_hash} НЕ финализирована полностью (см. СТОП выше) -- "
                  f"строка попытки НЕ пишется здесь во избежание дубликата, будет дописана при "
                  f"восстановлении на следующем запуске (resolve_pending_tx_if_any).")


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
    ap.add_argument("--new-session", action="store_true",
                     help="Пункт 5 (седьмой раунд, разбор владельца): явный перезапуск ПОСЛЕ уже "
                          "завершённого пилота (budget.pilot_completed=true) -- открывает НОВУЮ сессию "
                          "(pilot_completed/pilot_started_at сбрасываются, PilotBudget.start_new_session()), "
                          "СОХРАНЯЯ накопленный газ/PnL/общий лимит $20. Если пилот НЕ завершён (или "
                          "halted -- отдельная, ручная причина) -- флаг НИЧЕГО не меняет: НЕ обходит halt, "
                          "НЕ отменяет разрешение pending (то уже отработало раньше по коду, см. _main()).")
    args = ap.parse_args()

    # Пункт 8 (четвёртый раунд): ДО ЛЮБОГО RPC-вызова этого процесса --
    # см. докстринг LIVE_RPC_RETRY_BUDGET_S.
    alchemy_fallback.set_rate_limit_wait_budget_s(LIVE_RPC_RETRY_BUDGET_S)

    lock = _SingleInstanceLock(args.from_address)
    lock.acquire()
    try:
        _main(args)
    finally:
        lock.release()


def _main(args) -> None:
    # Пункт 6 (внешнее ревью, третий раунд): "отдельные файлы
    # nonce/бюджета/лока для отдельного кошелька пилота". ПО УМОЛЧАНИЮ --
    # ТЕ ЖЕ пути, что и раньше (ничего не меняется, если переменные
    # окружения не заданы); отдельный launch-конфиг НОВОГО кошелька
    # пилота задаёт свои три переменные, указывая на СВОИ файлы -- не
    # трогая состояние старого кошелька/других задач. Однопоточный лок
    # (_SingleInstanceLock, main()) уже ключуется адресом кошелька
    # отдельно от этого.
    budget = PilotBudget(state_path=os.environ.get(
        "PILOT_BUDGET_STATE_FILE", "/home/bot/data/task5_v4_pilot_budget_state.json"))
    attempt_table = AttemptTable(path=os.environ.get(
        "PILOT_ATTEMPT_TABLE_FILE", "/home/bot/data/task5_v4_pilot_attempts.jsonl"))
    reason_log = ReasonLog(path=os.environ.get(
        "PILOT_REASON_LOG_FILE", "/home/bot/data/task5_v4_pilot_no_send_log.jsonl"))

    sender = None
    if args.confirm_mainnet:
        import task5_bot_sender
        sender = task5_bot_sender.Sender()
        verify_wallet_consistency(args.contract_address, args.from_address, sender.address)

    # Пункт 4 (третий раунд): "на следующем запуске всегда сначала
    # разрешать pending, ДАЖЕ ЕСЛИ час пилота уже завершён -- завершение
    # часа никогда не переавторизует новую торговлю" -- поэтому resolve
    # ПЕРЕД проверкой pilot_completed (раньше было наоборот: pilot_completed
    # мог вернуться, даже не попытавшись разрешить незакрытую попытку).
    resolve_pending_tx_if_any(budget, sender, attempt_table)
    if sender is not None:
        verify_nonce_consistency(sender, budget)
    if budget.halted:
        print(f"[hotpath] ОСТАНОВЛЕН (см. состояние бюджета, после разрешения pending): {budget.halt_reason}")
        return

    # Пункт 5 (седьмой раунд, разбор владельца): --new-session -- ПОСЛЕ
    # halted (halt -- ручная причина, этот флаг её не обходит) и ПОСЛЕ
    # resolve_pending_tx_if_any (уже отработал выше, независимо от этого
    # флага) -- ТОЛЬКО если пилот ДЕЙСТВИТЕЛЬНО завершён, открываем новую
    # сессию (сохраняя газ/PnL/лимит, см. докстринг start_new_session()).
    if args.new_session and budget.pilot_completed:
        print(f"[hotpath] --new-session: предыдущая сессия была завершена ({budget.pilot_completed_reason}) -- "
              f"открываю НОВУЮ (накопленный газ ${budget.cumulative_gas_loss_usd:.2f}, net PnL "
              f"${budget.cumulative_net_pnl_usd:.2f} -- СОХРАНЕНЫ, общий лимит ${BUDGET_STOP_USD:.0f} не менялся)")
        budget.start_new_session()

    if budget.pilot_completed:
        print(f"[hotpath] ПИЛОТ УЖЕ ЗАВЕРШЁН ({budget.pilot_completed_reason}) -- новый час НЕ начинается "
              f"автоматически. Для нового пилота -- явное решение владельца (напр. новое состояние budget).")
        return

    # Пункт 3 (четвёртый раунд): "сохранять реестр и позицию обработанных
    # блоков для продолжения после рестарта" -- ПО УМОЛЧАНИЮ путь ниже
    # (новый, ничего раньше не занимал) -- если файла ещё нет,
    # bootstrap_registry() честно делает полный lookback-скан, как
    # раньше; со второго запуска -- инкрементальный докат от сохранённого
    # курсора.
    route_registry_state_path = os.environ.get(
        "ROUTE_REGISTRY_STATE_FILE", "/home/bot/data/task5_v4_route_registry_state.json")
    print("[hotpath] бутстрап реестра (сид/сохранённое состояние + обнаружение пулов арбитражника)...")
    registry, latest = bootstrap_registry(state_path=route_registry_state_path)
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
    background_worker = BackgroundRegistryWorker(registry, priority_hint, start_from_block=latest,
                                                  state_path=route_registry_state_path)
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
                # Пункт 5 (четвёртый раунд): "раздели 'поток закончил
                # текущую работу' и 'результат транзакции известен'".
                # РАНЬШЕ условие выхода требовало ЕЩЁ и budget.pending
                # is None -- после ограниченного ожидания рецепта
                # halt+pending СОХРАНЁН НАВСЕГДА (намеренно, никто
                # больше его не тронет, пока идёт остановка), поэтому
                # старое условие никогда не выполнялось -- главный цикл
                # печатал "жду завершения" бесконечно (реально
                # воспроизведено владельцем). Теперь выход -- как только
                # поток-оценщик закончил ТЕКУЩУЮ единицу работы,
                # НЕЗАВИСИМО от того, разрешилась ли pending-попытка;
                # "неизвестный результат" -- явная, отдельно
                # напечатанная терминальная ветка, а не блокирующее
                # ожидание.
                if hotpath.evaluator_finished_current_work():
                    reason = why if (budget.halted or budget.pilot_completed) else "истёк --duration-seconds"
                    unresolved = budget.pending is not None
                    # pilot_completed выставляется ВСЕГДА (даже при
                    # unresolved) -- пункт 4: "завершение часа никогда
                    # не переавторизует новую торговлю"; resolve_pending_
                    # tx_if_any на следующем запуске всё равно отработает
                    # ДО этой проверки (см. main()) и сможет закрыть
                    # pending, не начиная новых сделок.
                    budget.complete_pilot(reason)
                    print(f"[hotpath] === ИТОГ ПИЛОТА ===")
                    print(f"[hotpath]   причина завершения: {reason}")
                    print(f"[hotpath]   неизвестные (неразрешённые) результаты остались: {unresolved}")
                    if unresolved:
                        print(f"[hotpath]   ВНИМАНИЕ: результат попытки {budget.pending.get('tx_hash')} НЕ "
                              f"определён -- pending СОХРАНЁН, бюджет/резерв НЕ обнулены. Итог пилота -- "
                              f"'неизвестный результат', НЕ 'успешно завершён'. Требуется resolve_pending_tx_if_any "
                              f"на следующем запуске (читает receipt/завершает учёт, НЕ отправляет новых сделок).")
                    # Пункт 9 (четвёртый раунд): "известные ограничения
                    # указать в итоговом отчёте -- чистые V4-циклы,
                    # старт USDG/native ETH, текущая конечная сетка
                    # размеров. Не выдавать это за полный охват маршрутов
                    # чужого бота." -- честно печатается БЕЗУСЛОВНО, не
                    # только в чат-отчёте владельцу.
                    print(f"[hotpath]   ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ ЭТОГО ПИЛОТА (не полный охват): только чистые "
                          f"V4-циклы из 2-3 плеч (без смешанных V3+V4 маршрутов -- см. докстринг "
                          f"build_cycles_from_pools); маршруты обязаны начинаться/заканчиваться в "
                          f"USDG или нативном ETH (START_TOKENS); подбор размера -- по ФИКСИРОВАННОЙ сетке "
                          f"SIZE_GRID_BY_START_TOKEN, не непрерывный поиск оптимума. Это НЕ полный охват "
                          f"маршрутов, которые может использовать другой (наблюдаемый) арбитражник.")
                    _print_status_report("ИТОГ", registry, budget, attempt_table, pilot_start_wall)
                    print(f"[hotpath] ====================")
                    break
                else:
                    print(f"[hotpath] ...жду завершения текущей оценки перед остановкой "
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
        # Пункт 3 (четвёртый раунд): финальное сохранение реестра при
        # штатном завершении -- следующий запуск догонит от актуального
        # курсора, а не от того, что было записано до 30с назад.
        try:
            save_registry_state(registry, background_worker._last_checked_block, route_registry_state_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[hotpath] не удалось сохранить финальное состояние реестра (не критично): {exc}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
