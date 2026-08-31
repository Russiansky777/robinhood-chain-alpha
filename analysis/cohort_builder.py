"""Гейт 3: сборка когорты А (топ по July PnL) и когорты Б (случайный
контроль) из финального пула кошельков, прошедших гейты 1-2.
"""
from __future__ import annotations

import random

import pandas as pd

from config import CONFIG


def build_cohorts(gated_wallets: pd.DataFrame, cohort_size: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`gated_wallets` — результат sql/05_final_cohort_pool_july.sql:
    колонки wallet_address, trade_count, unique_tokens_traded,
    realized_pnl_usd, avg_hold_period_hours, pnl_rank.

    Возвращает (cohort_a, cohort_b), каждая с добавленной колонкой
    `cohort`.
    """
    n = cohort_size or CONFIG.cohort_size
    available = len(gated_wallets)
    if available < 2 * n:
        n = available // 2
        print(
            f"[cohort_builder] WARNING: после гейтов 1-2 доступно только "
            f"{available} кошельков — недостаточно для двух когорт по "
            f"{cohort_size or CONFIG.cohort_size}. Урезаю N до {n} на "
            f"когорту. См. docs/RESULTS.md sensitivity-секцию."
        )
    if n == 0:
        raise ValueError(
            "После гейтов 1-2 не осталось кошельков вообще (или их < 2) — "
            "нельзя собрать ни одну когорту. Проверьте пороги MIN_TRADES/"
            "MIN_UNIQUE_TOKENS и сами данные (возможно, дело в покрытии "
            "Robinhood Chain на Dune на момент запуска)."
        )

    sorted_df = gated_wallets.sort_values("realized_pnl_usd", ascending=False).reset_index(drop=True)
    cohort_a = sorted_df.iloc[:n].copy()
    cohort_a["cohort"] = "A_top_pnl"

    rest = sorted_df.iloc[n:].copy()
    rng = random.Random(CONFIG.random_seed)
    if len(rest) < n:
        raise ValueError(
            f"Остаток кошельков ({len(rest)}) меньше требуемого размера "
            f"когорты Б ({n}) — уменьшите COHORT_SIZE."
        )
    sampled_idx = rng.sample(range(len(rest)), n)
    cohort_b = rest.iloc[sampled_idx].copy()
    cohort_b["cohort"] = "B_control"

    assert set(cohort_a["wallet_address"]).isdisjoint(set(cohort_b["wallet_address"]))
    return cohort_a, cohort_b


def merge_august_pnl(cohort: pd.DataFrame, august_agg: pd.DataFrame) -> pd.DataFrame:
    """Джойнит августовскую агрегацию к когорте. Кошельки без свопов в
    августе получают realized_pnl_usd_august = 0 (не выбрасываются) —
    см. docs/README.md, Гейт 4.
    """
    merged = cohort.merge(
        august_agg[["wallet_address", "trade_count_august", "unique_tokens_august", "realized_pnl_usd_august"]],
        on="wallet_address",
        how="left",
    )
    merged["trade_count_august"] = merged["trade_count_august"].fillna(0).astype(int)
    merged["unique_tokens_august"] = merged["unique_tokens_august"].fillna(0).astype(int)
    merged["realized_pnl_usd_august"] = merged["realized_pnl_usd_august"].fillna(0.0)
    return merged
