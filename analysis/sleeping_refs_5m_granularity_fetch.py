#!/usr/bin/env python3
"""«Расхождение площадок» → проверка 2 владельца (2026-09-06, дословно):
"Гранулярность. Минутные свечи за последние 14 дней по пяти сильным
(HL точно отдаёт; Lighter -- проверить markPriceCandles с 1m или 5m)."

Реальная проверка (не по памяти, `lighter_markpricecandles_docs_probe_
result.json`, официальный OpenAPI-энум): Lighter `resolution` реально
поддерживает "1m"/"5m" наравне с "1h" -- ПОДТВЕРЖДЕНО. Тянем "5m"
напрямую (владелец: "пересчитать... на 5-минутном шаге" -- 5m, не 1m,
это и есть целевой шаг для пересчёта).

14 дней x 5м = 4032 бара/тикер/площадка -- пагинация назад (Lighter,
тот же метод, что часовой фетч, count_back=500/страница, эмпирически
подтверждено) и один вызов (HL candleSnapshot, эмпирически весь
диапазон одним ответом на часовом шаге -- на 5м честно ПРОВЕРЯЕМ
заново, не предполагаем то же самое."""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0"}
HL_HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0", "Content-Type": "application/json"}
OUT_DIR = Path("data/sleeping_refs_cache")
OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_5m_granularity_fetch_result.json")
LIGHTER_RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")

STRONG_TICKERS = ("BE", "USAR", "CRWV", "SNDK", "ORCL")
HISTORY_DAYS = 14
RESOLUTION_LIGHTER = "5m"
RESOLUTION_HL = "5m"
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 3.0


def retry_get(path: str, params: dict) -> requests.Response:
    last_exc = None
    for i in range(RETRY_ATTEMPTS):
        try:
            r = requests.get(f"{LIGHTER_API_BASE}{path}", params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** i)
                continue
            return r
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** i)
    raise RuntimeError(f"lighter GET не удался: {last_exc}")


def retry_post_hl(body: dict) -> requests.Response:
    last_exc = None
    for i in range(RETRY_ATTEMPTS):
        try:
            r = requests.post(f"{HYPERLIQUID_API_BASE}/info", headers=HL_HEADERS, data=json.dumps(body), timeout=30)
            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            time.sleep(RETRY_BACKOFF_S * (2 ** i))
    raise RuntimeError(f"HL POST не удался: {last_exc}")


def fetch_lighter_5m(market_id: int, since_unix: int) -> list[dict]:
    now = int(time.time())
    records: list[dict] = []
    end_ts = now
    seen: set[int] = set()
    for _ in range(50):  # 4032/500 ~= 9 страниц, запас x5
        r = retry_get("/api/v1/markPriceCandles", {
            "market_id": market_id, "resolution": RESOLUTION_LIGHTER,
            "start_timestamp": since_unix, "end_timestamp": end_ts, "count_back": 500,
        })
        if r.status_code != 200:
            print(f"    Lighter HTTP {r.status_code}: {r.text[:200]}")
            break
        page = r.json().get("c", [])
        if not page:
            break
        new_page = [x for x in page if x.get("t") not in seen]
        if not new_page:
            break
        records.extend(new_page)
        seen.update(x["t"] for x in new_page)
        oldest = min(x["t"] for x in new_page)
        if len(new_page) < 500 or oldest <= since_unix:
            break
        end_ts = oldest - 1
    return sorted(records, key=lambda x: x["t"])


def fetch_hl_5m(coin: str, since_ms: int) -> list[dict]:
    now_ms = int(time.time() * 1000)
    r = retry_post_hl({"type": "candleSnapshot",
                        "req": {"coin": coin, "interval": RESOLUTION_HL, "startTime": since_ms, "endTime": now_ms}})
    if r.status_code != 200:
        print(f"    HL HTTP {r.status_code}: {r.text[:200]}")
        return []
    data = r.json()
    if not isinstance(data, list):
        print(f"    HL неожиданная форма ответа: {str(data)[:300]}")
        return []
    return data


def run() -> int:
    lighter = json.loads(LIGHTER_RESULT_PATH.read_text())["markets"]
    now_utc = datetime.now(timezone.utc)
    since_dt = now_utc - timedelta(days=HISTORY_DAYS)
    since_unix = int(since_dt.timestamp())
    since_ms = since_unix * 1000
    print(f"[5m_fetch] реальное окно: {since_dt.isoformat()} .. {now_utc.isoformat()} ({HISTORY_DAYS} дней)")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "window_start_utc": since_dt.isoformat(), "history_days": HISTORY_DAYS,
                 "resolution": "5m", "tickers": {}}

    for ticker in STRONG_TICKERS:
        print(f"\n=== {ticker} ===")
        market_id = lighter[ticker]["market_id"]
        l_records = fetch_lighter_5m(market_id, since_unix)
        print(f"    Lighter (5m, реальная проверка резолюции): {len(l_records)} баров "
              f"(ожидается ~{HISTORY_DAYS*24*12})")

        h_records = fetch_hl_5m(f"xyz:{ticker}", since_ms)
        print(f"    HL (5m): {len(h_records)} баров (ожидается ~{HISTORY_DAYS*24*12})")

        if l_records:
            with open(OUT_DIR / f"markprice_5m_{ticker}_lighter.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["t", "o", "h", "l", "c"])
                w.writeheader()
                for row in l_records:
                    w.writerow({k: row.get(k) for k in ("t", "o", "h", "l", "c")})
        if h_records:
            with open(OUT_DIR / f"markprice_5m_{ticker}_xyz.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["t", "o", "h", "l", "c"])
                w.writeheader()
                for row in h_records:
                    w.writerow({"t": row.get("t"), "o": row.get("o"), "h": row.get("h"),
                                "l": row.get("l"), "c": row.get("c")})

        out["tickers"][ticker] = {
            "n_lighter_5m": len(l_records), "n_hl_5m": len(h_records),
            "lighter_first_t": l_records[0]["t"] if l_records else None,
            "lighter_last_t": l_records[-1]["t"] if l_records else None,
            "hl_first_t": h_records[0]["t"] if h_records else None,
            "hl_last_t": h_records[-1]["t"] if h_records else None,
        }
        time.sleep(0.3)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[5m_fetch] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
