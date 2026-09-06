#!/usr/bin/env python3
"""«Спящие референсы», п.1 (Lighter сток-перпы) -- перед тем, как
тянуть часовые mark price с 05.07, узнаём РЕАЛЬНЫЙ путь/параметры
эндпоинта. Найден в реальном индексе API (llms.txt, уже сохранён
ранее в lighter_rfq_and_docs_probe_result.json): `markPriceCandles`
-- "Get mark price candles",
https://apidocs.rh.lighter.xyz/reference/markpricecandles.md.

Тот же паттерн, что lighter_rfq_and_docs_probe.py: .md-версия доков
не JS-рендерится, реальный GET, только чтение."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_markpricecandles_docs_probe_result.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (robinhood-chain-alpha-sleeping-refs/1.0)"}

DOC_TARGETS = [
    "https://apidocs.rh.lighter.xyz/reference/markpricecandles.md",
    "https://apidocs.rh.lighter.xyz/reference/candles.md",
    "https://apidocs.rh.lighter.xyz/reference/marketpricecharts.md",
    "https://apidocs.rh.lighter.xyz/reference/get_markets.md",
]


def run() -> int:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "docs": {}}
    for url in DOC_TARGETS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
            print(f"=== {url} -- HTTP {r.status_code} ({len(r.text)} chars) ===")
            print(r.text[:4000])
            print()
            out["docs"][url] = {"status_code": r.status_code, "text": r.text}
        except Exception as e:  # noqa: BLE001
            print(f"=== {url} -- ERROR {e} ===")
            out["docs"][url] = {"error": str(e)}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\nрезультат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
