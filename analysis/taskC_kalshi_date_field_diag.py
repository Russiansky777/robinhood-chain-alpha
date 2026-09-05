#!/usr/bin/env python3
"""Задача C -- диагностика полей даты Kalshi (только чтение).

Гипотеза (не подтверждена на момент написания): matcher использует
`close_time` как прокси времени игры, но `close_time` может быть
административно смещён (найдено раньше для одного BTC-рынка, где
`expiration_time` был на неделю позже `expected_expiration_time` --
хотя для того рынка `close_time` и `expected_expiration_time` совпадали
почти точно, так что гипотеза про close_time для BTC не подтвердилась).

РЕАЛЬНАЯ проверка для спортивных серий: тикер игры сам кодирует дату
события (например `KXNFLGAME-26AUG28MINDEN` = игра 2026-08-28) --
это НАДЁЖНЫЙ независимый источник правды, не подверженный
административным смещениям (задаётся при создании рынка под конкретную
игру). Сравниваем close_time / expected_expiration_time / expiration_time
С ЭТОЙ датой из тикера для реальных NFL/MLB игр -- чтобы понять, какое
поле (если любое) реально соответствует времени игры."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-date-diag/1.0"}
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
OUT_PATH = Path("data/p3_guard_cache/taskC_kalshi_date_field_diag_result.json")

# Тикер формата "KXNFLGAME-26AUG28MINDEN" -- дата всегда YYDDDMMM сразу
# после дефиса, 2 цифры года + 2 цифры дня + 3 буквы месяца.
TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def parse_ticker_date(event_ticker: str) -> datetime | None:
    m = TICKER_DATE_RE.search(event_ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    month = MONTHS.get(mon)
    if not month:
        return None
    year = 2000 + int(yy)
    try:
        return datetime(year, month, int(dd), tzinfo=timezone.utc)
    except ValueError:
        return None


def run() -> int:
    samples = []
    for series in ("KXNFLGAME", "KXMLBGAME"):
        r = requests.get(f"{KALSHI_BASE}/markets", params={"series_ticker": series, "limit": 15, "status": "settled"},
                          headers=HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"[diag] {series}: status={r.status_code}")
            continue
        markets = r.json().get("markets", [])
        seen_events = set()
        for m in markets:
            ev = m.get("event_ticker")
            if ev in seen_events:
                continue
            seen_events.add(ev)
            ticker_date = parse_ticker_date(ev)
            entry = {
                "series": series, "event_ticker": ev, "ticker": m.get("ticker"),
                "close_time": m.get("close_time"),
                "expected_expiration_time": m.get("expected_expiration_time"),
                "expiration_time": m.get("expiration_time"),
                "open_time": m.get("open_time"),
                "ticker_embedded_date": ticker_date.isoformat() if ticker_date else None,
            }
            for field in ("close_time", "expected_expiration_time", "expiration_time", "open_time"):
                val = entry[field]
                if val and ticker_date:
                    try:
                        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                        entry[f"{field}_diff_days_from_ticker_date"] = (dt - ticker_date).total_seconds() / 86400
                    except ValueError:
                        pass
            samples.append(entry)
        time.sleep(0.3)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({"samples": samples}, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(samples[:10], indent=2, ensure_ascii=False, default=str))
    print(f"\n[diag] {len(samples)} игр проверено, результат в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
