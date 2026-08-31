"""Гейт 5: главный статистический тест — значимо ли когорта А лучше
когорты Б в августе (не просто 'есть ли прибыльные').

Все пороги/критерии зафиксированы в docs/README.md ДО просмотра
результатов.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats


@dataclass
class TestResult:
    mannwhitney_u: float
    mannwhitney_p_one_sided: float
    rank_biserial_effect_size: float
    bootstrap_median_diff: float
    bootstrap_ci_low: float
    bootstrap_ci_high: float
    pct_profitable_a: float
    pct_profitable_b: float
    fisher_p: float
    spearman_rho: float
    spearman_p: float
    median_pnl_a: float
    median_pnl_b: float
    verdict: str
    verdict_reasoning: str


def mann_whitney_one_sided(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """H1: a > b (стохастически). Возвращает (U, p_one_sided, rank_biserial)."""
    u_stat, p_one_sided = stats.mannwhitneyu(a, b, alternative="greater")
    n1, n2 = len(a), len(b)
    # scipy.mannwhitneyu(a, b) returns U for sample `a` (U1). Rank-biserial
    # here is defined so that +1 = a fully greater than b, -1 = reverse
    # (verified against a hand-separated example: a >> b -> U1=n1*n2 -> +1).
    rank_biserial = (2 * u_stat) / (n1 * n2) - 1
    return float(u_stat), float(p_one_sided), float(rank_biserial)


def bootstrap_median_diff(
    a: np.ndarray, b: np.ndarray, n_boot: int = 10_000, seed: int = 42, alpha: float = 0.05
) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        sample_a = rng.choice(a, size=len(a), replace=True)
        sample_b = rng.choice(b, size=len(b), replace=True)
        diffs[i] = np.median(sample_a) - np.median(sample_b)
    point = float(np.median(a) - np.median(b))
    lo, hi = np.percentile(diffs, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return point, float(lo), float(hi)


def proportion_profitable_test(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """Доля кошельков с PnL > 0 в каждой когорте + Fisher exact p-value.
    Вторичный тест — не главный вывод, см. docs/README.md."""
    a_prof = int((a > 0).sum())
    b_prof = int((b > 0).sum())
    table = [[a_prof, len(a) - a_prof], [b_prof, len(b) - b_prof]]
    _, p = stats.fisher_exact(table, alternative="greater")
    return a_prof / len(a), b_prof / len(b), float(p)


def spearman_persistence(july_pnl: np.ndarray, august_pnl: np.ndarray) -> tuple[float, float]:
    rho, p = stats.spearmanr(july_pnl, august_pnl)
    return float(rho), float(p)


def run_full_test(
    cohort_a: pd.DataFrame,
    cohort_b: pd.DataFrame,
    all_gated_july_pnl: np.ndarray | None = None,
    all_gated_august_pnl: np.ndarray | None = None,
    alpha: float = 0.05,
) -> TestResult:
    a = cohort_a["realized_pnl_usd_august"].to_numpy()
    b = cohort_b["realized_pnl_usd_august"].to_numpy()

    u_stat, p_one_sided, effect = mann_whitney_one_sided(a, b)
    diff, ci_lo, ci_hi = bootstrap_median_diff(a, b, alpha=alpha)
    pct_a, pct_b, fisher_p = proportion_profitable_test(a, b)

    if all_gated_july_pnl is not None and all_gated_august_pnl is not None:
        rho, spearman_p = spearman_persistence(all_gated_july_pnl, all_gated_august_pnl)
    else:
        rho, spearman_p = float("nan"), float("nan")

    median_a, median_b = float(np.median(a)), float(np.median(b))

    # Критерии вердикта зафиксированы в docs/README.md заранее.
    cond_p = p_one_sided < alpha
    cond_magnitude = median_a > median_b  # "экономически значимая величина" -
    # финальное суждение о величине оставлено человеку в отчёте (RESULTS.md),
    # автоматически проверяем только знак + p-value + spearman ниже.
    cond_spearman = (not np.isnan(rho)) and rho > 0 and spearman_p < alpha

    if cond_p and cond_magnitude and cond_spearman:
        verdict = "ДА"
        reasoning = (
            f"Mann-Whitney one-sided p={p_one_sided:.4f} < {alpha}, "
            f"медиана A (${median_a:,.0f}) > медиана B (${median_b:,.0f}), "
            f"Spearman ρ={rho:.3f} (p={spearman_p:.4f}) положительная и значима."
        )
    elif cond_p and cond_magnitude and not cond_spearman:
        verdict = "СЛАБЫЙ СИГНАЛ (не ДА)"
        reasoning = (
            f"Когортный тест проходит (p={p_one_sided:.4f}, медиана A > B), "
            f"но Spearman-персистентность по всему пулу не подтверждается "
            f"(ρ={rho:.3f}, p={spearman_p:.4f}) — эффект похож на выброс "
            f"верхних кошельков, а не на общую персистентность рангов."
        )
    else:
        verdict = "НЕТ"
        reasoning = (
            f"Не выполнен как минимум один из заранее заданных критериев: "
            f"p_one_sided={p_one_sided:.4f} (need <{alpha}), "
            f"median_A={median_a:,.0f} vs median_B={median_b:,.0f}, "
            f"spearman_rho={rho:.3f} (p={spearman_p:.4f})."
        )

    return TestResult(
        mannwhitney_u=u_stat,
        mannwhitney_p_one_sided=p_one_sided,
        rank_biserial_effect_size=effect,
        bootstrap_median_diff=diff,
        bootstrap_ci_low=ci_lo,
        bootstrap_ci_high=ci_hi,
        pct_profitable_a=pct_a,
        pct_profitable_b=pct_b,
        fisher_p=fisher_p,
        spearman_rho=rho,
        spearman_p=spearman_p,
        median_pnl_a=median_a,
        median_pnl_b=median_b,
        verdict=verdict,
        verdict_reasoning=reasoning,
    )


@dataclass
class TwoPartResult:
    """Sprint 1.5: двухчастный тест вместо location-shift по всей
    когорте (см. docs/README.md, "Sprint 1.5"). Активность (торговал ли
    кошелёк в августе вообще) сама по себе не считается скиллом —
    Часть 0 её просто фиксирует описательно, без критерия. Первичный
    критерий (Часть 1) и вторичный (Часть 2) считаются ТОЛЬКО среди
    августовски-активных кошельков в каждой когорте.
    """

    n_a: int
    n_b: int
    pct_active_a: float
    pct_active_b: float
    n_active_a: int
    n_active_b: int
    # Часть 1 (первичный): доля профитных среди активных
    prop_positive_a: float
    prop_positive_b: float
    lift: float
    fisher_p: float
    part1_pass: bool
    # Часть 2 (вторичный): Mann-Whitney по PnL среди активных
    mwu_u: float
    mwu_p: float
    median_active_a: float
    median_active_b: float
    part2_directional: bool
    part2_significant: bool
    verdict: str
    verdict_reasoning: str


def two_part_analysis(
    cohort_a: pd.DataFrame,
    cohort_b: pd.DataFrame,
    alpha1: float = 0.01,
    min_lift: float = 2.0,
    alpha2: float = 0.05,
) -> TwoPartResult:
    """cohort_a/cohort_b: колонки wallet_address, trade_count_august,
    realized_pnl_usd_august (уже смёрджены с августовской агрегацией,
    неактивные кошельки — trade_count_august=0, realized_pnl_usd_august=0,
    см. cohort_builder.merge_august_pnl).

    Вердикт ДА только если Часть 1 прошла ПОЛНОСТЬЮ (p<alpha1 И
    lift>=min_lift) И Часть 2 хотя бы направленно положительна
    (медиана А > медианы Б, без требования к p-value) — см.
    docs/README.md.
    """
    active_a = cohort_a[cohort_a["trade_count_august"] > 0]
    active_b = cohort_b[cohort_b["trade_count_august"] > 0]
    n_a, n_b = len(cohort_a), len(cohort_b)
    n_active_a, n_active_b = len(active_a), len(active_b)
    pct_active_a = n_active_a / n_a if n_a else float("nan")
    pct_active_b = n_active_b / n_b if n_b else float("nan")

    prof_a = int((active_a["realized_pnl_usd_august"] > 0).sum())
    prof_b = int((active_b["realized_pnl_usd_august"] > 0).sum())
    prop_a = prof_a / n_active_a if n_active_a else float("nan")
    prop_b = prof_b / n_active_b if n_active_b else float("nan")
    if prop_b and prop_b > 0:
        lift = prop_a / prop_b
    elif prop_a and prop_a > 0:
        lift = float("inf")
    else:
        lift = float("nan")

    if n_active_a > 0 and n_active_b > 0:
        table = [[prof_a, n_active_a - prof_a], [prof_b, n_active_b - prof_b]]
        _, fisher_p = stats.fisher_exact(table, alternative="greater")
    else:
        fisher_p = float("nan")

    part1_pass = (not np.isnan(fisher_p)) and fisher_p < alpha1 and (not np.isnan(lift)) and lift >= min_lift

    if n_active_a > 1 and n_active_b > 1:
        u, mwu_p = stats.mannwhitneyu(
            active_a["realized_pnl_usd_august"], active_b["realized_pnl_usd_august"], alternative="greater"
        )
    else:
        u, mwu_p = float("nan"), float("nan")

    median_a = float(active_a["realized_pnl_usd_august"].median()) if n_active_a else float("nan")
    median_b = float(active_b["realized_pnl_usd_august"].median()) if n_active_b else float("nan")
    part2_directional = (not np.isnan(median_a)) and (not np.isnan(median_b)) and median_a > median_b
    part2_significant = (not np.isnan(mwu_p)) and mwu_p < alpha2

    def _pct(x: float) -> str:
        return "n/a" if np.isnan(x) else f"{x:.1%}"

    def _lift(x: float) -> str:
        if np.isnan(x):
            return "n/a"
        return "∞" if np.isinf(x) else f"{x:.2f}x"

    def _usd(x: float) -> str:
        return "n/a" if np.isnan(x) else f"${x:,.0f}"

    def _p(x: float) -> str:
        return "n/a" if np.isnan(x) else f"{x:.5f}"

    verdict = "ДА" if (part1_pass and part2_directional) else "НЕТ"
    reasoning = (
        f"Часть 1 (первичный, среди {n_active_a} vs {n_active_b} августовски-активных): "
        f"доля профитных A={_pct(prop_a)} vs Б={_pct(prop_b)}, лифт={_lift(lift)}, "
        f"Fisher one-sided p={_p(fisher_p)} (нужно p<{alpha1} и лифт>={min_lift}x) -> "
        f"{'ПРОШЛА' if part1_pass else 'НЕ ПРОШЛА'}. "
        f"Часть 2 (вторичный): медиана августовского PnL среди активных "
        f"A={_usd(median_a)} vs Б={_usd(median_b)} "
        f"({'направленно положительна' if part2_directional else 'НЕ положительна'}), "
        f"Mann-Whitney p={_p(mwu_p)} ({'значим' if part2_significant else 'не значим'} при {alpha2})."
    )

    return TwoPartResult(
        n_a=n_a, n_b=n_b,
        pct_active_a=pct_active_a, pct_active_b=pct_active_b,
        n_active_a=n_active_a, n_active_b=n_active_b,
        prop_positive_a=prop_a, prop_positive_b=prop_b, lift=lift,
        fisher_p=float(fisher_p), part1_pass=part1_pass,
        mwu_u=float(u), mwu_p=float(mwu_p),
        median_active_a=median_a, median_active_b=median_b,
        part2_directional=part2_directional, part2_significant=part2_significant,
        verdict=verdict, verdict_reasoning=reasoning,
    )
