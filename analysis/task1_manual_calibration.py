#!/usr/bin/env python3
"""Ручная калибровка Задачи 1 на одних выходных (владелец, 2026-09-05:
"калибровка на одних выходных вручную перед выводом: X и Z из сырых
свопов, Y со Stooq [заменено на yfinance после бага #24], сверить с
тем, что выдал пайплайн" -- условие остаётся в силе после смены
источника Y). Не пересчитывает пайплайн заново -- берёт уже реальный,
сохранённый сырой Dune-кэш (`data/task1_weekend_gap_cache/task1_weekend_
windows_*.csv`, тот же файл, что использовал сам пайплайн) и уже
сохранённый результат пайплайна (`task1_weekend_gap_result.json`),
пересчитывает X/Z вручную по формуле из `task1_weekend_gap.py::run()`
и Y вручную через `yfinance_daily`+`friday_monday_gap` для НЕСКОЛЬКИХ
контрольных тикеров, сверяет с тем, что реально выдал пайплайн."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from task1_weekend_gap import yfinance_daily, friday_monday_gap  # noqa: E402

REGISTRY_PATH = Path("data/rwa_stock_token_registry.json")
RESULT_PATH = Path("data/p3_guard_cache/task1_weekend_gap_result.json")
CACHE_DIR = Path("data/task1_weekend_gap_cache")
CHECK_SYMBOLS = ["AAPL", "TSLA", "SPY", "GME", "NVDA"]
TOL = 1e-6


def find_windows_cache() -> Path:
    files = sorted(CACHE_DIR.glob("task1_weekend_windows_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit("нет реального сырого кэша task1_weekend_windows_*.csv -- сначала прогнать смоук пайплайна")
    return files[0]


def run() -> int:
    registry = json.loads(REGISTRY_PATH.read_text())["tokens"]
    result = json.loads(RESULT_PATH.read_text())
    pipeline_rows = {r["symbol"]: r for r in result["rows"]}
    friday = result["rows"][0]["friday_utc"][:10]

    cache_path = find_windows_cache()
    print(f"[calib] реальный сырой Dune-кэш: {cache_path}")
    raw = pd.read_csv(cache_path)

    all_ok = True
    for sym in CHECK_SYMBOLS:
        if sym not in pipeline_rows:
            print(f"  {sym}: НЕТ в результате пайплайна (не прошёл фильтр тонких пулов или не в реестре) -- пропуск")
            continue
        addr = registry[sym]["stock_token_address"].lower()
        rr = raw[raw["token_address"].str.lower() == addr]
        if not len(rr):
            print(f"  {sym}: НЕ найден в сыром кэше по адресу {addr} -- ОШИБКА")
            all_ok = False
            continue
        rr = rr.iloc[0]
        x_start_vwap = rr["x_start_vol"] / rr["x_start_qty"]
        x_end_vwap = rr["x_end_vol"] / rr["x_end_qty"]
        z_start_vwap = rr["z_start_vol"] / rr["z_start_qty"]
        z_end_vwap = rr["z_end_vol"] / rr["z_end_qty"]
        x_manual = x_end_vwap / x_start_vwap - 1
        z_manual = z_end_vwap / z_start_vwap - 1

        daily = yfinance_daily(sym, friday, (pd.Timestamp(friday) + pd.Timedelta(days=4)).strftime("%Y-%m-%d"))
        y_manual = None
        if daily is not None:
            g = friday_monday_gap(daily, friday)
            y_manual = g.get("gap")

        p = pipeline_rows[sym]
        x_ok = abs(x_manual - p["X"]) < TOL
        z_ok = abs(z_manual - p["Z"]) < TOL
        y_ok = (y_manual is not None and p.get("Y") is not None and abs(y_manual - p["Y"]) < TOL)
        all_ok = all_ok and x_ok and z_ok and y_ok
        print(f"  {sym}: X ручной={x_manual:.8f} пайплайн={p['X']:.8f} {'OK' if x_ok else 'РАСХОЖДЕНИЕ'} | "
              f"Z ручной={z_manual:.8f} пайплайн={p['Z']:.8f} {'OK' if z_ok else 'РАСХОЖДЕНИЕ'} | "
              f"Y ручной={y_manual} пайплайн={p.get('Y')} {'OK' if y_ok else 'РАСХОЖДЕНИЕ'}")

    print(f"\n[calib] ИТОГ: {'ВСЁ СОШЛОСЬ -- пайплайну можно доверять' if all_ok else 'ЕСТЬ РАСХОЖДЕНИЯ -- разбирать до полного прогона'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
