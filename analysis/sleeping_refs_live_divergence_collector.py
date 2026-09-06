#!/usr/bin/env python3
"""«Расхождение площадок» → проверка 3 владельца (2026-09-06, дословно):
"Исполнимое расхождение -- живой сбор. Cron на NL-хосте, каждую минуту,
пять тикеров: лучшие bid/ask на обеих площадках, глубина на $5000 в обе
стороны. Считать D_exec = (bid дорогой - ask дешёвой) / mid, за
вычетом комиссии HL. Три-пять дней."

Реальные комиссии (не выдумываем, найдено по факту, где возможно):
Lighter taker_fee = 0% -- РЕАЛЬНЫЙ факт из `p5_close_dry_run.py`
(живой аккаунт, ETH-рынок, поле `taker_fee` из ответа Lighter). HL --
см. `sleeping_refs_xyz_deployer_probe.py` (проверка 4, отдельно) --
если реальная ставка НЕ найдена там на момент запуска этого сборщика,
здесь честно фиксируется `hl_taker_fee_pct=None`, D_exec_net НЕ
считается молча с угаданным числом -- считается ТОЛЬКО D_exec (без
вычета), и отдельно, если ставка появится позже, пересчитывается
постфактум из сырых bid/ask (уже сохранены).

Каждый вызов -- ОДИН снимок (не цикл ожидания -- cron сам вызывает
раз в минуту, это соответствует установленному на VPS принципу
P5/funding/turnover_watch: скрипт делает ОДНО измерение и выходит).
Пишет ОДНУ JSON-строку в JSONL-файл (append), путь передаётся через
env `LIVE_DIVERGENCE_OUT` (на VPS -- файл ВНЕ git-репозитория, тот же
принцип, что все остальные живые cron-сборщики этого проекта)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0"}
HL_HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0", "Content-Type": "application/json"}

OUT_PATH = Path(os.environ.get("LIVE_DIVERGENCE_OUT", "data/sleeping_refs_cache/live_divergence.jsonl"))
LIGHTER_RESULT_PATH = Path("data/p3_guard_cache/lighter_stock_perp_markprice_history_result.json")
STRONG_TICKERS = ("BE", "USAR", "CRWV", "SNDK", "ORCL")
DEPTH_NOTIONAL_USD = 5000.0
HL_TAKER_FEE_PCT = None  # см. докстринг -- заполняется явным числом ТОЛЬКО когда найден реальный источник (проверка 4)
RETRY_ATTEMPTS = 2  # cron раз в минуту -- не тратим минуту на долгие ретраи, лучше честный пропуск точки
RETRY_BACKOFF_S = 1.5


def retry_get(url: str, params: dict) -> requests.Response | None:
    for i in range(RETRY_ATTEMPTS):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=8)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(RETRY_BACKOFF_S)
    return None


def retry_post_hl(body: dict) -> requests.Response | None:
    for i in range(RETRY_ATTEMPTS):
        try:
            r = requests.post(f"{HYPERLIQUID_API_BASE}/info", headers=HL_HEADERS, data=json.dumps(body), timeout=8)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(RETRY_BACKOFF_S)
    return None


def walk_book(levels: list[dict], notional_usd: float, price_key: str, qty_key: str) -> float | None:
    acc_notional, acc_qty = 0.0, 0.0
    for lvl in levels:
        price, qty = float(lvl[price_key]), float(lvl[qty_key])
        level_notional = price * qty
        if acc_notional + level_notional >= notional_usd:
            remaining_notional = notional_usd - acc_notional
            acc_qty += remaining_notional / price
            acc_notional = notional_usd
            break
        acc_notional += level_notional
        acc_qty += qty
    if acc_notional < notional_usd * 0.999 or acc_qty == 0:
        return None
    return acc_notional / acc_qty


def lighter_snapshot(market_id: int) -> dict | None:
    r = retry_get(f"{LIGHTER_API_BASE}/api/v1/orderBookOrders", {"market_id": market_id, "limit": 200})
    if r is None:
        return None
    body = r.json()
    bids = sorted(body.get("bids", []), key=lambda o: -float(o["price"]))
    asks = sorted(body.get("asks", []), key=lambda o: float(o["price"]))
    if not bids or not asks:
        return None
    return {
        "best_bid": float(bids[0]["price"]), "best_ask": float(asks[0]["price"]),
        "depth_avg_ask_5000": walk_book(asks, DEPTH_NOTIONAL_USD, "price", "remaining_base_amount"),
        "depth_avg_bid_5000": walk_book(bids, DEPTH_NOTIONAL_USD, "price", "remaining_base_amount"),
    }


def xyz_snapshot(ticker: str) -> dict | None:
    r = retry_post_hl({"type": "l2Book", "coin": f"xyz:{ticker}"})
    if r is None:
        return None
    body = r.json()
    levels = body.get("levels", []) if isinstance(body, dict) else []
    if len(levels) != 2 or not levels[0] or not levels[1]:
        return None
    bids, asks = levels[0], levels[1]
    return {
        "best_bid": float(bids[0]["px"]), "best_ask": float(asks[0]["px"]),
        "depth_avg_ask_5000": walk_book(asks, DEPTH_NOTIONAL_USD, "px", "sz"),
        "depth_avg_bid_5000": walk_book(bids, DEPTH_NOTIONAL_USD, "px", "sz"),
    }


def run() -> int:
    lighter_markets = json.loads(LIGHTER_RESULT_PATH.read_text())["markets"]
    ts = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []

    for ticker in STRONG_TICKERS:
        l_snap = lighter_snapshot(lighter_markets[ticker]["market_id"])
        x_snap = xyz_snapshot(ticker)
        row: dict = {"ts_utc": ts, "symbol": ticker, "lighter": l_snap, "xyz": x_snap}

        if l_snap and x_snap:
            mid_l = (l_snap["best_bid"] + l_snap["best_ask"]) / 2
            mid_x = (x_snap["best_bid"] + x_snap["best_ask"]) / 2
            mid = (mid_l + mid_x) / 2
            if mid_l > mid_x:  # Lighter дороже -> продать(bid) Lighter, купить(ask) xyz
                d_exec = (l_snap["best_bid"] - x_snap["best_ask"]) / mid
                expensive_venue = "lighter"
            else:
                d_exec = (x_snap["best_bid"] - l_snap["best_ask"]) / mid
                expensive_venue = "xyz"
            row["mid_lighter"] = mid_l
            row["mid_xyz"] = mid_x
            row["expensive_venue"] = expensive_venue
            row["d_exec_pct"] = d_exec * 100
            row["d_exec_net_pct"] = (d_exec * 100 - HL_TAKER_FEE_PCT) if HL_TAKER_FEE_PCT is not None else None
        else:
            row["error"] = f"нет реального снимка (lighter={l_snap is not None}, xyz={x_snap is not None})"
        rows.append(row)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "a") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    for row in rows:
        if "d_exec_pct" in row:
            print(f"{row['symbol']}: d_exec={row['d_exec_pct']:.4f}% (дороже: {row['expensive_venue']})")
        else:
            print(f"{row['symbol']}: {row.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
