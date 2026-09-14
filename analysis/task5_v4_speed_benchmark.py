#!/usr/bin/env python3
"""Задача (владелец): измерить ПРЕДЕЛ УСКОРЕНИЯ реального горячего пути
(analysis/task5_v4_hotpath.py) и Sender (analysis/task5_bot_sender.py) --
БЕЗ торговых отправок, БЕЗ покупки инфраструктуры, БЕЗ изменения
экономики стратегии.

ЧТО ЭТО: безопасный измерительный режим на РЕАЛЬНОМ, НЕИЗМЕНЁННОМ коде
продакшена. НЕ повторяет и НЕ переизобретает логику -- вызывает те же
самые функции/методы, что вызывает living hotpath (recompute_route,
build_execute_cycle_calldata, _quote_and_estimate_gas_consistent,
estimate_gas, quote_route_at_size, _token_balance,
current_weth_usdg_price, Sender.prepare_transaction_fields,
Sender.sign_prepared_transaction), с РЕАЛЬНЫМИ RPC-вызовами против
текущего состояния сети. ФИЗИЧЕСКИ исключена отправка: Sender.
submit_prepared заменён на функцию-заглушку, которая поднимает
_BenchmarkStopBeforeSend СРАЗУ после записи метки "готов к отправке" --
код НИКОГДА не доходит до w3.eth.send_raw_transaction. Подпись выполняется
ОДНОРАЗОВЫМ локальным тестовым ключом (сгенерирован здесь же,
Account.create(), никогда не PRIVATE_KEY_NOX) -- переменная окружения
PRIVATE_KEY_NOX временно устанавливается В ЭТОМ ПРОЦЕССЕ до импорта
task5_bot_sender и никогда не пишется на диск/в лог.

ЧТО ЭТО НЕ: не обычный LIVE-пилот (--duration-seconds здесь не участвует,
циклический hotpath.main() не запускается), не нагрузочный тест (N мал,
по умолчанию 3-5 независимых прогонов -- честно так и помечено), не
источник новых торговых решений (все РЕАЛЬНЫЕ RPC-чтения -- read-only:
eth_call/eth_estimateGas/eth_blockNumber/eth_gasPrice/eth_getBalance/
eth_getBlockByNumber; ни одного eth_sendRawTransaction).

СТАРЫЙ ЭКСПЕРИМЕНТ УСКОРЕНИЯ ВЫЧИСЛИТЕЛЬНОГО ЯДРА (владелец, п.1) --
ИСКАЛСЯ И НЕ НАЙДЕН: git log --all -i --grep/-S по "chatgpt",
"ускорение вычислительного ядра", "speedup", "compute core" -- ни одного
релевантного совпадения ни в истории коммитов, ни в текущем дереве.
Коэффициент ускорения из такого эксперимента НЕ подставляется -- он
просто не существует в этом репозитории (см. финальный отчёт).

ГРАНИЦЫ queue_wait_s/calc_duration_s/route_full_wait_s/signal_block/
quote_block (владелец, п.1): это ИЗМЕРЯЕМЫЕ поля лога попыток
(ReasonLog/AttemptTableRow, task5_v4_pilot_accounting.py), НЕ пороги/
таймауты -- ни один из них не ограничивает время выполнения. Реальные
константы, которые ДЕЙСТВИТЕЛЬНО гейтуют горячий путь: POLL_INTERVAL_S=
0.1с (опрос детектора), DISCOVERY_POLL_INTERVAL_S=1.0с,
TRADING_PRIORITY_WINDOW_S=2.0с, MAX_BACKGROUND_STARVATION_S=5.0с,
LIVENESS_REFRESH_INTERVAL_S=60с, UNRESOLVED_RECOVERY_TIMEOUT_S=300с,
MAX_MIN_PROFIT_RECONCILE_ROUNDS=2 (task5_v4_hotpath.py:180-252).

ДВА "БЕЗОПАСНЫХ" УЛУЧШЕНИЯ, проверяемых здесь (НЕ меняют экономику --
только транспорт одинаковых RPC-вызовов):
  A) "pool"  -- переиспользование HTTP-соединения (requests.Session)
     вместо requests.post(...) заново на КАЖДЫЙ вызов (alchemy_fallback.py
     делает голый requests.post -- новое TCP+TLS рукопожатие на каждый
     RPC, подтверждено чтением кода).
  B) "lane"  -- _token_balance()/current_weth_usdg_price() сейчас идут
     через МЕДЛЕННЫЙ, некоунченый _rpc_call (throttle 0.5с, публичный-
     RPC-первый), а не через уже используемый быстрый rpc_call_trading_path
     (throttle 0.1с, Alchemy-прямой) -- переключение лейна НЕ меняет ни
     метод, ни параметры запроса, только транспорт/приоритет.
Обе правки применяются ТОЛЬКО monkeypatch'ем В ЭТОМ скрипте -- ни один
файл продакшена не редактируется.

Команда запуска (read-only, на Ohio, через существующий workflow):
  venv/bin/python analysis/task5_v4_speed_benchmark.py --repeats 5 \\
      --variants baseline pool lane both --out-dir /home/bot/data/task5_v4_speed_benchmark
"""
from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import os  # noqa: E402
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

# --- Безопасность (владелец, п.2): одноразовый ЛОКАЛЬНЫЙ тестовый ключ.
# ДО импорта task5_bot_sender (он читает PRIVATE_KEY_NOX при импорте
# модуля -- см. task5_bot_sender.py:91). Ключ существует ТОЛЬКО в памяти
# этого процесса, никогда не записывается на диск и не логируется. ---
from eth_account import Account as _EthAccount  # noqa: E402

_BENCH_TEST_ACCOUNT = _EthAccount.create()
os.environ["PRIVATE_KEY_NOX"] = _BENCH_TEST_ACCOUNT.key.hex()
# КРИТИЧНО (реальная находка при чтении кода, не предположение):
# Sender.__init__ читает/пишет STATE_FILE = SENDER_STATE_FILE или ПО
# УМОЛЧАНИЮ /home/bot/data/sender_state.json -- ТОТ ЖЕ файл, что реальный
# продакшн-Sender. Без переопределения ЭТОТ бенчмарк (с чужим тестовым
# адресом/ключом) перезаписал бы РЕАЛЬНЫЙ nonce-стейт пилота (__init__
# делает get_transaction_count(self.address,"pending") + state.save(),
# self.address здесь -- тестовый, не пилотный). ОБЯЗАТЕЛЬНО принудительно
# (не setdefault) -- ДО импорта task5_bot_sender.
_BENCH_TMP_DIR_FOR_ENV = Path(
    os.environ.get("TASK5_V4_SPEED_BENCH_OUT_DIR", str(Path(__file__).parent.parent / "data" / "task5_v4_speed_benchmark")))
_BENCH_TMP_DIR_FOR_ENV.mkdir(parents=True, exist_ok=True)
os.environ["SENDER_STATE_FILE"] = str(_BENCH_TMP_DIR_FOR_ENV / "sender_state_BENCHMARK_ONLY.json")
# Верхний бюджет циклических daily-loss/consecutive-loss ограничителей
# Sender'а -- ничего не отправляем, но __init__ их читает; значения по
# умолчанию (task5_bot_sender.py:103-104) безопасны и не требуют правки.

import requests  # noqa: E402

import alchemy_fallback  # noqa: E402
import task5_bot_sender  # noqa: E402
import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_pilot_accounting import AttemptTable, PilotBudget, ReasonLog  # noqa: E402
from task5_v4_route_registry import seed_routes  # noqa: E402

CONTRACT_ADDRESS = "0xAB24907bceEF4EDC366a1E4DfA15ed8aA5fdfe71"
OWNER_ADDRESS = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
OUT_DIR_DEFAULT = Path(__file__).parent.parent / "data" / "task5_v4_speed_benchmark"

print(f"[bench] тестовый одноразовый адрес подписи (НЕ реальный пилот): {_BENCH_TEST_ACCOUNT.address}",
      file=sys.stderr)


class _BenchmarkStopBeforeSend(Exception):
    """Заглушка submit_prepared поднимает ЭТО сразу после фиксации метки
    'готов к отправке' -- реальная сеть/w3.eth.send_raw_transaction
    физически недостижимы. Перехватывается только здесь."""

    def __init__(self, prepared, t_ready_to_send: float) -> None:
        self.prepared = prepared
        self.t_ready_to_send = t_ready_to_send


def _stub_submit_prepared(self, prepared):  # noqa: ANN001
    raise _BenchmarkStopBeforeSend(prepared, time.monotonic())


task5_bot_sender.Sender.submit_prepared = _stub_submit_prepared


# ---------------------------------------------------------------------
# Инструментация: монотонные метки + CPU-время на каждый РЕАЛЬНЫЙ вызов,
# без изменения самих функций (обёртка поверх, оригинал вызывается как
# есть). Вложенные вызовы (напр. quote_route_at_size внутри
# recompute_route) дают ВЛОЖЕННЫЕ по времени записи в trace -- это
# ожидаемо и честно показывает структуру, не скрывается.
# ---------------------------------------------------------------------
class _Trace:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, stage: str, t_start: float, t_end: float, cpu_start: float, cpu_end: float,
               error: str | None = None) -> None:
        self.events.append({
            "stage": stage, "t_start_monotonic": t_start, "t_end_monotonic": t_end,
            "wall_s": t_end - t_start, "cpu_s": cpu_end - cpu_start, "error": error,
        })


_CURRENT_TRACE: _Trace | None = None


def _timed_call(stage: str, fn, *args, **kwargs):
    t_start = time.monotonic()
    cpu_start = time.process_time()
    error = None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        t_end = time.monotonic()
        cpu_end = time.process_time()
        if _CURRENT_TRACE is not None:
            _CURRENT_TRACE.record(stage, t_start, t_end, cpu_start, cpu_end, error)


@contextlib.contextmanager
def optimization_variant(name: str):
    """baseline -- ничего не патчим (реальный код продакшена как есть).
    pool -- HTTP keep-alive (requests.Session вместо requests.post
    заново). lane -- _token_balance/current_weth_usdg_price через уже
    существующий быстрый rpc_call_trading_path вместо медленного
    некоунченого _rpc_call. both -- обе правки. Каждая -- ЧИСТО
    транспортная: метод/параметры RPC-вызова НЕ меняются, ответ сети
    определяет исход ТАК ЖЕ, как в продакшене."""
    assert name in ("baseline", "pool", "lane", "both"), name
    session = None
    original_post = None
    original_rpc_call = None
    try:
        if name in ("pool", "both"):
            session = requests.Session()
            original_post = alchemy_fallback.requests.post
            alchemy_fallback.requests.post = session.post
        if name in ("lane", "both"):
            original_rpc_call = hp._rpc_call
            hp._rpc_call = hp.rpc_call_trading_path
        yield
    finally:
        if original_post is not None:
            alchemy_fallback.requests.post = original_post
        if original_rpc_call is not None:
            hp._rpc_call = original_rpc_call
        if session is not None:
            session.close()


def _fresh_accounting(tmp_dir: Path, tag: str):
    tmp_dir.mkdir(parents=True, exist_ok=True)
    budget = PilotBudget(state_path=str(tmp_dir / f"budget_{tag}.json"))
    attempt_table = AttemptTable(path=str(tmp_dir / f"attempts_{tag}.jsonl"))
    reason_log = ReasonLog(path=str(tmp_dir / f"no_send_{tag}.jsonl"))
    return budget, attempt_table, reason_log


def run_direct_primitive_probe(route, sender, run_index: int) -> dict:
    """Component A+C: таймит РЕАЛЬНЫЕ функции по отдельности, на РЕАЛЬНОМ
    текущем состоянии сети, НЕЗАВИСИМО от того, прибылен ли кандидат
    прямо сейчас (что типично -- см. итоговый отчёт, реальные
    прибыльные моменты редки). Честная, не выдуманная стоимость КАЖДОЙ
    стадии реального вызова."""
    global _CURRENT_TRACE
    trace = _Trace()
    _CURRENT_TRACE = trace
    hp._reset_rpc_call_count()
    result: dict = {"route_id": route.route_id, "route_label": route.label, "run_index": run_index}
    try:
        block_number = int(_timed_call("state_ready_block_fetch", hp.rpc_call_trading_path,
                                        "eth_blockNumber", []), 16)
        result["block_number"] = block_number
        recompute = _timed_call("route_search_and_sizing", hp.recompute_route, route, block_number)
        result["recompute_ok"] = recompute["ok"]
        if not recompute["ok"]:
            result["reason"] = recompute.get("reason")
            result["ok"] = True  # честный, реальный ранний отказ -- НЕ ошибка бенчмарка
            return result
        amount_in = recompute["amount_in"]
        result["amount_in_raw"] = amount_in
        result["profit_before_gas_raw"] = recompute["profit_raw"]
        first_amount_specified = -amount_in
        calldata = _timed_call("calldata_build", hp.build_execute_cycle_calldata, route,
                                first_amount_specified, min_profit=hp.MIN_PROFIT_FLOOR_RAW)
        check = _timed_call("simulate_and_gas_estimate", hp._quote_and_estimate_gas_consistent,
                             route, amount_in, CONTRACT_ADDRESS, calldata, OWNER_ADDRESS,
                             original_block=block_number, original_profit_raw=recompute["profit_raw"])
        result["quote_gas_check_ok"] = check["ok"]
        gas_estimate = None
        if check["ok"]:
            gas_estimate = check["gas_estimate"]
            gas_res = _timed_call("gas_estimate_direct", hp.estimate_gas, CONTRACT_ADDRESS, calldata,
                                   OWNER_ADDRESS)
            result["gas_estimate_direct_ok"] = gas_res["ok"]
            if gas_res["ok"]:
                gas_estimate = gas_res["gas_estimate"]
        route_tokens = sorted({leg.currency0.lower() for leg in route.legs} |
                               {leg.currency1.lower() for leg in route.legs})
        for token in route_tokens:
            _timed_call("token_balance_read", hp._token_balance, token, CONTRACT_ADDRESS)
        price = _timed_call("weth_price_read", hp.current_weth_usdg_price)
        result["weth_usdg_price"] = price
        gas_price = int(_timed_call("gas_price_read", hp.rpc_call_trading_path, "eth_gasPrice", []), 16)
        result["gas_price_wei"] = gas_price
        tx_fields = _timed_call("nonce_calldata_prepare", sender.prepare_transaction_fields,
                                 CONTRACT_ADDRESS, calldata, gas_estimate or 300_000)
        prepared = _timed_call("sign", sender.sign_prepared_transaction, tx_fields)
        result["prepared_tx_hash"] = prepared.tx_hash
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["rpc_call_count"] = hp._read_rpc_call_count()
        result["trace"] = list(trace.events)
        _CURRENT_TRACE = None
    return result


def run_full_attempt_probe(route, sender, tmp_dir: Path, tag: str) -> dict:
    """Полный, НЕИЗМЕНЁННЫЙ HotPath._evaluate_and_maybe_send() -- реальный
    метод продакшена, единственная подмена -- submit_prepared (заглушка).
    Останавливается ИЛИ на реальной причине отказа (типичный исход --
    см. отчёт), ИЛИ на _BenchmarkStopBeforeSend (кандидат дошёл до
    готовности к отправке). Своя, ОТДЕЛЬНАЯ, одноразовая бухгалтерия
    (throwaway-путь) -- реальный /home/bot/data/... пилота НЕ трогается."""
    global _CURRENT_TRACE
    trace = _Trace()
    _CURRENT_TRACE = trace
    hp._reset_rpc_call_count()
    budget, attempt_table, reason_log = _fresh_accounting(tmp_dir, tag)
    priority_hint = hp._RpcPriorityHint()
    hotpath = hp.HotPath(registry=None, contract_address=CONTRACT_ADDRESS, from_address=OWNER_ADDRESS,
                          budget=budget, attempt_table=attempt_table, reason_log=reason_log,
                          priority_hint=priority_hint, sender=sender, dry_run=False)
    result: dict = {"route_id": route.route_id, "route_label": route.label}
    recv_t = time.monotonic()
    try:
        block_number = int(_timed_call("state_ready_block_fetch", hp.rpc_call_trading_path,
                                        "eth_blockNumber", []), 16)
        result["block_number"] = block_number
        # ЧЕСТНО: t_dequeued=t_first_enqueued=recv_t -- реальной очереди
        # здесь НЕТ (прямой одиночный вызов). Реальная задержка очереди
        # моделируется ОТДЕЛЬНО в replay-компоненте (Component B), не
        # здесь -- не выдаём 0 за измеренный queue_wait_s продакшена.
        hotpath._evaluate_and_maybe_send(route, block_number, recv_t, recv_t, first_enqueued_t_monotonic=recv_t)
        result["outcome"] = "returned_without_send"
        last_reason = _last_jsonl_line(Path(reason_log.path))
        result["stop_reason"] = last_reason.get("reason") if last_reason else None
        result["stop_detail"] = last_reason.get("detail") if last_reason else None
    except _BenchmarkStopBeforeSend as stop:
        result["outcome"] = "reached_ready_to_send"
        result["ready_to_send_latency_s"] = stop.t_ready_to_send - recv_t
        result["prepared_tx_hash"] = stop.prepared.tx_hash
    except Exception as exc:  # noqa: BLE001
        result["outcome"] = "exception"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["recv_t_monotonic"] = recv_t
        result["rpc_call_count"] = hp._read_rpc_call_count()
        result["trace"] = list(trace.events)
        _CURRENT_TRACE = None
    return result


def _last_jsonl_line(path: Path) -> dict | None:
    if not path.exists():
        return None
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except Exception:  # noqa: BLE001
        return None


def _pctl(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def _stage_stats(probes: list[dict]) -> dict:
    """Агрегирует wall_s по имени стадии ПО ВСЕМ прогонам -- median/p95,
    честно только там, где стадия реально была достигнута (не все
    прогоны доходят одинаково далеко)."""
    by_stage: dict[str, list[float]] = {}
    total_wall: list[float] = []
    rpc_counts: list[int] = []
    for p in probes:
        events = p.get("trace", [])
        for e in events:
            by_stage.setdefault(e["stage"], []).append(e["wall_s"])
        if events:
            total_wall.append(sum(e["wall_s"] for e in events))
        if "rpc_call_count" in p:
            rpc_counts.append(p["rpc_call_count"])
    stats = {
        stage: {"n": len(vals), "median_s": statistics.median(vals), "p95_s": _pctl(vals, 0.95),
                "mean_s": statistics.mean(vals), "sum_s": sum(vals)}
        for stage, vals in by_stage.items()
    }
    return {
        "per_stage": stats,
        "n_probes": len(probes),
        "total_traced_wall_s_median": statistics.median(total_wall) if total_wall else None,
        "rpc_call_count_median": statistics.median(rpc_counts) if rpc_counts else None,
    }


def parity_check(routes) -> dict:
    """Владелец, п.4: 'проверяй совпадение результатов расчёта'. Пинует
    ОДИН явный, недавний блок и вызывает quote_route_at_size с
    ОДИНАКОВЫМ (route, amount_in, block) под baseline и под каждым
    оптимизированным транспортом -- ЭТО чистая проверка транспорта, не
    экономики (тот же метод/параметры RPC, тот же блок)."""
    fixed_block = int(hp.rpc_call_trading_path("eth_blockNumber", []), 16)  # свежий, но зафиксированный --
    # ОДИН и тот же для всех вариантов ниже, чтобы сравнение было честным (не "разный блок -> разный ответ")
    route = routes[0]
    amount_in = 10 ** 15  # фиксированный, произвольный, ОДИНАКОВЫЙ вход для всех вариантов
    reference = None
    results = {}
    for variant in ("baseline", "pool", "lane", "both"):
        with optimization_variant(variant):
            try:
                r = hp.quote_route_at_size(route, amount_in, fixed_block)
            except Exception as exc:  # noqa: BLE001
                r = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        results[variant] = r
        if variant == "baseline":
            reference = r
    matches = {}
    for variant, r in results.items():
        if variant == "baseline":
            continue
        matches[variant] = (r == reference)
    return {"fixed_block": fixed_block, "amount_in": amount_in, "results": results,
            "all_match_baseline": all(matches.values()), "per_variant_match": matches}


def idealized_limits(direct_stats: dict) -> dict:
    """Владелец, п.4: пределы БЕЗ ожидания RPC / БЕЗ очереди / CPU x2,x4
    -- ПОМЕЧЕНЫ как сценарии, НЕ достигнутые результаты. Вычислено из
    УЖЕ измеренных (не выдуманных) per-stage медиан этого же прогона."""
    per_stage = direct_stats.get("per_stage", {})
    rpc_stage_names = {
        "state_ready_block_fetch", "simulate_and_gas_estimate", "gas_estimate_direct",
        "token_balance_read", "weth_price_read", "gas_price_read", "nonce_calldata_prepare",
        "route_search_and_sizing",
    }
    cpu_stage_names = {"calldata_build", "sign"}
    total_median = direct_stats.get("total_traced_wall_s_median") or 0.0
    rpc_wall_sum = sum(v["median_s"] * v["n"] / max(v["n"], 1) for k, v in per_stage.items() if k in rpc_stage_names)
    # Грубая, честно ПОМЕЧЕННАЯ оценка: RPC-часть = сумма медиан
    # RPC-помеченных стадий (включая вложенные quote-вызовы внутри
    # route_search_and_sizing -- уже включены в её собственную сумму
    # времени, см. sum_s ниже для точности).
    rpc_wall_sum = sum(per_stage[k]["sum_s"] / max(per_stage[k]["n"], 1) for k in per_stage if k in rpc_stage_names)
    cpu_wall_sum = sum(per_stage[k]["sum_s"] / max(per_stage[k]["n"], 1) for k in per_stage if k in cpu_stage_names)
    return {
        "caveat": "СЦЕНАРИИ, НЕ измеренные результаты -- вычислены из уже собранных медиан этого прогона.",
        "measured_total_median_s": total_median,
        "measured_rpc_bound_portion_s": rpc_wall_sum,
        "measured_cpu_bound_portion_s": cpu_wall_sum,
        "scenario_no_rpc_wait_s": max(0.0, total_median - rpc_wall_sum),
        "scenario_no_queue_note": "нет реальной очереди в прямом вызове этого пробника -- см. отдельный "
                                   "replay-анализ (Component B) для оценки очереди.",
        "scenario_cpu_half_s": total_median - cpu_wall_sum + cpu_wall_sum / 2,
        "scenario_cpu_quarter_s": total_median - cpu_wall_sum + cpu_wall_sum / 4,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repeats", type=int, default=3, help="Независимых прогонов на маршрут/вариант "
                                                             "(МАЛО -- это не нагрузочный тест).")
    ap.add_argument("--variants", nargs="+", default=["baseline", "pool", "lane", "both"],
                     choices=["baseline", "pool", "lane", "both"])
    ap.add_argument("--out-dir", type=str, default=str(OUT_DIR_DEFAULT))
    ap.add_argument("--skip-full-attempt", action="store_true",
                     help="Пропустить полный _evaluate_and_maybe_send (только прямые пробники).")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    routes = seed_routes()

    sender = task5_bot_sender.Sender()  # реальный класс, submit_prepared уже заглушен глобально выше
    print(f"[bench] маршрутов (seed_routes, реально проверенных на форке): {len(routes)}", file=sys.stderr)
    for r in routes:
        print(f"[bench]   {r.route_id}: {r.label} ({len(r.legs)} плеч)", file=sys.stderr)

    report: dict = {"generated_at_unix": time.time(), "test_account_address": _BENCH_TEST_ACCOUNT.address,
                     "repeats": args.repeats, "variants": args.variants, "routes": [r.route_id for r in routes]}

    print("[bench] --- проверка совпадения результата расчёта под разными транспортами (п.4) ---", file=sys.stderr)
    report["parity_check"] = parity_check(routes)
    print(json.dumps(report["parity_check"], indent=2, default=str, ensure_ascii=False), file=sys.stderr)

    report["direct_probes"] = {}
    report["full_attempt_probes"] = {}
    for variant in args.variants:
        print(f"[bench] === вариант: {variant} ===", file=sys.stderr)
        with optimization_variant(variant):
            direct_all = []
            for route in routes:
                for i in range(args.repeats):
                    res = run_direct_primitive_probe(route, sender, i)
                    direct_all.append(res)
                    print(f"[bench][direct][{variant}] {route.route_id} run={i} ok={res.get('ok')} "
                          f"rpc_calls={res.get('rpc_call_count')}", file=sys.stderr)
            report["direct_probes"][variant] = {
                "raw": direct_all,
                "stats": _stage_stats(direct_all),
            }
            if not args.skip_full_attempt:
                full_all = []
                for route in routes:
                    for i in range(args.repeats):
                        tag = f"{variant}_{route.route_id}_{i}"
                        res = run_full_attempt_probe(route, sender, out_dir / "accounting_tmp", tag)
                        full_all.append(res)
                        print(f"[bench][full][{variant}] {route.route_id} run={i} outcome={res.get('outcome')} "
                              f"stop_reason={res.get('stop_reason')} rpc_calls={res.get('rpc_call_count')}",
                              file=sys.stderr)
                report["full_attempt_probes"][variant] = {
                    "raw": full_all,
                    "stats": _stage_stats(full_all),
                    "n_reached_ready_to_send": sum(1 for r in full_all if r.get("outcome") == "reached_ready_to_send"),
                    "n_returned_without_send": sum(1 for r in full_all if r.get("outcome") == "returned_without_send"),
                    "n_exception": sum(1 for r in full_all if r.get("outcome") == "exception"),
                }

    if "baseline" in report["direct_probes"]:
        report["idealized_limits_baseline"] = idealized_limits(report["direct_probes"]["baseline"]["stats"])

    out_path = out_dir / f"result_{int(time.time())}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    print(f"[bench] результат записан: {out_path}", file=sys.stderr)
    print(json.dumps({k: v for k, v in report.items() if k not in ("direct_probes", "full_attempt_probes")},
                      indent=2, default=str, ensure_ascii=False))
    print(json.dumps({
        variant: report["direct_probes"][variant]["stats"] for variant in report["direct_probes"]
    }, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
