#!/usr/bin/env python3
"""Задача 1, проверка 2 (владелец, 2026-09-05, дословно): "Тайминг. Z
разбить: Z1 = вс 20:00 -> 21:00 ET, Z2 = 21:00 -> пн 9:30. corr(X, Z1),
corr(X, Z2), доля обратных знаков для каждого. Где живёт возврат."

Реюз: X -- уже реально посчитан и сохранён в честном полном прогоне
(`data/p3_guard_cache/task1_weekend_gap_result.json`, 9 выходных,
N=197) -- НЕ пересчитывается заново, только джойнится по (symbol,
friday_utc). Z1/Z2 -- НОВЫЕ узкие брекеты внутри уже известного Z-окна
(`sql/task1/task1_z_decompose.sql`), тот же универсум токенов/выходных,
тот же фильтр тонких пулов (мин. 3 сделки в каждом брекете) -- НЕ новый
метод, применение уже согласованной методологии к более тонкому
временному срезу."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task1_weekend_gap")
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402
from task1_weekend_gap import real_fridays_since  # noqa: E402

BUDGET = 250.0
REGISTRY_PATH = Path("data/rwa_stock_token_registry.json")
MAIN_RESULT_PATH = Path("data/p3_guard_cache/task1_weekend_gap_result.json")
OUT_PATH = Path("data/p3_guard_cache/task1_z_decompose_result.json")
MIN_BRACKET_TRADES = 3


def run() -> int:
    credit_guard.ensure_namespace("task1_weekend_gap", BUDGET)

    if not MAIN_RESULT_PATH.exists():
        raise SystemExit("[z_decompose] нет task1_weekend_gap_result.json -- сначала полный прогон")
    main_result = json.loads(MAIN_RESULT_PATH.read_text())
    x_df = pd.DataFrame(main_result["rows"])[["symbol", "friday_utc", "X"]]
    print(f"[z_decompose] реальный X из основного прогона: {len(x_df)} строк (symbol x выходные)")

    registry = json.loads(REGISTRY_PATH.read_text())["tokens"]
    token_addrs = [t["stock_token_address"] for t in registry.values()]
    addr_to_symbol = {t["stock_token_address"].lower(): sym for sym, t in registry.items()}
    token_addrs_hex_list = ",".join(f"from_hex('{a[2:].lower()}')" for a in token_addrs)

    now_utc = datetime.now(timezone.utc)
    fridays = real_fridays_since("2026-07-01", now_utc)
    print(f"[z_decompose] те же {len(fridays)} выходных, что основной прогон: {fridays}")

    sql_template = read_sql("task1/task1_z_decompose")
    friday_list_sql = ",".join(f"timestamp '{f} 00:00:00'" for f in fridays)
    trades_start = fridays[0] + " 00:00:00"
    trades_end_dt = datetime.strptime(fridays[-1], "%Y-%m-%d") + timedelta(days=3, hours=14)
    trades_end = trades_end_dt.strftime("%Y-%m-%d %H:%M:%S")
    sql = (sql_template
           .replace("{{weekend_friday_list}}", friday_list_sql)
           .replace("{{token_address_list}}", token_addrs_hex_list)
           .replace("{{trades_start}}", trades_start)
           .replace("{{trades_end}}", trades_end))

    client = DuneClient()
    qid = client.create_query("task1_z_decompose", sql)
    df = client.run_sql_cached(
        "task1_z_decompose", sql, query_id=qid,
        estimated_credits=15.0,  # тот же порядок, что основной полный прогон (та же дата-партиция, узкие брекеты внутри неё)
        expected_max_rows=len(fridays) * len(token_addrs) + 100, expected_columns=11,
    )
    if df is None or not len(df):
        raise SystemExit("[z_decompose] Dune вернул пусто")
    print(f"[z_decompose] Dune: {len(df)} строк")

    df["symbol"] = df["token_address"].str.lower().map(addr_to_symbol)
    z2_start_vol, z2_start_qty, z2_start_n = df["z1_end_vol"], df["z1_end_qty"], df["z1_end_n"]  # переиспользуем границу

    ok = (
        (df["z1_start_n"].fillna(0) >= MIN_BRACKET_TRADES) & (df["z1_end_n"].fillna(0) >= MIN_BRACKET_TRADES)
        & (z2_start_n.fillna(0) >= MIN_BRACKET_TRADES) & (df["z2_end_n"].fillna(0) >= MIN_BRACKET_TRADES)
    )
    n_before = len(df)
    df = df[ok].copy()
    print(f"[z_decompose] фильтр тонких пулов (мин {MIN_BRACKET_TRADES} сделок в каждом из брекетов Z1/Z2): "
          f"осталось {len(df)} из {n_before}")

    df["z1_start_vwap"] = df["z1_start_vol"] / df["z1_start_qty"]
    df["z1_end_vwap"] = df["z1_end_vol"] / df["z1_end_qty"]
    df["z2_end_vwap"] = df["z2_end_vol"] / df["z2_end_qty"]
    df["Z1"] = df["z1_end_vwap"] / df["z1_start_vwap"] - 1
    df["Z2"] = df["z2_end_vwap"] / df["z1_end_vwap"] - 1  # z1_end_vwap == z2_start_vwap, та же граница

    merged = df.merge(x_df, on=["symbol", "friday_utc"], how="inner").dropna(subset=["X", "Z1", "Z2"])
    print(f"[z_decompose] финальная выборка после джойна с реальным X: N={len(merged)}")

    result = {
        "n_before_thin_filter": int(n_before), "n_after_thin_filter": int(len(df)),
        "n_final_joined_with_X": int(len(merged)), "min_bracket_trades_threshold": MIN_BRACKET_TRADES,
    }
    if len(merged) >= 3:
        corr_x_z1 = merged["X"].corr(merged["Z1"])
        corr_x_z2 = merged["X"].corr(merged["Z2"])
        opp_z1 = float((np.sign(merged["X"]) != np.sign(merged["Z1"])).mean())
        opp_z2 = float((np.sign(merged["X"]) != np.sign(merged["Z2"])).mean())
        result.update({
            "corr_X_Z1": corr_x_z1, "corr_X_Z2": corr_x_z2,
            "sign_opposite_fraction_X_Z1": opp_z1, "sign_opposite_fraction_X_Z2": opp_z2,
        })
        where = ("Z1 (вс 20:00-21:00 ET, разворот РАННИЙ)" if abs(corr_x_z1) > abs(corr_x_z2)
                 else "Z2 (21:00 ET-пн 9:30, разворот ПОЗДНИЙ/растянутый)")
        result["interpretation"] = f"сильнее по |corr(X,·)| и доле противоположных знаков: {where}"
        print(f"[z_decompose] corr(X,Z1)={corr_x_z1:.4f} (доля обратных={opp_z1:.1%})  "
              f"corr(X,Z2)={corr_x_z2:.4f} (доля обратных={opp_z2:.1%})")
        print(f"[z_decompose] {result['interpretation']}")
    else:
        result["interpretation"] = f"N={len(merged)} слишком мало для корреляции -- честно доложить"
        print(f"[z_decompose] {result['interpretation']}")

    result["rows"] = merged[["symbol", "friday_utc", "X", "Z1", "Z2"]].to_dict("records")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[z_decompose] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
