#!/usr/bin/env python3
"""Задача «форензика fomo» -- разведка №3 (только чтение).

Первые два прогона подтвердили: robinscan.io/leaderboard и Dune-
дашборды adam_tehc реально отдают HTML (не пустой JS-shell), но
таблица лидерборда данные НЕ встраивает в исходный HTML/RSC-payload --
похоже, подгружается отдельным клиентским запросом к API. Пробуем
НЕБОЛЬШОЕ (3-4 попытки, не бесконечный перебор) число правдоподобных
путей REST API эмпирически -- не гадаем дальше вслепую, если ни один
не сработает."""
from __future__ import annotations

import json
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-fomo-forensics-recon/1.0", "Accept": "application/json"}
OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_api_guess_probe_result.json")

CANDIDATE_PATHS = [
    "https://robinscan.io/api/leaderboard",
    "https://robinscan.io/api/leaderboard?period=30d",
    "https://robinscan.io/api/leaderboard?window=30d",
    "https://robinscan.io/api/v1/leaderboard",
    "https://robinscan.io/api/traders/leaderboard",
]


def run() -> int:
    out = {}
    for url in CANDIDATE_PATHS:
        entry = {}
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            entry["status"] = r.status_code
            entry["content_type"] = r.headers.get("content-type")
            entry["content_length"] = len(r.text)
            entry["looks_like_json"] = entry["content_type"] and "json" in entry["content_type"]
            entry["body_sample"] = r.text[:500]
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:300]
        out[url] = entry
        print(f"{url}: {entry}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[probe] результат в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
