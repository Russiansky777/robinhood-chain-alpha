#!/usr/bin/env python3
"""Проверка 2 к переключению Y (Задача 1) на Chainlink-оракул (владелец,
2026-09-05): "Сверка Y на трёх выходных с независимым биржевым
источником: yfinance с раннера, или Stooq с NL-хоста через существующий
workflow (другой IP). Расхождение > 0.2% -- разбирать до замены."

Пробуем yfinance с раннера первым (проще -- тот же исполнитель, что всё
остальное, без SSH-цикла на VPS) -- реальный результат, не
предполагаем заранее, что сработает (yfinance тоже может блокироваться
для дата-центровых IP, тот же класс риска, что Stooq, реально проверяем).

Chainlink "closing"/"opening" референс -- РЕАЛЬНОЕ последнее обновление
<= пт 20:00 UTC (16:00 ET, закрытие) и РЕАЛЬНОЕ первое обновление >= пн
13:30 UTC (9:30 ET, открытие), decimals=8 (подтверждено картой Sprint
R1, data/sprintR1_cache/r1_feed_token_map.csv)."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "task1_weekend_gap")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

import credit_guard  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from run_pipeline import read_sql  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task1_y_source_crosscheck_result.json")
DECIMALS = 8

FEEDS = {
    "TSLA": "0x7a6b81ba7fbcb90104d8c496158cf383cd7233b1",
    "AMZN": "0x93503dfc97157cdb8aadccaf70452621d598fdeb",
    "GOOGL": "0x11ed6d598ef565dda86fafe7e779303e7cc6b2bd",
    "SPY": "0x78bcb218fa04b9b3a278ebc865ed320bf8defbac",
    "QQQ": "0x25e996ce8b3529885d429241156e83e7b7744049",
}
DISCREPANCY_THRESHOLD = 0.002  # 0.2%


def real_last_n_complete_fridays(n: int, now_utc: datetime) -> list[str]:
    d = now_utc
    while d.weekday() != 4:
        d -= timedelta(days=1)
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    fridays = []
    while len(fridays) < n:
        sunday_2000_et_utc = d + timedelta(days=3)
        if sunday_2000_et_utc <= now_utc:
            fridays.append(d)
        d -= timedelta(days=7)
    return sorted(fridays)


def fetch_chainlink_prices(fridays: list[datetime]) -> pd.DataFrame:
    credit_guard.ensure_namespace("task1_weekend_gap", 250.0)
    window_start = (fridays[0] + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    window_end = (fridays[-1] + timedelta(days=3, hours=16)).strftime("%Y-%m-%d %H:%M:%S")
    feed_list_sql = ",".join(f"from_hex('{a[2:].lower()}')" for a in FEEDS.values())
    sql = (read_sql("task1/task1_chainlink_weekend_prices")
           .replace("{{feed_address_list}}", feed_list_sql)
           .replace("{{window_start}}", window_start)
           .replace("{{window_end}}", window_end))
    client = DuneClient()
    qid = client.create_query("task1_chainlink_weekend_prices", sql)
    df = client.run_sql_cached("task1_chainlink_weekend_prices", sql, query_id=qid,
                                 estimated_credits=3.0, expected_max_rows=2000, expected_columns=3)
    return df if df is not None else pd.DataFrame()


def closest_before(df: pd.DataFrame, feed: str, t: datetime) -> float | None:
    sub = df[(df["feed_address"].str.lower() == feed.lower()) & (df["evt_block_time_ts"] <= t)]
    if not len(sub):
        return None
    row = sub.sort_values("evt_block_time_ts").iloc[-1]
    return float(row["price_raw"]) / (10 ** DECIMALS)


def closest_after(df: pd.DataFrame, feed: str, t: datetime) -> float | None:
    sub = df[(df["feed_address"].str.lower() == feed.lower()) & (df["evt_block_time_ts"] >= t)]
    if not len(sub):
        return None
    row = sub.sort_values("evt_block_time_ts").iloc[0]
    return float(row["price_raw"]) / (10 ** DECIMALS)


def fetch_yfinance_ohlc(symbol: str, friday_date: str, monday_date: str) -> dict:
    try:
        import yfinance as yf
    except ImportError as e:
        return {"error": f"yfinance не установлен: {e}"}
    try:
        hist = yf.Ticker(symbol).history(start=friday_date, end=(pd.Timestamp(monday_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), interval="1d")
    except Exception as e:  # noqa: BLE001
        return {"error": f"yfinance history() упал: {e}"}
    if hist is None or not len(hist):
        return {"error": "yfinance вернул пустую историю"}
    hist = hist.reset_index()
    hist["date_str"] = hist["Date"].dt.strftime("%Y-%m-%d")
    fri_row = hist[hist["date_str"] == friday_date]
    mon_row = hist[hist["date_str"] == monday_date]
    if not len(fri_row) or not len(mon_row):
        return {"error": f"нет реальных строк на {friday_date}/{monday_date} в ответе yfinance (праздник/выходной?)",
                "real_dates_returned": hist["date_str"].tolist()}
    return {"friday_close": float(fri_row.iloc[0]["Close"]), "monday_open": float(mon_row.iloc[0]["Open"])}


def run() -> int:
    now_utc = datetime.now(timezone.utc)
    fridays = real_last_n_complete_fridays(3, now_utc)
    print(f"[y_crosscheck] 3 последних завершённых выходных: {[f.strftime('%Y-%m-%d') for f in fridays]}")

    raw = fetch_chainlink_prices(fridays)
    if not len(raw):
        print("[y_crosscheck] Dune вернул пусто -- проверить окно/фиды")
        return 1
    raw["evt_block_time_ts"] = pd.to_datetime(raw["evt_block_time"]).dt.tz_localize("UTC") if pd.to_datetime(raw["evt_block_time"]).dt.tz is None else pd.to_datetime(raw["evt_block_time"])
    print(f"[y_crosscheck] реальных обновлений в объединённом окне: {len(raw)}")

    rows = []
    for friday in fridays:
        friday_close_ref = friday + timedelta(hours=20)  # пт 20:00 UTC = 16:00 ET
        monday_open_ref = friday + timedelta(days=3, hours=13, minutes=30)  # пн 13:30 UTC = 9:30 ET
        friday_str = friday.strftime("%Y-%m-%d")
        monday_str = (friday + timedelta(days=3)).strftime("%Y-%m-%d")
        for symbol, feed in FEEDS.items():
            cl_close = closest_before(raw, feed, friday_close_ref)
            cl_open = closest_after(raw, feed, monday_open_ref)
            yf_data = fetch_yfinance_ohlc(symbol, friday_str, monday_str)
            row = {"symbol": symbol, "friday": friday_str, "monday": monday_str,
                   "chainlink_close": cl_close, "chainlink_open": cl_open, "yfinance": yf_data}
            if cl_close is not None and "friday_close" in yf_data and yf_data["friday_close"]:
                row["discrepancy_close_pct"] = abs(cl_close - yf_data["friday_close"]) / yf_data["friday_close"]
            if cl_open is not None and "monday_open" in yf_data and yf_data["monday_open"]:
                row["discrepancy_open_pct"] = abs(cl_open - yf_data["monday_open"]) / yf_data["monday_open"]
            rows.append(row)
            print(f"  {symbol} {friday_str}: chainlink_close={cl_close} chainlink_open={cl_open} yfinance={yf_data} "
                  f"disc_close={row.get('discrepancy_close_pct')} disc_open={row.get('discrepancy_open_pct')}")

    n_over_threshold = sum(
        1 for r in rows
        if (r.get("discrepancy_close_pct") or 0) > DISCREPANCY_THRESHOLD or (r.get("discrepancy_open_pct") or 0) > DISCREPANCY_THRESHOLD
    )
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "discrepancy_threshold_pct": DISCREPANCY_THRESHOLD, "rows": rows,
              "n_over_threshold": n_over_threshold, "n_total": len(rows)}
    print(f"\n[y_crosscheck] расхождений > {DISCREPANCY_THRESHOLD:.1%}: {n_over_threshold} из {len(rows)}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[y_crosscheck] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
