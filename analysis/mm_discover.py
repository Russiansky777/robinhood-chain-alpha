#!/usr/bin/env python3
"""Задача (владелец, 2026-09-02): "измерить экономику маркетмейкинга
сток-токенов в закрытые часы с хеджем перпом". П.6 (дисциплина):
"Оценить число RPC-вызовов до старта и доложить ожидаемое время. Не
пушить во время активного run."

Этот скрипт -- ПЕРВЫЙ шаг (discover + calibrate), НЕ полная выгрузка
сделок. Он:
  1. Подтверждает реальные адреса Uniswap v3 Factory / v4 PoolManager
     на Robinhood Chain (короткий chain-wide срез, дёшево) -- п.1
     задания прямо требует "определить из данных, не предполагать",
     включая версию протокола, а не только тикеры.
  2. Ищет ВСЕ v3/v4-пулы токенов из eligible_universe() за ПОЛНУЮ
     историю окна 01.07-01.09.2026 (address-scoped, редкое событие --
     дёшево даже на большом диапазоне).
  3. Калибрует плотность Swap-событий на 1-дневном срезе по найденным
     пулам и экстраполирует на всё окно -- ОЦЕНКА (не факт) числа
     запросов и времени полного прогона, печатается и сохраняется ДО
     того, как полная выгрузка вообще запускается (следующий скрипт,
     ещё не написан -- ждёт этой оценки).

Только чтение (`eth_getLogs`/`eth_call`), публичный RPC без ключа --
никаких транзакций, ключ не используется.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _chunked_get_logs, get_block, get_block_number  # noqa: E402
from mm_common import WINDOW_END_UTC, WINDOW_START_UTC, eligible_universe  # noqa: E402
from p3_common import (  # noqa: E402
    CANDIDATE_V3_FACTORY,
    TOPIC0_V3_POOL_CREATED,
    TOPIC0_V3_SWAP,
    TOPIC0_V4_INITIALIZE,
    TOPIC0_V4_SWAP,
    decode_pool_created,
    decode_v4_initialize,
)

OUT_PATH = Path("data/p3_guard_cache/mm_discover_result.json")

# Первый прогон (2026-09-02) не имел потолка и калибровался на ПОЛНЫЙ
# день (~860k блоков при spb~0.1с) на пул, вместо дешёвого малого среза
# -- при до 24 токенах x 2 версии протокола это могло уйти в десятки
# тысяч вызовов без страховки. Отменено вручную. Фикс: явный потолок (тот
# же принцип, что MAX_REQUESTS_PER_RUN в p3_dislocation_guard.py) +
# калибровка на МАЛОМ фиксированном срезе (не "1 день").
MAX_REQUESTS_PER_RUN = 20_000
CALIBRATION_BLOCKS = 20_000  # ~30-35 мин при spb~0.1с -- достаточно для оценки плотности, дёшево

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n
    if _request_count > MAX_REQUESTS_PER_RUN:
        raise RuntimeError(
            f"[mm_discover] СТОП: потолок запросов за прогон превышен "
            f"({_request_count} > {MAX_REQUESTS_PER_RUN}). См. MAX_REQUESTS_PER_RUN."
        )


def estimate_seconds_per_block(latest: int, lookback_blocks: int = 500_000) -> float:
    t_latest = int(get_block(latest)["timestamp"], 16)
    _count()
    early = max(1, latest - lookback_blocks)
    t_early = int(get_block(early)["timestamp"], 16)
    _count()
    dt, db = t_latest - t_early, latest - early
    if db <= 0 or dt <= 0:
        raise RuntimeError("[mm_discover] блоктайм не измерен (dt/db <= 0)")
    return dt / db


def block_for_timestamp(latest: int, spb: float, target: datetime) -> int:
    t_latest = int(get_block(latest)["timestamp"], 16)
    _count()
    delta_s = t_latest - target.timestamp()
    est = latest - int(round(delta_s / spb))
    return max(1, min(latest, est))


def scan(from_block: int, to_block: int, topics: list, address=None, chunk_size: int = 2000) -> list[dict]:
    n_chunks = max(1, (to_block - from_block) // chunk_size + 1)
    _count(n_chunks)
    return list(_chunked_get_logs(from_block, to_block, topics, chunk_size=chunk_size, address=address))


def run() -> int:
    t0 = time.time()
    latest = get_block_number()
    _count()
    spb = estimate_seconds_per_block(latest)
    window_start_block = block_for_timestamp(latest, spb, WINDOW_START_UTC)
    window_end_block = min(latest, block_for_timestamp(latest, spb, WINDOW_END_UTC))
    window_days = (WINDOW_END_UTC - WINDOW_START_UTC).total_seconds() / 86400

    print(f"[mm_discover] latest={latest} seconds_per_block~={spb:.4f} "
          f"окно=[{window_start_block},{window_end_block}] ({window_days:.1f} дней)")

    universe = eligible_universe()
    print(f"[mm_discover] eligible_universe (feed R1 ∩ Lighter perp market): "
          f"{len(universe)} токенов: {sorted(universe)}")
    token_addrs = {v["token_address"] for v in universe.values()}

    # --- Стадия 1: подтвердить реальные факторию/poolmanager (1-дневный chain-wide срез) ---
    blocks_1d = int(round(86400 / spb))
    discover_from = max(1, latest - blocks_1d)
    pc_logs = scan(discover_from, latest, [TOPIC0_V3_POOL_CREATED])
    init_logs = scan(discover_from, latest, [TOPIC0_V4_INITIALIZE])
    factories_seen = sorted({str(l["address"]).lower() for l in pc_logs})
    pool_managers_seen = sorted({str(l["address"]).lower() for l in init_logs})
    v3_factory = CANDIDATE_V3_FACTORY.lower() if CANDIDATE_V3_FACTORY.lower() in factories_seen else (factories_seen[0] if factories_seen else None)
    print(f"[mm_discover] срез 1д [{discover_from},{latest}]: "
          f"{len(pc_logs)} PoolCreated (factories={factories_seen}), "
          f"{len(init_logs)} v4 Initialize (poolManagers={pool_managers_seen})")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": "1_done_factories_confirmed",
        "factories_seen_1d_slice": factories_seen,
        "v3_factory_used": v3_factory,
        "pool_managers_seen_1d_slice": pool_managers_seen,
        "requests_used_so_far": _request_count,
    }, indent=2, default=str, ensure_ascii=False))

    # --- Стадия 2: полная история пулов для eligible_universe (редкое событие -- дёшево) ---
    v3_pools_all = scan(window_start_block, window_end_block, [TOPIC0_V3_POOL_CREATED], address=v3_factory, chunk_size=20_000) if v3_factory else []
    v4_inits_all: list[dict] = []
    for pm in pool_managers_seen:
        v4_inits_all += scan(window_start_block, window_end_block, [TOPIC0_V4_INITIALIZE], address=pm, chunk_size=20_000)

    pool_created = [decode_pool_created(l) for l in v3_pools_all]
    initializes = [decode_v4_initialize(l) for l in v4_inits_all]

    v3_by_token: dict[str, list[str]] = {}
    for r in pool_created:
        for side in ("token0", "token1"):
            if r[side] in token_addrs:
                v3_by_token.setdefault(r[side], []).append(r["pool"])

    v4_by_token: dict[str, list[tuple[str, str]]] = {}
    for r in initializes:
        for side in ("currency0", "currency1"):
            if r[side] in token_addrs:
                v4_by_token.setdefault(r[side], []).append((r["pool_manager_address"], r["pool_id"]))

    addr_to_symbol = {v["token_address"]: sym for sym, v in universe.items()}
    pools_by_symbol = {}
    for addr in token_addrs:
        sym = addr_to_symbol[addr]
        pools_by_symbol[sym] = {
            "token_address": addr,
            "v3_pools": sorted(set(v3_by_token.get(addr, []))),
            "v4_pool_ids": sorted(set(v4_by_token.get(addr, []))),
        }
    n_with_v3 = sum(1 for p in pools_by_symbol.values() if p["v3_pools"])
    n_with_v4 = sum(1 for p in pools_by_symbol.values() if p["v4_pool_ids"])
    n_with_any = sum(1 for p in pools_by_symbol.values() if p["v3_pools"] or p["v4_pool_ids"])
    print(f"[mm_discover] из {len(universe)} eligible-токенов: {n_with_v3} имеют v3-пул, "
          f"{n_with_v4} имеют v4-пул, {n_with_any} имеют хоть один")

    OUT_PATH.write_text(json.dumps({
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": "2_done_pools_found",
        "factories_seen_1d_slice": factories_seen,
        "v3_factory_used": v3_factory,
        "pool_managers_seen_1d_slice": pool_managers_seen,
        "pools_by_symbol": pools_by_symbol,
        "n_eligible_with_v3_pool": n_with_v3,
        "n_eligible_with_v4_pool": n_with_v4,
        "n_eligible_with_any_pool": n_with_any,
        "requests_used_so_far": _request_count,
    }, indent=2, default=str, ensure_ascii=False))

    # --- Стадия 3: калибровка плотности Swap на МАЛОМ срезе (CALIBRATION_BLOCKS) ВНУТРИ окна ---
    # НЕ полный день (баг первого прогона -- см. докстринг MAX_REQUESTS_PER_RUN
    # выше): дорого и без страховки при до 24 токенах x 2 версии протокола.
    calib_to = window_end_block
    calib_from = max(window_start_block, calib_to - CALIBRATION_BLOCKS)
    # Реальная длительность среза в секундах -- по фактическим таймстемпам
    # блоков, не по оценке spb (не накапливаем ошибку оценки поверх оценки).
    calib_to_ts = int(get_block(calib_to)["timestamp"], 16)
    _count()
    calib_from_ts = int(get_block(calib_from)["timestamp"], 16)
    _count()
    calib_duration_s = max(1, calib_to_ts - calib_from_ts)

    n_swap_calib = 0
    calib_calls = 0
    for sym, p in pools_by_symbol.items():
        if p["v3_pools"]:
            logs = scan(calib_from, calib_to, [TOPIC0_V3_SWAP], address=p["v3_pools"], chunk_size=2000)
            n_swap_calib += len(logs)
            calib_calls += max(1, (calib_to - calib_from) // 2000 + 1)
        for pm, pool_id in p["v4_pool_ids"]:
            logs = scan(calib_from, calib_to, [[TOPIC0_V4_SWAP], [pool_id]], address=pm, chunk_size=2000)
            n_swap_calib += len(logs)
            calib_calls += max(1, (calib_to - calib_from) // 2000 + 1)

    print(f"[mm_discover] калибровка [{calib_from},{calib_to}] (~{calib_duration_s/60:.1f} мин "
          f"по факту timestamp'ов блоков): {n_swap_calib} Swap-событий "
          f"по {n_with_any} токенам с пулом ({calib_calls} вызовов eth_getLogs)")

    # Экстраполяция на всё окно: (секунд в окне / секунд в калибровочном
    # срезе) x буфер x1.5 на неравномерность активности (явно назван
    # буфером, не фактом).
    window_seconds = (WINDOW_END_UTC - WINDOW_START_UTC).total_seconds()
    scale = window_seconds / calib_duration_s
    BUFFER = 1.5
    projected_swap_events = int(n_swap_calib * scale * BUFFER)
    projected_getlogs_calls = int(calib_calls * scale * BUFFER)
    # Плюс по одному eth_call на сделку для "цены до/после" не нужен --
    # цена уже в самом Swap-логе (sqrtPriceX96/amount0/amount1) --
    # никаких доп. RPC на сделку сверх getLogs не требуется для п.1.
    # П.2 (markout к перпу) добавит запросы к Lighter API (HTTP, не RPC,
    # отдельный бюджет) -- не входит в эту RPC-оценку.
    seconds_per_request_estimate = 0.5  # тот же self-throttle _MIN_REQUEST_INTERVAL_S, что и во всех остальных скриптах проекта
    projected_seconds = projected_getlogs_calls * seconds_per_request_estimate
    projected_minutes = projected_seconds / 60

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": "3_done_complete",
        "latest_block": latest,
        "seconds_per_block_estimate": spb,
        "window_start_utc": WINDOW_START_UTC.isoformat(),
        "window_end_utc": WINDOW_END_UTC.isoformat(),
        "window_start_block": window_start_block,
        "window_end_block": window_end_block,
        "window_days": window_days,
        "eligible_universe_symbols": sorted(universe),
        "n_eligible_universe": len(universe),
        "factories_seen_1d_slice": factories_seen,
        "v3_factory_used": v3_factory,
        "pool_managers_seen_1d_slice": pool_managers_seen,
        "pools_by_symbol": pools_by_symbol,
        "n_eligible_with_v3_pool": n_with_v3,
        "n_eligible_with_v4_pool": n_with_v4,
        "n_eligible_with_any_pool": n_with_any,
        "calibration_window": {"from_block": calib_from, "to_block": calib_to},
        "calibration_duration_s_by_block_timestamps": calib_duration_s,
        "calibration_n_swap_events": n_swap_calib,
        "calibration_n_getlogs_calls": calib_calls,
        "extrapolation_scale_window_over_calibration": scale,
        "extrapolation_buffer": BUFFER,
        "projected_swap_events_full_window": projected_swap_events,
        "projected_getlogs_calls_full_window": projected_getlogs_calls,
        "projected_runtime_minutes_full_window_estimate": projected_minutes,
        "requests_used_this_discover_run": _request_count,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[mm_discover] записано {OUT_PATH}")
    print(f"[mm_discover] ОЦЕНКА полной выгрузки Swap за окно {window_days:.0f} дней: "
          f"~{projected_swap_events} событий, ~{projected_getlogs_calls} вызовов eth_getLogs, "
          f"~{projected_minutes:.0f} мин (буфер x{BUFFER})")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
