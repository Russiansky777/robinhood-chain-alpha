#!/usr/bin/env python3
"""Задача B (владелец, 2026-09-05, после бэкфилла Lighter x Hyperliquid,
"из истории, без капитала"): фандинг на сток-перпах Lighter. Гипотеза:
ретейл структурно лонгует, фандинг смещён (структурно положительный,
long платит short) -- хедж собственной позиции = шорт акции у брокера
(наличие брокера -- вопрос владельца, здесь не считается).

Ограничение владельца: НЕ рассматривать площадки, где рабочий капитал
должен лежать крупной суммой на счету CEX с риском заморозки. Lighter
(on-chain, капитал заперт в конкретной позиции до закрытия, выводится
по факту) -- ДОПУСТИМАЯ площадка по этому критерию.

Реальный хост -- `api.rh.lighter.xyz` (ТОТ ЖЕ, что уже используется для
хеджа P5 и funding_pairs.json), НЕ mainnet.zklighter.elliot.ai (другой
инстанс, использован в Sprint P4 для другого исследования) -- нам важна
именно площадка, где реально можно исполнить хедж, не абстрактный
Lighter вообще.

Классификация "сток, не крипта" -- РЕЮЗ уже реального, оплаченного
классификатора Sprint P4 (`p4_lighter_markets.py::is_stock_like`,
`load_stock_symbols()`), тот же реестр Sprint R1 (194 реальных сток-
тикера, `data/sprintR1_cache/r1_rwa_full_universe.csv`) -- не гадаем
по паттерну имени заново.

История фандинга -- ТОТ ЖЕ метод, что уже реально исполнен и
подтверждён в Задаче B (funding_historical_backfill.py::
fetch_lighter_history, пагинация назад count_back=750), "с первой
доступной записи" -- без искусственной нижней границы (передаём
since_unix=0, пагинация останавливается сама на коротком/пустом ответе).

Глубина стакана -- ТОТ ЖЕ метод +-0.5% от мида, что funding_spread_
hourly_snapshot.py::fetch_lighter_depth_pct (уже реально проверен)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from funding_historical_backfill import fetch_lighter_history, ANNUALIZATION_HOURS  # noqa: E402
from p4_lighter_markets import load_stock_symbols, is_stock_like  # noqa: E402

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-lighter-stock-perp-funding/1.0"}
OUT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_funding_result.json")
DEPTH_BAND_PCT = 0.5  # тот же band, что funding_spread_hourly_snapshot.py
MEDIAN_ANNUAL_THRESHOLD = 25.0  # владелец: медиана >=25%/год одного знака
FRAC_SAME_SIGN_THRESHOLD = 0.70  # владелец: доля часов того же знака >=70%


def fetch_lighter_markets() -> list[dict]:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json().get("order_book_details", [])


def fetch_depth_pct(market_id: int, mid_price: float) -> dict:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookOrders", headers=HEADERS,
                      params={"market_id": market_id, "limit": 200}, timeout=20)
    r.raise_for_status()
    body = r.json()
    lo, hi = mid_price * (1 - DEPTH_BAND_PCT / 100), mid_price * (1 + DEPTH_BAND_PCT / 100)
    bid = sum(float(o["price"]) * float(o["remaining_base_amount"]) for o in body.get("bids", []) if lo <= float(o["price"]) <= mid_price)
    ask = sum(float(o["price"]) * float(o["remaining_base_amount"]) for o in body.get("asks", []) if mid_price <= float(o["price"]) <= hi)
    return {"bid_notional_usd": bid, "ask_notional_usd": ask}


def weekly_stability(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df["week"] = df["hour"].dt.to_period("W").apply(lambda p: p.start_time.strftime("%Y-%m-%d"))
    rows = []
    for week, g in sorted(df.groupby("week")):
        rows.append({"week_start": week, "n_hours": int(len(g)), "median_rate_annual_pct": float(g["rate_annual_pct"].median()),
                      "frac_positive": float((g["rate_annual_pct"] > 0).mean())})
    return rows


def run() -> int:
    markets = fetch_lighter_markets()
    print(f"[lighter_stock_perp] реальных рынков на api.rh.lighter.xyz: {len(markets)}")
    stock_symbols = load_stock_symbols()
    print(f"[lighter_stock_perp] реестр Sprint R1: {len(stock_symbols)} реальных сток-тикеров")

    stock_markets = []
    for m in markets:
        ok, reason = is_stock_like(m, stock_symbols)
        if ok:
            stock_markets.append((m, reason))
    print(f"[lighter_stock_perp] реальных сток-перпов на Lighter: {len(stock_markets)}")
    for m, reason in stock_markets:
        print(f"  {m.get('symbol')} (market_id={m.get('market_id')}) -- {reason}")

    results = []
    for m, reason in stock_markets:
        symbol, market_id = m.get("symbol"), m.get("market_id")
        print(f"\n=== {symbol} (market_id={market_id}) ===")
        records = fetch_lighter_history(market_id, since_unix=0)
        print(f"  реальных часовых записей фандинга: {len(records)}")
        if not records:
            results.append({"symbol": symbol, "market_id": market_id, "match_reason": reason, "error": "нет истории фандинга"})
            continue
        df = pd.DataFrame(records)
        df["rate_annual_pct"] = df["rate"].astype(float) * ANNUALIZATION_HOURS
        df["hour"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.floor("h")
        df = df.groupby("hour", as_index=False)["rate_annual_pct"].last()

        median_rate = float(df["rate_annual_pct"].median())
        mean_rate = float(df["rate_annual_pct"].mean())
        sign = "positive" if median_rate > 0 else ("negative" if median_rate < 0 else "zero")
        frac_same_sign = float(((df["rate_annual_pct"] > 0) if sign == "positive" else (df["rate_annual_pct"] < 0)).mean()) if sign != "zero" else None
        weekly = weekly_stability(df)
        weeks_same_sign = sum(1 for w in weekly if (w["median_rate_annual_pct"] > 0) == (sign == "positive") and w["median_rate_annual_pct"] != 0)
        weekly_stable = weeks_same_sign == len(weekly) if weekly else False

        try:
            mark_price = float(m.get("mark_price"))
            depth = fetch_depth_pct(market_id, mark_price)
        except Exception as exc:  # noqa: BLE001
            mark_price, depth = None, {"error": str(exc)[:200]}

        entry = {
            "symbol": symbol, "market_id": market_id, "match_reason": reason,
            "n_hours": int(len(df)), "median_rate_annual_pct": median_rate, "mean_rate_annual_pct": mean_rate,
            "sign": sign, "frac_hours_same_sign": frac_same_sign,
            "n_weeks": len(weekly), "n_weeks_same_sign_as_overall": weeks_same_sign, "weekly_stable": weekly_stable,
            "weekly": weekly,
            "mark_price_now": mark_price, "daily_quote_token_volume_usd_now": m.get("daily_quote_token_volume"),
            "depth_pm_0_5pct_now": depth,
        }
        alive = (abs(median_rate) >= MEDIAN_ANNUAL_THRESHOLD and frac_same_sign is not None
                 and frac_same_sign >= FRAC_SAME_SIGN_THRESHOLD and weekly_stable)
        entry["verdict"] = f"{'ЖИВА' if alive else 'НЕ ПРОХОДИТ'} (медиана={median_rate:.2f}%/год, доля_того_же_знака={frac_same_sign}, стабильна_по_неделям={weekly_stable})"
        print(f"  {entry['verdict']}")
        results.append(entry)

    n_alive = sum(1 for r in results if r.get("verdict", "").startswith("ЖИВА"))
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "n_stock_perps_found": len(stock_markets), "n_alive": n_alive, "results": results}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[lighter_stock_perp] найдено живых (по предрегистрации) кандидатов: {n_alive} из {len(stock_markets)}")
    print(f"[lighter_stock_perp] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
