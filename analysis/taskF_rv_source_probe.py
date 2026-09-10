#!/usr/bin/env python3
"""Задача F, Шаг 1a -- у Derive/Lyra НЕТ эндпоинта исторических свечей
индекса/перпа (подтверждено Шагом 0b: /public/get_ticker -- только
live, /public/get_candles и варианты -- реальные 404). Для расчёта
реализованной волатильности (RV) нужен ВНЕШНИЙ публичный источник
исторических OHLC BTC/ETH за 90+ дней. Проверяем РЕАЛЬНУЮ доступность
(не по памяти -- Binance исторически блокирует часть регионов/раннеров
по IP, не гадаем) трёх кандидатов: Kraken (нет привязки к US-персонам),
Coinbase Exchange (публичный, США-based, обычно без гео-блока), Binance
(широко используется, но реальный риск блокировки с раннера GH Actions)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskF-rv-source-probe/1.0"}
OUT_PATH = Path("data/p3_guard_cache/taskF_rv_source_probe_result.json")


def probe(name: str, url: str, params: dict) -> dict:
    entry = {"name": name, "url": url, "params": params}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        entry["status"] = r.status_code
        entry["body_snippet"] = r.text[:400]
        if r.status_code == 200:
            try:
                body = r.json()
                entry["json_top_level_type"] = type(body).__name__
                if isinstance(body, dict):
                    entry["json_keys"] = list(body.keys())[:10]
                elif isinstance(body, list):
                    entry["json_len"] = len(body)
                    entry["json_first_item"] = body[0] if body else None
            except ValueError:
                entry["json_parse_error"] = True
    except requests.exceptions.RequestException as exc:
        entry["exception"] = str(exc)[:300]
    return entry


def run() -> int:
    results = []

    # Kraken -- публичный OHLC, интервал в минутах, since -- unix seconds
    results.append(probe("kraken_btc_1h", "https://api.kraken.com/0/public/OHLC",
                          {"pair": "XBTUSD", "interval": 60, "since": int(time.time()) - 90 * 86400}))
    time.sleep(0.5)
    results.append(probe("kraken_eth_1h", "https://api.kraken.com/0/public/OHLC",
                          {"pair": "ETHUSD", "interval": 60, "since": int(time.time()) - 90 * 86400}))
    time.sleep(0.5)

    # Coinbase Exchange -- публичные свечи, ISO8601 start/end, granularity в секундах
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=2)).isoformat()  # Coinbase лимитирует ~300 свечей за запрос -- узкое окно для проверки достижимости
    end = now.isoformat()
    results.append(probe("coinbase_btc_1h", "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                          {"start": start, "end": end, "granularity": 3600}))
    time.sleep(0.5)
    results.append(probe("coinbase_eth_1h", "https://api.exchange.coinbase.com/products/ETH-USD/candles",
                          {"start": start, "end": end, "granularity": 3600}))
    time.sleep(0.5)

    # Binance -- реальная проверка доступности с этого раннера (не гадаем про геоблок)
    results.append(probe("binance_btc_1h", "https://api.binance.com/api/v3/klines",
                          {"symbol": "BTCUSDT", "interval": "1h", "limit": 10}))
    time.sleep(0.5)
    results.append(probe("binance_eth_1h", "https://api.binance.com/api/v3/klines",
                          {"symbol": "ETHUSDT", "interval": "1h", "limit": 10}))

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    for r in results:
        print(f"[rv_source_probe] {r['name']}: status={r.get('status', r.get('exception'))}")
    print(f"[rv_source_probe] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
