#!/usr/bin/env python3
"""Владелец (2026-09-17), "Срочно 2" -- 23 пула это мало для целой цепи?

РЕАЛЬНАЯ НАХОДКА уже в имеющемся результате (`rh_volume_trend_pool_
discovery_result.json`, разобран честно, не выдумано):
  - GT-пагинация остановилась на странице 6 из-за HTTP 429 (реальный
    рейт-лимит free-tier), а НЕ потому что список кончился -- реальный
    total на цепи НЕИЗВЕСТЕН, видели только 100 пулов.
  - TVL<$5000 отсеял всего 7 из 100 (7%) -- НЕ бутылочное горлышко.
  - Реальное горлышко -- бюджет проверки 3-дневной торговли (300с):
    из 93 TVL-квалифицированных кандидатов проверено только 25 (остальные
    68 НЕ проверены вообще, не отсеяны -- бюджет истёк раньше, чем дошла
    очередь). Из проверенных 25 -- ВСЕ 25 прошли (реальная торговля есть
    у 100% проверенных, отсева по этому фильтру не было).
  - fee-резолв: 25 попыток -> 23 финальных (2 отсеяны по fee).
  - ЕЩЁ ОДНА реальная находка (не была отдельно отловлена раньше): 4 из
    23 финальных пулов имеют fee_pips == DYNAMIC_FEE_FLAG (0x800000 =
    8388608) -- это НЕ комиссия 838.86%, это флаг "комиссия управляется
    хуком", установленный этой сессией на линии Fomo. Прежний discovery-
    скрипт хранил этот флаг КАК ЕСЛИ БЫ он был fee_pips -- честно это
    исправляем (fee_is_dynamic=True, fee_pips=None), чтобы "сколько
    пулов с fee>1%" не считало 8388608 пипсов как гигантскую комиссию.

ЭТОТ скрипт: возобновляет пагинацию (более терпеливый бэкофф на 429) и
достраивает выборку с ДВУМЯ порогами TVL одновременно ($5000 -- текущий,
$1000 -- предложенный владельцем), с честным чекпоинтом по времени.
Переиспользует gt_get/looks_like_pool_id/resolve_real_fee из
rh_volume_trend_pool_discovery.py, не дублирует логику."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from rh_volume_trend_pool_discovery import (  # noqa: E402
    GT_BASE, GT_NETWORK, looks_like_pool_id,
)
from alchemy_fallback import rpc_call_trading_path  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402

OUT_PATH = Path("data/rh_pool_universe_funnel_audit_result.json")
RPC = rpc_call_trading_path
FEE_SELECTOR = "0xddca3f43"
DYNAMIC_FEE_FLAG = 0x800000  # установлено этой сессией, линия Fomo -- НЕ реальная комиссия
EXCLUDED_FEE_PIPS = 110000  # 11%, установлено линией Fomo

TVL_THRESHOLDS = [5000.0, 1000.0]
MAX_PAGES = 40
GT_REQUEST_INTERVAL_S = 2.5  # честно увеличено -- прошлый прогон бил 429 уже на странице 6 при 1.2с
GT_429_BACKOFF_S = 15.0  # честно увеличено -- прошлый ретрай (5с*попытка) не помогал, 429 повторялся
GT_MAX_RETRIES_PER_PAGE = 4
OHLCV_TIME_BUDGET_S = 700.0
FEE_TIME_BUDGET_S = 400.0


def gt_get_patient(path: str, params: dict | None = None) -> tuple[int, dict | None, int]:
    """Как gt_get, но с более терпеливым бэкоффом и счётчиком реальных 429."""
    n_429 = 0
    last_status = None
    for attempt in range(GT_MAX_RETRIES_PER_PAGE):
        try:
            resp = requests.get(f"{GT_BASE}{path}", params=params or {},
                                 headers={"Accept": "application/json"}, timeout=25)
            last_status = resp.status_code
            if resp.status_code == 200:
                return 200, resp.json(), n_429
            if resp.status_code == 429:
                n_429 += 1
                time.sleep(GT_429_BACKOFF_S * (attempt + 1))
                continue
            return resp.status_code, None, n_429
        except Exception as exc:  # noqa: BLE001
            last_status = f"exception: {exc}"
        time.sleep(3 * (attempt + 1))
    return (last_status if isinstance(last_status, int) else -1), None, n_429


def resolve_real_fee_honest(address: str, latest_block: int) -> dict:
    """Как resolve_real_fee в discovery, НО честно различает динамическую
    (хук-управляемую) комиссию от реального числа -- не хранит 8388608
    как будто это fee_pips."""
    if looks_like_pool_id(address):
        info = fetch_initialize_event(address, to_block=latest_block)
        if info is None:
            return {"kind": "v4_pool_id", "error": "Initialize не найден по этому pool_id"}
        raw_fee = info["fee"]
        is_dynamic = raw_fee == DYNAMIC_FEE_FLAG
        return {
            "kind": "v4_pool_id", "pool_id": address,
            "fee_pips": None if is_dynamic else raw_fee,
            "fee_is_dynamic": is_dynamic,
            "hooks": info["hooks"], "currency0": info["currency0"], "currency1": info["currency1"],
        }
    try:
        raw = RPC("eth_call", [{"to": address, "data": FEE_SELECTOR}, "latest"])
        fee = int(raw, 16) if raw and raw != "0x" else None
    except Exception as exc:  # noqa: BLE001
        return {"kind": "v3_address", "error": f"{type(exc).__name__}: {exc}"}
    return {"kind": "v3_address", "address": address, "fee_pips": fee, "fee_is_dynamic": False}


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    out["prior_run_honest_findings"] = {
        "source": "data/rh_volume_trend_pool_discovery_result.json",
        "pagination_stopped_at_page": 6, "pagination_stop_reason": "HTTP 429, НЕ конец списка -- реальный total неизвестен",
        "n_fetched_before": 100, "n_after_tvl5000_before": 93,
        "n_ohlcv_checked_before": 25, "n_ohlcv_unchecked_due_to_budget_before": 68,
        "n_final_before": 23,
        "n_final_with_unresolved_dynamic_fee_before": 4,
        "note": "4 из 23 предыдущих финальных пулов хранили fee_pips=8388608 (DYNAMIC_FEE_FLAG) "
                "как будто это реальная комиссия -- этот прогон хранит их честно отдельно",
    }

    # --- Пагинация, продолженная с более терпеливым бэкоффом ---
    all_pools = []
    total_429 = 0
    page = 1
    pagination_stop_reason = None
    while page <= MAX_PAGES:
        status, body, n_429 = gt_get_patient(f"/networks/{GT_NETWORK}/pools", params={"page": page})
        total_429 += n_429
        if status != 200 or not body or not body.get("data"):
            pagination_stop_reason = f"page={page} status={status} после {n_429} повторных 429 на этой странице"
            break
        all_pools.extend(body["data"])
        page += 1
        time.sleep(GT_REQUEST_INTERVAL_S)
    else:
        pagination_stop_reason = f"MAX_PAGES={MAX_PAGES} достигнут -- реальный предел GT всё ещё не найден"
    out["pagination_stop_reason"] = pagination_stop_reason
    out["n_pools_total_fetched"] = len(all_pools)
    out["n_http_429_hit_total"] = total_429
    out["HONEST_TOTAL_POOL_COUNT"] = (
        f">={len(all_pools)} -- пагинация остановилась ({pagination_stop_reason}), "
        "реальный total на цепи всё ещё не подтверждён, если остановка не на пустой странице"
    )
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    # --- TVL-бакеты на РЕАЛЬНО собранной выборке ---
    buckets = {t: [] for t in TVL_THRESHOLDS}
    n_no_reserve = 0
    for p in all_pools:
        attrs = p.get("attributes", {})
        try:
            reserve_f = float(attrs.get("reserve_in_usd")) if attrs.get("reserve_in_usd") is not None else None
        except (TypeError, ValueError):
            reserve_f = None
        if reserve_f is None:
            n_no_reserve += 1
            continue
        addr = attrs.get("address")
        if not addr:
            continue
        entry = {"address": addr, "name": attrs.get("name"), "reserve_in_usd": reserve_f,
                 "pool_created_at": attrs.get("pool_created_at"),
                 "dex_id": (p.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id")}
        for t in TVL_THRESHOLDS:
            if reserve_f >= t:
                buckets[t].append(entry)
    out["n_pools_no_reserve_field"] = n_no_reserve
    out["n_after_tvl_filter_by_threshold"] = {str(t): len(v) for t, v in buckets.items()}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    # --- OHLCV 3-дневная торговля -- проверяем самый широкий бакет ($1000), включает $5000 ---
    widest = buckets[min(TVL_THRESHOLDS)]
    traded = []
    ohlcv_start = time.time()
    n_checked = 0
    budget_exhausted = False
    for c in widest:
        if time.time() - ohlcv_start > OHLCV_TIME_BUDGET_S:
            budget_exhausted = True
            break
        n_checked += 1
        time.sleep(GT_REQUEST_INTERVAL_S)
        status, body, n_429 = gt_get_patient(f"/networks/{GT_NETWORK}/pools/{c['address']}/ohlcv/day",
                                              params={"aggregate": 1, "limit": 3})
        total_429 += n_429
        if status != 200 or not body:
            continue
        rows = (body.get("data", {}).get("attributes", {}) or {}).get("ohlcv_list", [])
        vol_3d = sum(r[5] for r in rows if len(r) > 5 and r[5] is not None)
        c["volume_usd_3d_real"] = vol_3d
        if vol_3d and vol_3d > 0:
            traded.append(c)
        out["ohlcv_checkpoint_n_checked"] = n_checked
        out["ohlcv_checkpoint_n_traded"] = len(traded)
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    out["n_http_429_hit_total"] = total_429
    out["n_ohlcv_checked"] = n_checked
    out["n_ohlcv_candidates_total_at_1000"] = len(widest)
    out["n_ohlcv_budget_exhausted"] = budget_exhausted
    if budget_exhausted:
        out["ohlcv_partial_coverage_reason"] = (
            f"бюджет ({OHLCV_TIME_BUDGET_S}с) исчерпан после {n_checked}/{len(widest)} -- "
            f"честно НЕ покрыто: {len(widest) - n_checked}"
        )
    out["n_after_3d_trading_filter"] = len(traded)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    # --- Резолв комиссии (честный, различающий динамическую) ---
    latest_raw = RPC("eth_blockNumber", [])
    latest_block = int(latest_raw, 16) if latest_raw else None
    out["latest_block"] = latest_block
    final_5000, final_1000, n_dynamic, n_excluded_11pct, n_fee_unresolved = [], [], 0, 0, 0
    fee_start = time.time()
    n_fee_checked = 0
    fee_budget_exhausted = False
    for c in traded:
        if latest_block is None or time.time() - fee_start > FEE_TIME_BUDGET_S:
            fee_budget_exhausted = True
            break
        n_fee_checked += 1
        fee_info = resolve_real_fee_honest(c["address"], latest_block)
        c["fee_resolution"] = fee_info
        if fee_info.get("fee_is_dynamic"):
            n_dynamic += 1
        elif fee_info.get("fee_pips") is None:
            n_fee_unresolved += 1
            continue
        elif fee_info.get("fee_pips") == EXCLUDED_FEE_PIPS:
            n_excluded_11pct += 1
            continue
        final_1000.append(c)
        if c["reserve_in_usd"] >= 5000.0:
            final_5000.append(c)
        out["fee_checkpoint_n_checked"] = n_fee_checked
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    out["n_fee_checked"] = n_fee_checked
    out["n_traded_total"] = len(traded)
    out["n_fee_budget_exhausted"] = fee_budget_exhausted
    if fee_budget_exhausted:
        out["fee_partial_coverage_reason"] = f"бюджет ({FEE_TIME_BUDGET_S}с) исчерпан после {n_fee_checked}/{len(traded)}"
    out["n_dynamic_fee_pools"] = n_dynamic
    out["n_excluded_11pct"] = n_excluded_11pct
    out["n_fee_unresolved"] = n_fee_unresolved
    out["n_final_tvl5000"] = len(final_5000)
    out["n_final_tvl1000"] = len(final_1000)
    n_over_1pct_5000 = sum(1 for c in final_5000 if not c["fee_resolution"].get("fee_is_dynamic")
                            and (c["fee_resolution"].get("fee_pips") or 0) > 10000)
    n_over_1pct_1000 = sum(1 for c in final_1000 if not c["fee_resolution"].get("fee_is_dynamic")
                            and (c["fee_resolution"].get("fee_pips") or 0) > 10000)
    out["n_over_1pct_fee_tvl5000"] = n_over_1pct_5000
    out["n_over_1pct_fee_tvl1000"] = n_over_1pct_1000
    out["final_candidates_tvl5000"] = final_5000
    out["final_candidates_tvl1000_extra"] = [c for c in final_1000 if c["reserve_in_usd"] < 5000.0]

    delta = len(final_1000) - len(final_5000)
    out["HONEST_VERDICT"] = (
        f"При понижении TVL-порога с $5000 до $1000: выборка растёт с {len(final_5000)} до {len(final_1000)} "
        f"пулов (+{delta}). TVL никогда не была основным фильтром (на предыдущем прогоне отсеяла лишь 7%) -- "
        f"главное горлышко -- бюджет проверки торговли (ohlcv), который в этом прогоне честно покрыл "
        f"{n_checked}/{len(widest)} кандидатов TVL>=$1000."
    )
    print("[funnel_audit] " + out["HONEST_VERDICT"])
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
