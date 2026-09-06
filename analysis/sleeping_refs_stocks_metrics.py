#!/usr/bin/env python3
"""«Спящие референсы», метрики п.1 владельца (2026-09-06, дословно):
класс «отдельные акции» -- Lighter (25 тикеров с полной историей,
реально n_candles>=1900 -- владелец сказал "24", реальный подсчёт по
факту дал 25, честно используем реальное число, не подгоняем под
названное) и HL HIP-3 xyz (23 тикера пересечения с Lighter). Тикеры со
стартом истории 03-04.09 (13 "новых" на Lighter) -- уже исключены по
конструкции самим фильтром n_candles>=1900.

Окна (владелец, дословно): X = пт 20:00 -> вс 19:55 ET, Z1 = вс 20:00
-> 21:00 ET, Z2 = 21:00 ET -> пн 9:30 ET, Y -- yfinance закрытие пт
(16:00 ET, регулярная сессия) -> открытие пн (9:30 ET) -- тот же
реальный источник/метод, что уже подтверждён и оплачен в
`task1_weekend_gap.py` (yfinance, GH Actions -- Stooq и Chainlink уже
провалили калибровку, задокументировано там).

Метод "as-of"-цены для X/Z1/Z2 на собственных часовых свечах (Lighter/
xyz) -- см. `sleeping_refs_metrics_lib.py`. Ноль кредитов Dune -- ТОЛЬКО
yfinance (реальный внешний источник, требует интернет -> GH Actions,
локально заблокирован тем же прокси, что и всё остальное в этой
сессии)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from sleeping_refs_metrics_lib import (  # noqa: E402
    load_hourly_csv, price_asof, stock_class_windows, sign_opposite_fraction,
    cluster_sign_flip_test, round_trip_comparison, weekend_breakdown,
    real_fridays_since, yfinance_daily, friday_monday_gap,
)

OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_stocks_metrics_result.json")
CACHE_DIR = Path("data/sleeping_refs_cache")
LIGHTER_RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")
XYZ_RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_markprice_history_result.json")
FULL_HISTORY_MIN_CANDLES = 1900  # реальный порог "полная история" -- см. докстринг


def build_universe() -> dict:
    lighter = json.loads(LIGHTER_RESULT_PATH.read_text())
    xyz = json.loads(XYZ_RESULT_PATH.read_text())

    lighter_full = {
        sym: {"market_id": m["market_id"], "csv": CACHE_DIR / f"markprice_1h_{sym}_{m['market_id']}.csv",
              "round_trip_pct_500": m.get("round_trip_cost_pct_500"),
              "round_trip_pct_5000": m.get("round_trip_cost_pct_5000")}
        for sym, m in lighter["markets"].items() if m["n_candles"] >= FULL_HISTORY_MIN_CANDLES
    }
    xyz_overlap = {
        sym: {"csv": CACHE_DIR / f"hl_xyz_markprice_1h_{sym}.csv",
              "round_trip_pct_500": m.get("round_trip_cost_pct_500"),
              "round_trip_pct_5000": m.get("round_trip_cost_pct_5000")}
        for sym, m in xyz["assets"].items() if m.get("in_lighter_universe")
    }
    print(f"[stocks_metrics] Lighter полная история (n_candles>={FULL_HISTORY_MIN_CANDLES}): "
          f"{len(lighter_full)} тикеров -- {sorted(lighter_full)}")
    print(f"[stocks_metrics] xyz пересечение с Lighter: {len(xyz_overlap)} тикеров -- {sorted(xyz_overlap)}")
    return {"lighter": lighter_full, "xyz": xyz_overlap}


def compute_rows_for_venue(venue: str, universe: dict, fridays: list[str],
                            y_cache: dict[str, pd.DataFrame | None]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    drop_reasons: dict[str, int] = {}

    def bump(reason: str) -> None:
        drop_reasons[reason] = drop_reasons.get(reason, 0) + 1

    for sym, meta in universe.items():
        csv_path = meta["csv"]
        if not csv_path.exists():
            bump(f"{sym}: нет CSV свечей")
            continue
        candles = load_hourly_csv(csv_path)

        if sym not in y_cache:
            y_start = fridays[0]
            y_end = (datetime.strptime(fridays[-1], "%Y-%m-%d") + pd.Timedelta(days=4)).strftime("%Y-%m-%d")
            y_cache[sym] = yfinance_daily(sym, y_start, y_end)
            time.sleep(0.3)
        daily = y_cache[sym]

        for friday in fridays:
            w = stock_class_windows(friday)
            x_start_p = price_asof(candles, w["x_start"])
            x_end_p = price_asof(candles, w["x_end"])
            z1_start_p = price_asof(candles, w["z1_start"])
            z1_end_p = price_asof(candles, w["z1_end"])
            z2_start_p = price_asof(candles, w["z2_start"])
            z2_end_p = price_asof(candles, w["z2_end"])
            if None in (x_start_p, x_end_p, z1_start_p, z1_end_p, z2_start_p, z2_end_p):
                bump(f"{sym}: нет реальных свечей в пределах допуска для {friday} (данных ещё/уже нет)")
                continue
            if daily is None:
                bump(f"{sym}: yfinance не вернул дневные данные вообще")
                continue
            y = friday_monday_gap(daily, friday)
            if y.get("gap") is None:
                bump(f"{sym}: Y недоступен для {friday} -- {y.get('reason')}")
                continue

            rows.append({
                "venue": venue, "symbol": sym, "friday": friday,
                "X": x_end_p / x_start_p - 1, "Z1": z1_end_p / z1_start_p - 1,
                "Z2": z2_end_p / z2_start_p - 1, "Y": y["gap"],
                "round_trip_pct_500": meta.get("round_trip_pct_500"),
                "round_trip_pct_5000": meta.get("round_trip_pct_5000"),
            })
    return rows, drop_reasons


def compute_metrics(df: pd.DataFrame, label: str) -> dict:
    result: dict = {"n": len(df)}
    if len(df) < 3:
        result["note"] = f"N={len(df)} слишком мал для корреляций -- честно доложить, не считать"
        return result

    result["corr_X_Y"] = float(df["X"].corr(df["Y"]))
    result["corr_X_Z1"] = float(df["X"].corr(df["Z1"]))
    result["corr_X_Z2"] = float(df["X"].corr(df["Z2"]))
    result["sign_opposite_fraction_X_Y"] = sign_opposite_fraction(df["X"], df["Y"])
    result["sign_opposite_fraction_X_Z1"] = sign_opposite_fraction(df["X"], df["Z1"])
    result["sign_opposite_fraction_X_Z2"] = sign_opposite_fraction(df["X"], df["Z2"])

    result["weekend_breakdown_X_Z1"] = weekend_breakdown(df, "X", "Z1")
    result["weekend_breakdown_X_Y"] = weekend_breakdown(df, "X", "Y")

    clusters = df["symbol"].to_numpy()
    result["cluster_permutation_test"] = {
        "X_Y": cluster_sign_flip_test(df["X"].to_numpy(), df["Y"].to_numpy(), clusters),
        "X_Z1": cluster_sign_flip_test(df["X"].to_numpy(), df["Z1"].to_numpy(), clusters),
        "X_Z2": cluster_sign_flip_test(df["X"].to_numpy(), df["Z2"].to_numpy(), clusters),
    }

    rt500 = df.groupby("symbol")["round_trip_pct_500"].first()
    rt5000 = df.groupby("symbol")["round_trip_pct_5000"].first()
    per_ticker_rt: dict[str, dict] = {}
    for sym, g in df.groupby("symbol"):
        per_ticker_rt[sym] = round_trip_comparison(g["Z1"].abs(), rt500.get(sym), rt5000.get(sym))
    result["round_trip_comparison_by_ticker"] = per_ticker_rt
    valid_500 = [v["frac_abs_z_gt_roundtrip_500"] for v in per_ticker_rt.values() if v["frac_abs_z_gt_roundtrip_500"] is not None]
    valid_5000 = [v["frac_abs_z_gt_roundtrip_5000"] for v in per_ticker_rt.values() if v["frac_abs_z_gt_roundtrip_5000"] is not None]
    result["mean_frac_abs_z1_gt_roundtrip_500"] = float(np.mean(valid_500)) if valid_500 else None
    result["mean_frac_abs_z1_gt_roundtrip_5000"] = float(np.mean(valid_5000)) if valid_5000 else None

    corr_xy = result["corr_X_Y"]
    n = result["n"]
    frac5000 = result["mean_frac_abs_z1_gt_roundtrip_5000"]
    alive = (abs(corr_xy) < 0.3 and result["corr_X_Z1"] < -0.3 and n >= 30
             and frac5000 is not None and frac5000 > 0.5)
    result["preregistered_verdict"] = (
        f"{'ЖИВА' if alive else 'НЕ подтверждено по предрегистрации'} ({label}): "
        f"|corr(X,Y)|={abs(corr_xy):.3f}(<0.3?), corr(X,Z1)={result['corr_X_Z1']:.3f}(<-0.3?), "
        f"N={n}(>=30?), frac|Z1|>rt5000={frac5000}(>0.5?)"
    )
    return result


def run() -> int:
    universe = build_universe()

    now_utc = datetime.now(timezone.utc)
    all_fridays = real_fridays_since("2026-07-01", now_utc)
    print(f"[stocks_metrics] реальных завершённых выходных с 2026-07-01: {len(all_fridays)} -- {all_fridays}")

    y_cache: dict[str, pd.DataFrame | None] = {}
    all_rows: list[dict] = []
    all_drops: dict[str, dict] = {}
    for venue, univ in universe.items():
        rows, drops = compute_rows_for_venue(venue, univ, all_fridays, y_cache)
        print(f"[stocks_metrics] {venue}: {len(rows)} реальных строк (тикер x выходные) с полными X/Z1/Z2/Y")
        all_rows.extend(rows)
        all_drops[venue] = drops

    df = pd.DataFrame(all_rows)
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "weekends_considered": all_fridays, "full_history_min_candles": FULL_HISTORY_MIN_CANDLES,
        "drop_reasons_by_venue": all_drops,
    }
    for venue in universe:
        sub = df[df["venue"] == venue] if len(df) else pd.DataFrame()
        print(f"\n=== {venue.upper()} (N={len(sub)}) ===")
        m = compute_metrics(sub, venue)
        for k in ("corr_X_Y", "corr_X_Z1", "corr_X_Z2", "sign_opposite_fraction_X_Y",
                  "sign_opposite_fraction_X_Z1", "sign_opposite_fraction_X_Z2",
                  "mean_frac_abs_z1_gt_roundtrip_500", "mean_frac_abs_z1_gt_roundtrip_5000", "preregistered_verdict"):
            if k in m:
                print(f"  {k}: {m[k]}")
        result[venue] = m
        result[f"{venue}_rows"] = sub.to_dict("records")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[stocks_metrics] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
