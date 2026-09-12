#!/usr/bin/env python3
"""Задача 5, стадия 2 (продолжение): для хук-пула ETH/MOSIAI критично
знать, что именно возвращает V4Quoter.quoteExactInputSingle -- ВАЛОВУЮ
сумму свопа (как в поле amount1 самого Swap-события PoolManager) или
уже НЕТТО-сумму, реально достающуюся вызывающему ПОСЛЕ того, как хук
(AFTER_SWAP_RETURNS_DELTA) заберёт свою долю.

Реально измерено ранее в этой сессии (analysis/
task5_v4_hook_route_audit.py, data/task5_v4_hook_route_audit_result.json):
хук забирает РОВНО 2.00% выхода MOSIAI в ОБЕИХ контрольных tx маршрута
ETH->MOSIAI->USDG->ETH (58204.54 / 2910227.18 и 27218.51 / 1360925.36 --
оба ровно 0.02, независимо подтверждено дважды). Если Quoter вернёт
ВАЛОВУЮ цифру -- наш код будет считать несуществующие +2% прибыли на
каждой сделке через этот пул, и это надо ловить здесь, а не в проде.

Метод: для каждой из двух контрольных tx запрашиваем quoteExactInputSingle
на блоке (tx_block - 1) с ТЕМ ЖЕ входным размером ETH, что реально
использовался, и сравниваем результат с обеими реальными цифрами --
ВАЛОВОЙ (Swap.amount1) и НЕТТО (Transfer PoolManager->executor)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import requests  # noqa: E402

from alchemy_fallback import _alchemy_direct_endpoint, _BASE_HEADERS, _rpc_call  # noqa: E402

from task5_v4_pool_math import (  # noqa: E402
    PoolKey, decode_quote_result, quote_exact_input_single_calldata,
)

V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
ETH_CURRENCY = "0x0000000000000000000000000000000000000000"
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
HOOK_ETH_MOSIAI = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"

# Реальный PoolKey ETH/MOSIAI, взят из настоящего Initialize-события
# PoolManager (data/task5_v4_hook_route_audit_result.json), не предположен.
POOL_ETH_MOSIAI = PoolKey(ETH_CURRENCY, MOSIAI, 0, 200, HOOK_ETH_MOSIAI)

# Реальные наблюдения из data/task5_v4_hook_route_audit_result.json.
CASES = [
    {
        "tx_hash": "0x658c2ab808cd0fa30f5dc14a5ea0e6259fe11714b842efcb5721c3acd320274f",
        "tx_block": 61248715,  # hex "0x3a694cb" из receipt.blockNumber (data/task5_v4_hook_route_audit_result.json)
        "amount_in_eth_wei": 324259173170675712,
        "gross_swap_amount1": 2910227184914870645595017,
        "net_to_executor": 2852022641216573232683117,
        "hook_skim": 58204543698297412911900,
    },
    {
        "tx_hash": "0x9976a38fec4f4d2be228118da68b1276a26e7f33927ea606d11348d31e2ed98c",
        "tx_block": 61248775,  # hex "0x3a69507" из receipt.blockNumber (data/task5_v4_hook_route_audit_result.json)
        "amount_in_eth_wei": 81064793292668928,
        "gross_swap_amount1": 1360925357002413753012724,
        "net_to_executor": 1333706849862365477952470,
        "hook_skim": 27218507140048275060254,
    },
]


def _eth_call_alchemy_direct(to: str, data: str, block_tag: str) -> str | None:
    url = _alchemy_direct_endpoint()
    if not url:
        return None
    try:
        resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                         "params": [{"to": to, "data": data}, block_tag]},
                             headers=_BASE_HEADERS, timeout=20)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    body = resp.json()
    if "error" in body:
        print(f"[hook_quote_check] Alchemy напрямую тоже отказал: {body['error']}", file=sys.stderr)
        return None
    print("[hook_quote_check] Alchemy напрямую дал результат там, где публичный RPC отказал", file=sys.stderr)
    return body.get("result")


def quote_eth_to_mosiai(amount_in_wei: int, block_number: int) -> int:
    calldata = quote_exact_input_single_calldata(POOL_ETH_MOSIAI, True, amount_in_wei)
    block_tag = hex(block_number)
    try:
        raw = _rpc_call("eth_call", [{"to": V4_QUOTER, "data": calldata}, block_tag])
    except RuntimeError as exc:
        alt = _eth_call_alchemy_direct(V4_QUOTER, calldata, block_tag)
        if alt is None:
            raise RuntimeError(f"{exc} (Alchemy напрямую тоже не дал результата)") from exc
        raw = alt
    amount_out, _gas_estimate = decode_quote_result(raw)
    return amount_out


def main() -> None:
    result = {"cases": []}
    for case in CASES:
        n_minus_1_block = case["tx_block"] - 1
        row = {"tx_hash": case["tx_hash"], "quote_block": n_minus_1_block,
               "amount_in_eth_wei": case["amount_in_eth_wei"],
               "real_gross_swap_amount1": case["gross_swap_amount1"],
               "real_net_to_executor": case["net_to_executor"],
               "real_hook_skim": case["hook_skim"]}
        try:
            quoted = quote_eth_to_mosiai(case["amount_in_eth_wei"], n_minus_1_block)
            row["quoter_amount_out"] = quoted
            row["diff_vs_gross"] = quoted - case["gross_swap_amount1"]
            row["diff_vs_net"] = quoted - case["net_to_executor"]
            row["matches_gross"] = abs(row["diff_vs_gross"]) < max(1, case["gross_swap_amount1"] // 1_000_000)
            row["matches_net"] = abs(row["diff_vs_net"]) < max(1, case["net_to_executor"] // 1_000_000)
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)
        print(json.dumps(row, indent=2, default=str))
        result["cases"].append(row)

    out_path = Path(__file__).parent.parent / "data" / "task5_v4_hook_quote_check_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[hook_quote_check] сохранено: {out_path}")


if __name__ == "__main__":
    main()
