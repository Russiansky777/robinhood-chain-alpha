"""P4, дозапрос владельца (п.3, 2026-09-01): "назвать один стоковый перп
с максимальным часовым объёмом из последнего прогона, его текущие
rate и direction -- чтобы владелец открывал позиции именно там."

Целевой рынок выбран ДО этого скрипта, по уже опубликованным
агрегатам последнего полного прогона (`data/p4_lighter_cache/
p4_lighter_markets_result.json`, поле `hourly_volume_proxy_usd` =
`daily_quote_token_volume / 24`, см. `analysis/p4_lighter_markets.py`) --
SNDK (market_id=139), $933,214.74/час прокси-объёма, почти втрое выше
второго места (SPCX, $333,579.72/час). Не додумано -- прямое сравнение
всех 36 успешно опрошенных рынков, см. docs/P4_RECON.md.

Этот скрипт делает ТОЛЬКО 2 лёгких публичных REST-запроса (0 Dune, без
ключа) для ОДНОГО рынка -- не повторяет полный 45-рыночный прогон
`p4_lighter_markets.py` (который уже упирался в 429 на части рынков
из-за объёма запросов):
  1. GET /api/v1/orderBookDetails?filter=all -> находим market_id=139,
     печатаем СЫРУЮ запись целиком (диагностика -- есть ли в метаданных
     рынка поле текущей funding-ставки помимо истории /fundings).
  2. GET /api/v1/fundings?market_id=139&resolution=1h&count_back=5 ->
     последние (самые свежие) часовые записи -- "текущая" ставка в
     терминах этого API это последняя ЗАКРЫТАЯ часовая запись (сам API
     не даёт "живого тика посреди часа", только агрегаты по
     resolution=1h, см. analysis/p4_lighter_markets.py).

Результат -- ТОЛЬКО в лог (для интерактивной сессии, egress которой
блокирует mainnet.zklighter.elliot.ai) + JSON-кэш
data/p4_lighter_cache/p4_current_snapshot_<symbol>.json. Не пишет
Markdown-секцию сам -- это разбирается вручную после реального
результата (в отличие от p4_lighter_markets.py, здесь единичный
точечный запрос, не повторяемый регулярный прогон).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://mainnet.zklighter.elliot.ai"
HEADERS = {"User-Agent": "robinhood-chain-alpha-p4-snapshot/1.0"}
CACHE_DIR = Path("data/p4_lighter_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TARGET_MARKET_ID = 139
TARGET_SYMBOL = "SNDK"


def _get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(f"{BASE_URL}{path}", params=params or {}, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def run() -> int:
    now = int(time.time())
    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"[p4_snapshot] {now_iso} -- целевой рынок {TARGET_SYMBOL} (market_id={TARGET_MARKET_ID})")

    body = _get("/api/v1/orderBookDetails", {"filter": "all"})
    markets = body.get("order_book_details", [])
    market = next((m for m in markets if m.get("market_id") == TARGET_MARKET_ID), None)
    if market is None:
        print(f"[p4_snapshot] СТОП: market_id={TARGET_MARKET_ID} не найден в текущем списке "
              f"({len(markets)} рынков всего) -- рынок мог быть делистингован/переименован.")
        market = {}
    else:
        print(f"[p4_snapshot] сырая запись market metadata: {json.dumps(market, indent=2, default=str)}")

    fbody = _get("/api/v1/fundings", {
        "market_id": TARGET_MARKET_ID,
        "resolution": "1h",
        "start_timestamp": now - 2 * 86400,
        "end_timestamp": now,
        "count_back": 5,
    })
    fundings = fbody.get("fundings", [])
    fundings_sorted = sorted(fundings, key=lambda r: r.get("timestamp", 0))
    print(f"[p4_snapshot] последние {len(fundings_sorted)} часовых записей funding: "
          f"{json.dumps(fundings_sorted, indent=2, default=str)}")

    latest = fundings_sorted[-1] if fundings_sorted else None
    if latest:
        try:
            rate_raw = float(latest.get("rate"))
            annualized_pct = round(rate_raw * 24 * 365 * 100, 4)
        except (TypeError, ValueError):
            rate_raw = None
            annualized_pct = None
        ts = latest.get("timestamp")
        ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
        print(f"[p4_snapshot] ПОСЛЕДНЯЯ закрытая часовая запись: timestamp={ts} ({ts_iso}), "
              f"rate(сырое)={latest.get('rate')}, direction={latest.get('direction')}, "
              f"аннуализация (rate*24*365)={annualized_pct}%")
    else:
        print("[p4_snapshot] ВНИМАНИЕ: 0 записей funding за последние 2 дня для этого рынка.")

    out = {
        "generated_at": now_iso,
        "target_symbol": TARGET_SYMBOL,
        "target_market_id": TARGET_MARKET_ID,
        "market_metadata_raw": market,
        "recent_fundings_raw": fundings_sorted,
        "latest_funding": latest,
    }
    out_path = CACHE_DIR / f"p4_current_snapshot_{TARGET_SYMBOL}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"[p4_snapshot] записано {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
