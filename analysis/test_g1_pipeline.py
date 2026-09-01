#!/usr/bin/env python3
"""Юнит-тесты analysis/g1_pipeline.py -- ДО первого платного запроса на
реальных данных (тот же стандарт, что test_g1_stats.py/test_credit_guard.py).
Строит SQL локально и проверяет его текст/структуру (без сети), и
проверяет compute_returns/apply_filters на синтетике с известным ответом.

Использование: python analysis/test_g1_pipeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from config import CONFIG
from g1_pipeline import (
    build_extract_query, build_quote_distribution_query, horizon_delta, max_offset_s,
    apply_filters, compute_returns, STRESS_LOG_SENTINEL,
    project_full_estimate, calibrated_batch_size, batch_rows,
    CALIBRATION_SCALE_FACTOR, CALIBRATION_MAX_ESTIMATE,
)


def _synthetic_events(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "token": [f"0x{'a' * 39}{i}" for i in range(n)],
        "t0": pd.to_datetime(["2026-08-27 12:00:00", "2026-08-27 13:30:00", "2026-08-27 23:59:59"][:n]),
    })


def test_horizon_delta_matches_spec():
    # delta(h) = max(30, 0.1*h) -- §2.3
    assert horizon_delta(30) == 30
    assert horizon_delta(60) == 30
    assert horizon_delta(300) == 30
    assert horizon_delta(900) == 90
    assert horizon_delta(1800) == 180
    assert horizon_delta(3600) == 360
    assert horizon_delta(14400) == 1440
    assert horizon_delta(43200) == 4320
    assert horizon_delta(86400) == 8640
    print("[test_horizon_delta_matches_spec] OK")


def test_max_offset_is_86400_plus_8640():
    assert max_offset_s() == 86400 + 8640 == 95040
    print("[test_max_offset_is_86400_plus_8640] OK")


def test_build_extract_query_no_timestamp_offset_leak():
    sql = build_extract_query(_synthetic_events())
    assert "+00:00" not in sql and " UTC'" not in sql, "timestamp-литерал с offset -- невалидный синтаксис Trino"
    print("[test_build_extract_query_no_timestamp_offset_leak] OK")


def test_build_extract_query_values_row_count_matches_events():
    events = _synthetic_events(3)
    sql = build_extract_query(events)
    # Каждый адрес токена должен встретиться в VALUES ровно один раз.
    for tok in events["token"]:
        assert sql.count(tok) == 1, f"{tok} встречается {sql.count(tok)} раз(а), ожидался 1"
    print("[test_build_extract_query_values_row_count_matches_events] OK")


def test_build_extract_query_has_all_horizon_columns():
    sql = build_extract_query(_synthetic_events())
    for h in CONFIG.g1_horizons_s:
        assert f"as exit_n_{h}" in sql, f"нет колонки exit_n_{h}"
        assert f"as exit_vwap_{h}" in sql, f"нет колонки exit_vwap_{h}"
    print("[test_build_extract_query_has_all_horizon_columns] OK")


def test_build_extract_query_has_hard_limit():
    events = _synthetic_events(3)
    sql = build_extract_query(events)
    assert f"limit {len(events)}" in sql, "нет жёсткого LIMIT (вторая линия защиты, требование владельца)"
    print("[test_build_extract_query_has_hard_limit] OK")


def test_build_quote_distribution_query_values_row_count():
    events = _synthetic_events(3)
    sql = build_quote_distribution_query(events)
    assert sql.count("(0x") == 3
    assert "+00:00" not in sql
    print("[test_build_quote_distribution_query_values_row_count] OK")


def test_build_quote_distribution_query_has_time_bound():
    """Регрессия на run #16: первая версия не имела НИКАКОЙ границы по
    block_time -- просканировала всю историю dex.trades для 896 активно
    торгуемых токенов, факт 56.49 против заявленных 15.0 (>2x, 2x-гард
    остановил пайплайн). Каждый запрос к dex.trades в этом пайплайне
    ОБЯЗАН ограничивать block_time -- проверяем явно."""
    events = _synthetic_events(3)
    sql = build_quote_distribution_query(events)
    assert "block_time >" in sql and "block_time <=" in sql, "нет границы по block_time -- та же причина, что дала 56.49 вместо 15.0 в run #16"
    assert "interval '" in sql
    print("[test_build_quote_distribution_query_has_time_bound] OK")


def test_apply_filters_basic():
    df = pd.DataFrame({
        "n_buys_pre": [5, 1, 3],
        "vol_usd_pre": [500, 500, 100],
        "entry_n": [2, 0, 5],
    })
    out = apply_filters(df)
    # row0: n_buys>=3 и vol>=250 -> pass_filter True; entry_n>0 -> pass_entry True
    # row1: n_buys<3 -> pass_filter False (даже с vol OK); entry_n=0 -> pass_entry False
    # row2: vol<250 -> pass_filter False; entry_n>0 -> pass_entry True
    assert list(out["pass_filter"]) == [True, False, False]
    assert list(out["pass_entry"]) == [True, False, True]
    print("[test_apply_filters_basic] OK")


def test_compute_returns_known_values():
    # entry=100, exit_30=110 -> r = ln(1.1) - cost
    df = pd.DataFrame({
        "entry_vwap": [100.0, 100.0],
        "exit_vwap_30": [110.0, 90.0],
        "exit_n_30": [5, 5],
        "pass_filter": [True, True],
        "pass_entry": [True, True],
    })
    out = compute_returns(df, cost=0.03, horizons=(30,))
    r = out[30]
    assert len(r) == 2
    expected0 = np.log(110.0 / 100.0) - 0.03
    expected1 = np.log(90.0 / 100.0) - 0.03
    assert abs(sorted(r)[1] - expected0) < 1e-9 or abs(r[0] - expected0) < 1e-9
    assert abs(min(r) - expected1) < 1e-9
    print("[test_compute_returns_known_values] OK")


def test_compute_returns_excludes_non_analytic_rows():
    df = pd.DataFrame({
        "entry_vwap": [100.0, 100.0, 100.0],
        "exit_vwap_30": [110.0, 110.0, 110.0],
        "exit_n_30": [5, 5, 5],
        "pass_filter": [True, False, True],
        "pass_entry": [True, True, False],
    })
    out = compute_returns(df, cost=0.03, horizons=(30,))
    assert len(out[30]) == 1, "только 1 из 3 строк проходит и pass_filter, и pass_entry"
    print("[test_compute_returns_excludes_non_analytic_rows] OK")


def test_compute_returns_stress_substitutes_sentinel_for_no_liquidity():
    df = pd.DataFrame({
        "entry_vwap": [100.0, 100.0],
        "exit_vwap_30": [110.0, np.nan],  # второе событие -- no liquidity (LOCF тоже не нашёл)
        "exit_n_30": [5, 0],
        "pass_filter": [True, True],
        "pass_entry": [True, True],
    })
    out_normal = compute_returns(df, cost=0.03, horizons=(30,), stress=False)
    # без стресса: NaN exit_vwap -> r=NaN -> отфильтрован -> остаётся только 1 значение
    assert len(out_normal[30]) == 1
    out_stress = compute_returns(df, cost=0.03, horizons=(30,), stress=True)
    # со стрессом: событие с exit_n=0 получает r=STRESS_LOG_SENTINEL вместо NaN -> остаются оба
    assert len(out_stress[30]) == 2
    assert STRESS_LOG_SENTINEL in out_stress[30]
    print("[test_compute_returns_stress_substitutes_sentinel_for_no_liquidity] OK")


def test_project_full_estimate_known_values():
    # run #16-style scenario: факт 56.49 на 896 -- если бы 108 было
    # калибровочным срезом с той же per-unit ставкой, проекция на 896:
    per_unit = 56.49 / 896
    expected = per_unit * 896 * CALIBRATION_SCALE_FACTOR
    got = project_full_estimate(56.49, 896, 896)
    assert abs(got - expected) < 1e-9
    # Простая проверка масштабирования: вдвое больше n_среза -> при том
    # же per-unit ставка проекции на тот же target тоже вдвое меньше,
    # т.к. per_unit меньше -- проверим через прямую формулу.
    assert abs(project_full_estimate(10.0, 100, 100) - 10.0 * CALIBRATION_SCALE_FACTOR) < 1e-9
    assert abs(project_full_estimate(10.0, 100, 200) - 20.0 * CALIBRATION_SCALE_FACTOR) < 1e-9
    print("[test_project_full_estimate_known_values] OK")


def test_project_full_estimate_raises_on_zero_slice():
    try:
        project_full_estimate(10.0, 0, 100)
        assert False, "ожидался ValueError на slice_n=0"
    except ValueError:
        pass
    print("[test_project_full_estimate_raises_on_zero_slice] OK")


def test_calibrated_batch_size_stays_under_cap():
    # факт 56.49 на срезе 108 -> per_unit=0.523; батч должен быть таким,
    # чтобы project_full_estimate(56.49, 108, batch) <= 40 -- проверяем
    # напрямую, не пересчитывая формулу вручную (защита от рассинхрона).
    batch = calibrated_batch_size(56.49, 108)
    proj = project_full_estimate(56.49, 108, batch)
    assert proj <= CALIBRATION_MAX_ESTIMATE + 1e-9, f"batch={batch} даёт проекцию {proj} > {CALIBRATION_MAX_ESTIMATE}"
    # На единицу больше -- уже должно превышать порог (иначе batch_size
    # занижен сильнее, чем нужно, тратим лишние запросы впустую).
    proj_plus_one = project_full_estimate(56.49, 108, batch + 1)
    assert proj_plus_one > CALIBRATION_MAX_ESTIMATE, "batch_size занижен -- следующий размер тоже укладывался бы"
    print("[test_calibrated_batch_size_stays_under_cap] OK")


def test_calibrated_batch_size_degenerate_zero_actual():
    # Нулевая калибровочная стоимость (кэш-хит) -- не должно падать/делить на 0.
    assert calibrated_batch_size(0.0, 100) == 100
    assert calibrated_batch_size(10.0, 0) == 1
    print("[test_calibrated_batch_size_degenerate_zero_actual] OK")


def test_batch_rows_covers_all_rows_without_overlap():
    df = pd.DataFrame({"x": range(10)})
    batches = batch_rows(df, 3)
    assert [len(b) for b in batches] == [3, 3, 3, 1]
    reassembled = pd.concat(batches, ignore_index=True)
    assert list(reassembled["x"]) == list(range(10))
    print("[test_batch_rows_covers_all_rows_without_overlap] OK")


def test_partitioning_does_not_change_total_projected_cost():
    """Владелец: партиционирование НЕ меняет суммарную ожидаемую
    стоимость, только дробит её на куски под санитарным порогом --
    проверяем это свойство напрямую (сумма проекций по партициям ==
    проекция на весь объём)."""
    calib_actual, calib_n, full_n = 56.49, 108, 896
    batch_size = calibrated_batch_size(calib_actual, calib_n)
    n_batches = -(-full_n // batch_size)  # ceil div
    total_partitioned = sum(
        project_full_estimate(calib_actual, calib_n, min(batch_size, full_n - i * batch_size))
        for i in range(n_batches)
    )
    total_unpartitioned = project_full_estimate(calib_actual, calib_n, full_n)
    assert abs(total_partitioned - total_unpartitioned) < 1e-6, (total_partitioned, total_unpartitioned)
    print("[test_partitioning_does_not_change_total_projected_cost] OK")


def main() -> int:
    tests = [
        test_horizon_delta_matches_spec,
        test_max_offset_is_86400_plus_8640,
        test_build_extract_query_no_timestamp_offset_leak,
        test_build_extract_query_values_row_count_matches_events,
        test_build_extract_query_has_all_horizon_columns,
        test_build_extract_query_has_hard_limit,
        test_build_quote_distribution_query_values_row_count,
        test_build_quote_distribution_query_has_time_bound,
        test_apply_filters_basic,
        test_compute_returns_known_values,
        test_compute_returns_excludes_non_analytic_rows,
        test_compute_returns_stress_substitutes_sentinel_for_no_liquidity,
        test_project_full_estimate_known_values,
        test_project_full_estimate_raises_on_zero_slice,
        test_calibrated_batch_size_stays_under_cap,
        test_calibrated_batch_size_degenerate_zero_actual,
        test_batch_rows_covers_all_rows_without_overlap,
        test_partitioning_does_not_change_total_projected_cost,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed}/{len(tests)} тестов упало.")
        return 1
    print(f"\nВсе {len(tests)} тестов прошли.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
