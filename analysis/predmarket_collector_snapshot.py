#!/usr/bin/env python3
"""Владелец (2026-09-17): сборщик 2, снимок котировок каждые 5 минут.
Читает `data/predmarket_collector/matched_pairs.json` (собран
`predmarket_collector_select_pairs.py`) и по каждой паре снимает:
  - Kalshi: одиночный маркет `/markets/{ticker}` -- реальные поля
    yes_bid_dollars/yes_ask_dollars/last_price(_dollars) СОХРАНЯЮТСЯ
    КАК ЕСТЬ из ответа API (не гадаем точное имя поля -- сохраняем всё,
    что реально пришло, плюс явно вытащенные ключевые поля, если
    присутствуют).
  - Polymarket: CLOB `/book?token_id=` на каждый token id из
    clobTokenIds -- лучший бид/аск из реальной книги; Gamma
    `outcomePrices` -- берём из уже закэшированного объекта пары
    (per-market Gamma-фетч на КАЖДЫЙ снимок был бы избыточен -- цену
    последней сделки Gamma обновляет нечасто, полная книга важнее и
    снимается заново).

Ничего не анализируется. Одна строка на исход на снимок, JSONL, файл на
день: `data/predmarket_collector/YYYY-MM-DD.jsonl`. Каждый запуск --
одна строка в `data/predmarket_collector/run_log.jsonl` (факт запуска,
видимость тихой смерти)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

HEADERS = {"User-Agent": "robinhood-chain-alpha-predmarket-collector/1.0"}
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CLOB_BASE = "https://clob.polymarket.com"
REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
REQUEST_TIMEOUT_S = 15


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def fetch_kalshi_market(ticker: str) -> dict:
    try:
        r = requests.get(f"{KALSHI_BASE}/markets/{ticker}", headers=HEADERS, timeout=REQUEST_TIMEOUT_S)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        body = r.json().get("market", r.json())
        return {"raw": body}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def fetch_clob_best_bid_ask(token_id: str) -> dict:
    try:
        r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, headers=HEADERS, timeout=REQUEST_TIMEOUT_S)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        body = r.json()
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        best_bid = max((float(b["price"]) for b in bids if b.get("price") is not None), default=None)
        best_ask = min((float(a["price"]) for a in asks if a.get("price") is not None), default=None)
        return {"best_bid": best_bid, "best_ask": best_ask, "n_bid_levels": len(bids), "n_ask_levels": len(asks)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def run() -> int:
    root = find_repo_root()
    pairs_path = root.joinpath("data/predmarket_collector/matched_pairs.json")
    out_dir = root.joinpath("data/predmarket_collector")
    log_path = out_dir.joinpath("run_log.jsonl")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    today = time.strftime("%Y-%m-%d", time.gmtime())

    if not pairs_path.exists():
        with open(log_path, "a") as f:
            f.write(json.dumps({"ts": now, "status": "error", "reason": "matched_pairs.json не найден"}) + "\n")
        print("[snapshot] matched_pairs.json не найден -- нечего снимать")
        return 1

    pairs = json.loads(pairs_path.read_text()).get("pairs", [])
    out_lines = []
    n_ok, n_err = 0, 0

    for pair in pairs:
        token_ids_raw = pair.get("polymarket_clobTokenIds_raw")
        try:
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else (token_ids_raw or [])
        except (json.JSONDecodeError, TypeError):
            token_ids = []
        pm_outcomes = pair.get("polymarket_outcomes")
        try:
            outcome_names = json.loads(pm_outcomes) if isinstance(pm_outcomes, str) else (pm_outcomes or [])
        except (json.JSONDecodeError, TypeError):
            outcome_names = []

        for outcome in pair.get("kalshi_outcomes", []):
            ticker = outcome.get("ticker")
            if not ticker:
                continue
            kalshi = fetch_kalshi_market(ticker)
            time.sleep(0.2)

            clob_snapshot = None
            if token_ids:
                idx = None
                team_norm = (outcome.get("team") or "").lower()
                for i, oname in enumerate(outcome_names):
                    if isinstance(oname, str) and team_norm and team_norm in oname.lower():
                        idx = i
                        break
                if idx is not None and idx < len(token_ids):
                    clob_snapshot = fetch_clob_best_bid_ask(token_ids[idx])
                    time.sleep(0.2)

            row = {
                "snapshot_ts_utc": now, "kalshi_event_ticker": pair.get("kalshi_event_ticker"),
                "kalshi_ticker": ticker, "team": outcome.get("team"),
                "polymarket_slug": pair.get("polymarket_slug"),
                "polymarket_gameStartTime": pair.get("polymarket_gameStartTime"),
                "kalshi_market_raw": kalshi.get("raw"), "kalshi_error": kalshi.get("error"),
                "polymarket_clob": clob_snapshot,
            }
            if kalshi.get("error") or (clob_snapshot and clob_snapshot.get("error")):
                n_err += 1
            else:
                n_ok += 1
            out_lines.append(json.dumps(row, ensure_ascii=False, default=str))

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir.joinpath(f"{today}.jsonl"), "a") as f:
        for line in out_lines:
            f.write(line + "\n")

    with open(log_path, "a") as f:
        f.write(json.dumps({"ts": now, "status": "ok", "n_pairs": len(pairs), "n_rows": len(out_lines),
                             "n_ok": n_ok, "n_err": n_err}) + "\n")
    print(f"[snapshot] пар={len(pairs)} строк={len(out_lines)} ok={n_ok} err={n_err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
