#!/usr/bin/env python3
"""Задача 5, полный отчёт завершённой сессии run34788791689_1 --
ТОЛЬКО чтение уже существующих файлов на Ohio (no_send_log/attempts/
budget_state/run-лог), никакого нового скана/LIVE/изменения кода.

Границы сессии: budget_state.json.pilot_started_at (сохранён ботом
ПОСЛЕ bootstrap, ДО начала торговли) -- т.к. это ПОСЛЕДНИЙ прогон,
простой фильтр ts_wall >= pilot_started_at корректно и полностью
изолирует эту сессию (более новых записей физически быть не может).

Классификация -- ПО РЕАЛЬНЫМ константам/веткам кода (task5_v4_hotpath.py/
task5_v4_pilot_accounting.py), не по эвристике текста, где это возможно:
  - REASON_HOOK_MODEL_ABSENT / REASON_LIVENESS_RECHECK_DEFERRED --
    ВСЕГДА и ТОЛЬКО вытеснения из соответствующей ограниченной очереди
    (_mark_bounded) -- см. её докстринг, других мест логирования этих
    причин в коде нет.
  - "реальный расчёт" = calc_duration_s is not None (populated ТОЛЬКО
    внутри _evaluate_and_maybe_send/её except-ветки).
  - Среди реальных расчётов: quote_block is not None означает, что
    recompute_route уже вернул profit_raw>0 И код дошёл минимум до
    _quote_and_estimate_gas_consistent (ПЕРВЫЙ вызов estimateGas) --
    ДО этого момента quote_block в _log_reason НИКОГДА не передаётся
    (проверено чтением исходника _evaluate_and_maybe_send).
  - "финальная проверка" -- отдельный, второй _quote_and_estimate_gas_
    consistent, выполняется, ТОЛЬКО если блок успел уйти вперёд;
    детектируется по detail, содержащему 'финальная проверка размера'."""
from __future__ import annotations

import json
import math
import os

RUN_TAG = "run34788791689_1"
LOG_PATH = f"/home/bot/data/task5_v4_hotpath_live_{RUN_TAG}.log"
NO_SEND_PATH = "/home/bot/data/task5_v4_pilot_no_send_log.jsonl"
ATTEMPTS_PATH = "/home/bot/data/task5_v4_pilot_attempts.jsonl"
BUDGET_PATH = "/home/bot/data/task5_v4_pilot_budget_state.json"

REASON_HOOK_MODEL_ABSENT = "модель хука отсутствует (не оценивалось)"
REASON_LIVENESS_RECHECK_DEFERRED = "перепроверка живучести отложена (не выполнялась)"
REASON_NO_PROFITABLE_CYCLE = "нет прибыльного цикла"
SHUTDOWN_DRAIN_DETAIL = "пилот завершает работу (--duration-seconds истёк) -- новые кандидаты не берутся"
LIVENESS_RECHECK_PERFORMED_MARKER_1 = "перепроверка ПО СВЕЖЕМУ сигналу"
LIVENESS_RECHECK_PERFORMED_MARKER_2 = "проверка живучести по свежему сигналу не удалась"


def pctl(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> None:
    result: dict = {"run_tag": RUN_TAG}

    budget = json.loads(open(BUDGET_PATH).read())
    pilot_started_at = budget.get("pilot_started_at")
    result["pilot_started_at_wall"] = pilot_started_at
    result["budget_state_snapshot"] = {
        k: budget.get(k) for k in ("pilot_completed", "pilot_completed_reason", "cumulative_gas_loss_usd",
                                    "cumulative_net_pnl_usd", "halted", "halt_reason", "pending")
    }

    all_reason = [json.loads(l) for l in open(NO_SEND_PATH) if l.strip()]
    all_attempts = ([json.loads(l) for l in open(ATTEMPTS_PATH) if l.strip()]
                     if os.path.exists(ATTEMPTS_PATH) and os.path.getsize(ATTEMPTS_PATH) else [])

    if pilot_started_at is None:
        result["error"] = "pilot_started_at отсутствует в budget_state.json -- границы сессии неизвестны"
        print(json.dumps(result, ensure_ascii=False))
        return

    session_reason = [r for r in all_reason if r.get("ts_wall", 0) >= pilot_started_at]
    session_attempts = [a for a in all_attempts if a.get("ts_wall", 0) >= pilot_started_at]

    all_ts = [r["ts_wall"] for r in session_reason] + [a["ts_wall"] for a in session_attempts]
    result["n_reason_log_entries_session_total"] = len(session_reason)
    result["n_attempts_entries_session_total"] = len(session_attempts)
    result["session_first_entry_ts_wall"] = min(all_ts) if all_ts else None
    result["session_last_entry_ts_wall"] = max(all_ts) if all_ts else None
    result["session_span_first_to_last_entry_s"] = (max(all_ts) - min(all_ts)) if all_ts else None
    result["session_duration_from_start_to_last_entry_s"] = (max(all_ts) - pilot_started_at) if all_ts else None
    result["first_entry_delay_after_pilot_started_at_s"] = (min(all_ts) - pilot_started_at) if all_ts else None

    # --- Item 2/3: классификация по REASON и наличию таймингов ---
    real_calc = [r for r in session_reason if r.get("calc_duration_s") is not None]
    no_timing = [r for r in session_reason if r.get("calc_duration_s") is None]

    hook_evictions, liveness_evictions, shutdown_drained = [], [], []
    liveness_recheck_performed, unclassified_no_timing = [], []
    for r in no_timing:
        if r.get("reason") == REASON_HOOK_MODEL_ABSENT:
            hook_evictions.append(r)
        elif r.get("reason") == REASON_LIVENESS_RECHECK_DEFERRED:
            liveness_evictions.append(r)
        elif r.get("reason") == REASON_NO_PROFITABLE_CYCLE and r.get("detail") == SHUTDOWN_DRAIN_DETAIL:
            shutdown_drained.append(r)
        elif (LIVENESS_RECHECK_PERFORMED_MARKER_1 in (r.get("detail") or "")
              or LIVENESS_RECHECK_PERFORMED_MARKER_2 in (r.get("detail") or "")):
            liveness_recheck_performed.append(r)
        else:
            unclassified_no_timing.append(r)

    result["counters"] = {
        "n_fast_filter_passed": "неизвестно -- verdict='passed' не логируется отдельно, только конечный результат "
                                 "реального расчёта, до которого он ведёт (см. n_real_calc ниже)",
        "n_rejected_by_price": "неизвестно -- verdict='rejected_by_price' сознательно НЕ логируется вовсе "
                                "(cheap_filter_route/_admit_touched_route, дизайн: не создавать кандидата)",
        "n_skipped_cache_not_initialized": "неизвестно -- verdict='not_initialized' также НЕ логируется (тихий "
                                            "пропуск, транзиентно)",
        "n_hook_model_absent_evictions_logged": len(hook_evictions),
        "n_liveness_recheck_deferred_evictions_logged": len(liveness_evictions),
        "n_admitted_to_hook_review_queue_total": "неизвестно -- логируется только вытеснение (переполнение), "
                                                  "не факт постановки; успешно дождавшиеся оценки не отличимы в "
                                                  "логе от кандидатов главной очереди",
        "n_admitted_to_liveness_recheck_queue_total": "неизвестно -- та же причина, что выше",
    }

    result["real_calc"] = {
        "n_total": len(real_calc),
        "n_unique_route_ids": len({r.get("route_id") for r in real_calc if r.get("route_id")}),
    }
    def _has_numeric_quote(r: dict) -> bool:
        return r.get("quote_block") is not None or "профит(до газа)=" in (r.get("detail") or "")

    numeric_quote = [r for r in real_calc if _has_numeric_quote(r)]
    recompute_failed = [r for r in real_calc if not _has_numeric_quote(r)]
    positive_before_gas_reached_estimate_gas = [r for r in real_calc if r.get("quote_block") is not None]
    reached_final_check = [r for r in real_calc if "финальная проверка размера" in (r.get("detail") or "")]
    profit_after_gas_computed = [r for r in real_calc if "профит после газа" in (r.get("detail") or "")]

    result["real_calc"]["n_numeric_quote_obtained"] = len(numeric_quote)
    result["real_calc"]["n_recompute_failed_no_numeric_quote"] = len(recompute_failed)
    result["real_calc"]["n_recompute_failed_reason_breakdown"] = {}
    for r in recompute_failed:
        result["real_calc"]["n_recompute_failed_reason_breakdown"][r.get("reason")] = \
            result["real_calc"]["n_recompute_failed_reason_breakdown"].get(r.get("reason"), 0) + 1
    result["real_calc"]["n_positive_before_gas_reached_estimate_gas"] = len(positive_before_gas_reached_estimate_gas)
    result["real_calc"]["n_reached_final_check"] = len(reached_final_check)
    result["real_calc"]["n_profit_after_gas_computed"] = len(profit_after_gas_computed)

    result["no_timing_breakdown"] = {
        "n_hook_review_queue_evictions": len(hook_evictions),
        "n_liveness_recheck_queue_evictions": len(liveness_evictions),
        "n_drained_at_shutdown": len(shutdown_drained),
        "n_liveness_recheck_performed_not_evicted": len(liveness_recheck_performed),
        "n_unclassified_no_timing": len(unclassified_no_timing),
        "unclassified_sample": unclassified_no_timing[:5],
    }
    result["n_send_attempts_session"] = len(session_attempts)

    # --- Item 4: тайминги (реальные расчёты) ---
    qw = [r["queue_wait_s"] for r in real_calc if r.get("queue_wait_s") is not None]
    cd = [r["calc_duration_s"] for r in real_calc if r.get("calc_duration_s") is not None]
    rfw = [r["route_full_wait_s"] for r in real_calc if r.get("route_full_wait_s") is not None]
    result["timings_real_calc_only"] = {
        "queue_wait_s": {"n": len(qw), "median": pctl(qw, 0.5), "p95": pctl(qw, 0.95)},
        "calc_duration_s": {"n": len(cd), "median": pctl(cd, 0.5), "p95": pctl(cd, 0.95)},
        "route_full_wait_s": {"n": len(rfw), "median": pctl(rfw, 0.5), "p95": pctl(rfw, 0.95)},
        "note_queue_attribution": ("Данные НЕ позволяют разделить эти тайминги по очереди-источнику (главная / "
                                    "хук-пересмотр / живучесть-пересмотр) для успешно рассчитанных кандидатов -- "
                                    "evaluator_loop обрабатывает их одинаково после забора, происхождение не "
                                    "логируется. Раздельно по очередям доступны ТОЛЬКО счётчики вытеснений выше."),
    }

    # --- Item 5: все случаи, дошедшие до estimateGas ---
    estimate_gas_cases = []
    for r in positive_before_gas_reached_estimate_gas:
        estimate_gas_cases.append({
            "route_id": r.get("route_id"), "route_label": r.get("route_label"),
            "size_in_raw": r.get("size_in_raw"), "quote_block": r.get("quote_block"),
            "signal_block": r.get("signal_block"), "reason": r.get("reason"), "detail": r.get("detail"),
            "queue_wait_s": r.get("queue_wait_s"), "calc_duration_s": r.get("calc_duration_s"),
            "route_full_wait_s": r.get("route_full_wait_s"), "rpc_call_count": r.get("rpc_call_count"),
        })
    result["estimate_gas_cases_full"] = estimate_gas_cases

    print(json.dumps(result, default=str, ensure_ascii=False))
    out_path = "/home/bot/robinhood-chain-alpha/data/task5_v4_pilot_session2b_full_report_result.json"
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2, default=str, ensure_ascii=False)

    # --- Полные журналы этой сессии, отдельными файлами ---
    with open("/home/bot/robinhood-chain-alpha/data/task5_v4_pilot_session2b_only_no_send_log.jsonl", "w") as fh:
        for r in session_reason:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open("/home/bot/robinhood-chain-alpha/data/task5_v4_pilot_session2b_only_attempts.jsonl", "w") as fh:
        for a in session_attempts:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")
    print(f"[report] session-only files written: {len(session_reason)} reason-log lines, "
          f"{len(session_attempts)} attempts lines")


if __name__ == "__main__":
    main()
