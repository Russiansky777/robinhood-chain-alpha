#!/usr/bin/env python3
"""«Расхождение площадок» → проверка 2 владельца, часть 2 (2026-09-06):
"Пересчитать эпизоды и бэктест на 5-минутном шаге. Доходность на 5m
против 1h -- это множитель реалистичности."

Реюз `simulate_ticker` из `sleeping_refs_arb_backtest.py` НАПРЯМУЮ --
функция уже реально гранулярность-агностична (шаг фандинга и MAX_
HOLDING_HOURS считаются по реальной разнице timestamp'ов в мс, не по
числу строк) -- работает на 5м без изменений. Сравнение 1h vs 5m --
на ОДНОМ И ТОМ ЖЕ 14-дневном окне (последние 14 дней), не на полной
истории, иначе сравнение нечестное (разный охват).

ЛОКАЛЬНО после фетча 5м-свечей, 0 сети/кредитов."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

from sleeping_refs_arb_backtest import load_round_trip_sums, simulate_ticker  # noqa: E402

CACHE_DIR = Path("data/sleeping_refs_cache")
RAW_1H_PATH = CACHE_DIR / "hourly_divergence_full.csv"
FUNDING_PATH = CACHE_DIR / "sleeping_refs_funding_history_full.csv"
OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_5m_backtest_result.json")
STRONG_TICKERS = ("BE", "USAR", "CRWV", "SNDK", "ORCL")
NOTIONAL = 5000.0
MULTIPLIER = 1.5
ANNUALIZATION_DAYS = 365.0
HISTORY_DAYS = 14


def load_5m_merged(ticker: str) -> pd.DataFrame | None:
    l_path = CACHE_DIR / f"markprice_5m_{ticker}_lighter.csv"
    x_path = CACHE_DIR / f"markprice_5m_{ticker}_xyz.csv"
    if not l_path.exists() or not x_path.exists():
        return None
    l_df = pd.read_csv(l_path)[["t", "c"]].rename(columns={"c": "price_lighter"})
    x_df = pd.read_csv(x_path)[["t", "c"]].rename(columns={"c": "price_xyz"})
    merged = l_df.merge(x_df, on="t", how="inner").sort_values("t")
    if not len(merged):
        return None
    merged["D"] = merged["price_lighter"] / merged["price_xyz"] - 1
    merged["abs_D_pct"] = merged["D"].abs() * 100
    return merged


def backtest_summary(g: pd.DataFrame, rt_sum: float | None, fund_g: pd.DataFrame, days_span: float) -> dict:
    trades = simulate_ticker(g, fund_g, rt_sum, MULTIPLIER)
    if not trades:
        return {"n_trades": 0}
    tdf = pd.DataFrame(trades)
    return {
        "n_trades": len(tdf), "trades_per_month": len(tdf) / (days_span / 30.0),
        "mean_net_pnl_pct": float(tdf["net_pnl_pct"].mean()),
        "median_hours_held": float(tdf["hours_held"].median()),
        "frac_losing": float((tdf["net_pnl_pct"] < 0).mean()),
        "annualized_return_pct_on_capital": float(tdf["net_pnl_pct"].sum()) * (ANNUALIZATION_DAYS / days_span),
    }


def run() -> int:
    round_trip_sums = load_round_trip_sums()
    funding_df = pd.DataFrame()
    if FUNDING_PATH.exists():
        funding_df = pd.read_csv(FUNDING_PATH)
        funding_df["hour_ms"] = pd.to_datetime(funding_df["hour"], utc=True).astype("int64") // 1_000

    df_1h = pd.read_csv(RAW_1H_PATH)
    now_utc = datetime.now(timezone.utc)
    window_start_ms = int((now_utc - timedelta(days=HISTORY_DAYS)).timestamp() * 1000)
    df_1h_window = df_1h[df_1h["t"] >= window_start_ms]

    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "history_days": HISTORY_DAYS, "tickers": {}}

    for sym in STRONG_TICKERS:
        print(f"\n=== {sym} ===")
        rt_sum = round_trip_sums.get(sym, {}).get(NOTIONAL)
        fund_g = funding_df[funding_df["symbol"] == sym] if len(funding_df) else pd.DataFrame()

        g_1h = df_1h_window[df_1h_window["symbol"] == sym].sort_values("t")
        days_1h = (g_1h["t"].max() - g_1h["t"].min()) / 86_400_000.0 if len(g_1h) else 0
        summary_1h = backtest_summary(g_1h, rt_sum, fund_g, days_1h) if len(g_1h) else {"n_trades": 0}
        print(f"  1h  (те же {days_1h:.1f} дн.): {summary_1h}")

        g_5m = load_5m_merged(sym)
        if g_5m is None:
            print(f"  5m: нет данных")
            result["tickers"][sym] = {"backtest_1h_same_window": summary_1h, "backtest_5m": {"n_trades": 0}}
            continue
        days_5m = (g_5m["t"].max() - g_5m["t"].min()) / 86_400_000.0
        summary_5m = backtest_summary(g_5m, rt_sum, fund_g, days_5m)
        print(f"  5m  ({days_5m:.1f} дн., N={len(g_5m)} баров): {summary_5m}")

        ratio = None
        if summary_1h.get("annualized_return_pct_on_capital") and summary_5m.get("annualized_return_pct_on_capital"):
            ratio = summary_5m["annualized_return_pct_on_capital"] / summary_1h["annualized_return_pct_on_capital"]
            print(f"  множитель реалистичности (5m/1h доходность): {ratio:.3f}")

        result["tickers"][sym] = {
            "backtest_1h_same_window": summary_1h, "backtest_5m": summary_5m,
            "realism_multiplier_5m_over_1h": ratio,
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[5m_backtest] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
