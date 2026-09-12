#!/usr/bin/env python3
"""Robinhood Chain (chain 4663) -- реальное измерение расхождения цены
между ДВУМЯ известными пулами WETH/USDG:
  - 0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca (P5, основной)
  - 0x69bfaf19c9f377bb306a89aed9f6b07e2c1a8d9a (второй, менее активный)

Владелец: "взять последние 50 свопов каждого с временем и ценой, найти
моменты, когда цены расходились больше чем на 0.35%, и сколько секунд
это длилось до выравнивания. Кто выровнял -- какой адрес."

Метод -- РЕАЛЬНЫЙ, чисто RPC (без Dune): `eth_getLogs` (топик реального,
keccak-проверенного Swap-события Uniswap V3) на публичный RPC (широкий
диапазон блоков, тот же путь, что PoolCreated-скан бота -- НЕ через
провайдера, тот режет eth_getLogs до 10 блоков на free tier). Оба пула --
token0=WETH/token1=USDG в ОДНОМ порядке (проверено в реестре бота
заранее), поэтому raw-соотношение sqrtPriceX96 сравнимо НАПРЯМУЮ между
пулами без поправки на decimals (та же логика, что в
`check_all_pairs_price_divergence`).

Метод "кто выровнял": адрес `from` транзакции, чей своп СНИЗИЛ разрыв
ниже 0.35% (реальный `eth_getTransactionByHash`), а не предположение."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import RPC_URL_MAINNET
from task5_bot_pool_state import SWAP_TOPIC0, _rpc_call, _rpc_call_with_provider_fallback

POOL_A = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
POOL_B = "0x69bfaf19c9f377bb306a89aed9f6b07e2c1a8d9a"
N_SWAPS_PER_POOL = 50
DIVERGENCE_THRESHOLD = 0.0035  # владелец: "больше чем на 0.35%"
INITIAL_LOOKBACK_BLOCKS = 20_000
MAX_LOOKBACK_BLOCKS = 2_000_000


def _decode_int256(hex_word: str) -> int:
    v = int(hex_word, 16)
    if v >= 2 ** 255:
        v -= 2 ** 256
    return v


def _rpc_provider(method, params):
    return _rpc_call_with_provider_fallback(method, params, RPC_URL_MAINNET, timeout=20.0)


def fetch_last_n_swaps(pool_addr: str, latest_block: int, n: int = N_SWAPS_PER_POOL) -> list[dict]:
    lookback = INITIAL_LOOKBACK_BLOCKS
    logs: list[dict] = []
    while lookback <= MAX_LOOKBACK_BLOCKS:
        from_block = max(0, latest_block - lookback)
        logs = _rpc_call("eth_getLogs", [{
            "fromBlock": hex(from_block), "toBlock": hex(latest_block),
            "address": pool_addr, "topics": [SWAP_TOPIC0],
        }], RPC_URL_MAINNET)
        if len(logs) >= n:
            break
        lookback *= 4
    logs_sorted = sorted(logs, key=lambda log: (int(log["blockNumber"], 16), int(log["logIndex"], 16)))
    return logs_sorted[-n:]


def decode_swap_log(log: dict, pool_addr: str) -> dict:
    data = log["data"][2:]
    amount0 = _decode_int256(data[0:64])
    amount1 = _decode_int256(data[64:128])
    sqrt_price_x96 = int(data[128:192], 16)
    liquidity = int(data[192:256], 16)
    tick = _decode_int256(data[256:320])
    price = (sqrt_price_x96 / (2 ** 96)) ** 2  # token1/token0 raw ratio, БЕЗ поправки на decimals (см. докстринг)
    return {
        "pool": pool_addr, "block_number": int(log["blockNumber"], 16),
        "log_index": int(log["logIndex"], 16), "tx_hash": log["transactionHash"],
        "amount0_raw": amount0, "amount1_raw": amount1, "sqrt_price_x96": sqrt_price_x96,
        "liquidity": liquidity, "tick": tick, "raw_price_ratio": price,
    }


def main() -> None:
    latest_hex = _rpc_provider("eth_blockNumber", [])
    latest_block = int(latest_hex, 16)
    print(f"[divergence] последний блок: {latest_block}")

    swaps_a_raw = fetch_last_n_swaps(POOL_A, latest_block)
    swaps_b_raw = fetch_last_n_swaps(POOL_B, latest_block)
    print(f"[divergence] пул A ({POOL_A}): {len(swaps_a_raw)} свопов найдено")
    print(f"[divergence] пул B ({POOL_B}): {len(swaps_b_raw)} свопов найдено")

    swaps_a = [decode_swap_log(lg, POOL_A) for lg in swaps_a_raw]
    swaps_b = [decode_swap_log(lg, POOL_B) for lg in swaps_b_raw]

    # реальная текущая цена -- последний своп каждого пула
    current_price_a = swaps_a[-1]["raw_price_ratio"] if swaps_a else None
    current_price_b = swaps_b[-1]["raw_price_ratio"] if swaps_b else None
    print(f"[divergence] текущая цена (raw token1/token0) пул A: {current_price_a}")
    print(f"[divergence] текущая цена (raw token1/token0) пул B: {current_price_b}")
    if current_price_a and current_price_b:
        cur_rel = abs(current_price_a - current_price_b) / min(current_price_a, current_price_b)
        print(f"[divergence] текущее расхождение: {cur_rel:.4%}")

    # timestamp для каждого затронутого блока -- один реальный eth_getBlockByNumber на блок
    all_blocks = sorted({s["block_number"] for s in swaps_a} | {s["block_number"] for s in swaps_b})
    block_ts: dict[int, int] = {}
    for b in all_blocks:
        blk = _rpc_provider("eth_getBlockByNumber", [hex(b), False])
        block_ts[b] = int(blk["timestamp"], 16) if blk else None

    for s in swaps_a + swaps_b:
        s["timestamp"] = block_ts.get(s["block_number"])

    timeline = sorted(swaps_a + swaps_b, key=lambda s: (s["timestamp"] or 0, s["block_number"], s["log_index"]))

    last_price = {POOL_A: None, POOL_B: None}
    episodes = []
    in_divergence = False
    episode_start_ts = None
    episode_start_rel = None
    episode_peak_rel = None

    for ev in timeline:
        last_price[ev["pool"]] = ev["raw_price_ratio"]
        pa, pb = last_price[POOL_A], last_price[POOL_B]
        if pa is None or pb is None or ev["timestamp"] is None:
            continue
        rel = abs(pa - pb) / min(pa, pb)

        if not in_divergence and rel >= DIVERGENCE_THRESHOLD:
            in_divergence = True
            episode_start_ts = ev["timestamp"]
            episode_start_rel = rel
            episode_peak_rel = rel
        elif in_divergence:
            episode_peak_rel = max(episode_peak_rel, rel)
            if rel < DIVERGENCE_THRESHOLD:
                duration_s = ev["timestamp"] - episode_start_ts
                leveler_tx = ev["tx_hash"]
                try:
                    tx_obj = _rpc_provider("eth_getTransactionByHash", [leveler_tx])
                    leveler_addr = (tx_obj.get("from") or "").lower() if tx_obj else None
                except Exception as exc:
                    leveler_addr = None
                    print(f"[divergence] не удалось получить from для {leveler_tx}: {exc}", file=sys.stderr)
                episodes.append({
                    "start_ts": episode_start_ts, "end_ts": ev["timestamp"], "duration_s": duration_s,
                    "start_rel_divergence": episode_start_rel, "peak_rel_divergence": episode_peak_rel,
                    "leveling_pool": ev["pool"], "leveling_tx_hash": leveler_tx, "leveling_address": leveler_addr,
                })
                in_divergence = False
                episode_start_ts = None
                episode_start_rel = None
                episode_peak_rel = None

    # если датасет закончился, а расхождение так и не выровнялось -- честно фиксируем, не считаем эпизодом
    still_open = in_divergence

    span_s = (timeline[-1]["timestamp"] - timeline[0]["timestamp"]) if len(timeline) >= 2 and timeline[0]["timestamp"] and timeline[-1]["timestamp"] else None
    n_per_hour = (len(episodes) / span_s * 3600) if span_s else None
    avg_magnitude = (sum(e["peak_rel_divergence"] for e in episodes) / len(episodes)) if episodes else None
    avg_duration_s = (sum(e["duration_s"] for e in episodes) / len(episodes)) if episodes else None

    result = {
        "pool_a": POOL_A, "pool_b": POOL_B, "current_price_raw_a": current_price_a,
        "current_price_raw_b": current_price_b,
        "current_rel_divergence": (abs(current_price_a - current_price_b) / min(current_price_a, current_price_b)
                                    if current_price_a and current_price_b else None),
        "n_swaps_pool_a_found": len(swaps_a), "n_swaps_pool_b_found": len(swaps_b),
        "timeline_span_seconds": span_s,
        "threshold": DIVERGENCE_THRESHOLD, "n_episodes": len(episodes),
        "episode_still_open_at_end_of_dataset": still_open,
        "n_episodes_per_hour_extrapolated": n_per_hour,
        "avg_peak_divergence_fraction": avg_magnitude, "avg_duration_seconds": avg_duration_s,
        "episodes": episodes,
        "swaps_a": swaps_a, "swaps_b": swaps_b,
    }
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path("data/task5_pool_pair_divergence_measurement_result.json").write_text(text)
    print(f"\n[divergence] ИТОГ: {len(episodes)} эпизодов расхождения >= {DIVERGENCE_THRESHOLD:.2%} за "
          f"{span_s} сек охвата данных ({n_per_hour} в час, если экстраполировать), "
          f"средняя величина (пик) {avg_magnitude}, среднее время жизни {avg_duration_s} сек")


if __name__ == "__main__":
    main()
