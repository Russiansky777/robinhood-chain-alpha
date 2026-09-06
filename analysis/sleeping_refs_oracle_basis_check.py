#!/usr/bin/env python3
"""«Расхождение площадок» → проверка 5 владельца (2026-09-06, дословно):
"Оракул или рынок. По каждому из пяти тикеров: автокорреляция знака D
по часам суток и дням недели. Если D систематически одного знака в
определённые часы -- это разница оракулов, вычесть как базис и
пересчитать доходность на остатке."

Метод (владелец не задавал точный алгоритм -- решение здесь явное):
- "систематически одного знака" проверяется t-тестом mean(D)=0 по
  каждому часу ET (N~63-64 реальных дня на час) и по каждому дню
  недели (N~216-225) -- |t|>2 считаем реальным сигналом, не шумом.
- Базис = mean(D | тикер, час ET) -- почасовой, самый гранулярный
  разумный уровень агрегации, покрывает и "плоское" смещение (как у
  ORCL — почти одинаковый оффсет на каждом часе), и часо-специфичное.
- Остаток D_residual = D - basis[тикер, час]. Пересчитываем episodes/
  бэктест НА ОСТАТКЕ для сигнала входа/выхода (не на сырых D) -- P&L
  считается по-прежнему на РЕАЛЬНЫХ ценах (та же формула, что
  `simulate_ticker`), сравниваем годовую доходность residual-based
  версии с исходной (сырой D)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats as sstats  # noqa: E402

from sleeping_refs_arb_backtest import load_round_trip_sums, simulate_ticker  # noqa: E402

RAW_D_PATH = Path("data/sleeping_refs_cache/hourly_divergence_full.csv")
FUNDING_PATH = Path("data/sleeping_refs_cache/sleeping_refs_funding_history_full.csv")
OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_oracle_basis_check_result.json")
STRONG_TICKERS = ("BE", "USAR", "CRWV", "SNDK", "ORCL")
NOTIONAL = 5000.0
MULTIPLIER = 1.5
ANNUALIZATION_DAYS = 365.0
DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def run() -> int:
    df = pd.read_csv(RAW_D_PATH)
    sub = df[df["symbol"].isin(STRONG_TICKERS)].copy()
    round_trip_sums = load_round_trip_sums()
    funding_df = pd.DataFrame()
    if FUNDING_PATH.exists():
        funding_df = pd.read_csv(FUNDING_PATH)
        funding_df["hour_ms"] = pd.to_datetime(funding_df["hour"], utc=True).astype("int64") // 1_000

    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tickers": {}}

    for sym in STRONG_TICKERS:
        g = sub[sub["symbol"] == sym].copy()
        print(f"\n=== {sym} ===")

        # По часу ET
        sig_hours = []
        by_hour_all = {}
        for h, gh in g.groupby("hour_et"):
            if len(gh) < 5:
                continue
            t, p = sstats.ttest_1samp(gh["D"], 0)
            by_hour_all[int(h)] = {"mean_D_pct": float(gh["D"].mean() * 100), "t": float(t), "n": int(len(gh))}
            if abs(t) > 2:
                sig_hours.append(int(h))
        n_sig_hours = len(sig_hours)
        print(f"  значимых часов (|t|>2, из 24): {n_sig_hours} -- {sig_hours}")

        # По дню недели
        sig_dow = []
        by_dow_all = {}
        for d in DOW_ORDER:
            gd = g[g["dow_et"] == d]
            if len(gd) < 5:
                continue
            t, p = sstats.ttest_1samp(gd["D"], 0)
            by_dow_all[d] = {"mean_D_pct": float(gd["D"].mean() * 100), "t": float(t), "n": int(len(gd))}
            if abs(t) > 2:
                sig_dow.append(d)
        print(f"  значимых дней недели (|t|>2, из 7): {len(sig_dow)} -- {sig_dow}")

        # Плоский оффсет или час-специфичный? std по часам относительно overall mean
        hour_means = np.array([v["mean_D_pct"] for v in by_hour_all.values()])
        overall_mean = float(g["D"].mean() * 100)
        flatness = float(hour_means.std())  # маленький std -- "плоское" смещение (как ORCL), большой -- часо-специфичное
        print(f"  overall mean(D)={overall_mean:.4f}%, std по часам={flatness:.4f}pp "
              f"({'ПЛОСКИЙ базис (похоже на оракул)' if flatness < abs(overall_mean)*0.4 else 'час-специфичный/выходной паттерн'})")

        # Остаток -- вычитаем почасовой базис, пересчитываем episodes/бэктест
        basis_by_hour = g.groupby("hour_et")["D"].transform("mean")
        g["D_residual"] = g["D"] - basis_by_hour
        g["abs_D_residual_pct"] = g["D_residual"].abs() * 100

        rt_sum = round_trip_sums.get(sym, {}).get(NOTIONAL)
        fund_g = funding_df[funding_df["symbol"] == sym] if len(funding_df) else pd.DataFrame()

        # Исходный (сырой D) бэктест на полном периоде -- для сравнения
        g_raw = g.sort_values("t")
        trades_raw = simulate_ticker(g_raw, fund_g, rt_sum, MULTIPLIER)

        # Residual-бэктест: та же функция, но D/abs_D_pct = residual (P&L по-прежнему на реальных ценах внутри simulate_ticker)
        g_resid = g_raw.copy()
        g_resid["D"] = g_resid["D_residual"]
        g_resid["abs_D_pct"] = g_resid["abs_D_residual_pct"]
        trades_resid = simulate_ticker(g_resid, fund_g, rt_sum, MULTIPLIER)

        days_span = (g_raw["t"].max() - g_raw["t"].min()) / 86_400_000.0

        def summarize(trades: list[dict]) -> dict:
            if not trades:
                return {"n_trades": 0}
            tdf = pd.DataFrame(trades)
            return {
                "n_trades": len(tdf), "mean_net_pnl_pct": float(tdf["net_pnl_pct"].mean()),
                "frac_losing": float((tdf["net_pnl_pct"] < 0).mean()),
                "annualized_return_pct_on_capital": float(tdf["net_pnl_pct"].sum()) * (ANNUALIZATION_DAYS / days_span),
            }

        raw_summary = summarize(trades_raw)
        resid_summary = summarize(trades_resid)
        print(f"  СЫРОЙ D:      n={raw_summary.get('n_trades')}, доходность/год={raw_summary.get('annualized_return_pct_on_capital')}")
        print(f"  ОСТАТОК(D-basis): n={resid_summary.get('n_trades')}, доходность/год={resid_summary.get('annualized_return_pct_on_capital')}")

        result["tickers"][sym] = {
            "by_hour_et": by_hour_all, "by_day_of_week": by_dow_all,
            "significant_hours": sig_hours, "significant_days": sig_dow,
            "overall_mean_D_pct": overall_mean, "hour_profile_std_pct": flatness,
            "basis_character": "flat_offset_oracle_like" if flatness < abs(overall_mean) * 0.4 and abs(overall_mean) > 0.01 else "hour_or_weekend_specific",
            "backtest_raw": raw_summary, "backtest_residual_after_hourly_basis": resid_summary,
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[oracle_basis] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
