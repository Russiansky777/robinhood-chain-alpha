#!/usr/bin/env python3
"""«Спящие референсы» → расхождение площадок, п.5 владельца (2026-09-06,
дословно): "Фандинг. Позиция держится часами-днями, значит фандинг на
обеих ногах входит в P&L. У нас есть история обеих площадок -- учесть."

Реальные исторические часовые ставки фандинга для 23 общих тикеров, обе
площадки, с 05.07 -- ТЕ ЖЕ реальные, уже подтверждённые методы/эндпоинты,
что `funding_historical_backfill.py` (реюз функций напрямую, не
копипаста): Lighter `/api/v1/fundings` (market_id, backward, count_back
750), HL `POST /info {"type":"fundingHistory","coin",...}` (forward,
лимит страницы 500).

НЕ проверено ранее: работает ли `fundingHistory` для HIP-3-актива с
префиксом dex (`coin="xyz:TICKER"`) -- реальная проверка ниже, по
факту первого же реального вызова, не гадаем заранее. Ноль кредитов
Dune."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

from funding_historical_backfill import fetch_lighter_history, fetch_hyperliquid_history  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_funding_history_result.json")
RAW_CACHE_DIR = Path("data/sleeping_refs_cache")
LIGHTER_RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")
XYZ_RESULT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_xyz_markprice_history_result.json")
BACKFILL_START_UTC = "2026-07-05T00:00:00Z"


def run() -> int:
    lighter = json.loads(LIGHTER_RESULT_PATH.read_text())["markets"]
    xyz = json.loads(XYZ_RESULT_PATH.read_text())["assets"]
    overlap = sorted(t for t, m in xyz.items() if m.get("in_lighter_universe") and t in lighter)
    print(f"[funding_history] реальный универсум: {len(overlap)} -- {overlap}")

    since_unix = int(pd.Timestamp(BACKFILL_START_UTC).timestamp())
    since_ms = since_unix * 1000

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "backfill_start_utc": BACKFILL_START_UTC, "tickers": {}}
    all_rows: list[pd.DataFrame] = []
    hl_coin_format_confirmed: bool | None = None

    for i, ticker in enumerate(overlap):
        print(f"[{i+1}/{len(overlap)}] {ticker}...")
        l_records = fetch_lighter_history(lighter[ticker]["market_id"], since_unix)
        print(f"    Lighter: {len(l_records)} реальных часовых записей")

        hl_coin = f"xyz:{ticker}"
        h_records = fetch_hyperliquid_history(hl_coin, since_ms)
        print(f"    HL ({hl_coin}): {len(h_records)} реальных часовых записей")
        if hl_coin_format_confirmed is None:
            hl_coin_format_confirmed = len(h_records) > 0
            print(f"    !!! реальная проверка формата coin=\"xyz:TICKER\" для fundingHistory: "
                  f"{'РАБОТАЕТ' if hl_coin_format_confirmed else 'НЕ РАБОТАЕТ (0 записей на первом тикере)'}")

        l_df = pd.DataFrame(l_records)
        if len(l_df):
            l_df["hour"] = pd.to_datetime(l_df["timestamp"], unit="s", utc=True).dt.floor("h")
            l_df["lighter_rate_pct"] = l_df["rate"].astype(float)  # уже % за час, реальный факт (см. funding_historical_backfill.py)
            l_df = l_df[["hour", "lighter_rate_pct"]].groupby("hour", as_index=False).last()
        else:
            # Реальный баг (обнаружен по факту, SNDK: HL 0 записей): pd.DataFrame([])
            # не имеет колонки "hour" вообще -- .merge(on="hour") падает KeyError.
            # Пустой DataFrame С колонкой "hour" -- honest empty, не падаем.
            l_df = pd.DataFrame(columns=["hour", "lighter_rate_pct"])

        h_df = pd.DataFrame(h_records)
        if len(h_df):
            h_df["hour"] = pd.to_datetime(h_df["time"], unit="ms", utc=True).dt.floor("h")
            h_df["xyz_rate_pct"] = h_df["fundingRate"].astype(float) * 100  # доля -> %
            h_df = h_df.groupby("hour", as_index=False)["xyz_rate_pct"].last()
        else:
            h_df = pd.DataFrame(columns=["hour", "xyz_rate_pct"])

        n_l, n_h = len(l_df), len(h_df)
        merged = l_df.merge(h_df, on="hour", how="outer").sort_values("hour") if (n_l or n_h) else pd.DataFrame()
        if len(merged):
            merged["symbol"] = ticker
            all_rows.append(merged)

        out["tickers"][ticker] = {"n_lighter_records": n_l, "n_hl_records": n_h,
                                   "n_merged_hours": int(len(merged)) if len(merged) else 0}
        time.sleep(0.2)

    full = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    out["hl_coin_format_xyz_prefix_confirmed"] = hl_coin_format_confirmed
    out["n_total_merged_hours"] = int(len(full))
    print(f"\n[funding_history] реальных объединённых часовых строк (все тикеры): {len(full)}")

    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if len(full):
        full.to_csv(RAW_CACHE_DIR / "sleeping_refs_funding_history_full.csv", index=False)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[funding_history] результат записан в {OUT_PATH}, полный ряд -- в "
          f"{RAW_CACHE_DIR / 'sleeping_refs_funding_history_full.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
