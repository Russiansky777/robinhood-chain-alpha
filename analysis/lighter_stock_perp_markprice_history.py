#!/usr/bin/env python3
"""«Спящие референсы», п.1 (Lighter сток-перпы, 37 рынков) -- реальная
часовая история mark price с 05.07 + текущая глубина книги (round-trip
на $500 и $5000). Бесплатно (0 кредитов Dune), только чтение.

Реальный эндпоинт (найден через официальные .md-доки,
lighter_markpricecandles_docs_probe_result.json): `GET
https://api.rh.lighter.xyz/api/v1/markPriceCandles` -- обязательные
параметры market_id/resolution/start_timestamp/end_timestamp/count_back,
ответ `{code, r, c: [{t,o,h,l,c}, ...]}`. Пагинация НАЗАД -- тот же
реальный, уже проверенный метод, что funding_historical_backfill.py::
fetch_lighter_history (для /fundings) -- по аналогии, `candles`
(родственный эндпоинт) документированно отдаёт максимум 500/вызов,
не предполагаем, что markPriceCandles иначе, но проверяем эмпирически
по факту первого реального вызова (реальная длина страницы).

37 рынков -- РЕАЛЬНЫЙ список из уже оплаченного/выполненного прогона
Задачи B (`lighter_stock_perp_funding_result.json`), не тянем заново
классификатор -- те же symbol/market_id."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0"}
OUT_DIR = Path("data/sleeping_refs_cache")
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")

HISTORY_START_UTC = datetime(2026, 7, 5, tzinfo=timezone.utc)
RESOLUTION = "1h"
COUNT_BACK = 500  # см. докстринг -- проверяется эмпирически по первой реальной странице
DEPTH_NOTIONALS_USD = (500.0, 5000.0)


def load_markets() -> list[dict]:
    d = json.loads(Path("data/p3_guard_cache/lighter_stock_perp_funding_result.json").read_text())
    return [{"symbol": r["symbol"], "market_id": r["market_id"]} for r in d["results"]]


def retry_get(path: str, params: dict, tries: int = 5) -> requests.Response:
    for i in range(tries):
        try:
            r = requests.get(f"{LIGHTER_API_BASE}{path}", params=params, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** i)
                continue
            return r
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
    raise RuntimeError("unreachable")


def fetch_markprice_history(market_id: int) -> list[dict]:
    """Пагинация НАЗАД, тот же принцип, что fetch_lighter_history для
    /fundings -- останавливается на короткой/пустой странице ИЛИ при
    достижении HISTORY_START_UTC."""
    since_unix = int(HISTORY_START_UTC.timestamp())
    now = int(time.time())
    records: list[dict] = []
    end_ts = now
    seen: set[int] = set()
    for _ in range(50):  # генеральный предохранитель -- ~63 дня/500 = ~4 страницы при 1h, запас x10
        r = retry_get("/api/v1/markPriceCandles", {
            "market_id": market_id, "resolution": RESOLUTION,
            "start_timestamp": since_unix, "end_timestamp": end_ts, "count_back": COUNT_BACK,
        })
        if r.status_code != 200:
            print(f"    market_id={market_id}: HTTP {r.status_code} -- {r.text[:200]}")
            break
        body = r.json()
        page = body.get("c", [])
        if not page:
            break
        new_page = [x for x in page if x.get("t") not in seen]
        if not new_page:
            break
        records.extend(new_page)
        seen.update(x["t"] for x in new_page)
        oldest = min(x["t"] for x in new_page)
        if len(new_page) < COUNT_BACK or oldest <= since_unix:
            break
        end_ts = oldest - 1
    return sorted(records, key=lambda x: x["t"])


def fetch_round_trip_cost(market_id: int, mid_price: float) -> dict:
    """Реальная стоимость round-trip (купить, сразу продать) на
    заданный номинал -- ходим по книге от лучшей цены, накапливаем
    номинал до notional_usd, считаем средневзвешенную цену исполнения
    каждой стороны. cost_pct = (avg_ask - avg_bid) / mid * 100."""
    r = retry_get("/api/v1/orderBookOrders", {"market_id": market_id, "limit": 200})
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    body = r.json()
    bids = sorted(body.get("bids", []), key=lambda o: -float(o["price"]))
    asks = sorted(body.get("asks", []), key=lambda o: float(o["price"]))

    def walk(levels: list[dict], notional_usd: float) -> float | None:
        acc_notional, acc_qty = 0.0, 0.0
        for o in levels:
            price, qty = float(o["price"]), float(o["remaining_base_amount"])
            level_notional = price * qty
            if acc_notional + level_notional >= notional_usd:
                remaining_notional = notional_usd - acc_notional
                acc_qty += remaining_notional / price
                acc_notional = notional_usd
                break
            acc_notional += level_notional
            acc_qty += qty
        if acc_notional < notional_usd * 0.999 or acc_qty == 0:
            return None  # книга тоньше запрошенного номинала
        return acc_notional / acc_qty  # средневзвешенная цена исполнения

    out: dict = {}
    for notional in DEPTH_NOTIONALS_USD:
        avg_ask = walk(asks, notional)  # покупка -- идём по asks
        avg_bid = walk(bids, notional)  # продажа -- идём по bids
        if avg_ask is None or avg_bid is None:
            out[f"round_trip_cost_pct_{int(notional)}"] = None
            out[f"book_thin_at_{int(notional)}"] = True
        else:
            out[f"round_trip_cost_pct_{int(notional)}"] = (avg_ask - avg_bid) / mid_price * 100
            out[f"book_thin_at_{int(notional)}"] = False
    return out


def run() -> int:
    markets = load_markets()
    print(f"[markprice_history] реальных рынков (из уже известного списка): {len(markets)}")

    # Реальные текущие mark_price -- нужны для round-trip% и как sanity-check
    r = retry_get("/api/v1/orderBookDetails", {"filter": "all"})
    r.raise_for_status()
    ob_details = {m["market_id"]: m for m in r.json().get("order_book_details", [])}

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "history_start_utc": HISTORY_START_UTC.isoformat(), "resolution": RESOLUTION,
                 "markets": {}}

    for i, m in enumerate(markets):
        symbol, market_id = m["symbol"], m["market_id"]
        print(f"[{i+1}/{len(markets)}] {symbol} (market_id={market_id})...")
        candles = fetch_markprice_history(market_id)
        print(f"    {len(candles)} реальных часовых свечей")

        mid_price = float(ob_details.get(market_id, {}).get("mark_price", 0) or 0)
        depth = fetch_round_trip_cost(market_id, mid_price) if mid_price else {"error": "no mark_price"}
        print(f"    mid={mid_price}, round-trip: {depth}")

        # Сырые свечи -- в отдельный CSV на рынок (не раздуваем главный JSON)
        if candles:
            import csv
            csv_path = OUT_DIR / f"markprice_1h_{symbol}_{market_id}.csv"
            with open(csv_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["t", "o", "h", "l", "c"])
                w.writeheader()
                for row in candles:
                    w.writerow({k: row.get(k) for k in ("t", "o", "h", "l", "c")})

        out["markets"][symbol] = {
            "market_id": market_id, "n_candles": len(candles),
            "first_ts": candles[0]["t"] if candles else None,
            "last_ts": candles[-1]["t"] if candles else None,
            "mid_price_now": mid_price,
            **depth,
        }
        time.sleep(0.2)

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[markprice_history] результат записан в {RESULT_PATH}, сырые свечи -- в {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
