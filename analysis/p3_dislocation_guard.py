"""P3-гард (владелец, 2026-09-01): "Один дешёвый гард для P3 (тоже без
Dune, через RPC/Blockscout, узкий срез x2.5): доля кросс-версионных
(v3<->v4) закрытий дислокаций топ-5 сток-токенов, приходящаяся на
топ-3 адреса за последние 7 дней. Если > 80% -- P3 закрывается
окончательно." См. docs/P3_GUARD.md за результатом и полным разбором.

Источник данных: только eth_getLogs/eth_getTransactionByHash через
Alchemy RPC (analysis/alchemy_fallback.py) -- НЕ Dune. Интерактивная
песочница агента блокирует egress к *.alchemy.com (как и ко всем
внешним доменам кроме github.com) -- этот скрипт выполняется на GH
Actions runner'е (обычный исходящий интернет, тот же паттерн, что
analysis/r1_feed_match.py), НЕ локально в сессии. См.
.github/workflows/run_p3_guard.yml.

Методология "закрытия дислокации" (операциональное определение, не
из внешнего источника -- решение этого гарда, см. docs/P3_GUARD.md):
транзакция, чьи логи содержат ОБА -- V3 Swap-событие на v3-пуле этого
сток-токена И V4 Swap-событие на v4-poolId этого же токена. Совпадение
tx_hash -- признак атомарного кросс-пульного действия в одной
транзакции (иначе исполнение двух ног в разных блоках не гарантирует
единую цену на входе и выходе -- не "закрытие", а два независимых
свопа). Упрощение (явно, не скрыто): не проверяется знак
amount0/amount1 (встречные ли направления двух ног) -- см. docs/
P3_GUARD.md, "Ограничения".

Стадии (--stage):
  discover -- узкий срез (1 день, chain-wide, без address-фильтра):
    проверяет САМ факт существования v3/v4 пулов сток-токенов,
    подтверждает/опровергает адрес V3 Factory и находит адрес V4
    PoolManager по факту логов, а не по книге. Печатает калибровку
    x2.5 для стадии guard.
  guard -- полный прогон: address-scoped скан пулов сток-токенов с
    начала чейна (адреса из discover), скан Swap за последние 7 дней,
    топ-5 по числу свопов среди токенов с ОБЕИМИ версиями пула,
    поиск кросс-версионных tx, топ-3 адреса, вердикт >80%.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from alchemy_fallback import _chunked_get_logs, get_block, get_block_number, get_transaction
from p3_common import (
    CANDIDATE_V3_FACTORY,
    TOPIC0_V3_POOL_CREATED,
    TOPIC0_V3_SWAP,
    TOPIC0_V4_INITIALIZE,
    TOPIC0_V4_SWAP,
    decode_pool_created,
    decode_v3_swap,
    decode_v4_initialize,
    decode_v4_swap,
)

CACHE_DIR = Path("data/p3_guard_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

STOCK_UNIVERSE_PATH = Path("data/sprintR1_cache/r1_rwa_full_universe.csv")
CHAIN_GENESIS = datetime(2026, 7, 1, tzinfo=timezone.utc)
TOP_N_TOKENS = 5
TOP_N_ADDRESSES = 3
CLOSE_SHARE_KILL_THRESHOLD = 0.80

# Безопасный потолок запросов за один прогон (не "кредиты" -- это RPC,
# но тот же принцип принудительного стопа, что у Dune-гарда:
# analysis/credit_guard.py). Дискавери должен остаться на 2-3 порядка
# ниже этого при чейн-вайд скане за 1 день.
MAX_REQUESTS_PER_RUN = 8000

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n
    if _request_count > MAX_REQUESTS_PER_RUN:
        raise RuntimeError(
            f"[p3_guard] СТОП: превышен потолок запросов за прогон "
            f"({_request_count} > {MAX_REQUESTS_PER_RUN}). См. MAX_REQUESTS_PER_RUN."
        )


def load_stock_universe() -> dict[str, str]:
    """token_address (lowercase) -> symbol, из уже оплаченного и
    закэшированного Sprint R1 реестра (194 токена, r1_rwa_authoritative,
    Шаг 1 R1) -- НЕ новый запрос, 0 доп. кредитов/запросов."""
    df = pd.read_csv(STOCK_UNIVERSE_PATH)
    return {
        str(addr).lower(): str(sym)
        for addr, sym in zip(df["token_address"], df["token_symbol"])
    }


def estimate_seconds_per_block(latest: int, lookback_blocks: int = 200_000) -> float:
    """Эмпирическая оценка блоктайма (не берём документированные ~100мс
    на веру -- 2 точечных RPC-вызова, дёшево). Владелец, "никогда не
    выдумывай данные" -- измеряем, не гадаем."""
    t_latest = int(get_block(latest)["timestamp"], 16)
    _count()
    early = max(1, latest - lookback_blocks)
    t_early = int(get_block(early)["timestamp"], 16)
    _count()
    dt = t_latest - t_early
    db = latest - early
    if db <= 0 or dt <= 0:
        raise RuntimeError("[p3_guard] Не удалось измерить блоктайм (dt/db <= 0).")
    return dt / db


def blocks_for_days(seconds_per_block: float, days: float) -> int:
    return int(round(days * 86400 / seconds_per_block))


def block_for_timestamp(latest: int, seconds_per_block: float, target: datetime) -> int:
    t_latest = int(get_block(latest)["timestamp"], 16)
    _count()
    delta_s = t_latest - int(target.timestamp())
    est = latest - int(round(delta_s / seconds_per_block))
    return max(1, est)


def scan_logs(from_block: int, to_block: int, topics: list, address=None, chunk_size: int = 2000) -> list[dict]:
    n_chunks = (to_block - from_block) // chunk_size + 1
    _count(n_chunks)
    return list(_chunked_get_logs(from_block, to_block, topics, chunk_size=chunk_size, address=address))


def stage_discover() -> None:
    t0 = time.time()
    latest = get_block_number()
    _count()
    spb = estimate_seconds_per_block(latest)
    blocks_1d = blocks_for_days(spb, 1)
    from_block = max(1, latest - blocks_1d)

    print(f"[p3_guard][discover] latest={latest} seconds_per_block~={spb:.4f} "
          f"blocks_1d~={blocks_1d} slice=[{from_block}, {latest}]")

    pc_logs = scan_logs(from_block, latest, [TOPIC0_V3_POOL_CREATED])
    init_logs = scan_logs(from_block, latest, [TOPIC0_V4_INITIALIZE])

    pool_created = [decode_pool_created(l) for l in pc_logs]
    initializes = [decode_v4_initialize(l) for l in init_logs]

    factories_seen = sorted({r["factory_address"] for r in pool_created})
    pool_managers_seen = sorted({r["pool_manager_address"] for r in initializes})

    stock = load_stock_universe()
    v3_stock_pools = [r for r in pool_created if r["token0"] in stock or r["token1"] in stock]
    v4_stock_pools = [r for r in initializes if r["currency0"] in stock or r["currency1"] in stock]

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest_block": latest,
        "seconds_per_block_estimate": spb,
        "slice_from_block": from_block,
        "slice_to_block": latest,
        "n_pool_created_total_in_slice": len(pool_created),
        "n_v4_initialize_total_in_slice": len(initializes),
        "factories_seen": factories_seen,
        "candidate_v3_factory_confirmed": CANDIDATE_V3_FACTORY.lower() in factories_seen,
        "pool_managers_seen": pool_managers_seen,
        "n_v3_stock_pools_created_in_slice": len(v3_stock_pools),
        "n_v4_stock_pools_initialized_in_slice": len(v4_stock_pools),
        "v3_stock_pools_sample": v3_stock_pools[:20],
        "v4_stock_pools_sample": v4_stock_pools[:20],
        "requests_used": _request_count,
        "runtime_s": time.time() - t0,
    }

    # Калибровка x2.5 (та же дисциплина, что sprintR1/sprintSC1 -- см.
    # docs/PROJECT_STATE.md, "Правила работы с Dune", п.4, применена
    # здесь к RPC-запросам, а не к Dune-кредитам).
    projected_guard_requests = _request_count * 7 * 2.5
    result["projected_guard_stage_requests_x2.5"] = projected_guard_requests

    out_path = CACHE_DIR / "p3_discover_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[p3_guard][discover] записано {out_path}")
    print(json.dumps({k: v for k, v in result.items() if not k.endswith("_sample")}, indent=2, default=str))


def stage_guard() -> None:
    t0 = time.time()
    discover_path = CACHE_DIR / "p3_discover_result.json"
    if not discover_path.exists():
        print("[p3_guard][guard] СТОП: сначала --stage discover.")
        sys.exit(1)
    discover = json.loads(discover_path.read_text())

    factory = CANDIDATE_V3_FACTORY.lower() if discover["candidate_v3_factory_confirmed"] else None
    if factory is None and discover["factories_seen"]:
        factory = discover["factories_seen"][0]
    pool_managers = discover["pool_managers_seen"]

    if not pool_managers:
        _write_vacuous_result("В узком срезе discover не найдено ни одного адреса "
                               "V4 PoolManager (ни одного Initialize-события за 1 "
                               "день) -- нет оснований предполагать, что v4-активность "
                               "на сток-токенах вообще существует. Гард не может "
                               "найти кросс-версионные закрытия по определению.")
        return

    latest = get_block_number()
    _count()
    spb = discover["seconds_per_block_estimate"]
    genesis_block = block_for_timestamp(latest, spb, CHAIN_GENESIS)
    week_from_block = max(genesis_block, latest - blocks_for_days(spb, 7))

    print(f"[p3_guard][guard] latest={latest} genesis_block~={genesis_block} "
          f"week=[{week_from_block}, {latest}]")

    # Полная история пулов сток-токенов (address-scoped -- дёшево,
    # PoolCreated/Initialize редки относительно Swap).
    pc_logs = scan_logs(genesis_block, latest, [TOPIC0_V3_POOL_CREATED], address=factory) if factory else []
    pool_created = [decode_pool_created(l) for l in pc_logs]

    init_logs_all: list[dict] = []
    for pm in pool_managers:
        init_logs_all += scan_logs(genesis_block, latest, [TOPIC0_V4_INITIALIZE], address=pm)
    initializes = [decode_v4_initialize(l) for l in init_logs_all]

    stock = load_stock_universe()
    v3_by_token: dict[str, list[str]] = defaultdict(list)
    for r in pool_created:
        for side in ("token0", "token1"):
            if r[side] in stock:
                v3_by_token[r[side]].append(r["pool"])

    v4_by_token: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in initializes:
        for side in ("currency0", "currency1"):
            if r[side] in stock:
                v4_by_token[r[side]].append((r["pool_manager_address"], r["pool_id"]))

    both_versions = sorted(set(v3_by_token) & set(v4_by_token))
    print(f"[p3_guard][guard] сток-токенов с V3-пулом: {len(v3_by_token)}, "
          f"с V4-пулом: {len(v4_by_token)}, с ОБОИМИ: {len(both_versions)}")

    if not both_versions:
        _write_vacuous_result(
            f"Полная история с {CHAIN_GENESIS.date()}: {len(v3_by_token)} сток-токенов "
            f"имеют V3-пул, {len(v4_by_token)} имеют V4-пул, но их пересечение ПУСТО "
            f"-- ни один сток-токен из 194 (data/sprintR1_cache/r1_rwa_full_universe.csv) "
            f"не торгуется одновременно на v3 И v4. Явление 'кросс-версионная дислокация "
            f"сток-токена' на Robinhood Chain на сегодня не существует -- v4-пулы, "
            f"обнаруженные в discover/guard, относятся к другому продукту (см. "
            f"docs/G1_DESIGN.md: v4 -- hook-пул градуации мем-токенов pons.family, "
            f"НЕ RWA-сток-токены)."
        )
        return

    # Объём свопов за 7 дней -- ранжирование топ-5 ТОЛЬКО среди токенов
    # с обеими версиями (иначе "закрытие" по определению невозможно).
    counts: dict[str, int] = {}
    v3_swaps_by_token: dict[str, list[dict]] = {}
    v4_swaps_by_token: dict[str, list[dict]] = {}
    for tok in both_versions:
        v3_pools = sorted(set(v3_by_token[tok]))
        v3_logs = scan_logs(week_from_block, latest, [TOPIC0_V3_SWAP], address=v3_pools)
        v3_rows = [decode_v3_swap(l) for l in v3_logs]

        v4_rows: list[dict] = []
        for pm, pool_id in sorted(set(v4_by_token[tok])):
            v4_logs = scan_logs(week_from_block, latest, [[TOPIC0_V4_SWAP], [pool_id]], address=pm)
            v4_rows += [decode_v4_swap(l) for l in v4_logs]

        v3_swaps_by_token[tok] = v3_rows
        v4_swaps_by_token[tok] = v4_rows
        counts[tok] = len(v3_rows) + len(v4_rows)

    top5 = sorted(counts, key=lambda t: counts[t], reverse=True)[:TOP_N_TOKENS]
    print(f"[p3_guard][guard] топ-{len(top5)} по числу свопов (7д, v3+v4): "
          + ", ".join(f"{stock[t]}={counts[t]}" for t in top5))

    close_tx: set[str] = set()
    per_token_closes: dict[str, int] = {}
    for tok in top5:
        v3_tx = {r["tx_hash"] for r in v3_swaps_by_token[tok]}
        v4_tx = {r["tx_hash"] for r in v4_swaps_by_token[tok]}
        common = v3_tx & v4_tx
        per_token_closes[stock[tok]] = len(common)
        close_tx |= common

    if not close_tx:
        _write_vacuous_result(
            f"Топ-{len(top5)} токена с обеими версиями пула за 7 дней: "
            + ", ".join(f"{stock[t]} ({counts[t]} свопов)" for t in top5)
            + ". Ни одной транзакции с Swap-логами ОДНОВРЕМЕННО на v3 И v4 не найдено "
              "-- 0 закрытий дислокаций за окно. Доля топ-3 адресов не определена (0/0)."
        )
        return

    addr_counts: Counter[str] = Counter()
    for i, tx in enumerate(sorted(close_tx)):
        tx_info = get_transaction(tx)
        _count()
        frm = str(tx_info.get("from", "")).lower()
        addr_counts[frm] += 1

    total = sum(addr_counts.values())
    top3 = addr_counts.most_common(TOP_N_ADDRESSES)
    top3_sum = sum(c for _, c in top3)
    share = top3_sum / total if total else None
    verdict = "KILL (P3 закрывается окончательно)" if share is not None and share > CLOSE_SHARE_KILL_THRESHOLD else "не закрывается по этому гарду"

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "week_from_block": week_from_block,
        "week_to_block": latest,
        "tokens_with_both_versions": [stock[t] for t in both_versions],
        "top5_tokens_by_swap_count": {stock[t]: counts[t] for t in top5},
        "closes_per_token": per_token_closes,
        "total_close_tx": total,
        "top3_addresses": top3,
        "top3_share": share,
        "threshold": CLOSE_SHARE_KILL_THRESHOLD,
        "verdict": verdict,
        "requests_used": _request_count,
        "runtime_s": time.time() - t0,
    }
    out_path = CACHE_DIR / "p3_guard_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[p3_guard][guard] записано {out_path}")
    print(json.dumps(result, indent=2, default=str))


def _write_vacuous_result(reason: str) -> None:
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "N/A (явление не наблюдается -- см. reason)",
        "reason": reason,
        "requests_used": _request_count,
    }
    out_path = CACHE_DIR / "p3_guard_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[p3_guard][guard] {reason}")
    print(f"[p3_guard][guard] записано {out_path} (N/A)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["discover", "guard"], required=True)
    args = parser.parse_args()
    if args.stage == "discover":
        stage_discover()
    else:
        stage_guard()
