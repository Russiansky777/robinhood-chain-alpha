#!/usr/bin/env python3
"""Задача F, Шаг 0c -- реальная проверка ликвидности near-ATM опционов
на Derive. Предыдущий пробный запрос нашёл ТРЕВОЖНЫЙ сигнал: у одного
конкретного near-ATM инструмента 0 сделок за 90 дней, а get_trade_history
без правильной пагинации назад отдаёт лишь последние ~4.5 часа вместо
запрошенных 90 дней.

Проверяем ДВЕ вещи по-настоящему:
  1. Реальную глубину истории через пагинацию курсором (не один запрос).
  2. Агрегированное число сделок по ВСЕМ near-ATM (страйк в пределах
     +-15% от индекса, экспирация 7-30 дней) BTC и ETH опционам за
     последние ~14 дней -- единичная неликвидность одного контракта
     или системная картина по всему сегменту."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskF-liquidity-probe/1.0", "Accept": "application/json"}
BASE = "https://api.lyra.finance"
OUT_PATH = Path("data/p3_guard_cache/taskF_derive_liquidity_probe_result.json")


def paginate_trade_history(currency: str, from_ts_ms: int, to_ts_ms: int, max_pages: int = 15) -> tuple[list[dict], dict]:
    """Реальная пагинация назад по времени -- каждый следующий запрос
    сужает to_timestamp до времени самой старой сделки предыдущей
    страницы, а не полагается на курсор (реальная структура пагинации
    неизвестна заранее, поэтому используем универсальный, надёжный
    способ -- сужение окна по timestamp)."""
    all_trades = []
    cur_to = to_ts_ms
    n_pages = 0
    stop_reason = None
    for _ in range(max_pages):
        r = requests.get(f"{BASE}/public/get_trade_history", params={
            "currency": currency, "instrument_type": "option",
            "from_timestamp": from_ts_ms, "to_timestamp": cur_to, "page_size": 100,
        }, headers=HEADERS, timeout=20)
        n_pages += 1
        if r.status_code != 200:
            stop_reason = f"http_{r.status_code}_at_page_{n_pages}"
            break
        trades = r.json().get("result", {}).get("trades", [])
        if not trades:
            stop_reason = f"empty_at_page_{n_pages}"
            break
        all_trades.extend(trades)
        oldest_ts = min(t["timestamp"] for t in trades)
        if oldest_ts <= from_ts_ms:
            stop_reason = f"reached_from_ts_at_page_{n_pages}"
            break
        if len(trades) < 100:
            stop_reason = f"short_page_at_page_{n_pages}"
            break
        cur_to = oldest_ts - 1  # следующая страница -- строго раньше самой старой сделки этой
        time.sleep(0.15)
    else:
        stop_reason = f"max_pages_reached_({max_pages})"
    return all_trades, {"n_pages": n_pages, "stop_reason": stop_reason, "n_trades_total": len(all_trades)}


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    # 1. Реальная глубина через пагинацию назад, за 14 дней (не сразу 90 -- дешёвая проверка).
    fourteen_days_ago_ms = int((now - timedelta(days=14)).timestamp() * 1000)
    for currency in ("BTC", "ETH"):
        trades, diag = paginate_trade_history(currency, fourteen_days_ago_ms, now_ms)
        diag["oldest_reached"] = (datetime.fromtimestamp(min(t["timestamp"] for t in trades) / 1000, tz=timezone.utc).isoformat()
                                   if trades else None)
        diag["newest_reached"] = (datetime.fromtimestamp(max(t["timestamp"] for t in trades) / 1000, tz=timezone.utc).isoformat()
                                   if trades else None)
        result[f"{currency.lower()}_pagination_14d"] = diag

        # 2. Среди реально полученных сделок -- сколько относятся к near-ATM (страйк в +-15% от index_price на момент сделки)
        # и экспирация исходно 7-30 дней от даты сделки (парсим из instrument_name: CCY-YYYYMMDD-STRIKE-C/P).
        near_atm_trades = []
        for t in trades:
            try:
                parts = t["instrument_name"].split("-")
                expiry_str, strike_str = parts[1], parts[2]
                expiry_dt = datetime.strptime(expiry_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                trade_dt = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
                days_to_expiry_at_trade = (expiry_dt - trade_dt).days
                strike = float(strike_str)
                index_price = float(t["index_price"])
                pct_from_atm = abs(strike - index_price) / index_price
                if 7 <= days_to_expiry_at_trade <= 30 and pct_from_atm <= 0.15:
                    near_atm_trades.append(t)
            except (IndexError, ValueError, KeyError):
                continue
        result[f"{currency.lower()}_n_trades_fetched_14d"] = len(trades)
        result[f"{currency.lower()}_n_near_atm_trades_14d"] = len(near_atm_trades)
        result[f"{currency.lower()}_n_distinct_near_atm_instruments_14d"] = len({t["instrument_name"] for t in near_atm_trades})

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
