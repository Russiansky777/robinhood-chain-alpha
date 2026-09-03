#!/usr/bin/env python3
"""Задача MM, вариант 1 с ограничениями (владелец, 2026-09-03):
предфильтр по РЕАЛЬНОЙ ликвидности вместо дорогой калибровки плотности
Swap-логов по всем 1573 кандидатам из discover (`docs/MM_RECON.md`).

Метод чтения ликвидности v4-пула -- ПРАВИЛЬНЫЙ для Uniswap V4, НЕ
устаревший v2 `getReserves()`: v4 использует PoolManager-синглтон с
внутренним учётом через storage-слоты, читаемыми `extsload()`
(`IExtsload`, часть `PoolManager`). Формулы и константы взяты
ДОСЛОВНО через WebFetch реального `Uniswap/v4-core/src/libraries/
StateLibrary.sol` (2026-09-03, не по памяти):

    POOLS_SLOT = 6
    LIQUIDITY_OFFSET = 3
    stateSlot = keccak256(abi.encodePacked(poolId, POOLS_SLOT))
    slot0_raw = extsload(stateSlot)              -- packed: sqrtPriceX96(160 бит) | tick(24) | protocolFee(24) | lpFee(24)
    liquidity_raw = extsload(stateSlot + LIQUIDITY_OFFSET)  -- uint128(liquidity) в младших 128 битах

Оба слота читаются ОДНИМ вызовом через `extsload(bytes32[])` (batch),
не двумя отдельными -- вдвое дешевле.

Для v3-пулов (72 из 1573 кандидатов) -- стандартные публичные геттеры
`liquidity()` и `slot0()` того же формата sqrtPriceX96 (Q64.96),
дословно из `IUniswapV3PoolState`.

**Оценка глубины -- ЯВНО ПОМЕЧЕНА КАК ПРИБЛИЖЕНИЕ, не точный TVL**:
используется допущение "позиция на весь диапазон" (full-range), а не
реальный диапазон тика (который потребовал бы перечисления всех
позиций -- на порядки дороже). Стандартная формула при sqrtP в Q64.96:
    reserve_quote (token0) ~= L * 2**96 / sqrtPriceX96
    reserve_quote (token1) ~= L * sqrtPriceX96 / 2**96
Итоговая оценка глубины пула в USD = 2 x (котируемая_сторона_в_USD)
(симметричное приближение full-range).

Котируемые активы (те же 3, что в discover, УЖЕ подтверждены реальными
через СОВЕРШЕННО ДРУГОЙ источник -- депозиты Across, docs/
RELAYER_RECON.md): WETH, USDG, нативный ETH. Цена ETH/USD -- живой
CoinGecko (GH Actions runner). USDG считается ~$1 (по названию/тикеру
-- ПОМЕЧЕНО как допущение, не независимо проверено).

Лимит по времени (владелец, п.1): не более 60 минут RPC-работы. Скрипт
сам следит за временем (мягкий предел 55 мин, с запасом на запись) и
останавливается, сохраняя частичный прогресс, если не укладывается --
НЕ падает и НЕ продолжает бесконечно.

Только чтение (`eth_call`), ключ не используется, транзакций нет.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from Crypto.Hash import keccak  # noqa: E402
from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402

from alchemy_fallback import _rpc_call, topic0  # noqa: E402

DISCOVER_PATH = Path("data/p3_guard_cache/mm_discover_result.json")
OUT_PATH = Path("data/p3_guard_cache/mm_liquidity_prefilter_result.json")

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE = "0x0000000000000000000000000000000000000000"
QUOTE_DECIMALS = {WETH: 18, USDG: 6, NATIVE: 18}
QUOTE_LABEL = {WETH: "WETH", USDG: "USDG", NATIVE: "native ETH"}

TIME_BUDGET_S = 55 * 60  # мягкий предел (владелец: "не более 60 минут"), запас на финальную запись
POOLS_SLOT = 6
LIQUIDITY_OFFSET = 3

_request_count = 0


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def _eth_call(to: str, data: str) -> str:
    global _request_count
    _request_count += 1
    return _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])


def _get_pool_state_slot(pool_id_hex: str) -> int:
    # keccak256(abi.encodePacked(poolId, uint256(POOLS_SLOT))) -- дословно StateLibrary.sol
    packed = bytes.fromhex(pool_id_hex[2:].rjust(64, "0")) + (POOLS_SLOT).to_bytes(32, "big")
    h = keccak.new(digest_bits=256)
    h.update(packed)
    return int.from_bytes(h.digest(), "big")


def read_v4_pool(pool_manager: str, pool_id_hex: str) -> tuple[int, int] | None:
    """-> (sqrtPriceX96, liquidity) или None при ошибке."""
    state_slot = _get_pool_state_slot(pool_id_hex)
    liquidity_slot = state_slot + LIQUIDITY_OFFSET
    slots = [state_slot.to_bytes(32, "big"), liquidity_slot.to_bytes(32, "big")]
    calldata = "0x" + _selector("extsload(bytes32[])")[2:] + abi_encode(["bytes32[]"], [slots]).hex()
    try:
        result = _eth_call(pool_manager, calldata)
        (raw_words,) = abi_decode(["bytes32[]"], bytes.fromhex(result[2:]))
        slot0_int = int.from_bytes(raw_words[0], "big")
        liquidity_int = int.from_bytes(raw_words[1], "big") & ((1 << 128) - 1)
        sqrt_price_x96 = slot0_int & ((1 << 160) - 1)
        return sqrt_price_x96, liquidity_int
    except Exception as e:  # noqa: BLE001
        print(f"[mm_liquidity_prefilter] v4 extsload {pool_manager}/{pool_id_hex} не удался: {e}")
        return None


def read_v3_pool(pool_address: str) -> tuple[int, int] | None:
    """-> (sqrtPriceX96, liquidity) или None при ошибке. IUniswapV3PoolState, дословные сигнатуры."""
    try:
        liq_result = _eth_call(pool_address, "0x" + _selector("liquidity()")[2:])
        (liquidity_int,) = abi_decode(["uint128"], bytes.fromhex(liq_result[2:]))
        slot0_result = _eth_call(pool_address, "0x" + _selector("slot0()")[2:])
        decoded = abi_decode(
            ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
            bytes.fromhex(slot0_result[2:]),
        )
        sqrt_price_x96 = decoded[0]
        return sqrt_price_x96, liquidity_int
    except Exception as e:  # noqa: BLE001
        print(f"[mm_liquidity_prefilter] v3 {pool_address} не удался: {e}")
        return None


def quote_reserve_raw(sqrt_price_x96: int, liquidity: int, quote_is_token0: bool) -> int:
    """Full-range приближение (см. докстринг модуля) -- НЕ точный TVL."""
    if sqrt_price_x96 == 0:
        return 0
    if quote_is_token0:
        return (liquidity << 96) // sqrt_price_x96
    return (liquidity * sqrt_price_x96) >> 96


def eth_usd_price() -> tuple[float, str]:
    import requests
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        price = resp.json()["ethereum"]["usd"]
        return float(price), "CoinGecko live"
    except Exception as e:  # noqa: BLE001
        print(f"[mm_liquidity_prefilter] CoinGecko недоступен: {e} -- используется фолбэк")
        return 1895.565143603286, "fallback (Dune median 01-13.08.2026, STALE)"


def load_candidates() -> dict[str, dict]:
    """symbol -> {"v3": [(pool_addr, cp), ...], "v4": [(pm, pool_id, cp), ...]}
    -- РОВНО те 1573 кандидата (WETH/USDG/native counterparty), что
    были найдены и доложены владельцу в docs/MM_RECON.md, п.3 --
    воспроизводится тем же фильтром из уже закоммиченного discover-результата,
    БЕЗ повторного RPC-скана."""
    d = json.loads(DISCOVER_PATH.read_text())
    quotes = {WETH, USDG, NATIVE}
    out = {}
    for sym, p in d["pools_by_symbol"].items():
        v3 = [(pool, cp) for pool, cp in p["v3_pools_with_shared_counterparty"] if cp.lower() in quotes]
        v4 = [(pm, pid, cp) for pm, pid, cp in p["v4_pools_with_shared_counterparty"] if cp.lower() in quotes]
        out[sym] = {"v3": v3, "v4": v4, "token_address": p["token_address"]}
    return out


def run() -> int:
    t0 = time.time()
    candidates = load_candidates()
    n_v3_total = sum(len(v["v3"]) for v in candidates.values())
    n_v4_total = sum(len(v["v4"]) for v in candidates.values())
    print(f"[mm_liquidity_prefilter] кандидатов: {n_v3_total} v3 + {n_v4_total} v4 = {n_v3_total + n_v4_total}")

    eth_usd, eth_usd_source = eth_usd_price()
    print(f"[mm_liquidity_prefilter] ETH/USD = {eth_usd} ({eth_usd_source})")

    per_symbol_pools: dict[str, list[dict]] = {sym: [] for sym in candidates}
    stopped_early = False
    n_processed = 0
    n_total = n_v3_total + n_v4_total

    def _quote_usd_price(quote_addr: str) -> float:
        if quote_addr in (WETH, NATIVE):
            return eth_usd
        return 1.0  # USDG -- допущение ~$1 по тикеру, НЕ независимо проверено

    def _time_left_ok() -> bool:
        return (time.time() - t0) < TIME_BUDGET_S

    def _write_partial():
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps({
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stopped_early": stopped_early,
            "n_candidates_total": n_total,
            "n_processed": n_processed,
            "eth_usd_price": eth_usd,
            "eth_usd_source": eth_usd_source,
            "usdg_usd_assumption": "1.0 (по тикеру, не проверено независимо)",
            "per_symbol_pools_partial": per_symbol_pools,
            "requests_used": _request_count,
            "runtime_s": time.time() - t0,
        }, indent=2, default=str, ensure_ascii=False))

    for sym, c in candidates.items():
        token_addr = c["token_address"]

        for pool_addr, cp in c["v3"]:
            if not _time_left_ok():
                stopped_early = True
                break
            res = read_v3_pool(pool_addr)
            n_processed += 1
            if res is None:
                continue
            sqrt_price, liquidity = res
            quote_is_token0 = cp.lower() < token_addr.lower()
            reserve_raw = quote_reserve_raw(sqrt_price, liquidity, quote_is_token0)
            reserve_human = reserve_raw / (10 ** QUOTE_DECIMALS[cp.lower()])
            quote_usd = reserve_human * _quote_usd_price(cp.lower())
            value_usd = 2 * quote_usd
            per_symbol_pools[sym].append({
                "version": "v3", "pool_address": pool_addr, "quote_asset": cp.lower(),
                "quote_label": QUOTE_LABEL[cp.lower()], "liquidity_raw": liquidity,
                "sqrt_price_x96": sqrt_price, "value_usd_fullrange_approx": value_usd,
            })
        if stopped_early:
            break

        for pm, pool_id, cp in c["v4"]:
            if not _time_left_ok():
                stopped_early = True
                break
            res = read_v4_pool(pm, pool_id)
            n_processed += 1
            if res is None:
                continue
            sqrt_price, liquidity = res
            quote_is_token0 = cp.lower() < token_addr.lower()
            reserve_raw = quote_reserve_raw(sqrt_price, liquidity, quote_is_token0)
            reserve_human = reserve_raw / (10 ** QUOTE_DECIMALS[cp.lower()])
            quote_usd = reserve_human * _quote_usd_price(cp.lower())
            value_usd = 2 * quote_usd
            per_symbol_pools[sym].append({
                "version": "v4", "pool_manager": pm, "pool_id": pool_id, "quote_asset": cp.lower(),
                "quote_label": QUOTE_LABEL[cp.lower()], "liquidity_raw": liquidity,
                "sqrt_price_x96": sqrt_price, "value_usd_fullrange_approx": value_usd,
            })

        if n_processed % 100 < 2:  # периодическая промежуточная запись
            _write_partial()
            print(f"[mm_liquidity_prefilter] прогресс: {n_processed}/{n_total} "
                  f"({time.time()-t0:.0f}с, {_request_count} запросов)")

        if stopped_early:
            break

    _write_partial()
    print(f"\n[mm_liquidity_prefilter] завершено: {n_processed}/{n_total} обработано "
          f"({'ОСТАНОВЛЕНО по лимиту времени' if stopped_early else 'полностью'}), "
          f"{_request_count} запросов, {time.time()-t0:.0f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
