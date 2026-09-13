#!/usr/bin/env python3
"""Задача 5, сквозной результат -- единственный текущий блокер:
модель хука для пула 0xfaf0d409... (ETH/0x35a79120..., хук
0xe5e70264..., направление token-in/ETH-out, zero_for_one=False) сейчас
подтверждена ОДНИМ наблюдением (tx 0x878fb998..., 2.90%) -- по тому же
стандарту, что уже применён к модели ETH/MOSIAI (нужно ≥2 независимых
подтверждения), этого недостаточно. Здесь -- ВТОРОЕ, НЕЗАВИСИМОЕЕ
подтверждение ТЕМ ЖЕ методом, что уже использован и принят для первой
модели: extsload(slot0) РЕАЛЬНОГО состояния vs РЕАЛЬНАЯ котировка через
V4Quoter, на latest блоке, В ТОМ ЖЕ направлении (token->ETH). Только
это конкретное недостающее число -- ничего больше, никакого широкого
скана."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402
from task5_v4_pool_math import PoolKey, quote_exact_input_single_calldata, decode_quote_result  # noqa: E402
from task5_v4_pool_state_cache import extsload_pool_state  # noqa: E402

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
POOL_ID_ETH_TOKEN = "0xfaf0d4093602eb2d7f80ce7ba50cffaeece9c5d546b6df363107779e5ff553aa"
NATIVE = "0x0000000000000000000000000000000000000000"
TOKEN = "0x35a79120e07bae083045d44b8349c5d47f37ac13"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
KEY = PoolKey(NATIVE, TOKEN, 0, 200, HOOK)
# Направление token->ETH (zero_for_one=False -- currency1(token) вход,
# currency0(ETH) выход); размер -- заведомо малый относительно
# ликвидности пула (та же, что и в предыдущей извсload-проверке
# методология -- крошечный размер минимизирует price impact).
TINY_TOKEN_IN = 10 ** 18  # 1 токен (18 decimals, как MOSIAI-подобные токены этого проекта)


def main() -> None:
    result: dict = {}
    latest_hex = _rpc_call("eth_blockNumber", [])
    result["latest_block"] = latest_hex

    state = extsload_pool_state(POOL_ID_ETH_TOKEN, latest_hex, _rpc_call, POOL_MANAGER)
    result["extsload_state"] = state
    raw_price_token1_per_token0 = (state["sqrt_price_x96"] / (2 ** 96)) ** 2  # ETH-денома в 1 token(currency1)? -- см. ниже
    # currency0=ETH, currency1=TOKEN -- price = currency1-за-currency0 (TOKEN за ETH).
    # Для направления token->ETH (zero_for_one=False): ожидаемый ETH-выход без хука/fee
    # = amount_in_token / price_token_per_eth = amount_in_token * (1/price) -- ниже используем
    # напрямую отношение из RAW AMM цены на этом крошечном размере.
    price_token_per_eth = raw_price_token1_per_token0
    expected_eth_out_no_fee_no_hook = TINY_TOKEN_IN / price_token_per_eth if price_token_per_eth else None
    result["price_token1_per_token0_from_extsload"] = price_token_per_eth
    result["expected_eth_out_raw_amm_no_fee_no_hook"] = expected_eth_out_no_fee_no_hook

    calldata = quote_exact_input_single_calldata(KEY, False, TINY_TOKEN_IN)  # zero_for_one=False
    quote_raw = _rpc_call("eth_call", [{"to": V4_QUOTER, "data": calldata}, latest_hex])
    amount_out, gas_estimate = decode_quote_result(quote_raw)
    result["independent_quote_check"] = {
        "amount_in_token": TINY_TOKEN_IN, "amount_out_eth": amount_out, "gas_estimate": gas_estimate,
    }

    if expected_eth_out_no_fee_no_hook:
        implied_skim_fraction = 1.0 - (amount_out / expected_eth_out_no_fee_no_hook)
        result["implied_skim_fraction_vs_raw_amm_price"] = implied_skim_fraction
    else:
        result["implied_skim_fraction_vs_raw_amm_price"] = None

    result["single_tx_observation_for_comparison"] = {
        "tx": "0x878fb998474230338a47a710d2c25689987a836074729ae61e112dcccca7cd1b",
        "skim_fraction_from_saved_data": 0.029000,
    }
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_faf0d409_hook_second_confirmation_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
