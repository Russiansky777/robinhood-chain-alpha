#!/usr/bin/env python3
"""Юнит-тесты для analysis/g1_stats.py -- ПЕРЕД первым платным прогоном
на реальных данных (см. владелец, стандарт проекта: гард/статистика
доказываются тестом до прогона, не после). Plain assert, sys.exit(1) на
любой сбой -- тот же паттерн, что test_credit_guard.py.

Использование: python analysis/test_g1_stats.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np

from g1_stats import benjamini_hochberg, sign_test_one_sided, bootstrap_trimmed_mean_ci, run_horizon_stats


def test_bh_known_example():
    # Классический учебный пример (Benjamini & Hochberg 1995-style):
    # p = [0.01, 0.04, 0.03, 0.005, 0.20, 0.45], m=6, alpha=0.05.
    # Отсортировано: 0.005(1), 0.01(2), 0.03(3), 0.04(4), 0.20(5), 0.45(6).
    # BH-порог для ранга k: k/m*alpha = k*0.05/6.
    # rank1: 0.005 <= 1*0.05/6=0.00833 -- значим
    # rank2: 0.01  <= 2*0.05/6=0.01667 -- значим
    # rank3: 0.03  <= 3*0.05/6=0.025   -- НЕ значим по последовательному правилу,
    #        но т.к. q-value = min(p*m/k) идущий СВЕРХУ, посчитаем q явно ниже.
    p = [0.01, 0.04, 0.03, 0.005, 0.20, 0.45]
    result = benjamini_hochberg(p, alpha=0.05)
    # q-значения по формуле q(k) = min_{k'>=k} p(k')*m/k' (в порядке рангов 1..6):
    # rank6 (0.45): 0.45*6/6=0.45 -> q=0.45
    # rank5 (0.20): min(0.45, 0.20*6/5=0.24) -> q=0.24
    # rank4 (0.04): min(0.24, 0.04*6/4=0.06) -> q=0.06
    # rank3 (0.03): min(0.06, 0.03*6/3=0.06) -> q=0.06
    # rank2 (0.01): min(0.06, 0.01*6/2=0.03) -> q=0.03
    # rank1 (0.005): min(0.03, 0.005*6/1=0.03) -> q=0.03
    expected_q_by_p = {0.005: 0.03, 0.01: 0.03, 0.03: 0.06, 0.04: 0.06, 0.20: 0.24, 0.45: 0.45}
    for orig_p, (q, sig) in zip(p, result):
        exp_q = expected_q_by_p[orig_p]
        assert abs(q - exp_q) < 1e-9, f"p={orig_p}: q={q}, ожидалось {exp_q}"
        assert sig == (exp_q < 0.05), f"p={orig_p}: significant={sig}, ожидалось {exp_q < 0.05}"
    # Итог: значимы p=0.005 (q=0.03) и p=0.01 (q=0.03) -- ровно 2 из 6.
    n_sig = sum(1 for _, sig in result if sig)
    assert n_sig == 2, f"ожидалось 2 значимых, получили {n_sig}"
    print("[test_bh_known_example] OK")


def test_bh_all_null_none_significant():
    p = [0.5, 0.6, 0.7, 0.9]
    result = benjamini_hochberg(p, alpha=0.05)
    assert all(not sig for _, sig in result), result
    print("[test_bh_all_null_none_significant] OK")


def test_bh_empty():
    assert benjamini_hochberg([], 0.05) == []
    print("[test_bh_empty] OK")


def test_sign_test_matches_scipy_binomtest():
    from scipy import stats as sp_stats
    rng = np.random.default_rng(1)
    x = rng.normal(loc=0.02, scale=0.1, size=137)  # заведомо смещено в плюс
    n, n_ties, n_pos, p = sign_test_one_sided(x)
    assert n_ties == 0  # непрерывное распределение -- точных нулей не будет
    assert n == 137
    ref = sp_stats.binomtest(n_pos, n, 0.5, alternative="greater").pvalue
    assert abs(p - ref) < 1e-12
    # Смещение в плюс -> p должно быть маленьким (значимо на alpha=0.05 обычно)
    assert p < 0.05, f"ожидался маленький p для явно положительного сдвига, получили {p}"
    print("[test_sign_test_matches_scipy_binomtest] OK")


def test_sign_test_excludes_exact_zeros():
    x = np.array([0.0, 0.0, 0.01, -0.01, 0.02, -0.02, 0.03])
    n, n_ties, n_pos, p = sign_test_one_sided(x)
    assert n_ties == 2
    assert n == 5  # 7 - 2 нуля
    assert n_pos == 3  # 0.01, 0.02, 0.03
    print("[test_sign_test_excludes_exact_zeros] OK")


def test_sign_test_all_zero_returns_n0_p1():
    x = np.array([0.0, 0.0, 0.0])
    n, n_ties, n_pos, p = sign_test_one_sided(x)
    assert n == 0 and n_ties == 3 and p == 1.0
    print("[test_sign_test_all_zero_returns_n0_p1] OK")


def test_bootstrap_trimmed_mean_ci_positive_data_gives_positive_ci():
    rng = np.random.default_rng(2)
    x = rng.normal(loc=0.05, scale=0.02, size=300)  # явно положительно, узкий разброс
    point, lo, hi = bootstrap_trimmed_mean_ci(x, n_boot=2000, alpha=0.05, trim_pct=0.05, seed=3)
    assert lo > 0, f"ожидался положительный нижний край CI для явно положительных данных, получили lo={lo}"
    assert lo <= point <= hi
    print("[test_bootstrap_trimmed_mean_ci_positive_data_gives_positive_ci] OK")


def test_bootstrap_trimmed_mean_ci_empty():
    point, lo, hi = bootstrap_trimmed_mean_ci(np.array([]), n_boot=100, alpha=0.05, trim_pct=0.05)
    assert np.isnan(point) and np.isnan(lo) and np.isnan(hi)
    print("[test_bootstrap_trimmed_mean_ci_empty] OK")


def test_run_horizon_stats_end_to_end_synthetic():
    """Синтетика: горизонт 30с -- явно положительный сдвиг (должен выйти
    значимым), горизонт 3600с -- чистый шум вокруг нуля (не должен)."""
    rng = np.random.default_rng(7)
    data = {
        30: rng.normal(loc=0.03, scale=0.05, size=250),
        3600: rng.normal(loc=0.0, scale=0.05, size=250),
    }
    results = run_horizon_stats(data, alpha=0.05, n_boot=1000, trim_pct=0.05, seed=7)
    by_h = {r.horizon_s: r for r in results}
    assert by_h[30].significant, "ожидался значимый результат на явно положительном горизонте"
    assert by_h[30].median > 0
    assert not by_h[3600].significant, "не ожидался значимый результат на чистом шуме"
    print("[test_run_horizon_stats_end_to_end_synthetic] OK")


def main() -> int:
    tests = [
        test_bh_known_example,
        test_bh_all_null_none_significant,
        test_bh_empty,
        test_sign_test_matches_scipy_binomtest,
        test_sign_test_excludes_exact_zeros,
        test_sign_test_all_zero_returns_n0_p1,
        test_bootstrap_trimmed_mean_ci_positive_data_gives_positive_ci,
        test_bootstrap_trimmed_mean_ci_empty,
        test_run_horizon_stats_end_to_end_synthetic,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed}/{len(tests)} тестов упало.")
        return 1
    print(f"\nВсе {len(tests)} тестов прошли.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
