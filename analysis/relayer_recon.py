#!/usr/bin/env python3
"""Задача A, разведка (владелец, 2026-09-02): какие токены/маршруты
реально включены на SpokePool Robinhood Chain, и есть ли вообще живая
активность -- ДО того, как строить полный наблюдатель на 24ч. Только
чтение, ключ не используется.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import across_common as ac  # noqa: E402
from alchemy_fallback import _rpc_call  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/relayer_recon_result.json")
SAMPLE_WINDOW_BLOCKS = 500_000  # ~последние ~14ч при ~0.1с/блок -- достаточно, чтобы увидеть реальную активность, не гонять всю историю


def run() -> int:
    latest_block = int(_rpc_call("eth_blockNumber", []), 16)
    print(f"[relayer_recon] SpokePool={ac.SPOKE_POOL}, deploy_block={ac.SPOKE_POOL_DEPLOY_BLOCK}, latest={latest_block}")

    # Resume: скан EnabledDepositRoute -- 2625 вызовов, ~50 мин на паблик
    # RPC, за всю (неизменную) историю чейна -- если кэш от предыдущего
    # прогона уже содержит его, не пересчитывать (баг в декодинге
    # сэмпла свалил первый прогон ПОСЛЕ этого дорогого шага -- потеря
    # результата экономически ощутима).
    cached = None
    if OUT_PATH.exists():
        try:
            cached = json.loads(OUT_PATH.read_text())
        except Exception:  # noqa: BLE001
            cached = None

    if cached and cached.get("deploy_block") == ac.SPOKE_POOL_DEPLOY_BLOCK and "all_enabled_deposit_route_events" in cached:
        routes = cached["all_enabled_deposit_route_events"]
        enabled_routes_now = cached["enabled_routes_now"]
        token_info = cached["token_info"]
        print(f"[relayer_recon] resume: {len(routes)} EnabledDepositRoute событий взято из кэша {OUT_PATH}, скан НЕ повторяется")
    else:
        # --- 1. Все EnabledDepositRoute за всю историю (редкое событие, большой chunk) ---
        route_logs = ac.fetch_enabled_deposit_route_logs(ac.SPOKE_POOL_DEPLOY_BLOCK, latest_block)
        routes = [ac.decode_enabled_deposit_route(l) for l in route_logs]
        routes.sort(key=lambda r: r["block_number"])

        # Текущее состояние (последнее событие по каждой паре origin_token+destination_chain_id побеждает)
        current_state: dict[tuple[str, int], dict] = {}
        for r in routes:
            key = (r["origin_token"].lower(), r["destination_chain_id"])
            current_state[key] = r

        distinct_tokens = sorted({r["origin_token"] for r in routes})
        token_info = {}
        for addr in distinct_tokens:
            symbol = ac.erc20_symbol(addr)
            decimals = ac.erc20_decimals(addr)
            token_info[addr] = {"symbol": symbol, "decimals": decimals}
            print(f"[relayer_recon] token {addr}: symbol={symbol!r} decimals={decimals}")

        enabled_routes_now = [
            {**r, "token_symbol": token_info.get(r["origin_token"], {}).get("symbol")}
            for r in current_state.values() if r["enabled"]
        ]

        # Промежуточная запись ДО сэмпла свопов -- см. docstring выше.
        partial = {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "spoke_pool": ac.SPOKE_POOL,
            "deploy_block": ac.SPOKE_POOL_DEPLOY_BLOCK,
            "latest_block": latest_block,
            "all_enabled_deposit_route_events": routes,
            "enabled_routes_now": enabled_routes_now,
            "token_info": token_info,
            "sample_stage": "not_started",
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(partial, indent=2, default=str))
        print(f"[relayer_recon] промежуточная запись (маршруты+токены) -- {OUT_PATH}")

    # --- 2. Сэмпл недавней активности (депозиты И филлы) ---
    sample_from = max(ac.SPOKE_POOL_DEPLOY_BLOCK, latest_block - SAMPLE_WINDOW_BLOCKS)
    print(f"[relayer_recon] сэмпл активности: блоки {sample_from}..{latest_block}")

    n_dep_calls = 0

    def _count_dep(lo, hi, n):
        nonlocal n_dep_calls
        n_dep_calls += 1

    deposit_logs = list(ac.fetch_deposit_logs(sample_from, latest_block, on_call=_count_dep))
    deposits, deposit_decode_errors = [], []
    for l in deposit_logs:
        try:
            deposits.append(ac.decode_funds_deposited(l))
        except Exception as e:  # noqa: BLE001 -- баг первого прогона: NonEmptyPaddingBytes
            # на реальном логе -- вероятно, схема события на этом деплое
            # отличается от дословной V3SpokePoolInterface.sol (не
            # выдумываем расшифровку -- сохраняем сырой лог для разбора).
            deposit_decode_errors.append({"error": str(e), "raw_log": l})
    print(f"[relayer_recon] FundsDeposited в сэмпле: {len(deposits)} декодировано, "
          f"{len(deposit_decode_errors)} НЕ декодировано ({n_dep_calls} вызовов)")

    n_fill_calls = 0

    def _count_fill(lo, hi, n):
        nonlocal n_fill_calls
        n_fill_calls += 1

    fill_logs = list(ac.fetch_filled_relay_logs(sample_from, latest_block, on_call=_count_fill))
    fills, fill_decode_errors = [], []
    for l in fill_logs:
        try:
            fills.append(ac.decode_filled_relay(l))
        except Exception as e:  # noqa: BLE001
            fill_decode_errors.append({"error": str(e), "raw_log": l})
    print(f"[relayer_recon] FilledRelay в сэмпле: {len(fills)} декодировано, "
          f"{len(fill_decode_errors)} НЕ декодировано ({n_fill_calls} вызовов)")

    dest_chain_ids_seen = sorted({d["destination_chain_id"] for d in deposits})
    origin_chain_ids_seen = sorted({f["origin_chain_id"] for f in fills})
    input_tokens_seen_deposits = sorted({d["input_token"] for d in deposits})
    output_tokens_seen_fills = sorted({f["output_token"] for f in fills})

    # Символы токенов, увиденных в сэмпле (может быть не пересечение с EnabledDepositRoute,
    # напр. если чек делался ДО добавления маршрута, или используется дефолтный маршрут)
    for addr in set(input_tokens_seen_deposits) | set(output_tokens_seen_fills):
        if addr not in token_info:
            token_info[addr] = {"symbol": ac.erc20_symbol(addr), "decimals": ac.erc20_decimals(addr)}

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spoke_pool": ac.SPOKE_POOL,
        "deploy_block": ac.SPOKE_POOL_DEPLOY_BLOCK,
        "latest_block": latest_block,
        "all_enabled_deposit_route_events": routes,
        "enabled_routes_now": enabled_routes_now,
        "token_info": token_info,
        "sample_window": {"from_block": sample_from, "to_block": latest_block},
        "sample_n_deposits": len(deposits),
        "sample_n_fills": len(fills),
        "sample_n_deposit_decode_errors": len(deposit_decode_errors),
        "sample_n_fill_decode_errors": len(fill_decode_errors),
        "sample_destination_chain_ids_seen_in_deposits": dest_chain_ids_seen,
        "sample_origin_chain_ids_seen_in_fills": origin_chain_ids_seen,
        "sample_deposits": deposits,
        "sample_fills": fills,
        "sample_deposit_decode_errors": deposit_decode_errors,
        "sample_fill_decode_errors": fill_decode_errors,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str))
    print(f"\n[relayer_recon] записано {OUT_PATH}")
    print(f"[relayer_recon] маршруты сейчас включены: {json.dumps(enabled_routes_now, indent=2, default=str)}")
    print(f"[relayer_recon] chainId контрагентов, увиденные в депозитах (Robinhood->X): {dest_chain_ids_seen}")
    print(f"[relayer_recon] chainId контрагентов, увиденные в филлах (X->Robinhood): {origin_chain_ids_seen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
