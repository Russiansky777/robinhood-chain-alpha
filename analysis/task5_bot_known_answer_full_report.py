#!/usr/bin/env python3
"""Полный отчёт по 10 захватам known-answer теста -- владелец попросил
(1) полные хэши/прибыль/пулы (уже есть в data/task5_bot_known_answer_test_
result.json, но требуется явный вывод) и (2) для ОДНОГО захвата -- разбор
по реальным Swap-логам: сколько токена вошло, сколько вышло, в каком
пуле, честная арифметика (не гадаем decimals -- для WETH/USDG знаем, для
прочих токенов печатаем raw amount и явно помечаем decimals неизвестны)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import RPC_URL_MAINNET, USDG, USDG_DECIMALS, WETH, WETH_DECIMALS
from task5_bot_pool_state import _rpc_call_with_provider_fallback

V3_SWAP_EVENT_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

FULL_TX_TARGET = "0x7AF12717684AEA0384AD50B9F99270853132B30BA786325F88B590CD64321F82"


def _rpc(method, params):
    return _rpc_call_with_provider_fallback(method, params, RPC_URL_MAINNET, timeout=20.0)


def _decode_int256(hex_word: str) -> int:
    v = int(hex_word, 16)
    if v >= 2 ** 255:
        v -= 2 ** 256
    return v


def _human(token_addr: str, raw: int) -> str:
    a = token_addr.lower()
    if a == WETH.lower():
        return f"{raw / 10 ** WETH_DECIMALS:.8f} WETH"
    if a == USDG.lower():
        return f"{raw / 10 ** USDG_DECIMALS:.6f} USDG"
    return f"{raw} raw (decimals неизвестны для {token_addr})"


def decode_swap_logs(tx_hash: str) -> list[dict]:
    receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return []
    out = []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != V3_SWAP_EVENT_TOPIC0:
            continue
        data = log["data"][2:]
        # Swap(address indexed sender, address indexed recipient, int256 amount0,
        # int256 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick)
        # -- non-indexed args в data, 5 слов по 32 байта (64 hex-символа)
        amount0 = _decode_int256(data[0:64])
        amount1 = _decode_int256(data[64:128])
        sqrt_price_x96 = int(data[128:192], 16)
        liquidity = int(data[192:256], 16)
        tick = _decode_int256(data[256:320])
        out.append({
            "pool": log.get("address"), "amount0_raw": amount0, "amount1_raw": amount1,
            "sqrt_price_x96_after": sqrt_price_x96, "liquidity_after": liquidity, "tick_after": tick,
        })
    return out


if __name__ == "__main__":
    result_path = Path("data/task5_bot_known_answer_test_result.json")
    known = json.loads(result_path.read_text())
    print("=== 10 захватов известного ответа: полные хэши / прибыль / пулы ===")
    table = []
    for row in known["rows"]:
        table.append({
            "tx_hash": row["tx_hash"], "block_number": row["block_number"],
            "executor": row["executor"], "profit_usd": row["profit_usd"],
            "real_pools_touched": row["real_pools_touched"],
            "would_detect": row["would_detect"], "fail_step": row["fail_step"],
        })
        print(json.dumps(table[-1], ensure_ascii=False))

    print(f"\n=== Разбор по Swap-логам ОДНОГО захвата: {FULL_TX_TARGET} ===")
    swaps = decode_swap_logs(FULL_TX_TARGET)
    known_row = next((r for r in known["rows"] if r["tx_hash"].lower() == FULL_TX_TARGET.lower()), None)
    breakdown = {"tx_hash": FULL_TX_TARGET, "profit_usd_reported": known_row["profit_usd"] if known_row else None,
                 "n_swap_logs": len(swaps), "swaps": []}
    for i, sw in enumerate(swaps):
        # Пул WETH/USDG (0x52e65b17...) -- token0=WETH, token1=USDG (см. task5_bot_config.py).
        # Пул 0xd4eb2120... -- token0=USDG, token1=неизвестный токен (по task5_bot_pool_registry_cache.json).
        pool_l = sw["pool"].lower()
        note = ""
        if pool_l == "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca":
            token0, token1 = WETH, USDG
        elif pool_l == "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3":
            token0, token1 = USDG, "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"
            note = "token1 -- decimals неизвестны в этой сессии, честно raw"
        else:
            token0 = token1 = None
        entry = {
            "leg": i, "pool": sw["pool"],
            "amount0_raw": sw["amount0_raw"], "amount1_raw": sw["amount1_raw"],
            "amount0_human": _human(token0, sw["amount0_raw"]) if token0 else f"{sw['amount0_raw']} raw",
            "amount1_human": _human(token1, sw["amount1_raw"]) if token1 else f"{sw['amount1_raw']} raw",
            "note": note,
            "interpretation": ("положительная amount = ток вошёл В пул (пул получил), "
                                "отрицательная = вышел ИЗ пула (пул отдал)"),
        }
        breakdown["swaps"].append(entry)
        print(json.dumps(entry, ensure_ascii=False, indent=2))

    Path("data/task5_bot_known_answer_full_report.json").write_text(
        json.dumps({"table": table, "one_tx_breakdown": breakdown}, indent=2, ensure_ascii=False))
    print("\n[full_report] записано в data/task5_bot_known_answer_full_report.json")
