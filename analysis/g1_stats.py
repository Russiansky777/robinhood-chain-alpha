"""Sprint G1 §2.6 -- статистический план по горизонтам (пивот на V2, не
меняет §2, только реализация замороженного плана).

Основной тест: односторонний знаковый (биномиальный) тест H1 "медиана
r(h) > 0" на каждом горизонте при базовой стоимости (config.
g1_cost_scenario_base = 3%), поправка Бенджамини-Хохберга по 10
горизонтам, alpha = config.g1_bh_alpha (0.05).
Секондари: бутстреп-CI (config.g1_bootstrap_n = 10 000 ресемплов) для
5%-усечённого среднего (config.g1_trimmed_mean_pct) на каждом
горизонте.

Юнит-тестировано на синтетике до первого прогона на реальных данных --
см. analysis/test_g1_stats.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass
class HorizonResult:
    horizon_s: int
    n: int              # ненулевых наблюдений, вошедших в знаковый тест
    n_ties: int          # r(h) == 0 ровно -- исключены из знакового теста (стандартная практика)
    n_pos: int
    median: float
    p_one_sided: float
    q_bh: float
    significant: bool
    trimmed_mean: float
    boot_ci_low: float
    boot_ci_high: float


def sign_test_one_sided(returns: np.ndarray) -> tuple[int, int, int, float]:
    """H1: медиана r(h) > 0. Возвращает (n, n_ties, n_pos, p) --
    односторонний биномиальный тест p(X >= n_pos | n, 0.5). Точные нули
    (r(h) == 0) исключаются из n -- стандартная обработка "ties" в
    знаковом тесте (Wilcoxon/sign test conventions), логируется
    отдельно (n_ties), не молча отбрасывается."""
    n_ties = int((returns == 0).sum())
    nz = returns[returns != 0]
    n = len(nz)
    n_pos = int((nz > 0).sum())
    if n == 0:
        return 0, n_ties, 0, 1.0
    p = stats.binomtest(n_pos, n, 0.5, alternative="greater").pvalue
    return n, n_ties, n_pos, float(p)


def benjamini_hochberg(p_values: list[float], alpha: float) -> list[tuple[float, bool]]:
    """Классическая монотонная BH-коррекция. Возвращает [(q, significant)]
    В ИСХОДНОМ порядке p_values (не в отсортированном)."""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])  # индексы по возрастанию p
    q_sorted = [0.0] * m
    min_so_far = 1.0
    for rank in range(m, 0, -1):  # от m (наибольший p) вниз до 1 (наименьший p)
        idx = order[rank - 1]
        candidate = p_values[idx] * m / rank
        min_so_far = min(min_so_far, candidate)
        q_sorted[rank - 1] = min_so_far
    q = [0.0] * m
    for rank in range(m):
        q[order[rank]] = q_sorted[rank]
    return [(q[i], q[i] < alpha) for i in range(m)]


def bootstrap_trimmed_mean_ci(
    x: np.ndarray, n_boot: int, alpha: float, trim_pct: float, seed: int = 42,
) -> tuple[float, float, float]:
    """Возвращает (точечная 5%-усечённая средняя, CI_low, CI_high)."""
    n = len(x)
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot)
    for i in range(n_boot):
        sample = rng.choice(x, size=n, replace=True)
        boots[i] = stats.trim_mean(sample, trim_pct)
    point = float(stats.trim_mean(x, trim_pct))
    lo, hi = np.percentile(boots, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return point, float(lo), float(hi)


def run_horizon_stats(
    returns_by_horizon: dict[int, np.ndarray],
    alpha: float,
    n_boot: int,
    trim_pct: float,
    seed: int = 42,
) -> list[HorizonResult]:
    """returns_by_horizon: {horizon_s: np.array(r_i(h))} -- уже посчитанные
    r_i(h) = ln(Exit_i(h)/Entry_i) - c (базовая стоимость) для событий,
    вошедших в АНАЛИТИЧЕСКОЕ N (§2.2 пройден И entry-окно непустое, см.
    владелец: "N в §2.7 -- события, реально вошедшие в расчёт
    доходностей")."""
    horizons = list(returns_by_horizon.keys())
    raw = [sign_test_one_sided(returns_by_horizon[h]) for h in horizons]
    p_values = [r[3] for r in raw]
    bh = benjamini_hochberg(p_values, alpha)
    results = []
    for h, (n, n_ties, n_pos, p), (q, sig) in zip(horizons, raw, bh):
        x = returns_by_horizon[h]
        med = float(np.median(x)) if len(x) else float("nan")
        tm, lo, hi = bootstrap_trimmed_mean_ci(x, n_boot, alpha, trim_pct, seed)
        results.append(HorizonResult(h, n, n_ties, n_pos, med, p, q, sig, tm, lo, hi))
    return results
