#!/usr/bin/env python3
"""«Спящие референсы», метрики п.4 владельца (2026-09-06, дословно):
расхождение площадок. По 23 реальным тикерам, торгуемым И на Lighter,
И на HL HIP-3 xyz (пересечение из `hyperliquid_hip3_xyz_markprice_
history_result.json::in_lighter_universe`): D = (цена Lighter / цена
xyz - 1) в вс 19:55 ET, и его изменение к пн 9:30 ET. Распределение
|D|, доля выходных с |D| > суммы round-trip ОБЕИХ площадок (реальный
буквальный смысл: сделка требует ОДИН round-trip на каждой ноге --
открыть+закрыть на Lighter, открыть+закрыть на xyz -- сумма двух
round-trip, не деление пополам), доля случаев схождения к
понедельнику (|D_пн| < |D_вс|).

ЧИСТО ЛОКАЛЬНО -- обе цены уже реально получены (свечи Lighter/xyz),
Dune/интернет не нужны, 0 кредитов, без сети."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from sleeping_refs_metrics_lib import load_hourly_csv, price_asof, et_to_utc, real_fridays_since  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_cross_venue_divergence_result.json")
CACHE_DIR = Path("data/sleeping_refs_cache")
LIGHTER_RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")
XYZ_RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_markprice_history_result.json")


def build_universe() -> dict:
    lighter = json.loads(LIGHTER_RESULT_PATH.read_text())["markets"]
    xyz = json.loads(XYZ_RESULT_PATH.read_text())["assets"]
    overlap = [t for t, m in xyz.items() if m.get("in_lighter_universe")]
    print(f"[cross_venue] реальное пересечение (23 ожидается): {len(overlap)} -- {sorted(overlap)}")

    universe: dict = {}
    for sym in overlap:
        lighter_m = lighter.get(sym)
        if lighter_m is None:
            print(f"    {sym}: реально НЕТ на Lighter (не должно происходить -- список из пересечения) -- пропуск")
            continue
        lighter_csv = CACHE_DIR / f"markprice_1h_{sym}_{lighter_m['market_id']}.csv"
        xyz_csv = CACHE_DIR / f"hl_xyz_markprice_1h_{sym}.csv"
        if not lighter_csv.exists() or not xyz_csv.exists():
            print(f"    {sym}: CSV не найден (lighter={lighter_csv.exists()}, xyz={xyz_csv.exists()}) -- пропуск")
            continue
        universe[sym] = {
            "lighter_csv": lighter_csv, "xyz_csv": xyz_csv,
            "lighter_n_candles": lighter_m["n_candles"],
            "lighter_round_trip_pct_500": lighter_m.get("round_trip_cost_pct_500"),
            "lighter_round_trip_pct_5000": lighter_m.get("round_trip_cost_pct_5000"),
            "xyz_round_trip_pct_500": xyz[sym].get("round_trip_cost_pct_500"),
            "xyz_round_trip_pct_5000": xyz[sym].get("round_trip_cost_pct_5000"),
        }
    return universe


def run() -> int:
    universe = build_universe()
    now_utc = datetime.now(timezone.utc)
    all_fridays = real_fridays_since("2026-07-01", now_utc)
    print(f"[cross_venue] реальных завершённых выходных: {len(all_fridays)} -- {all_fridays}")

    rows: list[dict] = []
    drop_reasons: dict[str, int] = {}

    def bump(reason: str) -> None:
        drop_reasons[reason] = drop_reasons.get(reason, 0) + 1

    for sym, meta in universe.items():
        lighter_candles = load_hourly_csv(meta["lighter_csv"])
        xyz_candles = load_hourly_csv(meta["xyz_csv"])
        for friday in all_fridays:
            fri = datetime.strptime(friday, "%Y-%m-%d")
            sun, mon = fri + timedelta(days=2), fri + timedelta(days=3)
            t_sun = et_to_utc(sun.year, sun.month, sun.day, 19, 55)
            t_mon = et_to_utc(mon.year, mon.month, mon.day, 9, 30)

            p_l_sun = price_asof(lighter_candles, t_sun)
            p_x_sun = price_asof(xyz_candles, t_sun)
            p_l_mon = price_asof(lighter_candles, t_mon)
            p_x_mon = price_asof(xyz_candles, t_mon)
            if None in (p_l_sun, p_x_sun, p_l_mon, p_x_mon) or p_x_sun <= 0 or p_x_mon <= 0:
                bump(f"{sym}: нет реальных цен на обеих площадках для {friday} (окно вне истории Lighter/xyz)")
                continue

            d_sun = p_l_sun / p_x_sun - 1
            d_mon = p_l_mon / p_x_mon - 1
            rt_sum_500 = (meta["lighter_round_trip_pct_500"] or 0) + (meta["xyz_round_trip_pct_500"] or 0)
            rt_sum_5000 = (meta["lighter_round_trip_pct_5000"] or 0) + (meta["xyz_round_trip_pct_5000"] or 0)
            rows.append({
                "symbol": sym, "friday": friday,
                "price_lighter_sun": p_l_sun, "price_xyz_sun": p_x_sun,
                "price_lighter_mon": p_l_mon, "price_xyz_mon": p_x_mon,
                "D_sun": d_sun, "D_mon": d_mon, "D_change": d_mon - d_sun,
                "abs_D_sun_pct": abs(d_sun) * 100, "abs_D_mon_pct": abs(d_mon) * 100,
                "round_trip_sum_pct_500": rt_sum_500, "round_trip_sum_pct_5000": rt_sum_5000,
                "converged_by_monday": abs(d_mon) < abs(d_sun),
            })

    df = pd.DataFrame(rows)
    print(f"\n[cross_venue] реальных строк (тикер x выходные) с полными ценами на обеих площадках: {len(df)}")
    for reason, cnt in drop_reasons.items():
        print(f"    пропущено: {reason} (n={cnt})")

    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "weekends_considered": all_fridays, "universe": sorted(universe), "n": len(df),
        "drop_reasons": drop_reasons,
    }

    if len(df):
        result["abs_D_sun_pct_distribution"] = {
            "mean": float(df["abs_D_sun_pct"].mean()), "median": float(df["abs_D_sun_pct"].median()),
            "p75": float(df["abs_D_sun_pct"].quantile(0.75)), "p90": float(df["abs_D_sun_pct"].quantile(0.90)),
            "max": float(df["abs_D_sun_pct"].max()),
        }
        result["frac_abs_D_gt_roundtrip_sum_500"] = float((df["abs_D_sun_pct"] > df["round_trip_sum_pct_500"]).mean())
        result["frac_abs_D_gt_roundtrip_sum_5000"] = float((df["abs_D_sun_pct"] > df["round_trip_sum_pct_5000"]).mean())
        result["frac_converged_by_monday"] = float(df["converged_by_monday"].mean())

        per_symbol = {}
        for sym, g in df.groupby("symbol"):
            per_symbol[sym] = {
                "n": int(len(g)), "mean_abs_D_sun_pct": float(g["abs_D_sun_pct"].mean()),
                "frac_gt_roundtrip_sum_500": float((g["abs_D_sun_pct"] > g["round_trip_sum_pct_500"]).mean()),
                "frac_gt_roundtrip_sum_5000": float((g["abs_D_sun_pct"] > g["round_trip_sum_pct_5000"]).mean()),
                "frac_converged_by_monday": float(g["converged_by_monday"].mean()),
            }
        result["per_symbol"] = per_symbol

        print(f"\n=== ИТОГО (N={len(df)}) ===")
        print(f"  |D_вс| распределение: {result['abs_D_sun_pct_distribution']}")
        print(f"  доля |D| > sum(round-trip $500): {result['frac_abs_D_gt_roundtrip_sum_500']:.2%}")
        print(f"  доля |D| > sum(round-trip $5000): {result['frac_abs_D_gt_roundtrip_sum_5000']:.2%}")
        print(f"  доля схождения к понедельнику: {result['frac_converged_by_monday']:.2%}")

        # Робастность (устоявшееся правило проекта: ВСЕГДА проверять
        # чувствительность к выбросам ДО того, как доверять headline --
        # см. task1_z_decompose.py). Порог |D_вс|>1% выбран ПОСЛЕ
        # просмотра результата (реально только 1 наблюдение выше --
        # USAR 07-03, 3.23%) -- честно помечено как post-hoc, не
        # предрегистрировано.
        robust_mask = df["abs_D_sun_pct"] <= 1.0
        robust = df[robust_mask]
        n_excluded = int((~robust_mask).sum())
        if len(robust) >= 3:
            result["robustness_check_post_hoc"] = {
                "threshold_note": "порог |D_вс|<=1% выбран ПОСЛЕ просмотра данных (post-hoc, не предрегистрирован)",
                "n_excluded": n_excluded, "n_robust": int(len(robust)),
                "mean_abs_D_sun_pct": float(robust["abs_D_sun_pct"].mean()),
                "median_abs_D_sun_pct": float(robust["abs_D_sun_pct"].median()),
                "frac_gt_roundtrip_sum_500": float((robust["abs_D_sun_pct"] > robust["round_trip_sum_pct_500"]).mean()),
                "frac_gt_roundtrip_sum_5000": float((robust["abs_D_sun_pct"] > robust["round_trip_sum_pct_5000"]).mean()),
                "frac_converged_by_monday": float(robust["converged_by_monday"].mean()),
            }
            print(f"  РОБАСТНОСТЬ (исключено {n_excluded} выброс(ов) |D_вс|>1%): "
                  f"{result['robustness_check_post_hoc']}")
    else:
        result["note"] = "N=0 -- честно доложить, нет ни одной пары наблюдений с ценами на обеих площадках"

    result["rows"] = rows
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[cross_venue] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
