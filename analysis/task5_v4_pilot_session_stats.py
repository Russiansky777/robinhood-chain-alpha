#!/usr/bin/env python3
"""Задача 5: ЧИСТО ЧИТАЮЩИЙ разбор журналов ЗАВЕРШЁННОГО пилота
run34771657422_1 (завершение подтверждено НЕЗАВИСИМО от статуса GitHub-джобы --
см. task5_v4_live_pilot_status_check.py: PID отсутствовал, pilot_completed=true,
собственный итог бота в логе). По прямому указанию владельца: "новый запуск,
изменения кода и скан сети пока не нужны" -- этот скрипт ТОЛЬКО читает уже
существующие файлы на диске (no_send_log.jsonl, attempts.jsonl,
budget_state.json), ничего не пишет в торговые файлы, ничего не отправляет,
новых RPC/сети/тестов/пилотов не запускает. Единственная запись на диск --
ДВА новых файла-среза (только записи ЭТОЙ сессии) для передачи владельцу.

Граница сессии: ts_wall >= pilot_started_at (см. PilotBudget.ensure_pilot_started()
-- выставляется РОВНО один раз, ПОСЛЕ полного bootstrap_registry(), т.е. это
честная граница "начало реальной торговли", исключающая и bootstrap-фазу
ЭТОГО прогона, и все более старые записи ПРЕЖНЕГО пилота в том же файле
(тот же путь используется для всех прогонов подряд)."""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

REASON_LOG_FILE = Path("/home/bot/data/task5_v4_pilot_no_send_log.jsonl")
ATTEMPT_TABLE_FILE = Path("/home/bot/data/task5_v4_pilot_attempts.jsonl")
BUDGET_STATE_FILE = Path("/home/bot/data/task5_v4_pilot_budget_state.json")

OUT_SESSION_REASON_LOG = Path("/home/bot/data/task5_v4_pilot_session_only_no_send_log.jsonl")
OUT_SESSION_ATTEMPTS = Path("/home/bot/data/task5_v4_pilot_session_only_attempts.jsonl")

# Реальный словарь reason из кода (task5_v4_hotpath.py + task5_v4_pilot_accounting.py) --
# ТОЛЬКО эти строки реально пишутся в no_send_log.jsonl. Владелец запросил 5 ДРУГИХ,
# более крупных категорий -- ниже честная, явно помеченная ЭВРИСТИКА текста detail,
# а НЕ отдельные ветки кода.
REASON_NO_PROFITABLE_CYCLE = "нет прибыльного цикла"
REASON_NO_LIQUIDITY = "нет ликвидности"
REASON_CALC_ERROR = "ошибка расчёта"
REASON_SIMULATION_FAILED = "симуляция не прошла"


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open("r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, json.JSONDecodeError):
                continue
    return out


def classify_bucket(reason: str, detail: str) -> str:
    """ЭВРИСТИЧЕСКОЕ (не по коду -- по тексту detail) отображение реального,
    более узкого словаря reason на 5 категорий, запрошенных владельцем:
    отрицательная котировка / прибыль не покрывает газ / revert / ошибка RPC /
    устаревшее состояние. Где текст не совпал уверенно ни с одним шаблоном --
    честно возвращает отдельную категорию "не удалось однозначно отнести",
    а не досчитывает наугад в одну из пяти."""
    d = detail or ""
    dl = d.lower()
    if reason == REASON_NO_PROFITABLE_CYCLE:
        if "устарел" in d or "сдвинулся" in d or "уже не подходит" in d:
            return "устаревшее состояние"
        if "после газа" in d:
            return "прибыль не покрывает газ"
        if "профит(до газа)" in d:
            return "отрицательная котировка"
        return "не удалось однозначно отнести (нет прибыльного цикла, текст не совпал с известными шаблонами)"
    if reason == REASON_NO_LIQUIDITY:
        return "revert (NotEnoughLiquidity при котировке через Quoter)"
    if reason == REASON_CALC_ERROR:
        rpc_markers = ("timeout", "timed out", "connection", "http", "429", "rate limit",
                       "econnreset", "econnrefused", "temporarily unavailable")
        if any(m in dl for m in rpc_markers):
            return "ошибка RPC"
        return "не удалось однозначно отнести (ошибка расчёта, текст не подтверждает RPC явно)"
    if reason == REASON_SIMULATION_FAILED:
        if "revert" in dl or "execution reverted" in dl:
            return "revert (eth_estimateGas отклонён контрактом)"
        if ("согласовать minprofit" in dl or "prepare_transaction_fields" in dl
                or "недоступен" in d or "курс weth/usdg" in dl):
            return "не удалось однозначно отнести (сбой подготовки транзакции/цены, не явный revert и не явный RPC)"
        return "revert (наиболее вероятная причина отказа eth_estimateGas, текст не уточняет иначе)"
    return f"не удалось однозначно отнести (нестандартная причина, вероятно гейт бюджета/остановки: {reason!r})"


def pctl(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def extract_profit_before_gas(detail: str) -> int | None:
    m = re.search(r"профит\(до газа\)=(-?\d+)", detail or "")
    return int(m.group(1)) if m else None


def extract_profit_after_gas(detail: str) -> float | None:
    m = re.search(r"после газа (-?\d+\.\d+)", detail or "")
    return float(m.group(1)) if m else None


def main() -> None:
    result: dict = {}

    budget = json.loads(BUDGET_STATE_FILE.read_text()) if BUDGET_STATE_FILE.exists() else {}
    pilot_started_at = budget.get("pilot_started_at")
    result["pilot_started_at"] = pilot_started_at
    result["pilot_completed"] = budget.get("pilot_completed")
    result["pilot_completed_reason"] = budget.get("pilot_completed_reason")

    if pilot_started_at is None:
        result["ok"] = False
        result["error"] = "pilot_started_at отсутствует в состоянии бюджета -- честную границу сессии определить нельзя"
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    all_reason = read_jsonl(REASON_LOG_FILE)
    all_attempts = read_jsonl(ATTEMPT_TABLE_FILE)

    session_reason = [r for r in all_reason if r.get("ts_wall") is not None and r["ts_wall"] >= pilot_started_at]
    session_attempts = [a for a in all_attempts if a.get("ts_wall") is not None and a["ts_wall"] >= pilot_started_at]

    result["n_reason_log_entries_total_file"] = len(all_reason)
    result["n_reason_log_entries_excluded_as_bootstrap_or_previous_pilot"] = len(all_reason) - len(session_reason)
    result["n_attempts_entries_total_file"] = len(all_attempts)
    result["n_attempts_entries_excluded_as_bootstrap_or_previous_pilot"] = len(all_attempts) - len(session_attempts)

    all_ts = [r["ts_wall"] for r in session_reason] + [a["ts_wall"] for a in session_attempts]
    result["session_trading_start_ts_wall"] = pilot_started_at
    result["session_last_logged_entry_ts_wall"] = max(all_ts) if all_ts else None
    result["session_duration_from_start_to_last_entry_s"] = (max(all_ts) - pilot_started_at) if all_ts else None
    result["note_on_session_end"] = (
        "Конец сессии = ts_wall ПОСЛЕДНЕЙ реально записанной строки в no_send_log/attempts внутри сессии. "
        "Это НЕ момент выхода процесса: после последней оценки процесс ещё печатает '=== ИТОГ ПИЛОТА ===' "
        "и завершается на несколько секунд позже. Отдельного wall-time штампа у строк [hotpath] в самом "
        "логе процесса нет, поэтому точнее эту границу не определить без придумывания."
    )

    result["n_completed_calculations_session"] = len(session_reason)
    result["n_unique_route_ids_session"] = len({r.get("route_id") for r in session_reason if r.get("route_id")})
    result["n_send_attempts_session"] = len(session_attempts)

    # -- п.3: причины отказа --
    raw_reason_counts: dict[str, int] = {}
    bucket_counts: dict[str, int] = {}
    for r in session_reason:
        raw = r.get("reason", "?")
        raw_reason_counts[raw] = raw_reason_counts.get(raw, 0) + 1
        b = classify_bucket(raw, r.get("detail", ""))
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    result["raw_reason_value_counts_GROUND_TRUTH"] = raw_reason_counts
    result["reason_bucket_counts_HEURISTIC_MAPPING_TO_5_REQUESTED_CATEGORIES"] = bucket_counts
    result["bucket_mapping_methodology_note"] = (
        "В коде реально существуют ТОЛЬКО 4 константы reason ('нет прибыльного цикла', 'нет ликвидности', "
        "'ошибка расчёта', 'симуляция не прошла') плюс отдельные строки гейта can_send() (бюджет/halt/"
        "pilot_completed/pending) -- НЕ 5 отдельных программных веток запрошенных категорий. Отображение "
        "выше получено ЭВРИСТИКОЙ по тексту detail (см. classify_bucket() в этом скрипте), а не читает "
        "отдельное поле кода. Там, где текст не совпал уверенно ни с одним известным шаблоном -- это "
        "честно помечено 'не удалось однозначно отнести', а НЕ досчитано в одну из 5 категорий наугад."
    )

    # -- п.4: медианы/p95 --
    qw = [r["queue_wait_s"] for r in session_reason if r.get("queue_wait_s") is not None]
    cd = [r["calc_duration_s"] for r in session_reason if r.get("calc_duration_s") is not None]
    result["queue_wait_s_n_present"] = len(qw)
    result["queue_wait_s_n_missing"] = len(session_reason) - len(qw)
    result["queue_wait_s_median"] = statistics.median(qw) if qw else None
    result["queue_wait_s_p95"] = pctl(qw, 0.95) if qw else None
    result["calc_duration_s_n_present"] = len(cd)
    result["calc_duration_s_n_missing"] = len(session_reason) - len(cd)
    result["calc_duration_s_median"] = statistics.median(cd) if cd else None
    result["calc_duration_s_p95"] = pctl(cd, 0.95) if cd else None

    result["n_candidates_unprocessed_at_shutdown"] = None
    result["note_on_unprocessed_candidates"] = (
        "В коде НЕТ счётчика глубины очереди/оставшихся необработанных кандидатов на момент остановки "
        "(_CoalescingRouteQueue не логирует qsize периодически и не печатает его при выходе, "
        "evaluator_finished_current_work() сообщает только 'текущая единица работы завершена', без счёта "
        "остатка). Честно: данных нет, оценку не делаю."
    )

    # -- п.5: топ-5 --
    enriched = []
    for r in session_reason:
        pb = extract_profit_before_gas(r.get("detail", ""))
        pa = extract_profit_after_gas(r.get("detail", ""))
        enriched.append({
            "route_id": r.get("route_id"), "route_label": r.get("route_label"),
            "size_in_raw": r.get("size_in_raw"), "quote_block": r.get("quote_block"),
            "signal_block": r.get("signal_block"), "reason": r.get("reason"), "detail": r.get("detail"),
            "profit_before_gas_raw_PARSED_FROM_DETAIL": pb,
            "profit_after_gas_PARSED_FROM_DETAIL": pa,
            "ts_wall": r.get("ts_wall"),
        })
    with_pb = [e for e in enriched if e["profit_before_gas_raw_PARSED_FROM_DETAIL"] is not None]
    with_pb.sort(key=lambda e: e["profit_before_gas_raw_PARSED_FROM_DETAIL"], reverse=True)
    top5 = with_pb[:5]
    for e in top5:
        for k, v in list(e.items()):
            if v is None:
                e[k] = "ОТСУТСТВУЕТ"
    result["top5_best_computed_results_by_profit_before_gas"] = top5
    result["top5_methodology_note"] = (
        "ReasonLog.log() не хранит числовой profit_raw отдельным полем -- только внутри текста detail, и "
        "ТОЛЬКО для reason=='нет прибыльного цикла' (случаи 'нет ликвидности'/'ошибка расчёта'/'симуляция "
        "не прошла' вообще не содержат числового профита -- расчёт до этого числа там не дошёл, это не "
        "потерянные данные). Топ-5 отсортирован по реально распарсенному 'профит(до газа)' из detail; "
        "'после газа' указан там, где он в принципе присутствует в тексте (часть кандидатов отсеивается "
        "раньше, до расчёта газа). Отсутствующие поля помечены 'ОТСУТСТВУЕТ', не додуманы."
    )

    with OUT_SESSION_REASON_LOG.open("w") as fh:
        for r in session_reason:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with OUT_SESSION_ATTEMPTS.open("w") as fh:
        for a in session_attempts:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")
    result["session_only_reason_log_saved_to"] = str(OUT_SESSION_REASON_LOG)
    result["session_only_attempts_saved_to"] = str(OUT_SESSION_ATTEMPTS)
    result["session_only_reason_log_line_count"] = len(session_reason)
    result["session_only_attempts_line_count"] = len(session_attempts)

    result["ok"] = True
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
