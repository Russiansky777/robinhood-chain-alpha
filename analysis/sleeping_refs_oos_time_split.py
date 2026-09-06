#!/usr/bin/env python3
"""«Расхождение площадок» → проверка 1 владельца (2026-09-06, дословно):
"Out-of-sample по времени. Разбить историю: 05.07-05.08 и 06.08-06.09.
На первой половине -- топ-5 по доходности ($5000, x1.5). На второй --
доходность именно этой пятёрки, БЕЗ переотбора. Сравнить с 96%/78%.
Отдельно: совпадает ли пятёрка второй половины с первой."

Реюз `simulate_ticker`/`load_round_trip_sums` из `sleeping_refs_arb_
backtest.py` НАПРЯМУЮ (тот же метод входа/выхода, та же формула P&L) --
единственное отличие: фильтр часового D-ряда по периоду ДО симуляции.
ЧЕСТНОЕ ОГРАНИЧЕНИЕ (то же, что в основном бэктесте, не повторяем
отдельно ниже): round-trip costs -- ТЕКУЩИЙ снимок, применён
ретроактивно к обеим половинам одинаково.

ЛОКАЛЬНО, 0 сети/кредитов -- используются уже реально полученные данные
(часовой D-ряд, $20k глубина, история фандинга)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from sleeping_refs_arb_backtest import load_round_trip_sums, simulate_ticker  # noqa: E402

RAW_D_PATH = Path("data/sleeping_refs_cache/hourly_divergence_full.csv")
FUNDING_PATH = Path("data/sleeping_refs_cache/sleeping_refs_funding_history_full.csv")
OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_oos_time_split_result.json")

NOTIONAL = 5000.0
MULTIPLIER = 1.5
ANNUALIZATION_DAYS = 365.0
PERIOD_A = ("2026-07-05", "2026-08-05")  # реальный старт данных (xyz) -- реальный конец A
PERIOD_B = ("2026-08-06", "2026-09-06")


def compute_top5(d_df: pd.DataFrame, funding_df: pd.DataFrame, round_trip_sums: dict,
                  start: str, end: str) -> tuple[dict, float]:
    t_start = pd.Timestamp(start, tz="UTC").value // 1_000_000
    t_end = pd.Timestamp(end, tz="UTC").value // 1_000_000
    sub = d_df[(d_df["t"] >= t_start) & (d_df["t"] < t_end)]
    days_span = (t_end - t_start) / 86_400_000.0

    per_ticker: dict = {}
    for sym, g in sub.groupby("symbol"):
        rt_sum = round_trip_sums.get(sym, {}).get(NOTIONAL)
        fund_g = funding_df[funding_df["symbol"] == sym] if len(funding_df) else pd.DataFrame()
        trades = simulate_ticker(g.sort_values("t"), fund_g, rt_sum, MULTIPLIER)
        if not trades:
            per_ticker[sym] = {"n_trades": 0}
            continue
        tdf = pd.DataFrame(trades)
        annualized = float(tdf["net_pnl_pct"].sum()) * (ANNUALIZATION_DAYS / days_span)
        per_ticker[sym] = {
            "n_trades": len(tdf), "mean_net_pnl_pct": float(tdf["net_pnl_pct"].mean()),
            "frac_losing": float((tdf["net_pnl_pct"] < 0).mean()),
            "annualized_return_pct_on_capital": annualized,
        }
    return per_ticker, days_span


def run() -> int:
    d_df = pd.read_csv(RAW_D_PATH)
    round_trip_sums = load_round_trip_sums()
    funding_df = pd.DataFrame()
    if FUNDING_PATH.exists():
        funding_df = pd.read_csv(FUNDING_PATH)
        funding_df["hour_ms"] = pd.to_datetime(funding_df["hour"], utc=True).astype("int64") // 1_000  # см. фикс в arb_backtest.py

    real_start = pd.to_datetime(d_df["t"].min(), unit="ms", utc=True)
    real_end = pd.to_datetime(d_df["t"].max(), unit="ms", utc=True)
    print(f"[oos_split] реальный полный диапазон данных: {real_start} .. {real_end}")
    print(f"[oos_split] период A: {PERIOD_A}, период B: {PERIOD_B}")

    per_ticker_a, days_a = compute_top5(d_df, funding_df, round_trip_sums, *PERIOD_A)
    per_ticker_b, days_b = compute_top5(d_df, funding_df, round_trip_sums, *PERIOD_B)
    print(f"[oos_split] реальных дней в A={days_a:.1f}, в B={days_b:.1f}")

    def top5(per_ticker: dict) -> list[str]:
        ranked = sorted(
            ((sym, v["annualized_return_pct_on_capital"]) for sym, v in per_ticker.items() if v.get("n_trades", 0) > 0),
            key=lambda kv: -kv[1],
        )
        return [sym for sym, _ in ranked[:5]]

    top5_a = top5(per_ticker_a)
    top5_b = top5(per_ticker_b)
    print(f"\n[oos_split] топ-5 на периоде A (05.07-05.08): {top5_a}")
    for sym in top5_a:
        print(f"    {sym}: {per_ticker_a[sym]}")
    print(f"\n[oos_split] топ-5 на периоде B (независимо, для сверки состава): {top5_b}")
    for sym in top5_b:
        print(f"    {sym}: {per_ticker_b[sym]}")

    overlap = sorted(set(top5_a) & set(top5_b))
    print(f"\n[oos_split] пересечение топ-5 A и топ-5 B: {len(overlap)}/5 -- {overlap}")

    print(f"\n[oos_split] РЕАЛЬНАЯ проверка -- доходность ИМЕННО пятёрки A на периоде B (без переотбора):")
    b_returns_for_a_five = {}
    for sym in top5_a:
        v = per_ticker_b.get(sym, {"n_trades": 0})
        b_returns_for_a_five[sym] = v
        print(f"    {sym}: n_trades_B={v.get('n_trades')}, доходность/год_B={v.get('annualized_return_pct_on_capital')}")

    valid = [v["annualized_return_pct_on_capital"] for v in b_returns_for_a_five.values() if v.get("n_trades", 0) > 0]
    mean_b = float(np.mean(valid)) if valid else None
    median_b = float(np.median(valid)) if valid else None
    print(f"\n[oos_split] пятёрка A на B: mean={mean_b}, медиана={median_b} "
          f"(референс из полного периода: mean=96.29%, медиана=77.90%)")

    oos_pass_40 = median_b is not None and median_b >= 40.0
    print(f"[oos_split] предрегистрационный порог владельца п.(1): медиана B >= 40% -- "
          f"{'ПРОЙДЕНО' if oos_pass_40 else 'НЕ пройдено'} (медиана={median_b})")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "period_a": PERIOD_A, "period_b": PERIOD_B, "days_a": days_a, "days_b": days_b,
        "per_ticker_period_a": per_ticker_a, "per_ticker_period_b": per_ticker_b,
        "top5_period_a": top5_a, "top5_period_b_independent": top5_b,
        "top5_overlap_a_b": overlap,
        "period_a_five_on_period_b": b_returns_for_a_five,
        "period_a_five_on_b_mean_pct": mean_b, "period_a_five_on_b_median_pct": median_b,
        "reference_full_period_mean_pct": 96.29, "reference_full_period_median_pct": 77.90,
        "oos_check1_pass_median_ge_40pct": oos_pass_40,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[oos_split] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
