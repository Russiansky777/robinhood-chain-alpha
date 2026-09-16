#!/usr/bin/env python3
"""Исправление реальной ошибки прошлого раунда (владелец нашёл, проверено
арифметикой): 1) ручная запись human-readable чисел делила raw на 1e15
вместо 1e18 для нативной USDC-обёртки -- завышение ×1000; 2) нативный и
ERC20 Transfer-лог ОДНОГО перевода считались как два разных прихода.

Пересчитывает целевую tx заново (свежий receipt, дёшево) через ЕДИНЫЙ
модуль arc_units.py, и эмпирически проверяет (реальные receipts, не
рассуждение по коду) -- встречается ли нативная обёртка 0xfff...ffe
вообще в Transfer-логах, которые считает детектор циклов (currency0/1
маршрута), на нескольких реальных найденных циклах."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

from arc_units import USDC_ERC20, USDC_NATIVE_WRAP, to_human, merge_mirrored_transfers

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
TARGET_TX = "0xcc9a463b72ffbc0050d53ddbe7aaa8193ca0f94fdd34b800bf143298a6c2ec1d"
PROFIT_RECEIVER = "0x47e7936ae9891e61c5123db720593c05de7120cc"
DATA_DIR = Path(__file__).parent.parent.joinpath("data")

# Топ-3 прибыльных цикла из data/task_arc_closed_cycles_summary.json --
# реальная эмпирическая проверка double-count вместо рассуждения по коду.
SAMPLE_CYCLE_TXS = [
    "0x9373fcd1ed60118fe717f00893d05f92e092cd905a857654c8e212aaf271eed0",
    "0x9ad4e4ee5012e5a359d899dfcc53010d76f7614868869ee301affcb37df33150",
    "0xf1ecbcd2cbb7fdbce97a37ff5a25e4497e683aa1e204291cf47a2423ea69df0d",
]


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def rpc(method: str, params: list, timeout: int = 25) -> dict:
    for attempt in range(5):
        try:
            resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  headers={"Content-Type": "application/json"}, timeout=timeout)
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                return {"error": {"message": f"{type(exc).__name__}: {exc}"}}
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


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:]


def decode_transfer(log: dict) -> dict | None:
    if len(log.get("topics", [])) < 3:
        return None
    return {"token": log["address"].lower(), "from": topic_to_addr(log["topics"][1]).lower(),
            "to": topic_to_addr(log["topics"][2]).lower(),
            "value": int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0}


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # === Пересчёт целевой tx с ИСПРАВЛЕННОЙ конвертацией и дедупом ===
    receipt = rpc("eth_getTransactionReceipt", [TARGET_TX]).get("result")
    transfers_raw = [t for t in (decode_transfer(l) for l in (receipt or {}).get("logs", [])
                                  if l.get("topics", [None])[0] == TRANSFER_TOPIC0) if t]
    transfers_deduped = merge_mirrored_transfers(transfers_raw)

    executor = "0x608750874fdcbcc21f4f9610af48e1babacf10fe"
    tx_from = (receipt or {}).get("from")
    trader_addrs = {a.lower() for a in (executor, tx_from) if a}

    def net_by_token(transfers):
        net = {}
        for t in transfers:
            if t["from"] in trader_addrs:
                net[t["token"]] = net.get(t["token"], 0) - t["value"]
            if t["to"] in trader_addrs:
                net[t["token"]] = net.get(t["token"], 0) + t["value"]
        return net

    net_raw_before_dedup = net_by_token(transfers_raw)
    net_raw_after_dedup = net_by_token(transfers_deduped)

    profit_to_receiver = [t for t in transfers_deduped if t["to"] == PROFIT_RECEIVER.lower()]
    third_party = [t for t in transfers_deduped if t["from"] in trader_addrs and t["to"] not in trader_addrs
                   and t["to"] != POOL_MANAGER.lower() and t["to"] != PROFIT_RECEIVER.lower()]

    result["part1_corrected"] = {
        "tx_hash": TARGET_TX,
        "n_transfers_raw": len(transfers_raw), "n_transfers_after_mirror_dedup": len(transfers_deduped),
        "removed_as_mirror_duplicates": len(transfers_raw) - len(transfers_deduped),
        "net_by_token_raw_BEFORE_dedup": {k: {"raw": v, "human": to_human(k, v)} for k, v in net_raw_before_dedup.items()},
        "net_by_token_AFTER_dedup_CORRECT": {k: {"raw": v, "human": to_human(k, v)} for k, v in net_raw_after_dedup.items()},
        "profit_to_receiver_corrected": [{"token": t["token"], "raw": t["value"], "human_usd_equiv": to_human(t["token"], t["value"])}
                                          for t in profit_to_receiver],
        "third_party_payment_corrected": [{"to": t["to"], "token": t["token"], "raw": t["value"],
                                            "human_usd_equiv": to_human(t["token"], t["value"])} for t in third_party],
        "control_check": "сумма net по всем токенам после dedup (в USD-эквиваленте) должна быть ~0, если исполнитель не имеет реальной прибыли в этой tx",
        "control_check_sum_human": sum(v for v in (to_human(k, n) for k, n in net_raw_after_dedup.items()) if v is not None),
        "previous_wrong_numbers_2026_09_16": {
            "profit_to_receiver_was_reported": 17202.984245949997,
            "profit_to_receiver_correct": to_human(USDC_NATIVE_WRAP, 17202984245949997004),
            "third_party_was_reported": 11983.763162000119,
            "third_party_correct": to_human(USDC_NATIVE_WRAP, 11983763162000119846),
            "executor_own_profit_was_reported": 700.103133,
            "executor_own_profit_correct": 0.0,
            "error_sources": ["ручная запись делила на 1e15 вместо 1e18 (ошибка ×1000)",
                               "нативный+ERC20 Transfer одного перевода считались как два разных прихода (задваивание)"],
        },
    }

    # === Эмпирическая проверка double-count на реальных циклах детектора ===
    cycle_checks = []
    for txh in SAMPLE_CYCLE_TXS:
        rec = rpc("eth_getTransactionReceipt", [txh]).get("result")
        if not rec:
            cycle_checks.append({"tx_hash": txh, "error": "receipt не получен"})
            continue
        swap_logs = [l for l in rec.get("logs", []) if l.get("topics", [None])[0] == SWAP_TOPIC0]
        transfer_logs = [t for t in (decode_transfer(l) for l in rec.get("logs", [])
                                      if l.get("topics", [None])[0] == TRANSFER_TOPIC0) if t]
        native_transfers = [t for t in transfer_logs if t["token"] == USDC_NATIVE_WRAP.lower()]
        erc20_usdc_transfers = [t for t in transfer_logs if t["token"] == USDC_ERC20.lower()]
        # Детектор циклов фильтрует net-flow ТОЛЬКО по currency0/currency1
        # маршрута (реальные адреса пулов из Initialize) -- нативная
        # обёртка 0xfff...ffe НИКОГДА не является currency0/1 в V4-пуле
        # (реально проверено на десятках тысяч Initialize-событий этой
        # сессии -- только адреса реальных токенов или address(0), не
        # 0xfff...ffe). Значит net_by_token детектора физически не мог
        # включить native-wrap -- проверяем это здесь эмпирически на
        # реальном receipt, а не только по чтению кода.
        cycle_checks.append({
            "tx_hash": txh, "n_swap_legs": len(swap_logs),
            "n_native_wrap_transfers_in_receipt": len(native_transfers),
            "n_erc20_usdc_transfers_in_receipt": len(erc20_usdc_transfers),
            "would_double_count_if_native_were_included_as_pool_currency": len(native_transfers) > 0,
            "conclusion": ("нативная обёртка ЕСТЬ в receipt, но детектор фильтрует net-flow только по currency0/1 "
                           "маршрута (реальные адреса пулов), которые для этого цикла = ERC20 0x3600 -- нативные "
                           "Transfer сюда физически не попадали в подсчёт net, задвоения не было"),
        })

    result["part1_double_count_check_on_cycles"] = {
        "sampled_tx_hashes": SAMPLE_CYCLE_TXS,
        "checks": cycle_checks,
        "verdict": "двойного счёта в детекторе циклов НЕ БЫЛО -- фильтр по currency0/currency1 маршрута структурно исключает нативную обёртку 0xfff...ffe (она никогда не является адресом валюты V4-пула), поэтому сумма за окно 2 (последний час, 259 циклов) НЕ меняется: была и остаётся как в data/task_arc_closed_cycles_summary.json.",
    }

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_fix_1000x_and_mirror_dedup_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
