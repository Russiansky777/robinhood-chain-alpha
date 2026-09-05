#!/usr/bin/env python3
"""Задача C -- проверка, реально ли Polymarket Gamma API фильтрует по
`end_date_min`/`end_date_max` на /markets (только чтение).

tag_slug и search УЖЕ реально подтверждены нерабочими (см.
taskC_polymarket_btc_probe_result.json / taskC_sports_match_probe_
result.json) -- не повторяем ту же ошибку с датой: emпирически
проверяем, а не полагаемся на память о документации API."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-daterange-probe/1.0"}
POLYMARKET_BASE = "https://gamma-api.polymarket.com"
OUT_PATH = Path("data/p3_guard_cache/taskC_polymarket_daterange_probe_result.json")


def run() -> int:
    now = datetime.now(timezone.utc)
    window_min = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_max = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")  # узкое окно -- если фильтр реально работает, endDate всех строк должен попасть сюда

    out = {}

    # Без фильтра -- baseline
    r0 = requests.get(f"{POLYMARKET_BASE}/markets", params={
        "limit": 20, "closed": "true", "order": "endDate", "ascending": "false",
    }, headers=HEADERS, timeout=30)
    out["baseline_no_daterange"] = {"status": r0.status_code,
                                     "end_dates": [m.get("endDate") for m in r0.json()[:20]] if r0.status_code == 200 else None}

    # С фильтром end_date_min/end_date_max -- проверяем, реально ли сужает выборку
    r1 = requests.get(f"{POLYMARKET_BASE}/markets", params={
        "limit": 20, "closed": "true", "order": "endDate", "ascending": "false",
        "end_date_min": window_min, "end_date_max": window_max,
    }, headers=HEADERS, timeout=30)
    out["with_end_date_range"] = {"status": r1.status_code, "window": [window_min, window_max],
                                   "end_dates": [m.get("endDate") for m in r1.json()[:20]] if r1.status_code == 200 else None}

    # Альтернативные имена параметров, которые встречаются в разных версиях Gamma API
    r2 = requests.get(f"{POLYMARKET_BASE}/markets", params={
        "limit": 20, "closed": "true", "order": "endDate", "ascending": "false",
        "endDateMin": window_min, "endDateMax": window_max,
    }, headers=HEADERS, timeout=30)
    out["with_endDateMin_camel"] = {"status": r2.status_code,
                                     "end_dates": [m.get("endDate") for m in r2.json()[:20]] if r2.status_code == 200 else None}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[probe] результат в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
