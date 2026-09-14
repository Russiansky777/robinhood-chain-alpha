#!/usr/bin/env python3
"""Локальная точечная проверка (владелец, разбор InsufficientFunds
route_7aebd805z0..., 2026-09-14): "Подтверди одной локальной проверкой,
что при отрицательной повторной котировке estimateGas не вызывается."

Без сети -- rpc_call_trading_path/quote_route_at_size/estimate_gas
подменены локальными заглушками (тот же приём, что round4/round6
pointchecks: подмена атрибута МОДУЛЯ, функция ищет имя в глобальном
пространстве этого же модуля при каждом вызове). Проверяется РЕАЛЬНАЯ,
не переписанная копия task5_v4_hotpath._quote_and_estimate_gas_consistent.

Числа заглушки -- те же, что в уже подтверждённом реальном случае
(route_7aebd805z0_19285e18z0_24107d15z1, блок сигнала 62335119,
profit_raw=+5020; согласованный блок 62335187, profit_raw=-18134) --
воспроизводит именно этот, ранее диагностированный случай, НЕ
утверждает что-либо про остальные 61 откат estimateGas этой сессии."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", "dummy"))

import task5_v4_hotpath as hp  # noqa: E402

CONTRACT_ADDRESS = "0xAB24907bceEF4EDC366a1E4DfA15ed8aA5fdfe71"
FROM_ADDRESS = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"

ORIGINAL_BLOCK = 62335119
ORIGINAL_PROFIT_RAW = 5020
REQUOTE_BLOCK = 62335187
REQUOTE_PROFIT_RAW = -18134


def main() -> None:
    calls = {"estimate_gas": 0, "quote_route_at_size": 0, "eth_block_number": 0}

    def fake_rpc_call_trading_path(method: str, params: list) -> str:
        assert method == "eth_blockNumber", f"неожиданный метод в заглушке: {method}"
        calls["eth_block_number"] += 1
        return hex(REQUOTE_BLOCK)

    def fake_quote_route_at_size(route, amount_in: int, block_number: int) -> dict:
        calls["quote_route_at_size"] += 1
        assert block_number == REQUOTE_BLOCK
        return {"ok": True, "amount_in": amount_in, "amount_out": amount_in + REQUOTE_PROFIT_RAW,
                "profit_raw": REQUOTE_PROFIT_RAW}

    def fake_estimate_gas(contract_address, calldata, from_address, block_number=None) -> dict:
        calls["estimate_gas"] += 1
        raise AssertionError("estimate_gas НЕ должен вызываться, когда повторная котировка сама убыточна")

    orig_rpc = hp.rpc_call_trading_path
    orig_quote = hp.quote_route_at_size
    orig_gas = hp.estimate_gas
    hp.rpc_call_trading_path = fake_rpc_call_trading_path
    hp.quote_route_at_size = fake_quote_route_at_size
    hp.estimate_gas = fake_estimate_gas
    try:
        result = hp._quote_and_estimate_gas_consistent(
            route=None, amount_in=5_000_000, contract_address=CONTRACT_ADDRESS,
            calldata=b"\x00", from_address=FROM_ADDRESS,
            original_block=ORIGINAL_BLOCK, original_profit_raw=ORIGINAL_PROFIT_RAW,
        )
    finally:
        hp.rpc_call_trading_path = orig_rpc
        hp.quote_route_at_size = orig_quote
        hp.estimate_gas = orig_gas

    assert calls["estimate_gas"] == 0, f"estimate_gas вызван {calls['estimate_gas']} раз(а) -- ожидалось 0"
    assert calls["quote_route_at_size"] == 1, f"повторная котировка вызвана {calls['quote_route_at_size']} раз(а) -- ожидалось 1"
    assert result["ok"] is False
    assert result["reason"] == hp.REASON_REQUOTE_UNPROFITABLE, result["reason"]
    assert result["original_block"] == ORIGINAL_BLOCK
    assert result["original_profit_raw"] == ORIGINAL_PROFIT_RAW
    assert result["requote_block"] == REQUOTE_BLOCK
    assert result["requote_profit_raw"] == REQUOTE_PROFIT_RAW
    assert str(ORIGINAL_BLOCK) in result["detail"] and str(REQUOTE_BLOCK) in result["detail"]
    assert str(ORIGINAL_PROFIT_RAW) in result["detail"] and str(REQUOTE_PROFIT_RAW) in result["detail"]

    print("OK: при profit_raw<=0 у повторной котировки estimateGas НЕ вызван (0 вызовов); "
          "REASON_REQUOTE_UNPROFITABLE; оба блока и обе прибыли сохранены в результате.")
    print(result)


if __name__ == "__main__":
    main()
