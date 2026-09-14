#!/usr/bin/env python3
"""СРОЧНО (5 мин на первый результат, лимит 10 мин / 20 RPC):
декодировать 4 уже найденные (сохранённые в предыдущем прогоне)
транзакции блока B-1=62790343, реально коснувшиеся пулов маршрута
(pool1 x3, V3-пул x1) -- ЧТО именно они сделали (Swap-направление,
суммы, ETH/WETH не путать). Плюс, если бюджет позволит, 2 калибровочных
eth_call ЧЕРЕЗ ALCHEMY НАПРЯМУЮ (публичный RPC НЕ трогаем повторно --
уже подтверждённо не поддерживает историч. eth_call) до/после
кандидата-триггера, чтобы проверить гипотезу, а не просто
задекларировать последнее изменение как момент возникновения."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import requests  # noqa: E402

from alchemy_fallback import _rpc_call, _alchemy_direct_endpoint, _BASE_HEADERS, topic0  # noqa: E402
from task5_v4_pool_math import (  # noqa: E402
    PoolKey, decode_v4_swap_log_data, quote_exact_input_single_calldata, decode_quote_result,
)

CALL_COUNT = 0
START = time.time()


def rpc(method, params):
    global CALL_COUNT
    if CALL_COUNT >= 20:
        raise RuntimeError("20-RPC budget reached")
    if time.time() - START > 9 * 60:
        raise RuntimeError("9-минутный внутренний потолок (запас перед 10-мин лимитом)")
    CALL_COUNT += 1
    return _rpc_call(method, params)


def alchemy_direct_call(to: str, data: str, block_tag: str):
    global CALL_COUNT
    if CALL_COUNT >= 20:
        return None
    url = _alchemy_direct_endpoint()
    if not url:
        return {"error": "alchemy endpoint не настроен"}
    CALL_COUNT += 1
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                     "params": [{"to": to, "data": data}, block_tag]},
                         headers=_BASE_HEADERS, timeout=20)
    return resp.json()


TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V4_SWAP_TOPIC0 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
V3_SWAP_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
TOKEN_X = "0x78b96280c3347e0f58a7147b73eb0ec5ffff025d"
V3_POOL = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
POOL1_ID = "0x935c1401e40a4eeec0e722f1841cc4be9d7a523cf90cdc52900f9e343304f0fe"
POOL1_KEY = PoolKey("0x0000000000000000000000000000000000000000", TOKEN_X, 0, 200,
                     "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")
B = 62_790_344

CANDIDATES = [
    {"tx_hash": "0xe60697bc593cb910ac5d98157563bd89070213d4d3486100e2bb72e86549aabb", "pool": "pool1"},
    {"tx_hash": "0x3fdbaccf668869d2c35a456d7134f9a8de99190c108690217ba368fb32ae900c", "pool": "pool1"},
    {"tx_hash": "0xdabdb359980d0633c33df153d2b320529651e26d3a361faf9027e06d5ca45a07", "pool": "pool1"},
    {"tx_hash": "0xb09332acb935eb354b2e9651c4bb74d92bfcbce1093b2634d041837b56291a4e", "pool": "v3_pool"},
]


def to_addr(topic: str) -> str:
    return "0x" + topic[-40:]


def decode_tx(tx_hash: str) -> dict:
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    tx_obj = rpc("eth_getTransactionByHash", [tx_hash])
    row = {"tx_hash": tx_hash, "block_number": receipt.get("blockNumber"),
           "transaction_index": receipt.get("transactionIndex"),
           "from": tx_obj.get("from"), "to": tx_obj.get("to"), "actions": []}
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0].lower()
        addr = (log.get("address") or "").lower()
        if t0 == V4_SWAP_TOPIC0 and addr == POOL_MANAGER.lower() and topics[1].lower() == POOL1_ID.lower():
            d = decode_v4_swap_log_data(log["data"])
            row["actions"].append({"kind": "v4_swap_pool1", "log_index": log["logIndex"],
                                    "sender": to_addr(topics[2]), **d})
        elif t0 == V3_SWAP_TOPIC0 and addr == V3_POOL.lower():
            h = log["data"][2:]
            words = [h[i:i + 64] for i in range(0, len(h), 64)]

            def s(w):
                v = int(w, 16)
                return v - (1 << 256) if v >= (1 << 255) else v
            row["actions"].append({
                "kind": "v3_swap", "log_index": log["logIndex"],
                "sender": to_addr(topics[1]), "recipient": to_addr(topics[2]),
                "amount0_weth": s(words[0]), "amount1_usdg": s(words[1]),
                "sqrt_price_x96": int(words[2], 16), "tick": s(words[4]),
            })
        elif t0 == TRANSFER_TOPIC0 and len(topics) >= 3:
            token = addr
            frm, to = to_addr(topics[1]), to_addr(topics[2])
            val = int(log["data"], 16) if log["data"] not in ("0x", "") else 0
            label = {WETH.lower(): "WETH(ERC20)", USDG.lower(): "USDG",
                     TOKEN_X.lower(): "TOKEN_X"}.get(token, token)
            row["actions"].append({"kind": "transfer", "log_index": log["logIndex"],
                                    "token": label, "from": frm, "to": to, "raw": val})
    row["actions"].sort(key=lambda a: int(a["log_index"], 16))
    return row


def main() -> None:
    out = {"generated_for": "0xcc630e7d525909dd32453101baf311588813279edb5a97e0d386ea9cc4c2ede3",
           "block_B": B, "decoded_B_minus_1_txs": [], "calibration": {}}
    for c in CANDIDATES:
        try:
            out["decoded_B_minus_1_txs"].append(decode_tx(c["tx_hash"]))
        except Exception as exc:  # noqa: BLE001
            out["decoded_B_minus_1_txs"].append({"tx_hash": c["tx_hash"], "error": str(exc)})
        # промежуточное сохранение -- честно на случай обрыва по бюджету
        Path(__file__).parent.parent.joinpath("data", "task5_arb_b1_decode_result.json").write_text(
            json.dumps(out, indent=2, ensure_ascii=False, default=str))

    # Калибровка через Alchemy НАПРЯМУЮ (публичный RPC уже подтверждён
    # неработоспособным на историч. блоках -- НЕ повторяем): котировка
    # leg1 (TOKEN_X->WETH, реальное направление арбитража) на B-2 (ДО
    # трёх событий B-1) и на B-1 (ПОСЛЕ них) при ОДНОМ и том же размере
    # (реальный net TOKEN_X, дошедший до исполнителя в самом арбитраже:
    # 2151779353383218906585472) -- если котировка WETH-выхода заметно
    # выросла к B-1, это подтверждает направление гипотезы количественно.
    try:
        REAL_TOKENX_NET = 2151779353383218906585472
        calldata = quote_exact_input_single_calldata(POOL1_KEY, False, REAL_TOKENX_NET)
        V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
        for label, block_tag in [("at_B_minus_2_before_B1_events", hex(B - 2)),
                                  ("at_B_minus_1_after_B1_events", hex(B - 1))]:
            body = alchemy_direct_call(V4_QUOTER, calldata, block_tag)
            row = {"block_tag": block_tag, "raw_response": body}
            if body and "result" in body:
                amount_out, _ = decode_quote_result(body["result"])
                row["quoted_weth_out_raw"] = amount_out
                row["quoted_weth_out"] = amount_out / 1e18
            out["calibration"][label] = row
    except Exception as exc:  # noqa: BLE001
        out["calibration"]["error"] = str(exc)

    out["n_rpc_calls_used"] = CALL_COUNT
    out["elapsed_s"] = time.time() - START
    path = Path(__file__).parent.parent / "data" / "task5_arb_b1_decode_result.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
