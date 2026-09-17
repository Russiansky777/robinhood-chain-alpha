#!/usr/bin/env python3
"""Владелец (2026-09-17), Задача 3 -- тренд по цепи, этап A (открытие
пулов). ДО построения полного конвейера (почасовой оборот через
extsload feeGrowthGlobal, детекция разгона, форвард-цена, lead/lag,
издержки) -- честная проба: как GeckoTerminal вообще представляет V4
пулы сети `robinhood` (singleton PoolManager, у пула нет своего
контрактного адреса) -- поле `address` в ответе GT может быть либо
реальным адресом (V3-стиль дексов вроде `uniswap-v3-robinhood`), либо
чем-то иным для `uniswap-v4-robinhood`/`bankr-robinhood` (сами эти
dex-слаги на сети `robinhood` уже видели в этой сессии, см. паспорт,
раздел RWA-поиска). Строить конвейер на угаданном формате -- значит
рисковать всем расчётом на неверных pool_id.

ПОРЯДОК (честно, по шагам, с ранним остановом при провале):
  0. Проба: GET /networks/robinhood/pools?page=1 -- реальные поля
     ответа, dex-слаги, формат `address`.
  1. Пагинация (реальный предел страниц -- у free-tier GeckoTerminal
     обычно до 10 страниц по ~20 пулов = ~200 -- проверяем эмпирически,
     останавливаемся на первой пустой странице/ошибке).
  2. Фильтр TVL >= $5000 (поле `reserve_in_usd`, реальное, без
     доп. вызовов).
  3. Фильтр "реальная торговля за 3 суток": OHLCV день (`/ohlcv/day
     ?limit=3`) на кандидата, сумма объёма > 0 -- НЕ используем только
     `volume_usd.h24` из листинга, владелец явно просил "3 суток".
  4. Резолв реальной комиссии пула:
       - если `address` -- реальный 20-байтный адрес контракта (V3-
         стиль дексов) -- прямой eth_call fee()/slot0() на САМ этот
         адрес;
       - если `address` выглядит как 32-байтный pool_id (V4 singleton)
         -- targeted fetch_initialize_event(pool_id) (уже
         провалидированная функция, task5_v4_hook_route_audit.py),
         НЕ поиск заново.
     Оба пути читают РЕАЛЬНУЮ комиссию с цепи, не из GT (GT её не
     публикует структурированно для всех дексов).
  5. Исключить fee == 110000 (11%, установлено линией Fomo -- высокая
     комиссия убивает любую краткосрочную стратегию).
  6. Если итоговых кандидатов < 5 -- честно сказать и остановиться
     (само по себе ответ, как просил владелец), не выдумывать данные.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402

GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_NETWORK = "robinhood"
OUT_PATH = Path("data/rh_volume_trend_pool_discovery_result.json")

TVL_MIN_USD = 5000.0
EXCLUDED_FEE_PIPS = 110000
MAX_PAGES = 10
GT_REQUEST_INTERVAL_S = 1.2  # честная задержка -- публичный free-tier GT, без ключа
RPC = rpc_call_trading_path
FEE_SELECTOR = "0xddca3f43"  # fee() -- стандартный getter Uniswap V3 pool
SLOT0_SELECTOR = "0x3850c7bd"


def gt_get(path: str, params: dict | None = None, max_retries: int = 3) -> tuple[int, dict | None]:
    last_status = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(f"{GT_BASE}{path}", params=params or {},
                                 headers={"Accept": "application/json"}, timeout=25)
            last_status = resp.status_code
            if resp.status_code == 200:
                return 200, resp.json()
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return resp.status_code, None
        except Exception as exc:  # noqa: BLE001
            last_status = f"exception: {exc}"
        time.sleep(2 * (attempt + 1))
    return (last_status if isinstance(last_status, int) else -1), None


def looks_like_pool_id(addr: str) -> bool:
    h = addr[2:] if addr.startswith("0x") else addr
    return len(h) == 64  # 32 байта -- V4 pool_id, не адрес контракта (20 байт=40 hex)


def resolve_real_fee(address: str, latest_block: int) -> dict:
    if looks_like_pool_id(address):
        info = fetch_initialize_event(address, to_block=latest_block)
        if info is None:
            return {"kind": "v4_pool_id", "error": "Initialize не найден по этому pool_id"}
        return {"kind": "v4_pool_id", "pool_id": address, "fee_pips": info["fee"], "hooks": info["hooks"],
                "currency0": info["currency0"], "currency1": info["currency1"]}
    try:
        raw = RPC("eth_call", [{"to": address, "data": FEE_SELECTOR}, "latest"])
        fee = int(raw, 16) if raw and raw != "0x" else None
    except Exception as exc:  # noqa: BLE001
        return {"kind": "v3_address", "error": f"{type(exc).__name__}: {exc}"}
    return {"kind": "v3_address", "address": address, "fee_pips": fee}


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # --- Шаг 0: проба ---
    status0, body0 = gt_get(f"/networks/{GT_NETWORK}/pools", params={"page": 1})
    out["probe_page1_status"] = status0
    if status0 != 200 or not body0:
        out["STOPPED"] = f"GET /networks/{GT_NETWORK}/pools?page=1 не вернул 200 (status={status0}) -- дальше не идём, не выдумываем"
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print("[discovery] " + out["STOPPED"])
        return

    sample = (body0.get("data") or [])[:1]
    out["probe_sample_attributes_keys"] = list(sample[0].get("attributes", {}).keys()) if sample else []
    out["probe_sample_relationships_keys"] = list(sample[0].get("relationships", {}).keys()) if sample else []
    out["probe_sample_raw"] = sample[0] if sample else None
    out["probe_n_pools_page1"] = len(body0.get("data") or [])

    # --- Шаг 1: пагинация ---
    all_pools = list(body0.get("data") or [])
    for page in range(2, MAX_PAGES + 1):
        time.sleep(GT_REQUEST_INTERVAL_S)
        status, body = gt_get(f"/networks/{GT_NETWORK}/pools", params={"page": page})
        if status != 200 or not body or not body.get("data"):
            out["pagination_stopped_at_page"] = page
            out["pagination_stop_status"] = status
            break
        all_pools.extend(body["data"])
    else:
        out["pagination_stopped_at_page"] = MAX_PAGES + 1
        out["pagination_stop_status"] = "MAX_PAGES достигнут, реальный предел GT не найден в этом прогоне"
    out["n_pools_total_fetched"] = len(all_pools)

    # dex-слаги, реально встреченные -- для честности, не гадаем список заранее
    dex_ids_seen = sorted({(p.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id")
                            for p in all_pools if p.get("relationships")})
    out["dex_ids_seen"] = dex_ids_seen

    # --- Шаг 2: TVL >= $5000 ---
    tvl_candidates = []
    for p in all_pools:
        attrs = p.get("attributes", {})
        reserve = attrs.get("reserve_in_usd")
        try:
            reserve_f = float(reserve) if reserve is not None else None
        except (TypeError, ValueError):
            reserve_f = None
        if reserve_f is not None and reserve_f >= TVL_MIN_USD:
            tvl_candidates.append({
                "gt_id": p.get("id"), "address": attrs.get("address"), "name": attrs.get("name"),
                "reserve_in_usd": reserve_f, "pool_created_at": attrs.get("pool_created_at"),
                "volume_usd_h24": attrs.get("volume_usd", {}).get("h24") if isinstance(attrs.get("volume_usd"), dict) else None,
                "dex_id": (p.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id"),
                "base_token_id": (p.get("relationships", {}).get("base_token", {}).get("data", {}) or {}).get("id"),
                "quote_token_id": (p.get("relationships", {}).get("quote_token", {}).get("data", {}) or {}).get("id"),
            })
    out["n_after_tvl_filter"] = len(tvl_candidates)

    # --- Шаг 3: реальная торговля за 3 суток (OHLCV день, сумма объёма > 0) ---
    traded_candidates = []
    for c in tvl_candidates:
        addr = c["address"]
        if not addr:
            continue
        time.sleep(GT_REQUEST_INTERVAL_S)
        status, body = gt_get(f"/networks/{GT_NETWORK}/pools/{addr}/ohlcv/day", params={"aggregate": 1, "limit": 3})
        if status != 200 or not body:
            c["ohlcv_3d_status"] = status
            continue
        rows = (body.get("data", {}).get("attributes", {}) or {}).get("ohlcv_list", [])
        vol_3d = sum(r[5] for r in rows if len(r) > 5 and r[5] is not None)
        c["volume_usd_3d_real"] = vol_3d
        c["n_daily_candles_returned"] = len(rows)
        if vol_3d and vol_3d > 0:
            traded_candidates.append(c)
    out["n_after_3d_trading_filter"] = len(traded_candidates)

    # --- Шаг 4+5: реальная комиссия + исключить 11% ---
    latest_block_raw = RPC("eth_blockNumber", [])
    latest_block = int(latest_block_raw, 16) if latest_block_raw else None
    out["latest_block_for_fee_resolution"] = latest_block
    if latest_block is None:
        out["STOPPED"] = "не удалось получить latest_block для резолва комиссии -- дальше не идём"
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        print("[discovery] " + out["STOPPED"])
        return

    final_candidates = []
    for c in traded_candidates:
        fee_info = resolve_real_fee(c["address"], latest_block)
        c["fee_resolution"] = fee_info
        fee_pips = fee_info.get("fee_pips")
        if fee_pips is None:
            continue
        if fee_pips == EXCLUDED_FEE_PIPS:
            continue
        final_candidates.append(c)
    out["n_final_candidates_fee_ne_11pct"] = len(final_candidates)
    out["final_candidates"] = final_candidates

    if len(final_candidates) < 5:
        out["HONEST_ANSWER"] = (
            f"Итоговых кандидатов после всех фильтров (TVL>=$5000, реальная торговля за 3 суток, "
            f"fee!=11%): {len(final_candidates)} -- МЕНЬШЕ 5, это само по себе ответ по правилу владельца. "
            "Полный конвейер (почасовой оборот/цена, детекция разгона, форвард-горизонты) НЕ запускается "
            "на этом прогоне -- нужно либо ослабить фильтры, либо признать выборку слишком узкой."
        )
        print("[discovery] " + out["HONEST_ANSWER"])
    else:
        out["NEXT_STEP"] = f"{len(final_candidates)} кандидатов -- достаточно для этапа Б (почасовые ряды + детекция разгона)."
        print(f"[discovery] {out['NEXT_STEP']}")

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
