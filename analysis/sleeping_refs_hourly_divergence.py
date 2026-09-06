#!/usr/bin/env python3
"""«Спящие референсы» → расхождение площадок, п.1 владельца (2026-09-06,
дословно): "Не только выходные. Пересчитать |D| по всем часам с 05.07
по 23 общим тикерам, не только вс 19:55. Распределение |D| по часам
суток и дням недели: живёт ли расхождение в будни, есть ли суточный
профиль."

D = цена_Lighter/цена_xyz - 1, реальная часовая пара цен (JOIN по
точному общему часовому таймстемпу `t`, обе свечи уже реально получены
в предыдущих шагах -- НИКАКИХ новых сетевых запросов, чисто локально,
0 кредитов/сети)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from sleeping_refs_metrics_lib import ET  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_hourly_divergence_result.json")
CACHE_DIR = Path("data/sleeping_refs_cache")
LIGHTER_RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")
XYZ_RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_markprice_history_result.json")


def build_universe() -> dict:
    lighter = json.loads(LIGHTER_RESULT_PATH.read_text())["markets"]
    xyz = json.loads(XYZ_RESULT_PATH.read_text())["assets"]
    overlap = [t for t, m in xyz.items() if m.get("in_lighter_universe")]

    universe: dict = {}
    for sym in overlap:
        lighter_m = lighter.get(sym)
        if lighter_m is None:
            continue
        lighter_csv = CACHE_DIR / f"markprice_1h_{sym}_{lighter_m['market_id']}.csv"
        xyz_csv = CACHE_DIR / f"hl_xyz_markprice_1h_{sym}.csv"
        if not lighter_csv.exists() or not xyz_csv.exists():
            continue
        universe[sym] = {
            "lighter_csv": lighter_csv, "xyz_csv": xyz_csv,
            "lighter_round_trip_pct_5000": lighter_m.get("round_trip_cost_pct_5000"),
            "xyz_round_trip_pct_5000": xyz[sym].get("round_trip_cost_pct_5000"),
        }
    print(f"[hourly_divergence] реальный универсум (23 ожидается): {len(universe)} -- {sorted(universe)}")
    return universe


def load_candles(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["t"] = pd.to_numeric(df["t"], errors="coerce")
    df = df.dropna(subset=["t", "c"])
    return df[["t", "c"]].rename(columns={"c": "close"})


def run() -> int:
    universe = build_universe()
    all_rows: list[pd.DataFrame] = []

    for sym, meta in universe.items():
        l_df = load_candles(meta["lighter_csv"]).rename(columns={"close": "price_lighter"})
        x_df = load_candles(meta["xyz_csv"]).rename(columns={"close": "price_xyz"})
        merged = l_df.merge(x_df, on="t", how="inner")
        if not len(merged):
            print(f"    {sym}: 0 общих часовых точек -- пропуск")
            continue
        merged["symbol"] = sym
        merged["D"] = merged["price_lighter"] / merged["price_xyz"] - 1
        merged["abs_D_pct"] = merged["D"].abs() * 100
        merged["round_trip_sum_pct_5000"] = (meta["lighter_round_trip_pct_5000"] or 0) + (meta["xyz_round_trip_pct_5000"] or 0)
        merged["dt_utc"] = pd.to_datetime(merged["t"], unit="ms", utc=True)
        merged["dt_et"] = merged["dt_utc"].dt.tz_convert(ET)
        merged["hour_et"] = merged["dt_et"].dt.hour
        merged["dow_et"] = merged["dt_et"].dt.day_name()
        merged["is_weekend"] = merged["dt_et"].dt.weekday >= 5  # сб/вс (владелец: "будни" -- явно противопоставляет)
        all_rows.append(merged)
        print(f"    {sym}: {len(merged)} общих часовых точек, mean|D|={merged['abs_D_pct'].mean():.4f}%")

    df = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    print(f"\n[hourly_divergence] реальных часовых наблюдений (все тикеры, всё время): {len(df)}")

    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "universe": sorted(universe), "n_total_hourly_obs": len(df),
    }
    if not len(df):
        result["note"] = "N=0 -- честно доложить"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    result["overall_abs_D_pct"] = {
        "mean": float(df["abs_D_pct"].mean()), "median": float(df["abs_D_pct"].median()),
        "p75": float(df["abs_D_pct"].quantile(0.75)), "p90": float(df["abs_D_pct"].quantile(0.90)),
        "p95": float(df["abs_D_pct"].quantile(0.95)), "max": float(df["abs_D_pct"].max()),
    }
    result["frac_gt_roundtrip_5000_overall"] = float((df["abs_D_pct"] > df["round_trip_sum_pct_5000"]).mean())

    # Будни vs выходные -- владелец: "живёт ли расхождение в будни"
    for label, mask in (("weekday", ~df["is_weekend"]), ("weekend", df["is_weekend"])):
        sub = df[mask]
        result[f"{label}_stats"] = {
            "n": int(len(sub)), "mean_abs_D_pct": float(sub["abs_D_pct"].mean()) if len(sub) else None,
            "median_abs_D_pct": float(sub["abs_D_pct"].median()) if len(sub) else None,
            "frac_gt_roundtrip_5000": float((sub["abs_D_pct"] > sub["round_trip_sum_pct_5000"]).mean()) if len(sub) else None,
        }
        print(f"[hourly_divergence] {label}: n={result[f'{label}_stats']['n']}, "
              f"mean|D|={result[f'{label}_stats']['mean_abs_D_pct']}, "
              f"frac>rt5000={result[f'{label}_stats']['frac_gt_roundtrip_5000']}")

    # Суточный профиль (час ET) -- владелец: "есть ли суточный профиль"
    by_hour = df.groupby("hour_et").agg(
        n=("abs_D_pct", "size"), mean_abs_D_pct=("abs_D_pct", "mean"),
        median_abs_D_pct=("abs_D_pct", "median"),
        frac_gt_rt5000=("abs_D_pct", lambda s: float((s > df.loc[s.index, "round_trip_sum_pct_5000"]).mean())),
    ).reset_index().sort_values("hour_et")
    result["by_hour_et"] = by_hour.to_dict("records")

    # По дню недели
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    by_dow = df.groupby("dow_et").agg(
        n=("abs_D_pct", "size"), mean_abs_D_pct=("abs_D_pct", "mean"),
        median_abs_D_pct=("abs_D_pct", "median"),
    ).reindex(dow_order).reset_index()
    result["by_day_of_week_et"] = by_dow.to_dict("records")
    print("\n[hourly_divergence] по дню недели (ET):")
    for row in result["by_day_of_week_et"]:
        print(f"    {row['dow_et']}: n={row['n']}, mean|D|={row['mean_abs_D_pct']:.4f}%, median|D|={row['median_abs_D_pct']:.4f}%")

    # По тикеру
    per_symbol = {}
    for sym, g in df.groupby("symbol"):
        per_symbol[sym] = {
            "n": int(len(g)), "mean_abs_D_pct": float(g["abs_D_pct"].mean()),
            "median_abs_D_pct": float(g["abs_D_pct"].median()),
            "frac_gt_roundtrip_5000": float((g["abs_D_pct"] > g["round_trip_sum_pct_5000"]).mean()),
        }
    result["per_symbol"] = per_symbol

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    # Полный часовой D-ряд -- отдельно, нужен следующему шагу (эпизоды/бэктест)
    raw_path = Path("data/sleeping_refs_cache/hourly_divergence_full.csv")
    df.to_csv(raw_path, index=False)
    print(f"\n[hourly_divergence] результат записан в {OUT_PATH}, полный ряд -- в {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
