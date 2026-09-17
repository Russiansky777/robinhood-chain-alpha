#!/usr/bin/env python3
"""Владелец (2026-09-17): сборщик 1, ежедневный шаг -- отбор пулов для
почасового/15-минутного сбора оборота (линия трендов). Раз в сутки на
VPS (cron), отдельно от снимка каждые 15 минут.

Правила владельца:
  - Отбор: TVL >= $5000 И реальная торговля за последние сутки (GT
    OHLCV день, сумма объёма > 0) -- НЕ фильтруем по комиссии (в
    отличие от разового анализа Задачи 3 -- сборщик собирает ВСЁ, что
    качественно живо, фильтрация по fee -- дело анализа, не сбора).
  - "Мёртвые и упавшие пулы НЕ выкидывать" -- список ТОЛЬКО РАСТЁТ:
    новый прогон ДОБАВЛЯЕТ вновь квалифицировавшиеся пулы к уже
    накопленному `data/trend_collector/pool_universe.json`, никогда не
    удаляет ранее добавленные (даже если пул перестал соответствовать
    порогу сегодня) -- иначе ошибка выжившего в анализе через неделю.
  - Реальный тип и комиссия резолвятся ОДИН РАЗ при первом добавлении
    (v4 pool_id через fetch_initialize_event, v3 address через fee()) --
    переиспользует функции rh_volume_trend_pool_discovery.py, не
    дублирует логику.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rh_volume_trend_pool_discovery import (  # noqa: E402
    gt_get, GT_NETWORK, TVL_MIN_USD, MAX_PAGES, GT_REQUEST_INTERVAL_S,
    OHLCV_STAGE_TIME_BUDGET_S, FEE_RESOLUTION_TIME_BUDGET_S, resolve_real_fee,
)
from alchemy_fallback import rpc_call_trading_path  # noqa: E402

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
RPC = rpc_call_trading_path


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def load_universe(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"pools": {}}  # keyed by address/pool_id (lowercase)


def run() -> int:
    root = find_repo_root()
    universe_path = root.joinpath("data/trend_collector/pool_universe.json")
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    universe = load_universe(universe_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    all_pools = []
    for page in range(1, MAX_PAGES + 1):
        status, body = gt_get(f"/networks/{GT_NETWORK}/pools", params={"page": page})
        if status != 200 or not body or not body.get("data"):
            break
        all_pools.extend(body["data"])
        time.sleep(GT_REQUEST_INTERVAL_S)
    print(f"[select_pools] GT: {len(all_pools)} пулов за {page if all_pools else 0} страниц")

    tvl_candidates = []
    for p in all_pools:
        attrs = p.get("attributes", {})
        try:
            reserve_f = float(attrs.get("reserve_in_usd")) if attrs.get("reserve_in_usd") is not None else None
        except (TypeError, ValueError):
            reserve_f = None
        if reserve_f is not None and reserve_f >= TVL_MIN_USD:
            addr = attrs.get("address")
            if addr:
                tvl_candidates.append({"address": addr, "name": attrs.get("name"),
                                        "reserve_in_usd": reserve_f,
                                        "dex_id": (p.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id")})
    print(f"[select_pools] TVL>=${TVL_MIN_USD:.0f}: {len(tvl_candidates)} кандидатов")

    # ЧЕСТНАЯ НАХОДКА ("Срочно 2", 2026-09-17, аудит воронки): реестр рос лишь
    # до 23 пулов, хотя реальная воронка (тот же метод, более терпеливая
    # пагинация) дала 53 -- потому что бюджет OHLCV-проверки КАЖДЫЙ день
    # тратился на ПОВТОРНУЮ проверку уже зарегистрированных пулов (список
    # "никогда не уменьшается", повторная проверка их торговли не нужна --
    # решение о добавлении уже принято раз и навсегда). Уже зарегистрированные
    # пулы обновляют last_qualified_utc СРАЗУ, без похода в OHLCV -- весь
    # бюджет уходит на ДЕЙСТВИТЕЛЬНО новые кандидаты, и реестр сможет
    # догнать реальную воронку за несколько дней, а не топтаться на месте.
    already_registered = [c for c in tvl_candidates if c["address"].lower() in universe["pools"]]
    new_candidates = [c for c in tvl_candidates if c["address"].lower() not in universe["pools"]]
    for c in already_registered:
        universe["pools"][c["address"].lower()]["last_qualified_utc"] = now
    print(f"[select_pools] уже в реестре (last_qualified_utc обновлён, OHLCV не тратится): {len(already_registered)}, "
          f"новых кандидатов на проверку: {len(new_candidates)}")

    ohlcv_start = time.time()
    traded = []
    for c in new_candidates:
        if time.time() - ohlcv_start > OHLCV_STAGE_TIME_BUDGET_S:
            print("[select_pools] бюджет OHLCV-проверки исчерпан, честно останавливаемся")
            break
        time.sleep(GT_REQUEST_INTERVAL_S)
        status, body = gt_get(f"/networks/{GT_NETWORK}/pools/{c['address']}/ohlcv/day", params={"aggregate": 1, "limit": 1})
        if status != 200 or not body:
            continue
        rows = (body.get("data", {}).get("attributes", {}) or {}).get("ohlcv_list", [])
        vol_1d = sum(r[5] for r in rows if len(r) > 5 and r[5] is not None)
        if vol_1d and vol_1d > 0:
            traded.append(c)
    print(f"[select_pools] реальная торговля за сутки (среди НОВЫХ кандидатов): {len(traded)}")

    latest_raw = RPC("eth_blockNumber", [])
    latest_block = int(latest_raw, 16) if latest_raw else None

    n_added = 0
    fee_start = time.time()
    for c in traded:  # traded -- ТОЛЬКО новые кандидаты, already_registered обработаны выше
        key = c["address"].lower()
        if latest_block is None or time.time() - fee_start > FEE_RESOLUTION_TIME_BUDGET_S:
            continue  # честно пропускаем новые до следующего дня, а не гадаем
        fee_info = resolve_real_fee(c["address"], latest_block)
        kind = fee_info.get("kind")
        # ЧЕСТНАЯ НАХОДКА (реальный прогон на VPS сломал снимок каждые 15 минут
        # TypeError'ом): resolve_real_fee может вернуть kind="v4_pool_id" БЕЗ
        # ключа "pool_id" (Initialize не найден -- info is None) -- такой пул
        # снимку читать нечем, честно НЕ добавляем его в реестр вместо того,
        # чтобы копить мусорную запись, которая валит снимок каждый раз.
        # (Динамическая комиссия -- pool_id ЕСТЬ, fee_pips=None, fee_is_dynamic=
        # True -- это НЕ повод исключать: снимку для extsload нужен только
        # pool_id, а не число комиссии.)
        if kind == "v4_pool_id" and not fee_info.get("pool_id"):
            continue
        if kind == "v3_address" and fee_info.get("fee_pips") is None:
            continue
        universe["pools"][key] = {
            "address": c["address"], "name": c.get("name"), "dex_id": c.get("dex_id"),
            "kind": kind, "fee_pips": fee_info.get("fee_pips"), "fee_is_dynamic": fee_info.get("fee_is_dynamic", False),
            "pool_id": fee_info.get("pool_id"), "hooks": fee_info.get("hooks"),
            "currency0": fee_info.get("currency0"), "currency1": fee_info.get("currency1"),
            "first_seen_utc": now, "last_qualified_utc": now,
            "first_seen_reserve_in_usd": c["reserve_in_usd"],
        }
        n_added += 1

    universe["last_run_utc"] = now
    universe["n_pools_total"] = len(universe["pools"])
    universe_path.write_text(json.dumps(universe, ensure_ascii=False, indent=2, default=str))
    print(f"[select_pools] добавлено новых: {n_added}, всего в реестре (никогда не уменьшается): {len(universe['pools'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
