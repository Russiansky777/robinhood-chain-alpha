#!/usr/bin/env python3
"""Задача D -- прямой минимальный тест: работает ли closed=true +
end_date_min/end_date_max ВООБЩЕ для окон годичной давности. Оригинальная
проверка этого механизма (taskC_polymarket_daterange_probe_result.json,
2026-09-05) тестировала ТОЛЬКО ближнее окно (2026-08-06..08-16) --
никогда не проверялась на датах 2025 года. Реальный найденный результат
после трёх фиксов -- n_polymarket_markets_scanned=0 на всех 4
исторических окнах -- нужно решить напрямую, не гадая, реальный ли это
предел API или ещё один баг в обёртке."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-gamma-historical-probe/1.0"}
BASE = "https://gamma-api.polymarket.com"
OUT_PATH = Path("data/p3_guard_cache/taskD_gamma_historical_probe_result.json")


def probe(label: str, params: dict) -> dict:
    r = requests.get(f"{BASE}/markets", params=params, headers=HEADERS, timeout=30)
    entry = {"label": label, "params": params, "status": r.status_code}
    if r.status_code == 200:
        try:
            body = r.json()
            entry["n_results"] = len(body) if isinstance(body, list) else None
            entry["sample"] = body[:3] if isinstance(body, list) else body
        except ValueError:
            entry["json_parse_error"] = True
    else:
        entry["body_snippet"] = r.text[:300]
    return entry


def run() -> int:
    results = []
    windows = [
        ("recent_known_working_2026-08", {"closed": "true", "order": "endDate", "ascending": "false",
                                            "end_date_min": "2026-08-06T00:00:00Z", "end_date_max": "2026-08-16T00:00:00Z",
                                            "limit": 20}),
        ("nfl_week2_2025-09", {"closed": "true", "order": "endDate", "ascending": "false",
                                 "end_date_min": "2025-09-14T00:00:00Z", "end_date_max": "2025-09-23T00:00:00Z",
                                 "limit": 20}),
        ("nfl_week2_2025-09_ascending", {"closed": "true", "order": "endDate", "ascending": "true",
                                           "end_date_min": "2025-09-14T00:00:00Z", "end_date_max": "2025-09-23T00:00:00Z",
                                           "limit": 20}),
        ("2025-09_no_order", {"closed": "true",
                               "end_date_min": "2025-09-14T00:00:00Z", "end_date_max": "2025-09-23T00:00:00Z",
                               "limit": 20}),
        ("2025_broad_full_year", {"closed": "true", "order": "endDate", "ascending": "false",
                                    "end_date_min": "2025-01-01T00:00:00Z", "end_date_max": "2025-12-31T00:00:00Z",
                                    "limit": 20}),
        ("no_date_filter_closed_only", {"closed": "true", "order": "endDate", "ascending": "false", "limit": 20}),
    ]
    for label, params in windows:
        entry = probe(label, params)
        results.append(entry)
        print(f"[gamma_hist_probe] {label}: status={entry['status']} n_results={entry.get('n_results')}")
        if entry.get("sample"):
            for m in entry["sample"][:2]:
                if isinstance(m, dict):
                    print(f"    -- {m.get('question')!r} endDate={m.get('endDate')} slug={m.get('slug')}")
        time.sleep(0.4)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({"results": results}, indent=2, ensure_ascii=False, default=str))
    print(f"\n[gamma_hist_probe] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
