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
видимость тихой смерти).

Владелец (2026-09-17, продолжение, "Срочно 1"): реальный первый прогон
`select_pairs` дал 0 сопоставленных пар (0 из 1091 игр Kalshi) --
владелец явно разрешил фолбэк "снимать котировки БЕЗ сопоставления".
`select_pairs` теперь пишет ещё `standalone_kalshi`/`standalone_polymarket`
(ликвидные рынки без пары, 0 доп. запросов на отборе) -- этот файл
ТЕПЕРЬ снимает и их, отдельными строками (`row_kind` различает
"pair"/"standalone_kalshi"/"standalone_polymarket"), чтобы сбор не был
пустым, пока сопоставление не восстановлено. Мягкий бюджет времени
(`TIME_BUDGET_S`) -- снимок каждые 5 минут не должен наехать на
следующий тик, честно останавливаемся и пишем, сколько реально успели."""
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
TIME_BUDGET_S = 240.0
CALL_SLEEP_S = 0.2


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

    doc = json.loads(pairs_path.read_text())
    pairs = doc.get("pairs", [])
    standalone_kalshi = doc.get("standalone_kalshi", [])
    standalone_polymarket = doc.get("standalone_polymarket", [])
    out_lines = []
    n_ok, n_err = 0, 0
    start = time.monotonic()
    budget_hit = {"pairs": False, "standalone_kalshi": False, "standalone_polymarket": False}

    for pair in pairs:
        if time.monotonic() - start > TIME_BUDGET_S:
            budget_hit["pairs"] = True
            break
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
            time.sleep(CALL_SLEEP_S)

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
                    time.sleep(CALL_SLEEP_S)

            row = {
                "row_kind": "pair", "snapshot_ts_utc": now, "kalshi_event_ticker": pair.get("kalshi_event_ticker"),
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

    # --- Фолбэк (владелец, "Срочно 1"): ликвидные рынки БЕЗ пары -- снимаем
    # их отдельно, чтобы сбор не был пустым, пока сопоставление не работает.
    for entry in standalone_kalshi:
        if time.monotonic() - start > TIME_BUDGET_S:
            budget_hit["standalone_kalshi"] = True
            break
        ticker = entry.get("ticker")
        if not ticker:
            continue
        kalshi = fetch_kalshi_market(ticker)
        time.sleep(CALL_SLEEP_S)
        row = {
            "row_kind": "standalone_kalshi", "snapshot_ts_utc": now,
            "kalshi_event_ticker": entry.get("kalshi_event_ticker"), "kalshi_ticker": ticker,
            "team": entry.get("team"), "series": entry.get("series"),
            "kalshi_market_raw": kalshi.get("raw"), "kalshi_error": kalshi.get("error"),
        }
        if kalshi.get("error"):
            n_err += 1
        else:
            n_ok += 1
        out_lines.append(json.dumps(row, ensure_ascii=False, default=str))

    for entry in standalone_polymarket:
        if time.monotonic() - start > TIME_BUDGET_S:
            budget_hit["standalone_polymarket"] = True
            break
        raw_ids = entry.get("clobTokenIds_raw")
        try:
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else (raw_ids or [])
        except (json.JSONDecodeError, TypeError):
            token_ids = []
        raw_outcomes = entry.get("outcomes_raw")
        try:
            outcome_names = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else (raw_outcomes or [])
        except (json.JSONDecodeError, TypeError):
            outcome_names = []
        books = []
        any_error = False
        for i, token_id in enumerate(token_ids):
            book = fetch_clob_best_bid_ask(token_id)
            time.sleep(CALL_SLEEP_S)
            outcome_name = outcome_names[i] if i < len(outcome_names) else None
            books.append({"outcome": outcome_name, **book})
            if book.get("error"):
                any_error = True
        row = {
            "row_kind": "standalone_polymarket", "snapshot_ts_utc": now,
            "polymarket_slug": entry.get("slug"), "polymarket_question": entry.get("question"),
            "polymarket_books": books,
        }
        if any_error or not books:
            n_err += 1
        else:
            n_ok += 1
        out_lines.append(json.dumps(row, ensure_ascii=False, default=str))

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir.joinpath(f"{today}.jsonl"), "a") as f:
        for line in out_lines:
            f.write(line + "\n")

    with open(log_path, "a") as f:
        f.write(json.dumps({
            "ts": now, "status": "ok", "n_pairs": len(pairs),
            "n_standalone_kalshi": len(standalone_kalshi), "n_standalone_polymarket": len(standalone_polymarket),
            "n_rows": len(out_lines), "n_ok": n_ok, "n_err": n_err, "budget_hit": budget_hit,
        }) + "\n")
    print(f"[snapshot] пар={len(pairs)} standalone_kalshi={len(standalone_kalshi)} "
          f"standalone_polymarket={len(standalone_polymarket)} строк={len(out_lines)} "
          f"ok={n_ok} err={n_err} budget_hit={budget_hit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
