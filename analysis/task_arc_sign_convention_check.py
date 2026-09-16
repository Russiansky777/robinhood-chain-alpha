#!/usr/bin/env python3
"""Задание владельца, п.1: "Проверить направление на одной реальной
транзакции глазами до массового прогона и написать в лог, как именно
интерпретировал знак." Берём реальный, недавний Swap на уже
каталогизированном пуле USDC/AROS (currency0=USDC 0x3600..., currency1=
AROS 0x9615...), тянем его полный receipt, смотрим РЕАЛЬНЫЕ Transfer-логи
обеих ног и сверяем с знаком amount0/amount1 из события Swap. Ничего не
предполагаем про конвенцию V3/V4 заранее -- выводим её из этих данных."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOL_ID = "0x3d3f72f78bdc6e6510650ea59b508064c897d1c384cdeaf7b3e052ce38b5ba00"  # USDC/AROS, уже подтверждён
CURRENCY0 = "0x3600000000000000000000000000000000000000"  # USDC, decimals=6
CURRENCY1 = "0x96150e514a726492b5b3446d9008b04c22e4d317"  # AROS, decimals=18


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    for attempt in range(5):
        try:
            resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  headers={"Content-Type": "application/json"}, timeout=timeout)
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                return {"error": {"message": str(exc)}}
            time.sleep(2 * (attempt + 1))
            continue
        err = body.get("error")
        if err and "rate limit" in str(err.get("message", "")).lower():
            if attempt == 4:
                return body
            time.sleep(2 * (attempt + 1))
            continue
        return body
    return {"error": {"message": "unreachable"}}


def word(data_bytes: bytes, i: int) -> bytes:
    return data_bytes[i * 32:(i + 1) * 32]


def to_int_signed(w: bytes) -> int:
    v = int.from_bytes(w, "big")
    return v - 2 ** 256 if v >= 2 ** 255 else v


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:]


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "pool_id": POOL_ID, "currency0": CURRENCY0, "currency1": CURRENCY1}

    latest = int(rpc("eth_blockNumber", [])["result"], 16)
    from_block = max(0, latest - 3000)
    logs_body = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(latest),
                                      "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, POOL_ID]}])
    if "error" in logs_body:
        result["error"] = logs_body["error"]
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    logs = logs_body.get("result", [])
    result["n_recent_swaps_found_on_this_pool"] = len(logs)
    if not logs:
        result["conclusion"] = "Не найдено ни одного Swap на этом пуле за последние 3000 блоков -- не проверено."
        print(json.dumps(result, indent=2, ensure_ascii=False))
        Path(__file__).parent.parent.joinpath("data", "task_arc_sign_convention_check_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False))
        return

    log = logs[-1]  # самый свежий
    data = bytes.fromhex(log["data"][2:])
    amount0 = to_int_signed(word(data, 0))
    amount1 = to_int_signed(word(data, 1))
    sender = topic_to_addr(log["topics"][2])
    tx_hash = log["transactionHash"]

    result["swap_event"] = {
        "tx_hash": tx_hash, "block_number": int(log["blockNumber"], 16), "log_index": int(log["logIndex"], 16),
        "sender_from_swap_topic": sender, "amount0_raw": amount0, "amount1_raw": amount1,
        "amount0_human": amount0 / 1e6, "amount1_human": amount1 / 1e18,
    }

    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    r = receipt.get("result")
    tx = rpc("eth_getTransactionByHash", [tx_hash]).get("result") or {}
    result["tx_from"] = tx.get("from")

    transfers = []
    for l in (r or {}).get("logs", []):
        if l.get("topics", [None])[0] == TRANSFER_TOPIC0 and len(l["topics"]) >= 3:
            val = int(l["data"], 16) if l.get("data") and l["data"] != "0x" else 0
            transfers.append({
                "token": l["address"], "token_is_currency0": l["address"].lower() == CURRENCY0.lower(),
                "token_is_currency1": l["address"].lower() == CURRENCY1.lower(),
                "from": topic_to_addr(l["topics"][1]), "to": topic_to_addr(l["topics"][2]), "value_raw": val,
            })
    result["real_transfer_logs_in_receipt"] = transfers

    # Реальный вывод конвенции: смотрим, что случилось с currency0/currency1
    # у sender/tx.from на основе ФАКТИЧЕСКИХ Transfer, а не документации.
    c0_transfers = [t for t in transfers if t["token_is_currency0"]]
    c1_transfers = [t for t in transfers if t["token_is_currency1"]]
    trader_addrs = {sender.lower(), (tx.get("from") or "").lower()}

    def net_for_trader(tlist):
        net = 0
        for t in tlist:
            if t["from"].lower() in trader_addrs:
                net -= t["value_raw"]
            if t["to"].lower() in trader_addrs:
                net += t["value_raw"]
        return net

    trader_net_c0 = net_for_trader(c0_transfers)
    trader_net_c1 = net_for_trader(c1_transfers)
    result["trader_real_net_transfer_currency0"] = trader_net_c0
    result["trader_real_net_transfer_currency1"] = trader_net_c1

    interpretation = None
    if trader_net_c0 != 0 or trader_net_c1 != 0:
        # Сверяем знак: если amount0>0 совпадает по знаку с "трейдер ОТДАЛ
        # currency0" (net<0 у трейдера), то amount0>0 = приток в пул.
        if amount0 != 0 and trader_net_c0 != 0:
            same_sign_means_pool_inflow = (amount0 > 0) == (trader_net_c0 < 0)
            interpretation = (
                "amount0>0 означает ПРИТОК currency0 В ПУЛ (трейдер отдал currency0) -- "
                "стандартная V3/V4-конвенция, ПОДТВЕРЖДЕНО реальными Transfer-логами"
                if same_sign_means_pool_inflow else
                "amount0>0 означает ОТТОК currency0 ИЗ ПУЛА (трейдер получил currency0) -- "
                "ПРОТИВОПОЛОЖНО стандартной V3/V4-конвенции, ПОДТВЕРЖДЕНО реальными Transfer-логами"
            )
    result["interpretation"] = interpretation or (
        "Не удалось однозначно сверить по этой транзакции (нулевой net или не найдены Transfer-логи "
        "нужного токена) -- нужен другой пример."
    )
    result["applied_convention_for_mass_run"] = (
        "amount0>0/amount1>0 = токен ПРИШЁЛ В ПУЛ (это входная нога трейдера, token_in); "
        "amount0<0/amount1<0 = токен УШЁЛ ИЗ ПУЛА (это выходная нога трейдера, token_out). "
        "Применяется в task_arc_closed_cycle_detector.py ТОЛЬКО если interpretation выше подтверждает "
        "стандартную конвенцию; иначе конвенция инвертируется явно."
    )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_sign_convention_check_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
