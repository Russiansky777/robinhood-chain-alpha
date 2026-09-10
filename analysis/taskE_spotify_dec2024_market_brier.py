#!/usr/bin/env python3
"""Задача E владельца (2026-09-10), пункт 6 -- дёшево, БЕЗ LLM.

Проверка гипотезы "структурная слепота толпы в нише Spotify-чартов":
берём реальные Polymarket-рынки "#1 song this week" за декабрь 2024
(если существовали), их РЕАЛЬНУЮ цену за ~7 дней до разрешения и
РЕАЛЬНЫЙ исход, считаем Brier score САМОГО РЫНКА (не LLM -- LLM здесь
вообще не участвует, поэтому структурное разделение по времени не
нужно: некому и нечему "утекать" в память модели, если модель не
делает никакой оценки). Если рыночный Brier снова около ~0.2 (как в
Шаге 2 доказательного прогона для Spotify-кластера) -- это воспроизводимая
из года в год слепота рынка в этой нише, самостоятельный кандидат для
будущей стратегии, а не шум одного случая."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskE-spotify-dec2024/1.0"}
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

WINDOW_MIN = datetime(2024, 11, 15, tzinfo=timezone.utc)  # с запасом до начала декабря
WINDOW_MAX = datetime(2025, 1, 5, tzinfo=timezone.utc)  # с запасом после конца декабря
N_DAYS_BEFORE_RESOLUTION = 7

OUT_PATH = Path("data/p3_guard_cache/taskE_spotify_dec2024_market_brier_result.json")


def fetch_closed_spotify_markets() -> tuple[list[dict], dict]:
    out = {}
    window_min = WINDOW_MIN.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_max = WINDOW_MAX.strftime("%Y-%m-%dT%H:%M:%SZ")
    n_pages = 0
    stop_reason = None
    for offset in range(0, 30 * 100, 100):
        r = requests.get(f"{GAMMA_BASE}/markets", params={
            "limit": 100, "offset": offset, "closed": "true",
            "order": "endDate", "ascending": "true",
            "end_date_min": window_min, "end_date_max": window_max,
        }, headers=HEADERS, timeout=30)
        n_pages += 1
        if r.status_code != 200:
            stop_reason = f"http_status_{r.status_code}_at_page_{n_pages}"
            break
        body = r.json()
        if not isinstance(body, list) or not body:
            stop_reason = f"empty_body_at_page_{n_pages}"
            break
        for m in body:
            q = str(m.get("question", "")).lower()
            if "spotify" in q and "1 song" in q:
                out[m["slug"]] = m
        if len(body) < 100:
            stop_reason = f"short_page_at_page_{n_pages}"
            break
        time.sleep(0.15)
    else:
        stop_reason = "max_pages_reached"
    return list(out.values()), {"n_pages": n_pages, "stop_reason": stop_reason, "n_matched": len(out)}


def fetch_price_snapshot(clob_token_id: str, end_date: datetime) -> float | None:
    window_start_ts = int((end_date - timedelta(days=14)).timestamp())
    window_end_ts = int((end_date - timedelta(days=3)).timestamp())
    preferred_ts = int((end_date - timedelta(days=N_DAYS_BEFORE_RESOLUTION)).timestamp())
    try:
        r = requests.get(f"{CLOB_BASE}/prices-history", params={
            "market": clob_token_id, "startTs": window_start_ts, "endTs": window_end_ts, "fidelity": 60,
        }, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        history = r.json().get("history", [])
        if not history:
            return None
        best = min(history, key=lambda pt: abs(pt.get("t", preferred_ts) - preferred_ts))
        return float(best["p"])
    except Exception:  # noqa: BLE001
        return None


def fetch_actual_outcome(slug: str) -> float | None:
    """Реальный исход запрашивается ЗДЕСЬ намеренно -- в этом скрипте
    LLM не участвует вообще, оцениваемого мнения не существует, поэтому
    структурное разделение "код не видит исход" не нужно: разделять
    нечего, это не блайнд-тест, а прямой замер рыночной калибровки."""
    r = requests.get(f"{GAMMA_BASE}/markets", params={"slug": slug, "closed": "true"}, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return None
    body = r.json()
    if not isinstance(body, list) or not body:
        return None
    raw = body[0].get("outcomePrices")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        return float(prices[0]) if prices else None
    except (json.JSONDecodeError, TypeError, ValueError, IndexError):
        return None


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "window_min": WINDOW_MIN.isoformat(), "window_max": WINDOW_MAX.isoformat()}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    markets, fetch_diag = fetch_closed_spotify_markets()
    result["fetch_diagnostics"] = fetch_diag
    print(f"[taskE_spotify_dec2024] найдено Spotify '#1 song' рынков в окне: {len(markets)}")

    if not markets:
        result["blocker"] = "Ни одного Spotify '#1 song' рынка не найдено в окне ноя2024-янв2025 -- кандидат закрывается: данных нет."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"[taskE_spotify_dec2024] {result['blocker']}")
        return 0

    n_price_ok = 0
    n_outcome_ok = 0
    sq_errs = []
    for m in markets:
        end_date_str = m.get("endDate")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            continue
        tokens_raw = m.get("clobTokenIds")
        try:
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except (json.JSONDecodeError, TypeError):
            tokens = None
        if not tokens:
            continue
        price = fetch_price_snapshot(tokens[0], end_date)
        time.sleep(0.12)
        if price is None:
            continue
        n_price_ok += 1
        actual = fetch_actual_outcome(m["slug"])
        time.sleep(0.12)
        if actual is None:
            continue
        n_outcome_ok += 1
        sq_errs.append((price - actual) ** 2)

    result["n_markets_total"] = len(markets)
    result["n_price_snapshot_ok"] = n_price_ok
    result["n_outcome_ok"] = n_outcome_ok
    result["n_scored"] = len(sq_errs)
    result["market_brier_score"] = (sum(sq_errs) / len(sq_errs)) if sq_errs else None
    result["note"] = ("Только рыночная калибровка (Brier рынка против фактического исхода) -- "
                       "LLM не участвует, сравнивать не с чем, это НЕ блайнд-тест.")

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[taskE_spotify_dec2024] n_scored={len(sq_errs)}, рыночный Brier={result['market_brier_score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
