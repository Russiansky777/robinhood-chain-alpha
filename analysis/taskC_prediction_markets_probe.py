#!/usr/bin/env python3
"""Задача C, шаг 0 (владелец, 2026-09-05, "Предикт-маркеты -- Polymarket
x Kalshi в первую очередь, плюс SX Bet и новые площадки на BNB/других
цепях, если есть публичный API"): разведка реальных публичных API ДО
любого сопоставления рынков -- не гадаем форму ответа заранее.

Проверяем на КАЖДОЙ площадке: доступен ли публичный REST без ключа,
есть ли список активных/недавних рынков, есть ли история СДЕЛОК и/или
СТАКАНА (для владельца важно явно: "если история только по сделкам, а
не по стакану -- сказать явно, длительность окна тогда не измерить"),
формат цены (доля/центы), формат времени резолюции.

НЕ считает никакой метрики -- только фиксирует реальную форму ответа
каждого API, чтобы дальше строить сопоставление рынков на фактах, а не
на предположении о структуре."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-probe/1.0"}
OUT_PATH = Path("data/p3_guard_cache/taskC_prediction_markets_probe_result.json")
TIMEOUT = 20


def safe_get(url: str, **kwargs) -> dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
        entry = {"url": r.url, "status": r.status_code}
        try:
            body = r.json()
            entry["json_type"] = type(body).__name__
            if isinstance(body, list):
                entry["n_items"] = len(body)
                entry["sample"] = body[:2]
            elif isinstance(body, dict):
                entry["keys"] = list(body.keys())[:30]
                entry["sample"] = {k: body[k] for k in list(body.keys())[:5]}
            else:
                entry["sample"] = body
        except Exception as e:  # noqa: BLE001
            entry["error_parsing_json"] = str(e)[:200]
            entry["raw_head"] = r.text[:500]
        return entry
    except requests.exceptions.RequestException as exc:
        return {"url": url, "error": str(exc)[:300]}


def probe_polymarket() -> dict:
    out = {}
    # Gamma API -- метаданные рынков (публичная, без ключа, по документации Polymarket)
    out["gamma_markets"] = safe_get("https://gamma-api.polymarket.com/markets", params={"limit": 5, "active": "true", "closed": "false"})
    time.sleep(0.5)
    # CLOB API -- книга ордеров / история сделок
    out["clob_sampling_markets"] = safe_get("https://clob.polymarket.com/sampling-markets", params={"next_cursor": ""})
    time.sleep(0.5)
    out["data_api_trades"] = safe_get("https://data-api.polymarket.com/trades", params={"limit": 5})
    return out


def probe_kalshi() -> dict:
    out = {}
    out["markets"] = safe_get("https://api.elections.kalshi.com/trade-api/v2/markets", params={"limit": 5, "status": "open"})
    time.sleep(0.5)
    out["events"] = safe_get("https://api.elections.kalshi.com/trade-api/v2/events", params={"limit": 5})
    return out


def probe_sxbet() -> dict:
    out = {}
    out["markets"] = safe_get("https://api.sx.bet/markets/active")
    time.sleep(0.5)
    out["orders"] = safe_get("https://api.sx.bet/orders", params={"marketHashes": ""})
    return out


def run() -> int:
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print("[taskC_probe] Polymarket...")
    result["polymarket"] = probe_polymarket()
    print("[taskC_probe] Kalshi...")
    result["kalshi"] = probe_kalshi()
    print("[taskC_probe] SX Bet...")
    result["sxbet"] = probe_sxbet()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    for platform, entries in result.items():
        if platform == "generated_at_utc":
            continue
        print(f"\n=== {platform} ===")
        for name, entry in entries.items():
            print(f"  {name}: status={entry.get('status', entry.get('error'))} keys/type={entry.get('keys', entry.get('json_type'))}")
    print(f"\n[taskC_probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
