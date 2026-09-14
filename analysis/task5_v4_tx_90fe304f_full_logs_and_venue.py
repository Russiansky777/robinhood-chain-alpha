#!/usr/bin/env python3
"""Продолжение разбора 0x90fe304f... : ПОЛНЫЙ дамп всех логов рецепта
(не только Transfer/V4-Swap, которые уже разобраны в первом результате)
+ проверка, является ли контрагент 0x654e4143e82a5824445ade0824351c2a9acd95a8
контрактом и не эмитировал ли он СВОЙ собственный Swap-лог (например,
V3-стиля -- Swap(address,address,int256,int256,uint160,uint128,int24)) --
владелец явно просил не отбрасывать пример из-за отсутствия поддержки
V3 в основном коде, поэтому здесь -- отдельная, честная попытка
декодировать именно это событие, если оно есть, БЕЗ добавления его в
основной (V4-only) конвейер проекта.

read-only. Ничего не меняет, contract/Sender не трогает."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call, topic0  # noqa: E402

TX_HASH = "0x90fe304f43209732e4607840e5b602ca74081f75dca26be2c4072c6ba8f745d3"
COUNTERPARTY = "0x654e4143e82a5824445ade0824351c2a9acd95a8"

# V3-стиля Swap: Swap(address indexed sender, address indexed recipient,
# int256 amount0, int256 amount1, uint160 sqrtPriceX96, uint128 liquidity,
# int24 tick) -- реальная, широко известная сигнатура Uniswap V3 core.
V3_SWAP_TOPIC0 = topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")


def to_signed(v: int, bits: int = 256) -> int:
    return v - (1 << bits) if v >= (1 << (bits - 1)) else v


def main() -> None:
    result: dict = {"tx_hash": TX_HASH}
    receipt = _rpc_call("eth_getTransactionReceipt", [TX_HASH])
    if receipt is None:
        result["error"] = "рецепт не найден"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    block_number = int(receipt["blockNumber"], 16)
    result["block_number"] = block_number

    all_logs = []
    for log in receipt["logs"]:
        all_logs.append({
            "log_index": int(log["logIndex"], 16),
            "address": log["address"].lower(),
            "topic0": log["topics"][0] if log["topics"] else None,
            "n_topics": len(log["topics"]),
            "data_len_bytes": (len(log["data"]) - 2) // 2 if log["data"] not in ("0x", "") else 0,
        })
    result["all_logs_summary"] = sorted(all_logs, key=lambda x: x["log_index"])
    result["v3_swap_topic0_computed"] = V3_SWAP_TOPIC0

    # Код по адресу контрагента -- контракт или EOA? "latest" (не
    # исторический блок) -- байткод контракта после деплоя практически
    # не меняется, а "eth_getCode" на некоторых блоках вернул честную
    # ошибку провайдера "metadata is not found" (недоступность именно
    # ЭТОГО исторического снимка для eth_getCode, не для eth_getLogs/
    # eth_call) -- не нужный здесь риск, вопрос только "это контракт?".
    code = _rpc_call("eth_getCode", [COUNTERPARTY, "latest"])
    result["counterparty_address"] = COUNTERPARTY
    result["counterparty_is_contract"] = bool(code and code != "0x")
    result["counterparty_code_size_bytes"] = (len(code) - 2) // 2 if code else 0

    # Искал ли контрагент СВОЙ Swap-лог (V3-стиля) в ЭТОЙ ЖЕ tx?
    counterparty_logs = [l for l in receipt["logs"] if l["address"].lower() == COUNTERPARTY.lower()]
    result["counterparty_own_logs_in_this_tx"] = [
        {"log_index": int(l["logIndex"], 16), "topic0": l["topics"][0] if l["topics"] else None,
         "n_topics": len(l["topics"]), "data_len_bytes": (len(l["data"]) - 2) // 2}
        for l in counterparty_logs
    ]

    v3_swap_logs = [l for l in counterparty_logs if l["topics"] and l["topics"][0].lower() == V3_SWAP_TOPIC0.lower()]
    if v3_swap_logs:
        log = v3_swap_logs[0]
        sender = "0x" + log["topics"][1][-40:]
        recipient = "0x" + log["topics"][2][-40:]
        data = log["data"][2:]
        words = [data[i:i + 64] for i in range(0, len(data), 64)]
        decoded = {
            "sender": sender, "recipient": recipient,
            "amount0": to_signed(int(words[0], 16)),
            "amount1": to_signed(int(words[1], 16)),
            "sqrt_price_x96": int(words[2], 16),
            "liquidity": int(words[3], 16),
            "tick": to_signed(int(words[4], 16), 24) if len(words) > 4 else None,
        }
        result["v3_style_swap_decoded"] = decoded

        # token0()/token1() -- реальные read-only eth_call на сам контрагент,
        # если это действительно V3-подобный пул.
        for fn_name, selector in (("token0", "0x0dfe1681"), ("token1", "0xd21220a7"), ("fee", "0xddca3f43")):
            try:
                raw = _rpc_call("eth_call", [{"to": COUNTERPARTY, "data": selector}, hex(block_number)])
                if fn_name == "fee":
                    result[f"counterparty_{fn_name}"] = int(raw, 16) if raw and raw != "0x" else None
                else:
                    result[f"counterparty_{fn_name}"] = ("0x" + raw[-40:]) if raw and raw != "0x" else None
            except Exception as exc:  # noqa: BLE001
                result[f"counterparty_{fn_name}_error"] = str(exc)
    else:
        result["v3_style_swap_decoded"] = None
        result["note_no_v3_swap_log"] = (
            "контрагент НЕ эмитировал в этой tx лог с топиком стандартного V3 Swap "
            f"({V3_SWAP_TOPIC0}) -- либо это не V3-пул, либо у него другая сигнатура события, "
            "либо его роль в этой tx не 'исполнение своего свопа' (см. полный список его "
            "собственных логов выше)")

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_tx_90fe304f_full_logs_and_venue_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[full_logs_and_venue] сохранено: {out_path}")


if __name__ == "__main__":
    main()
