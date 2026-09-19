#!/usr/bin/env python3
"""Владелец, 2026-09-19: бесплатная (без Dune) диагностика "ровных
нулей" в Фазе 3. Список из 20 событий с ровно нулевым ростом (+30с) и
5 полностью пропавших событий дня 09-19 уже отобран локально
(data/solana_phase3_zero_diag_targets.json, скрипт
solana_phase3_prices_and_answers_v3.build_sample + сырые цены). Здесь
только сетевая часть, требующая интернета (заблокирован в песочнице
политикой egress) -- через GitHub Actions.

Для каждого минта: список пулов GeckoTerminal (публичный API, без
ключа) с ликвидностью и котируемым активом (WSOL/USDC/иное) -- честно
проверяем гипотезу владельца "может, торговля идёт не в WSOL, а Dune-
джойн ловит только WSOL-пары". Плюс минутные OHLCV топ-пула вокруг
целевого окна (t0-300..t0+180) -- смотрим объём (volume) в барах,
попадающих в (t0+5, t0+180] -- если объём есть, а Dune ничего не
нашёл -- подозрение на реальный пропуск в dex_solana.trades (не
обязательно из-за валюты пары, если пул как раз WSOL); если объём тоже
пуст -- "ровный ноль" реален, не баг.

НЕ угадываем: если GeckoTerminal не дотягивается так далеко назад
(история слишком старая для минутных свечей конкретного пула) --
честно помечаем недоступность, не выдаём молчание за подтверждение."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_PATH = REPO_ROOT / "data" / "solana_phase3_zero_diag_targets.json"
OUT_PATH = REPO_ROOT / "data" / "solana_phase3_zero_diag_gecko_result.json"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def gecko_get(path: str, params: dict) -> dict:
    backoff = 1.0
    last: dict = {"http_status": None}
    for _ in range(8):
        try:
            resp = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30,
                                 headers={"Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            last = {"http_status": None, "exception": str(exc)[:200]}
            time.sleep(backoff)
            backoff = min(backoff * 2, 20)
            continue
        if resp.status_code == 429:
            time.sleep(backoff)
            backoff = min(backoff * 2, 20)
            continue
        return {"http_status": resp.status_code, "body": resp.json() if resp.ok else None}
    return last


def quote_label(mint: str) -> str:
    return {WSOL_MINT: "WSOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}.get(mint, mint[:10] + "..")


def get_pools(mint: str) -> list[dict]:
    r = gecko_get(f"/networks/solana/tokens/{mint}/pools", {})
    data = (r.get("body") or {}).get("data") if r.get("http_status") == 200 else None
    if not data:
        return []
    out = []
    for p in data:
        attrs = p.get("attributes") or {}
        rel = p.get("relationships") or {}
        base_id = (((rel.get("base_token") or {}).get("data") or {}).get("id") or "")
        quote_id = (((rel.get("quote_token") or {}).get("data") or {}).get("id") or "")
        base_mint = base_id.split("_")[-1] if base_id else None
        quote_mint = quote_id.split("_")[-1] if quote_id else None
        out.append({"address": attrs.get("address"), "name": attrs.get("name"),
                     "reserve_in_usd": attrs.get("reserve_in_usd"),
                     "base_mint": base_mint, "quote_mint": quote_mint,
                     "quote_label": quote_label(quote_mint) if quote_mint else None,
                     "pool_created_at": attrs.get("pool_created_at")})
    out.sort(key=lambda p: float(p.get("reserve_in_usd") or 0), reverse=True)
    return out


def ohlcv_volume_in_window(pool_addr: str, lo_epoch: int, hi_epoch: int) -> dict:
    r = gecko_get(f"/networks/solana/pools/{pool_addr}/ohlcv/minute",
                  {"aggregate": 1, "before_timestamp": hi_epoch + 120, "limit": 30, "currency": "usd"})
    if r.get("http_status") != 200:
        return {"available": False, "http_status": r.get("http_status")}
    rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    in_window = [c for c in rows if lo_epoch <= c[0] < hi_epoch]
    total_vol = sum(c[5] for c in in_window)
    oldest_bar = min((c[0] for c in rows), default=None)
    return {"available": True, "n_bars_returned": len(rows), "n_bars_in_target_window": len(in_window),
            "volume_usd_in_target_window": total_vol,
            "oldest_bar_reached_target_lo": (oldest_bar is not None and oldest_bar <= lo_epoch),
            "oldest_bar_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(oldest_bar)) if oldest_bar else None}


def diagnose_event(ev: dict, lo_key: str, hi_key: str) -> dict:
    mint = ev["mint"]
    pools = get_pools(mint)
    lo, hi = ev.get(lo_key), ev.get(hi_key)
    lo_epoch = ev["block_time_epoch"] + 5 if "block_time_epoch" in ev else None
    out = {"event_id": ev["event_id"], "mint": mint, "is_leader": ev.get("is_leader"),
           "spend_sol_equiv": ev.get("spend_sol_equiv"), "window": [lo, hi],
           "n_pools_found": len(pools), "pools": pools[:5]}
    has_wsol_pool = any(p["quote_mint"] == WSOL_MINT or p["base_mint"] == WSOL_MINT for p in pools)
    out["has_wsol_pool"] = has_wsol_pool
    out["top_pool_is_wsol"] = bool(pools) and (pools[0]["quote_mint"] == WSOL_MINT or pools[0]["base_mint"] == WSOL_MINT)
    if pools:
        top = pools[0]
        window_lo_epoch = time.strptime(lo, "%Y-%m-%dT%H:%M:%SZ")
        window_hi_epoch = time.strptime(hi, "%Y-%m-%dT%H:%M:%SZ")
        import calendar
        lo_e, hi_e = calendar.timegm(window_lo_epoch), calendar.timegm(window_hi_epoch)
        out["top_pool_ohlcv_check"] = ohlcv_volume_in_window(top["address"], lo_e, hi_e)
    return out


def main() -> None:
    targets = json.loads(IN_PATH.read_text())
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "zero_growth_20": [], "day_19_missing_5": []}
    for i, ev in enumerate(targets["zero_growth_20"]):
        print(f"[zero_diag_gecko] zero_growth {i+1}/20 mint={ev['mint'][:10]}..", flush=True)
        d = diagnose_event(ev, "window_lo_utc", "window_hi_utc")
        result["zero_growth_20"].append(d)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    for i, ev in enumerate(targets["day_19_missing_5"]):
        print(f"[zero_diag_gecko] day19_missing {i+1}/5 mint={ev['mint'][:10]}..", flush=True)
        d = diagnose_event(ev, "window_lo_utc", "window_hi_utc")
        result["day_19_missing_5"].append(d)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    n_no_wsol_pool = sum(1 for d in result["zero_growth_20"] if not d["has_wsol_pool"])
    n_top_not_wsol = sum(1 for d in result["zero_growth_20"] if not d["top_pool_is_wsol"])
    n_real_volume_in_window = sum(
        1 for d in result["zero_growth_20"]
        if (d.get("top_pool_ohlcv_check") or {}).get("volume_usd_in_target_window", 0) > 0
    )
    result["SUMMARY"] = {
        "n_events": len(result["zero_growth_20"]),
        "n_no_wsol_pool_at_all": n_no_wsol_pool,
        "n_top_liquidity_pool_not_wsol": n_top_not_wsol,
        "n_with_real_gecko_volume_in_the_zero_window": n_real_volume_in_window,
        "note": "n_with_real_gecko_volume_in_the_zero_window>0 значит GeckoTerminal видит реальный объём "
                "именно в окне, где Dune насчитал ровный ноль -- это подозрение на пропуск в dex_solana.trades "
                "(не обязательно из-за валюты пары, если топ-пул как раз WSOL, тогда это отдельный вопрос "
                "к самой таблице/материализации). Если объёма и там, и там нет -- ровный ноль реален.",
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[zero_diag_gecko] ИТОГ: {result['SUMMARY']}", flush=True)


if __name__ == "__main__":
    main()
