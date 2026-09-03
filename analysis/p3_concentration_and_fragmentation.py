#!/usr/bin/env python3
"""Владелец, 2026-09-03 (дозапрос после mm_p3_v4leg_check.py -- ОБЕ ноги
с ликвидностью нашлись у всех 9 проверенных токенов):

"P3 — гард, а не разработка. Прежде чем что-либо строить: по 3 токенам
с наибольшим расхождением глубины (NVDA, COST и любой третий) за
последние 7 дней — какая доля сделок, закрывающих расхождение цены
между v3 и v4, приходится на топ-3 адреса, и каков типичный размер
расхождения в процентах. Порог регистрирую сейчас: >80% у топ-3 → P3
закрывается окончательно."

"Фрагментация — попутно, из тех же данных: какая доля объёма по
каждому токену идёт через второй по глубине пул и насколько там хуже
цена."

Определение "закрытия дислокации" -- НЕ придумано заново, то же самое,
что уже зарегистрировано владельцем 2026-09-01 в analysis/
p3_dislocation_guard.py / docs/P3_GUARD.md: транзакция, чьи логи
содержат ОДНОВРЕМЕННО V3 Swap-событие на v3-пуле токена И V4
Swap-событие на любом из v4 pool_id этого же токена (общий tx_hash --
атомарное кросс-пульное действие в одной транзакции).

Экономия: адреса/pool_id уже известны из УЖЕ ОПЛАЧЕННЫХ прогонов
(mm_pool_verify_result.json, mm_p3_v4leg_check_result.json) -- НЕ
повторное discovery факторики/PoolManager, сразу address-scoped скан
Swap-логов за 7 дней.

Выбор 3-го токена: владелец назвал NVDA и COST явно ("любой третий" --
на усмотрение). Берём по наибольшему расхождению глубины (макс.
liquidity_raw среди v4-пулов с ликвидностью / liquidity_raw v3-пула,
или обратное отношение, если v4 глубже) среди оставшихся 7 уже
проверенных токенов -- чисто из уже посчитанных данных, без нового
запроса (см. THIRD_TOKEN ниже, вычисляется в run()).

Только чтение, ключ не используется, транзакций нет.
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import decode as abi_decode  # noqa: E402

from alchemy_fallback import (  # noqa: E402
    _chunked_get_logs, _rpc_call, get_block, get_block_number, get_transaction_fast, topic0,
)
from mm_p5_setup import sqrt_price_to_usd  # noqa: E402

CACHE_DIR = Path("data/p3_guard_cache")
OUT_PATH = CACHE_DIR / "p3_concentration_result.json"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
USDG_DECIMALS = 6
STOCK_DECIMALS = 18

TOPIC0_V3_SWAP = topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")
TOPIC0_V4_SWAP = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")

TOP_N_ADDRESSES = 3
CLOSE_SHARE_KILL_THRESHOLD = 0.80
WINDOW_DAYS = 7
MAX_REQUESTS_PER_RUN = 12000
TIME_BUDGET_S = 80 * 60  # мягкий предел -- запас под 90-минутную дисциплину владельца

# NVDA/COST -- дословно от владельца. Третий выбирается ниже, по кэшу.
FORCED_TOKENS = ["NVDA", "COST"]

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n
    if _request_count > MAX_REQUESTS_PER_RUN:
        raise RuntimeError(f"[p3_concentration] СТОП: превышен потолок запросов "
                            f"({_request_count} > {MAX_REQUESTS_PER_RUN}).")


def _rpc(method: str, params: list):
    _count()
    return _rpc_call(method, params)


def load_known_pools() -> dict:
    """Собирает per-token адреса/pool_id из уже оплаченных прогонов --
    БЕЗ нового RPC. Источники: mm_pool_verify_result.json (v3-адреса
    NVDA/QQQ/RDDT/COST/GME/RBLX/LLY + v4 currency для SPY/MSTR),
    mm_p3_v4leg_check_result.json (все v4 pool_id с ликвидностью для
    первых 7 + все v3-пулы SPY/MSTR)."""
    pv = json.loads((CACHE_DIR / "mm_pool_verify_result.json").read_text())["part_a_pool_verification"]
    v4leg = json.loads((CACHE_DIR / "mm_p3_v4leg_check_result.json").read_text())["p3_cross_version_check"]

    out = {}
    for sym, v in v4leg["v3_confirmed_tokens_v4_leg"].items():
        v3info = pv[sym]["v3_hypothesis"]
        v4_pools = [p for p in v["v4_pools_found"] if p.get("has_liquidity")]
        out[sym] = {
            "v3_currency0": v3info["token0"], "v3_currency1": v3info["token1"],
            "v3_liquidity_raw": v3info["liquidity_raw"],
            "v4_pool_ids": [p["pool_id"] for p in v4_pools],
            "v4_liquidity_raw": {p["pool_id"]: p["liquidity_raw"] for p in v4_pools},
            "v4_currency0": {p["pool_id"]: p["currency0"] for p in v4_pools},
            "v4_currency1": {p["pool_id"]: p["currency1"] for p in v4_pools},
        }
    for sym, v in v4leg["v4_confirmed_tokens_v3_leg"].items():
        v4info = pv[sym]
        v3_pools = [p for p in v["v3_pools_found"] if p.get("has_liquidity")]
        # SPY/MSTR: единственная v4-нога -- берём poolId из USER_POOLS (mm_pool_verify.py), а
        # v3 -- список найденных здесь адресов (может быть >1, берём все с ликвидностью).
        out[sym] = {
            "v3_pool_addresses": [(p["pool_address"], p["liquidity_raw"]) for p in v3_pools],
            "v4_currency0": v4info["currency0"], "v4_currency1": v4info["currency1"],
            "v4_liquidity_raw": v4info["liquidity_raw"],
            "v4_pool_id": USER_POOLS[sym],
        }
    return out


# poolId владельца (mm_pool_verify.py, дословно) -- нужен для SPY/MSTR (единственная v4-нога)
USER_POOLS = {
    "SPY": "0xfe2a80bb5618fd14984b92ca6d45bf5ba67443ddb1435e28b2e48df2fc1526cd",
    "MSTR": "0x319bac87e616a89e241c10aeb8afd4892a852cdd8b373cd9765ecddc40b87cfe",
}
# v3-адреса владельца (mm_pool_verify.py, дословно) -- для первых 7 токенов
USER_POOL_ADDR = {
    "NVDA": "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3",
    "QQQ": "0xd60a5d14db690b7afad71f76b108071d7175597d",
    "RDDT": "0xa8744e76aed23b05f0126335e7bd38f7935d19fe",
    "COST": "0x0a2121a50a09ed0796ae81f9c53ff9398355a398",
    "GME": "0xe2b46c905e12ab8e2f864e4821a4325884c1b126",
    "RBLX": "0x1bdb8e3a79cb1a7f228808739311e23098d33d43",
    "LLY": "0xd2038788ebe1e0bfd7c0a6112f09778f3aeaeca6",
}


def select_third_token(pools: dict) -> tuple[str, dict]:
    """Расхождение глубины = max(v3/v4_max, v4_max/v3) среди уже
    известных чисел (без нового запроса). Среди оставшихся 7 (не
    NVDA/COST), берём наибольшее."""
    ratios = {}
    for sym, info in pools.items():
        if sym in FORCED_TOKENS or sym in ("SPY", "MSTR"):
            continue
        if "v3_liquidity_raw" not in info or not info.get("v4_liquidity_raw"):
            continue
        v3 = info["v3_liquidity_raw"]
        v4max = max(info["v4_liquidity_raw"].values())
        if v3 <= 0 or v4max <= 0:
            continue
        ratios[sym] = max(v3 / v4max, v4max / v3)
    third = max(ratios, key=ratios.get)
    return third, ratios


def estimate_seconds_per_block(latest: int) -> float:
    t_latest = int(get_block(latest)["timestamp"], 16)
    _count()
    early = max(1, latest - 200_000)
    t_early = int(get_block(early)["timestamp"], 16)
    _count()
    return (t_latest - t_early) / (latest - early)


def decode_v3_swap_full(log: dict) -> dict:
    data = bytes.fromhex(str(log["data"])[2:])
    amount0, amount1, sqrt_price_x96, liquidity, tick = abi_decode(
        ["int256", "int256", "uint160", "uint128", "int24"], data)
    return {"tx_hash": log["transactionHash"], "block_number": int(log["blockNumber"], 16),
            "log_index": int(log["logIndex"], 16), "amount0": amount0, "amount1": amount1,
            "sqrt_price_x96": sqrt_price_x96, "liquidity": liquidity}


def decode_v4_swap_full(log: dict) -> dict:
    data = bytes.fromhex(str(log["data"])[2:])
    amount0, amount1, sqrt_price_x96, liquidity, tick, fee = abi_decode(
        ["int128", "int128", "uint160", "uint128", "int24", "uint24"], data)
    pool_id = str(log["topics"][1]).lower()
    return {"tx_hash": log["transactionHash"], "block_number": int(log["blockNumber"], 16),
            "log_index": int(log["logIndex"], 16), "pool_id": pool_id, "amount0": amount0,
            "amount1": amount1, "sqrt_price_x96": sqrt_price_x96, "liquidity": liquidity}


def _price_and_usd_volume(sqrt_price_x96: int, amount0: int, amount1: int,
                           currency0: str, currency1: str) -> tuple[float, float]:
    """-> (цена токена в USDG, объём в USDG за этот своп)."""
    stock_is_token1 = currency1.lower() != USDG
    dec0 = USDG_DECIMALS if currency0.lower() == USDG else STOCK_DECIMALS
    dec1 = USDG_DECIMALS if currency1.lower() == USDG else STOCK_DECIMALS
    price = sqrt_price_to_usd(sqrt_price_x96, dec0, dec1, stock_is_token1)
    usdg_amount_raw = amount1 if currency1.lower() == USDG else amount0
    usdg_volume = abs(usdg_amount_raw) / (10 ** USDG_DECIMALS)
    return price, usdg_volume


def analyze_token(sym: str, info: dict, from_block: int, to_block: int) -> dict:
    print(f"\n[p3_concentration] === {sym} ===")
    is_v4_primary = "v3_pool_addresses" in info  # SPY/MSTR: единственная v4-нога, несколько v3-пулов

    if is_v4_primary:
        v3_pools = info["v3_pool_addresses"]  # [(addr, liquidity_raw)]
        v4_pool_ids = [info["v4_pool_id"]]
        v4_liq = {info["v4_pool_id"]: info["v4_liquidity_raw"]}
        v4_c0 = {info["v4_pool_id"]: info["v4_currency0"]}
        v4_c1 = {info["v4_pool_id"]: info["v4_currency1"]}
    else:
        v3_pools = [(USER_POOL_ADDR[sym], info["v3_liquidity_raw"])]
        v4_pool_ids = info["v4_pool_ids"]
        v4_liq = info["v4_liquidity_raw"]
        v4_c0 = info["v4_currency0"]
        v4_c1 = info["v4_currency1"]
        v3_c0, v3_c1 = info["v3_currency0"], info["v3_currency1"]

    # --- V3 Swap-логи (все известные v3-пулы токена) ---
    v3_swaps = []
    for addr, _liq in v3_pools:
        logs = list(_chunked_get_logs(from_block, to_block, [TOPIC0_V3_SWAP], chunk_size=50_000,
                                       address=addr, on_call=lambda lo, hi, n: _count(1)))
        for l in logs:
            row = decode_v3_swap_full(l)
            row["pool_address"] = addr
            v3_swaps.append(row)
    print(f"[p3_concentration] {sym}: v3 свопов за {WINDOW_DAYS}д: {len(v3_swaps)} "
          f"(пулов: {len(v3_pools)})")

    # --- V4 Swap-логи (OR-фильтр по всем pool_id этого токена сразу) ---
    v4_swaps = []
    if v4_pool_ids:
        logs = list(_chunked_get_logs(
            from_block, to_block, [TOPIC0_V4_SWAP, v4_pool_ids], chunk_size=50_000,
            address=POOL_MANAGER, on_call=lambda lo, hi, n: _count(1),
        ))
        v4_swaps = [decode_v4_swap_full(l) for l in logs]
    print(f"[p3_concentration] {sym}: v4 свопов за {WINDOW_DAYS}д: {len(v4_swaps)} "
          f"(pool_id с ликвидностью: {len(v4_pool_ids)})")

    # --- Цены/объём в USDG для каждого свопа ---
    for row in v3_swaps:
        if is_v4_primary:
            c0, c1 = None, None  # определим ниже per-pool (несколько v3-пулов у SPY/MSTR)
        else:
            c0, c1 = v3_c0, v3_c1
        if c0 is None:
            # для SPY/MSTR читаем token0()/token1() один раз на пул (кэшируем на уровне функции)
            c0, c1 = _pool_currencies_cache(row["pool_address"])
        price, vol = _price_and_usd_volume(row["sqrt_price_x96"], row["amount0"], row["amount1"], c0, c1)
        row["price_usdg"], row["volume_usdg"] = price, vol

    for row in v4_swaps:
        c0, c1 = v4_c0[row["pool_id"]], v4_c1[row["pool_id"]]
        price, vol = _price_and_usd_volume(row["sqrt_price_x96"], row["amount0"], row["amount1"], c0, c1)
        row["price_usdg"], row["volume_usdg"] = price, vol

    # --- Фрагментация: депозит-ранжирование ВСЕХ известных пулов (v3 + v4) по глубине ---
    depth_rank = [(f"v3:{addr}", liq) for addr, liq in v3_pools] + \
                 [(f"v4:{pid}", v4_liq[pid]) for pid in v4_pool_ids]
    depth_rank.sort(key=lambda x: x[1], reverse=True)

    vol_by_pool: dict[str, float] = {}
    price_by_pool: dict[str, list[float]] = {}
    for row in v3_swaps:
        key = f"v3:{row['pool_address']}"
        vol_by_pool[key] = vol_by_pool.get(key, 0.0) + row["volume_usdg"]
        price_by_pool.setdefault(key, []).append(row["price_usdg"])
    for row in v4_swaps:
        key = f"v4:{row['pool_id']}"
        vol_by_pool[key] = vol_by_pool.get(key, 0.0) + row["volume_usdg"]
        price_by_pool.setdefault(key, []).append(row["price_usdg"])

    total_vol = sum(vol_by_pool.values())
    fragmentation = None
    if len(depth_rank) >= 2 and total_vol > 0:
        top_key, second_key = depth_rank[0][0], depth_rank[1][0]
        top_vol = vol_by_pool.get(top_key, 0.0)
        second_vol = vol_by_pool.get(second_key, 0.0)
        top_prices = price_by_pool.get(top_key, [])
        second_prices = price_by_pool.get(second_key, [])
        top_avg_price = sum(top_prices) / len(top_prices) if top_prices else None
        second_avg_price = sum(second_prices) / len(second_prices) if second_prices else None
        price_worse_pct = None
        if top_avg_price and second_avg_price:
            price_worse_pct = (second_avg_price - top_avg_price) / top_avg_price * 100
        fragmentation = {
            "deepest_pool": top_key, "deepest_pool_liquidity_raw": depth_rank[0][1],
            "second_deepest_pool": second_key, "second_deepest_pool_liquidity_raw": depth_rank[1][1],
            "total_volume_usdg_week": total_vol,
            "second_pool_volume_share_of_total": second_vol / total_vol if total_vol else None,
            "second_pool_had_any_trades": second_vol > 0,
            "top_pool_avg_price_usdg": top_avg_price, "second_pool_avg_price_usdg": second_avg_price,
            "second_pool_price_worse_pct": price_worse_pct,
        }

    # --- Закрытия дислокаций: tx с V3 И V4 Swap одновременно ---
    v3_tx = {r["tx_hash"] for r in v3_swaps}
    v4_tx = {r["tx_hash"] for r in v4_swaps}
    close_tx = v3_tx & v4_tx

    # price gap % на каждой closing-tx: последняя v3-цена и последняя v4-цена ВНУТРИ этой tx
    v3_by_tx: dict[str, list[dict]] = {}
    for r in v3_swaps:
        v3_by_tx.setdefault(r["tx_hash"], []).append(r)
    v4_by_tx: dict[str, list[dict]] = {}
    for r in v4_swaps:
        v4_by_tx.setdefault(r["tx_hash"], []).append(r)

    gaps_pct = []
    addr_counts: Counter[str] = Counter()
    for tx in sorted(close_tx):
        v3_p = sorted(v3_by_tx[tx], key=lambda r: r["log_index"])[-1]["price_usdg"]
        v4_p = sorted(v4_by_tx[tx], key=lambda r: r["log_index"])[-1]["price_usdg"]
        if v3_p and v4_p:
            gaps_pct.append(abs(v3_p - v4_p) / v4_p * 100)
        tx_info = get_transaction_fast(tx)
        _count()
        addr_counts[str(tx_info.get("from", "")).lower()] += 1

    total_closes = sum(addr_counts.values())
    top3 = addr_counts.most_common(TOP_N_ADDRESSES)
    top3_sum = sum(c for _, c in top3)
    top3_share = top3_sum / total_closes if total_closes else None
    median_gap_pct = sorted(gaps_pct)[len(gaps_pct) // 2] if gaps_pct else None
    avg_gap_pct = sum(gaps_pct) / len(gaps_pct) if gaps_pct else None

    result = {
        "n_v3_swaps": len(v3_swaps), "n_v4_swaps": len(v4_swaps),
        "n_v4_pool_ids_with_liquidity": len(v4_pool_ids),
        "n_close_tx": len(close_tx),
        "close_tx_top3_addresses": top3, "close_tx_top3_share": top3_share,
        "typical_gap_pct_median": median_gap_pct, "typical_gap_pct_avg": avg_gap_pct,
        "gap_pct_samples_n": len(gaps_pct),
        "fragmentation": fragmentation,
        "requests_used_cumulative": _request_count,
    }
    print(f"[p3_concentration] {sym}: closing tx={len(close_tx)}, top3_share={top3_share}, "
          f"typical_gap_median%={median_gap_pct}")
    if fragmentation:
        print(f"[p3_concentration] {sym}: фрагментация -- 2й по глубине пул "
              f"{fragmentation['second_deepest_pool']}, доля объёма недели "
              f"{fragmentation['second_pool_volume_share_of_total']}, "
              f"цена хуже на {fragmentation['second_pool_price_worse_pct']}%")
    return result


_pool_currency_cache: dict[str, tuple[str, str]] = {}


def _pool_currencies_cache(pool_addr: str) -> tuple[str, str]:
    if pool_addr in _pool_currency_cache:
        return _pool_currency_cache[pool_addr]
    t0 = _rpc("eth_call", [{"to": pool_addr, "data": "0x" + topic0("token0()")[2:10]}, "latest"])
    t1 = _rpc("eth_call", [{"to": pool_addr, "data": "0x" + topic0("token1()")[2:10]}, "latest"])
    c0, c1 = "0x" + t0[-40:], "0x" + t1[-40:]
    _pool_currency_cache[pool_addr] = (c0, c1)
    return c0, c1


def run() -> int:
    t0 = time.time()
    pools = load_known_pools()
    third, ratios = select_third_token(pools)
    tokens = FORCED_TOKENS + [third]
    print(f"[p3_concentration] Токены: {tokens} (3-й выбран по макс. расхождению глубины "
          f"среди оставшихся: {json.dumps(ratios, default=str)})")

    latest = get_block_number()
    _count()
    spb = estimate_seconds_per_block(latest)
    from_block = max(1, latest - int(WINDOW_DAYS * 86400 / spb))
    print(f"[p3_concentration] latest={latest} spb~={spb:.4f} окно=[{from_block},{latest}] "
          f"(~{WINDOW_DAYS}д)")

    # НАЙДЕНО 2026-09-03 (первый прогон, публичный RPC): реальная плотность
    # свопов на этих пулах оказалась намного выше предположенной (NVDA --
    # 694 467 v3-свопов/7д, ~4133/час) -- один NVDA занял ~59 минут, весь
    # прогон 3 токенов ушёл далеко за 90-минутную дисциплину, пришлось
    # отменить на QQQ. Мягкий самоконтроль по токену (не только общий
    # потолок запросов) -- частичный результат honest, не тихая отмена.
    per_token = {}
    for sym in tokens:
        elapsed = time.time() - t0
        if elapsed > TIME_BUDGET_S:
            print(f"[p3_concentration] мягкий бюджет времени ({TIME_BUDGET_S}с) исчерпан ДО {sym} -- "
                  f"стоп, частичный результат по {list(per_token.keys())}")
            break
        per_token[sym] = analyze_token(sym, pools[sym], from_block, latest)

    overall_kill = any(v["close_tx_top3_share"] is not None and v["close_tx_top3_share"] > CLOSE_SHARE_KILL_THRESHOLD
                        for v in per_token.values())

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_days": WINDOW_DAYS, "from_block": from_block, "to_block": latest,
        "tokens_requested": tokens, "tokens_analyzed": list(per_token.keys()),
        "third_token_selection_ratios": ratios,
        "close_share_kill_threshold": CLOSE_SHARE_KILL_THRESHOLD,
        "per_token": per_token,
        "any_token_over_threshold": overall_kill,
        "verdict": ("P3 ЗАКРЫВАЕТСЯ ОКОНЧАТЕЛЬНО (>80% у топ-3 хотя бы на одном из проверенных токенов)"
                    if overall_kill else
                    "P3 НЕ закрывается этим гардом -- ни один из 3 проверенных токенов не превысил 80% "
                    "у топ-3 (или закрывающих транзакций слишком мало для вывода -- см. n_close_tx)"),
        "requests_used": _request_count, "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p3_concentration] записано {OUT_PATH}, {_request_count} запросов, {time.time()-t0:.0f}с")
    print(f"[p3_concentration] ВЕРДИКТ: {result['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
