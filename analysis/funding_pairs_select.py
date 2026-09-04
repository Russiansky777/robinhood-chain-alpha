#!/usr/bin/env python3
"""Владелец, 2026-09-04: логгер спреда фандинга Lighter <-> Hyperliquid,
п.1 -- отбор пар, ОДИН РАЗ, зафиксировать до сбора.

Lighter -- ТОТ ЖЕ хост, что уже используется для хеджа P5
(api.rh.lighter.xyz, см. analysis/p5_live_precheck.py::LIGHTER_API_BASE)
-- "Lighter on Robinhood Chain", не оригинальный zkLighter mainnet
(mainnet.zklighter.elliot.ai, использован в P4 для другого рынка сток-
перпов -- см. analysis/p4_lighter_markets.py, это ДРУГОЙ деплой).

Hyperliquid: POST /info {"type": "metaAndAssetCtxs"} -- публичный,
без ключа (см. docstring analysis/hyperliquid_jurisdiction_probe.py
про геоблок веб-морды -- REST API отдельно, не проверялся на блок
до этого вызова, проверяем по факту здесь).

Список после записи в data/funding_pairs.json НЕ МЕНЯТЬ (владелец) --
этот скрипт запускается один раз, повторный запуск с force=True нужен
явно, не по умолчанию.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/funding_pairs.json")
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-funding-logger/1.0"}
TOP_N = 15
PRIMARY_N = 3


def normalize_symbol(sym: str) -> str:
    """Убираем суффиксы типа -PERP/-USD, приводим к верхнему регистру --
    для сопоставления тикеров между биржами (владелец: "проверить
    маппинг SOL / SOL-PERP и подобное")."""
    s = sym.upper()
    for suffix in ("-PERP", "-USD", "-USDT", "PERP"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.strip("-_")


def fetch_lighter_markets() -> list[dict]:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"},
                      headers=HEADERS, timeout=20)
    r.raise_for_status()
    body = r.json()
    markets = body.get("order_book_details", body.get("markets", body if isinstance(body, list) else []))
    out = []
    for m in markets:
        symbol = m.get("symbol")
        vol = m.get("daily_quote_token_volume")
        if symbol is None:
            continue
        out.append({
            "raw_symbol": symbol, "normalized_symbol": normalize_symbol(symbol),
            "market_id": m.get("market_id"), "daily_quote_token_volume_usd": float(vol) if vol is not None else None,
            "mark_price": m.get("mark_price"),
        })
    return out


def fetch_hyperliquid_markets() -> list[dict]:
    r = requests.post(f"{HYPERLIQUID_API_BASE}/info", json={"type": "metaAndAssetCtxs"},
                       headers=HEADERS, timeout=20)
    r.raise_for_status()
    meta, asset_ctxs = r.json()
    universe = meta.get("universe", [])
    out = []
    for u, ctx in zip(universe, asset_ctxs):
        symbol = u.get("name")
        if symbol is None:
            continue
        out.append({
            "raw_symbol": symbol, "normalized_symbol": normalize_symbol(symbol),
            "funding_raw": ctx.get("funding"), "mark_px": ctx.get("markPx"),
            "day_ntl_vlm_usd": ctx.get("dayNtlVlm"), "open_interest": ctx.get("openInterest"),
        })
    return out


def run(force: bool = False) -> int:
    if OUT_PATH.exists() and not force:
        print(f"[funding_pairs] {OUT_PATH} уже существует -- список зафиксирован владельцем, "
              "не меняю без force=True. Ничего не сделано.")
        return 0

    t0 = time.time()
    print("=== 1. Рынки Lighter (api.rh.lighter.xyz, тот же хост, что хедж P5) ===")
    lighter_markets = fetch_lighter_markets()
    print(f"[funding_pairs] Lighter: {len(lighter_markets)} рынков")

    print("\n=== 2. Рынки Hyperliquid (info API, публично) ===")
    try:
        hl_markets = fetch_hyperliquid_markets()
        hl_error = None
    except Exception as exc:  # noqa: BLE001
        hl_markets = []
        hl_error = str(exc)
    print(f"[funding_pairs] Hyperliquid: {len(hl_markets)} рынков" + (f" (ОШИБКА: {hl_error})" if hl_error else ""))

    hl_by_norm = {m["normalized_symbol"]: m for m in hl_markets}
    intersection = []
    for lm in lighter_markets:
        hl = hl_by_norm.get(lm["normalized_symbol"])
        if hl is None:
            continue
        intersection.append({
            "symbol": lm["normalized_symbol"],
            "lighter_raw_symbol": lm["raw_symbol"], "lighter_market_id": lm["market_id"],
            "hyperliquid_raw_symbol": hl["raw_symbol"],
            "lighter_daily_volume_usd": lm["daily_quote_token_volume_usd"],
        })
    print(f"\n=== 3. Пересечение тикеров: {len(intersection)} ===")

    intersection.sort(key=lambda x: -(x["lighter_daily_volume_usd"] or 0))
    top = intersection[:TOP_N]
    for i, pair in enumerate(top):
        pair["cohort"] = "primary" if i < PRIMARY_N else "exploratory"
    print(f"[funding_pairs] топ-{TOP_N} по объёму на Lighter, {PRIMARY_N} primary + {len(top) - PRIMARY_N} exploratory:")
    for p in top:
        print(f"  [{p['cohort']}] {p['symbol']}: Lighter ${p['lighter_daily_volume_usd']:,.0f}/сутки "
              f"(lighter='{p['lighter_raw_symbol']}', hl='{p['hyperliquid_raw_symbol']}')")

    result = {
        "selected_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_lighter_markets": len(lighter_markets), "n_hyperliquid_markets": len(hl_markets),
        "hyperliquid_fetch_error": hl_error,
        "n_intersection": len(intersection), "pairs": top,
        "note": "Список ЗАФИКСИРОВАН на этот момент -- не менять, повторный отбор требует force=True (владелец).",
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[funding_pairs] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
