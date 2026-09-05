#!/usr/bin/env python3
"""Задача 1, проверка 1 -- финал (владелец, 2026-09-05, дословно):
"v3-подмножество, текущий TVL как приближение с пометкой, round-trip
на $500 = fee x 2 + проскальзывание по резервам. Сравнить с медианным
|Z| из проверки 3 по тем же тикерам. Доля наблюдений, где |Z| >
round-trip. Для v4 -- 'не оценено', не гадать."

Реюз: `task1_pool_liquidity.py` (адрес/fee/TVL/round-trip), реальный
кэш `task1_pool_addresses_by_token` (`task1_liquidity_probe2.py`),
реальные X/Z из `task1_weekend_gap_result.json` (честный полный
прогон, N=197). Только читает уже существующие файлы + живые
fee()/GT-запросы по РЕАЛЬНЫМ адресам пулов -- НЕ Dune (0 новых
Dune-кредитов)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from task1_pool_liquidity import evaluate_symbol_liquidity  # noqa: E402

MAIN_RESULT_PATH = Path("data/p3_guard_cache/task1_weekend_gap_result.json")
POOL_ADDR_CACHE_DIR = Path("data/task1_weekend_gap_cache")
OUT_PATH = Path("data/p3_guard_cache/task1_liquidity_v3_roundtrip_result.json")


def find_pool_addr_cache() -> Path:
    files = sorted(POOL_ADDR_CACHE_DIR.glob("task1_pool_addresses_by_token_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit("нет реального кэша task1_pool_addresses_by_token_*.csv -- сначала task1_liquidity_probe2.py")
    return files[0]


def run() -> int:
    main_result = json.loads(MAIN_RESULT_PATH.read_text())
    rows_df = pd.DataFrame(main_result["rows"])
    symbols = sorted(rows_df["symbol"].dropna().unique())
    print(f"[v3_roundtrip] реальных уникальных тикеров в основном прогоне: {len(symbols)}")

    pool_addr_path = find_pool_addr_cache()
    print(f"[v3_roundtrip] реальный кэш адресов пулов: {pool_addr_path}")
    pool_df = pd.read_csv(pool_addr_path)

    evaluations = []
    for sym in symbols:
        print(f"  оцениваю {sym}...")
        ev = evaluate_symbol_liquidity(sym, pool_df)
        sym_rows = rows_df[rows_df["symbol"] == sym]
        ev["median_abs_Z_pct"] = float(sym_rows["Z"].abs().median() * 100)
        ev["n_rows_this_symbol"] = int(len(sym_rows))
        if ev.get("status") == "оценено":
            ev["median_Z_gt_round_trip"] = bool(ev["median_abs_Z_pct"] > ev["round_trip_cost_pct"])
            print(f"    {sym}: TVL=${ev['tvl_usd_now']:,.0f} fee={ev['fee_bps']/100:.2f}bp round_trip={ev['round_trip_cost_pct']:.3f}% "
                  f"median|Z|={ev['median_abs_Z_pct']:.3f}% {'ТОРГУЕМО (медиана)' if ev['median_Z_gt_round_trip'] else 'НЕ покрывает издержки (медиана)'}")
        else:
            print(f"    {sym}: {ev['status']}")
        evaluations.append(ev)

    evaluated = [e for e in evaluations if e.get("status") == "оценено"]
    not_evaluated = [e for e in evaluations if e.get("status") != "оценено"]

    # Доля НАБЛЮДЕНИЙ (строк symbol x выходные из основного прогона),
    # где |Z| > round-trip издержка ТОГО ЖЕ тикера (текущая
    # TVL/fee -- честно применяется задним числом ко всем историческим
    # строкам этого тикера, не пересчитывается по историческим датам).
    row_level = []
    for ev in evaluated:
        sym_rows = rows_df[rows_df["symbol"] == ev["symbol"]]
        for _, r in sym_rows.iterrows():
            row_level.append({"symbol": ev["symbol"], "friday_utc": r["friday_utc"],
                               "abs_Z_pct": abs(r["Z"]) * 100, "round_trip_cost_pct": ev["round_trip_cost_pct"],
                               "z_gt_round_trip": abs(r["Z"]) * 100 > ev["round_trip_cost_pct"]})
    row_level_df = pd.DataFrame(row_level)
    frac_rows_z_gt_cost = float(row_level_df["z_gt_round_trip"].mean()) if len(row_level_df) else None

    n_v3_tvl_ok = sum(1 for e in evaluated if e.get("tvl_ok_gt_200k"))
    result = {
        "n_symbols_total": len(symbols), "n_symbols_evaluated_v3": len(evaluated),
        "n_symbols_not_evaluated": len(not_evaluated), "n_symbols_v3_tvl_gt_200k": n_v3_tvl_ok,
        "n_rows_scored": len(row_level_df), "n_rows_total_main": len(rows_df),
        "fraction_rows_abs_Z_gt_round_trip_cost": frac_rows_z_gt_cost,
        "fraction_rows_abs_Z_gt_round_trip_cost_TVL_ok_only": (
            float(row_level_df[row_level_df["symbol"].isin([e["symbol"] for e in evaluated if e.get("tvl_ok_gt_200k")])]["z_gt_round_trip"].mean())
            if len(row_level_df) else None
        ),
        "evaluations": evaluations,
    }
    print(f"\n[v3_roundtrip] v3-подмножество: {len(evaluated)}/{len(symbols)} тикеров оценено, "
          f"{n_v3_tvl_ok} из них TVL>$200k")
    print(f"[v3_roundtrip] доля наблюдений |Z|>round-trip (все оценённые): {frac_rows_z_gt_cost}")
    print(f"[v3_roundtrip] доля наблюдений |Z|>round-trip (только TVL>$200k): {result['fraction_rows_abs_Z_gt_round_trip_cost_TVL_ok_only']}")

    # Робастность (тот же урок, что уже дважды подтверждён в проекте --
    # исключить |X|>0.5, артефакты тонкой ликвидности искажают headline).
    rows_robust = rows_df[rows_df["X"].abs() <= 0.5]
    row_level_robust = []
    for ev in evaluated:
        sym_rows = rows_robust[rows_robust["symbol"] == ev["symbol"]]
        for _, r in sym_rows.iterrows():
            row_level_robust.append({"symbol": ev["symbol"], "z_gt_round_trip": bool(abs(r["Z"]) * 100 > ev["round_trip_cost_pct"]),
                                      "tvl_ok": bool(ev.get("tvl_ok_gt_200k"))})
    rl_df = pd.DataFrame(row_level_robust)
    low_n = [e["symbol"] for e in evaluated if e.get("n_rows_this_symbol", 0) <= 1]
    result["robustness_check"] = {
        "note": "Исключены строки |X|>0.5 (та же граница, что в проверках 2/3) -- проверка чувствительности headline к выбросам тонкой ликвидности.",
        "n_rows_robust": int(len(rl_df)),
        "fraction_rows_abs_Z_gt_round_trip_cost_robust": float(rl_df["z_gt_round_trip"].mean()) if len(rl_df) else None,
        "fraction_rows_abs_Z_gt_round_trip_cost_robust_TVL_ok_only": (
            float(rl_df[rl_df["tvl_ok"]]["z_gt_round_trip"].mean()) if len(rl_df) and rl_df["tvl_ok"].any() else None
        ),
        "low_n_symbols_warning": low_n if low_n else None,
    }
    print(f"[v3_roundtrip] РОБАСТНОСТЬ (|X|<=0.5): доля={result['robustness_check']['fraction_rows_abs_Z_gt_round_trip_cost_robust']} "
          f"(TVL-ok: {result['robustness_check']['fraction_rows_abs_Z_gt_round_trip_cost_robust_TVL_ok_only']})")
    if low_n:
        print(f"[v3_roundtrip] ВНИМАНИЕ: низкое N (<=1 наблюдение) у {low_n} -- медиана по ним не устойчива")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[v3_roundtrip] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
