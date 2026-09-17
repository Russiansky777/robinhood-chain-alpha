#!/usr/bin/env python3
"""Задача 5, владелец 2026-09-17, Часть Б: гипотеза "цену двигает не своп, а
Mint/Burn ликвидности Uniswap v3" как альтернативный катализатор для
none-бакета ($138,589.41 прибыли без объяснённого катализатора, см.
docs/PROJECT_STATE.md, "A1" / task5_none_bucket_wash_overlap_result.json).

ЧЕСТНАЯ ОГОВОРКА ПО ВЫБОРКЕ (владелец явно разрешил РЕПРЕЗЕНТАТИВНУЮ
выборку, если полного списка tx_hash none-бакета нет в git -- его
действительно нет: task5_none_bucket_wash_overlap_result.json хранит
ТОЛЬКО агрегированные Dune group-by строки (bucket x category), Dune
никогда не отдавал построчный список отдельных tx_hash/pool/block --
агрегация была сделана НА СТОРОНЕ Dune ради экономии кредитов):

Вместо повторного дорогого Dune-запроса за списком (это был бы тот же
self-join, что и раньше, ~250+ кредитов) берём РЕАЛЬНЫЙ локальный
источник: `data/p3_guard_cache/task5_pool_map_result.json` -- список
Uniswap v3 пулов, где 2026-09-10 00:00-03:00 UTC УЖЕ БЫЛИ найдены реальные
прибыльные арбитражные захваты (>= $10, той же методологией closed-cycle,
что и none-бакет). Это НЕ точный список none-бакет транзакций (профиль
катализатора здесь не проверялся отдельно для каждого захвата), а
представительная выборка ИЗ ТЕХ ЖЕ пулов/того же дня/того же типа
активности -- честно заявлено как приближение, не точное совпадение.

Для каждого такого пула и окна берём ВСЕ реальные Swap-логи напрямую по
RPC (eth_getLogs, без Dune) как выборку "потенциально арбитражных"
транзакций (каждый Swap с уникальным tx_hash), затем для каждой такой
транзакции ищем в предыдущих 100 блоках ТОГО ЖЕ пула события Mint/Burn.

КОНТРОЛЬ (обязателен, иначе цифра ничего не доказывает): та же проверка
на случайных блоках из того же диапазона/тех же пулов, НЕ привязанных к
Swap-событиям -- даёт базовую (ожидаемую) частоту "есть Mint/Burn в
предыдущих 100 блоках" для этих пулов вообще.

Метрика: ТОЛЬКО доля по числу транзакций (честно, а не $ -- у нас нет
profit_usd на уровне отдельной транзакции в этой RPC-only выборке, в
отличие от Dune-агрегатов)."""
from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import (  # noqa: E402
    UNISWAP_V3_SWAP_SIG,
    _chunked_get_logs,
    get_block,
    topic0,
)

OUT_PATH = Path(__file__).parent.parent / "data" / "task5_liquidity_catalyst_check_result.json"
POOL_MAP_PATH = Path(__file__).parent.parent / "data" / "p3_guard_cache" / "task5_pool_map_result.json"

MINT_SIG = "Mint(address,address,int24,int24,uint128,uint256,uint256)"
BURN_SIG = "Burn(address,int24,int24,uint128,uint256,uint256)"

WINDOW_START_ISO = "2026-09-10T00:00:00Z"
WINDOW_END_ISO = "2026-09-10T03:00:00Z"  # тот же 3ч калибровочный интервал, что дал список пулов task5_pool_map_result.json
LOOKBACK_BLOCKS = 100
MAX_SAMPLE_TXS = 300  # потолок на выборку (репрезентативная, не полная популяция) -- честно, не вся генеральная совокупность
RANDOM_SEED = 20260917  # фиксирован для воспроизводимости


def _to_unix(iso: str) -> int:
    import calendar
    import datetime as dt

    d = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    return calendar.timegm(d.timetuple())


def _find_block_for_timestamp(target_ts: int, lo: int, hi: int) -> int:
    """Бинарный поиск блока с block.timestamp наиболее близким к target_ts
    (первый блок с timestamp >= target_ts). Несколько десятков реальных
    eth_getBlockByNumber -- копейки по сравнению со сканом логов."""
    lo_ts = int(get_block(lo)["timestamp"], 16)
    hi_ts = int(get_block(hi)["timestamp"], 16)
    if target_ts <= lo_ts:
        return lo
    if target_ts >= hi_ts:
        return hi
    while lo < hi:
        mid = (lo + hi) // 2
        mid_ts = int(get_block(mid)["timestamp"], 16)
        if mid_ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def load_v3_pools() -> list[str]:
    data = json.loads(POOL_MAP_PATH.read_text())
    pools = []
    for row in data.get("pools", []):
        if row.get("version") == "v3":
            key = row["pool_key"]
            if len(key) == 40:  # адрес пула v3 (не составной v4-ключ)
                pools.append("0x" + key.lower())
    # де-дупликация с сохранением порядка
    seen = set()
    out = []
    for p in pools:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def fetch_events(topics: list[str], pools: list[str], from_block: int, to_block: int) -> list[dict]:
    return list(
        _chunked_get_logs(
            from_block,
            to_block,
            topics,
            chunk_size=5000,
            address=pools,
        )
    )


def run() -> int:
    random.seed(RANDOM_SEED)
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hypothesis": "Mint/Burn ликвидности v3 в предыдущих 100 блоках объясняет часть none-бакета",
        "window_start_utc": WINDOW_START_ISO,
        "window_end_utc": WINDOW_END_ISO,
        "lookback_blocks": LOOKBACK_BLOCKS,
        "max_sample_txs": MAX_SAMPLE_TXS,
        "random_seed": RANDOM_SEED,
        "sample_source_note": (
            "Пулы -- из data/p3_guard_cache/task5_pool_map_result.json "
            "(реальные v3 пулы с прибыльными closed-cycle захватами >= $10 "
            "за 2026-09-10 00:00-03:00 UTC). Список tx_hash none-бакета "
            "(13,400 tx, $138,589.41) НЕ существует локально построчно -- "
            "Dune вернул только агрегаты (group by bucket x category), "
            "см. task5_none_bucket_wash_overlap_result.json. Это "
            "приближение (те же пулы/день/тип активности), НЕ точное "
            "совпадение с none-бакетом."
        ),
    }

    pools = load_v3_pools()
    result["v3_pools_used"] = pools
    result["n_pools"] = len(pools)
    print(f"[liquidity_catalyst] v3-пулов в выборке: {len(pools)}")
    if not pools:
        result["error"] = "не найдено ни одного v3-пула в task5_pool_map_result.json"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    # Реальная "текущая" верхняя граница блоков -- нужен разумный hi для
    # бинарного поиска. Берём блок из last known ссылки в репозитории
    # (task5_v4_block_timestamps_check.py) с большим запасом наверх,
    # затем проверяем/расширяем реальным eth_blockNumber.
    from alchemy_fallback import get_block_number

    chain_head = get_block_number()
    print(f"[liquidity_catalyst] текущая голова цепи: {chain_head}")

    start_ts = _to_unix(WINDOW_START_ISO)
    end_ts = _to_unix(WINDOW_END_ISO)

    lo_search = 1
    hi_search = chain_head
    from_block = _find_block_for_timestamp(start_ts, lo_search, hi_search)
    to_block = _find_block_for_timestamp(end_ts, lo_search, hi_search)
    print(f"[liquidity_catalyst] диапазон блоков окна: {from_block} .. {to_block} ({to_block - from_block} блоков)")
    result["from_block"] = from_block
    result["to_block"] = to_block
    result["n_blocks_in_window"] = to_block - from_block

    scan_from = max(1, from_block - LOOKBACK_BLOCKS)  # захват lookback ДО начала окна для крайних Swap'ов

    t0 = time.time()
    print("[liquidity_catalyst] тяну Swap-логи (реальный RPC, без Dune)...")
    swap_logs = fetch_events([topic0(UNISWAP_V3_SWAP_SIG)], pools, from_block, to_block)
    print(f"[liquidity_catalyst] Swap-логов: {len(swap_logs)} ({time.time()-t0:.1f}с)")

    t0 = time.time()
    print("[liquidity_catalyst] тяну Mint-логи...")
    mint_logs = fetch_events([topic0(MINT_SIG)], pools, scan_from, to_block)
    print(f"[liquidity_catalyst] Mint-логов: {len(mint_logs)} ({time.time()-t0:.1f}с)")

    t0 = time.time()
    print("[liquidity_catalyst] тяну Burn-логи...")
    burn_logs = fetch_events([topic0(BURN_SIG)], pools, scan_from, to_block)
    print(f"[liquidity_catalyst] Burn-логов: {len(burn_logs)} ({time.time()-t0:.1f}с)")

    result["n_swap_logs_raw"] = len(swap_logs)
    result["n_mint_logs_raw"] = len(mint_logs)
    result["n_burn_logs_raw"] = len(burn_logs)

    # per-pool отсортированные списки блоков Mint/Burn
    mb_blocks: dict[str, list[int]] = {p: [] for p in pools}
    for log in mint_logs + burn_logs:
        addr = log["address"].lower()
        if addr in mb_blocks:
            mb_blocks[addr].append(int(log["blockNumber"], 16))
    for p in mb_blocks:
        mb_blocks[p].sort()

    result["n_mint_plus_burn_by_pool"] = {p: len(mb_blocks[p]) for p in pools}

    # уникальные (pool, tx_hash, block) из Swap-логов -- популяция для выборки
    seen_tx = set()
    swap_events = []
    for log in swap_logs:
        addr = log["address"].lower()
        txh = log["transactionHash"]
        blk = int(log["blockNumber"], 16)
        key = (addr, txh)
        if key in seen_tx:
            continue
        seen_tx.add(key)
        swap_events.append((addr, blk, txh))

    result["n_unique_swap_txs_population"] = len(swap_events)
    print(f"[liquidity_catalyst] уникальных Swap-транзакций (популяция): {len(swap_events)}")

    if not swap_events:
        result["error"] = "нет Swap-транзакций в выбранном окне -- нечего проверять"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1

    sample_size = min(MAX_SAMPLE_TXS, len(swap_events))
    sample = random.sample(swap_events, sample_size)
    result["n_sample_txs"] = sample_size

    import bisect

    def has_event_in_window(pool: str, block: int) -> bool:
        blocks = mb_blocks.get(pool, [])
        lo = block - LOOKBACK_BLOCKS
        hi = block - 1
        if hi < lo:
            return False
        i = bisect.bisect_left(blocks, lo)
        return i < len(blocks) and blocks[i] <= hi

    hits = 0
    sample_detail = []
    for pool, block, txh in sample:
        hit = has_event_in_window(pool, block)
        hits += int(hit)
        sample_detail.append({"pool": pool, "block": block, "tx_hash": txh, "mint_or_burn_in_prev_100": hit})

    treatment_hit_rate = hits / sample_size
    result["treatment_hits"] = hits
    result["treatment_hit_rate"] = treatment_hit_rate
    result["sample_detail_first_20"] = sample_detail[:20]
    print(f"[liquidity_catalyst] TREATMENT: {hits}/{sample_size} = {treatment_hit_rate:.4f} имели Mint/Burn в предыдущих 100 блоках того же пула")

    # КОНТРОЛЬ: случайные блоки (НЕ привязанные к Swap) из того же
    # диапазона/тех же пулов, тот же размер выборки -- честная база
    # сравнения (иначе "50% Swap-транзакций имели Mint/Burn где-то в
    # окне" ничего не доказывает, если Mint/Burn и так происходят часто).
    control_hits = 0
    control_detail = []
    control_lo = from_block
    control_hi = to_block
    for _ in range(sample_size):
        pool = random.choice(pools)
        block = random.randint(control_lo, control_hi)
        hit = has_event_in_window(pool, block)
        control_hits += int(hit)
        control_detail.append({"pool": pool, "block": block, "mint_or_burn_in_prev_100": hit})

    control_hit_rate = control_hits / sample_size
    result["control_hits"] = control_hits
    result["control_hit_rate"] = control_hit_rate
    result["control_detail_first_20"] = control_detail[:20]
    print(f"[liquidity_catalyst] CONTROL (случайные блоки, те же пулы/окно): {control_hits}/{sample_size} = {control_hit_rate:.4f}")

    # Второй, независимый контроль: непересекающиеся бины по 100 блоков
    # по каждому пулу -- доля бинов с >=1 событием Mint/Burn (без всякой
    # привязки к случайности выбора блока внутри бина).
    bin_hits = 0
    bin_total = 0
    for pool in pools:
        blocks = mb_blocks.get(pool, [])
        b = from_block
        while b <= to_block:
            bin_hi = min(b + LOOKBACK_BLOCKS - 1, to_block)
            i = bisect.bisect_left(blocks, b)
            has = i < len(blocks) and blocks[i] <= bin_hi
            bin_hits += int(has)
            bin_total += 1
            b += LOOKBACK_BLOCKS
    bin_control_rate = (bin_hits / bin_total) if bin_total else None
    result["bin_control_hits"] = bin_hits
    result["bin_control_total_bins"] = bin_total
    result["bin_control_hit_rate"] = bin_control_rate
    print(f"[liquidity_catalyst] CONTROL (непересекающиеся 100-блочные бины): {bin_hits}/{bin_total} = {bin_control_rate}")

    lift_vs_random_control = (treatment_hit_rate - control_hit_rate)
    lift_vs_bin_control = (treatment_hit_rate - bin_control_rate) if bin_control_rate is not None else None
    result["lift_treatment_minus_random_control"] = lift_vs_random_control
    result["lift_treatment_minus_bin_control"] = lift_vs_bin_control

    # Порог вердикта: владелец не задал точное число -- используем
    # разумный, явно заявленный порог "заметно выше базовой" = lift >=
    # 0.15 (15 п.п.) абсолютной разницы И относительный lift >= 30%,
    # ОБЕИМИ контрольными группами одновременно (осторожный порог, чтобы
    # не подогнать вывод под шум одной случайной выборки).
    def significant(rate: float, control: float) -> bool:
        if control is None:
            return False
        abs_diff = rate - control
        rel_diff = (rate - control) / control if control > 0 else float("inf")
        return abs_diff >= 0.15 and rel_diff >= 0.30

    verdict_vs_random = significant(treatment_hit_rate, control_hit_rate)
    verdict_vs_bin = significant(treatment_hit_rate, bin_control_rate) if bin_control_rate is not None else False
    verdict_confirmed = bool(verdict_vs_random and verdict_vs_bin)
    result["verdict_confirmed"] = verdict_confirmed
    result["verdict_text"] = (
        "ЛИКВИДНОСТЬ ОБЪЯСНЯЕТ ЗАМЕТНУЮ ЧАСТЬ -- отдельная ниша, где скорость не нужна"
        if verdict_confirmed
        else "ЭФФЕКТ НЕ ОТЛИЧИМ ОТ БАЗОВОЙ ЧАСТОТЫ -- гипотеза не подтверждена"
    )
    print(f"[liquidity_catalyst] ВЕРДИКТ: {result['verdict_text']}")
    print(
        f"[liquidity_catalyst] treatment={treatment_hit_rate:.4f} "
        f"random_control={control_hit_rate:.4f} bin_control={bin_control_rate}"
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[liquidity_catalyst] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
