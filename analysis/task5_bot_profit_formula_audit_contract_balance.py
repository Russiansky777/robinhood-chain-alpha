#!/usr/bin/env python3
"""Задача 5 -- ЗАКРЫТИЕ "НЕИЗВЕСТНО" из аудита формулы прибыли. Владелец,
2026-09-12: 15 из 20 транзакций с наибольшей "прибылью" имеют одного
инициатора (`0x0a45dcd332083d412c047f7a20ce804a3fc32c25`), но НИ ОДИН
Transfer-лог не касается его EOA напрямую -- сделка идёт через контракт,
который этот EOA вызывает (`receipt.to`). Единственная задача этого
скрипта: посчитать баланс ИМЕННО ЭТОГО КОНТРАКТА (не EOA) по всем
Transfer-логам транзакции -- что пришло на него, что ушло, по каждому
токену. Вердикт: один токен net-положительный, остальные обнулились ->
ЗАМКНУТЫЙ ЦИКЛ (реальный арбитраж); токен A ушёл, токен B пришёл ->
ОБМЕН. Плюс: `eth_getCode` (длина байткода), проверка против уже
известных списков роутеров/самоторговцев этого проекта, и попытка узнать
деплойера через публичный Blockscout API (`robinhoodchain.blockscout.com`,
без ключа, уже используется в проекте -- `task5_bot_pretrust_checks.py`).

НИЧЕГО НЕ ЧИНИТ, НЕ ОПТИМИЗИРУЕТ -- чисто аудиторский, финальный шаг
проверки существования предмета."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import PATH_A_KNOWN_ROUTER_ADDRESSES_FROM_HISTOGRAM, RPC_URL_MAINNET
from task5_bot_pool_state import _rpc_call_with_provider_fallback
from task5_bot_router_decode import KNOWN_SELF_TRADE_ADDRESSES

TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DUST_RAW_THRESHOLD = 1000
BLOCKSCOUT_API = "https://robinhoodchain.blockscout.com/api/v2"

KNOWN_WASH_ADDRESSES_FROM_OVERLAP_SCRIPT = {
    "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc",
    "0xcaf681a66d020601342297493863e78c959e5cb2",
    "0x39b38686a19836ac10162c490e4558e120cbbe5f",
    "0xe492912f37c2a4eca45d42dc67548f4c6cd7ce2b",
    "0x8876789976decbfcbbbe364623c63652db8c0904",
}

# Владелец, тот же разговор: те же 15 транзакций с "НЕИЗВЕСТНО" (одна и та
# же реальная строка, что и в task5_bot_profit_formula_audit.py -- ниже 5
# позиций с профитом, уже подтверждённых как "ОБМЕН", исключены).
UNKNOWN_15 = [
    {"tx_hash": "0xcfa1226d5ebe46b43c22ef9075e71a4e685bd91bed2889293cd0664502227985", "profit_usd": 19209.39},
    {"tx_hash": "0xa5f9d2b18eec0b4412e8c3f7e4a7c2b12fd4e637ef00de322239874c36c74c6d", "profit_usd": 19150.97},
    {"tx_hash": "0xe3a892c894963c8fd93f290b9af75f78a3c7492c30ece7985d8ea79643e7c88d", "profit_usd": 18531.85},
    {"tx_hash": "0x182e446150892f7b65cde45b192aaf45488a48d01cb64e3bd0eadb1129e2da9d", "profit_usd": 17862.51},
    {"tx_hash": "0x09b349dd463391c7b1ca37429d1649ad4ff01a5541375f2ab41c3f70fa44d30d", "profit_usd": 17733.81},
    {"tx_hash": "0x7f966b3c71545e4726f2361107a1d2b16294a5f0830cbb002aaaf966d4c848be", "profit_usd": 13794.18},
    {"tx_hash": "0x6af4b7422b073126c1f1a0f8dc6edba4d65137ab0d0aef7fd7441c01bf7239e4", "profit_usd": 13651.06},
    {"tx_hash": "0xfecae362a91dd90ac81150deeb323eaa4f2195dfa297b0c8492e7ccfd386581c", "profit_usd": 13276.19},
    {"tx_hash": "0xb2a713b1b1a34d319a89fe236844074426c2fdede936d5d694fac4998f052c45", "profit_usd": 13042.26},
    {"tx_hash": "0x35c37cbaa8dd4757a4191842ed191a5764a0d63e5328db475db116dca95e4b09", "profit_usd": 12151.01},
    {"tx_hash": "0xf0641c750fc5a600047f2fc5db703afed4d9488cb0d556c49bb9223c6dfcd4bc", "profit_usd": 11852.60},
    {"tx_hash": "0x32dbe510452c638f135a6f6f4535eba7695867c45a2d171b7d13db3a1c746e3e", "profit_usd": 11448.98},
    {"tx_hash": "0x91f47618399904b059146eace6fb529a945d394ee8680e7f42ceeddee4eeded8", "profit_usd": 11277.13},
    {"tx_hash": "0x7cda784dc06f7ee4b6b9c139f28b6796e672b807acbec0bb54af68b08acb0aa7", "profit_usd": 11256.06},
    {"tx_hash": "0x1f1287b2c0c07cbf905c0d77d5c10424d3444e1441145218f9d8da4fe8af5242", "profit_usd": 10444.83},
]


def _rpc(method, params):
    return _rpc_call_with_provider_fallback(method, params, RPC_URL_MAINNET, timeout=25.0)


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:]


def blockscout_address_info(address: str) -> dict:
    try:
        resp = requests.get(f"{BLOCKSCOUT_API}/addresses/{address}", timeout=15,
                             headers={"User-Agent": "robinhood-chain-alpha-audit/1.0"})
        if resp.status_code == 200:
            return resp.json()
        return {"http_status": resp.status_code}
    except Exception as exc:
        return {"error": str(exc)}


def audit_one(entry: dict) -> dict:
    tx_hash = entry["tx_hash"]
    row: dict = {**entry}

    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception as exc:
        row["error"] = f"eth_getTransactionReceipt: {exc}"
        return row
    if not receipt:
        row["error"] = "рецепт не найден"
        return row

    contract_addr = (receipt.get("to") or "").lower()
    row["contract_to"] = contract_addr

    contract_in: dict[str, int] = {}
    contract_out: dict[str, int] = {}
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC0 or len(topics) < 3:
            continue
        token = (log.get("address") or "").lower()
        frm = _topic_to_addr(topics[1])
        to = _topic_to_addr(topics[2])
        value = int(log.get("data") or "0x0", 16)
        if frm == contract_addr:
            contract_out[token] = contract_out.get(token, 0) + value
        if to == contract_addr:
            contract_in[token] = contract_in.get(token, 0) + value

    net = {t: contract_in.get(t, 0) - contract_out.get(t, 0) for t in set(contract_in) | set(contract_out)}
    positive = {t: v for t, v in net.items() if v > DUST_RAW_THRESHOLD}
    negative = {t: v for t, v in net.items() if v < -DUST_RAW_THRESHOLD}
    row["contract_net_positive_tokens_raw"] = positive
    row["contract_net_negative_tokens_raw"] = negative

    if len(positive) == 1 and len(negative) == 0:
        row["verdict"] = "ЗАМКНУТЫЙ ЦИКЛ (один токен net-положителен на контракте, остальные обнулились)"
    elif positive and negative:
        row["verdict"] = "ОБМЕН (токен ушёл с контракта, другой пришёл -- не арбитраж)"
    elif not positive and not negative:
        row["verdict"] = "НЕИЗВЕСТНО (баланс контракта по Transfer-логам не изменился вообще)"
    else:
        row["verdict"] = f"ЧЕСТНО НЕОДНОЗНАЧНО (positive={len(positive)}, negative={len(negative)})"

    # --- контракт: код, известные списки, деплойер ---
    try:
        code = _rpc("eth_getCode", [contract_addr, "latest"])
        code_len_bytes = (len(code) - 2) // 2 if code and code != "0x" else 0
    except Exception as exc:
        code_len_bytes = None
        row["eth_getcode_error"] = str(exc)
    row["contract_code_len_bytes"] = code_len_bytes
    row["is_known_router_from_histogram"] = contract_addr in PATH_A_KNOWN_ROUTER_ADDRESSES_FROM_HISTOGRAM
    row["is_known_self_trade_address"] = contract_addr in KNOWN_SELF_TRADE_ADDRESSES
    row["is_known_wash_address"] = contract_addr in KNOWN_WASH_ADDRESSES_FROM_OVERLAP_SCRIPT

    bs_info = blockscout_address_info(contract_addr)
    row["blockscout_is_contract"] = bs_info.get("is_contract") if isinstance(bs_info, dict) else None
    row["blockscout_creator_address"] = bs_info.get("creator_address_hash") if isinstance(bs_info, dict) else None
    row["blockscout_creation_tx"] = bs_info.get("creation_tx_hash") if isinstance(bs_info, dict) else None
    row["blockscout_name"] = bs_info.get("name") if isinstance(bs_info, dict) else None
    row["blockscout_raw_error_or_status"] = (bs_info.get("error") or bs_info.get("http_status")
                                              if isinstance(bs_info, dict) else None)
    return row


if __name__ == "__main__":
    rows = []
    for entry in UNKNOWN_15:
        print(f"[contract_balance_audit] {entry['tx_hash']} (заявленная прибыль ${entry['profit_usd']:,.2f})...")
        row = audit_one(entry)
        rows.append(row)
        print(f"    -> contract={row.get('contract_to')} code_len={row.get('contract_code_len_bytes')} "
              f"вердикт: {row.get('verdict', row.get('error'))}")

    n_closed_cycle = sum(1 for r in rows if r.get("verdict", "").startswith("ЗАМКНУТЫЙ"))
    n_exchange = sum(1 for r in rows if r.get("verdict", "").startswith("ОБМЕН"))
    n_other = len(rows) - n_closed_cycle - n_exchange
    usd_confirmed_real_profit = sum(r["profit_usd"] for r in rows if r.get("verdict", "").startswith("ЗАМКНУТЫЙ"))
    usd_now_confirmed_exchange = sum(r["profit_usd"] for r in rows if r.get("verdict", "").startswith("ОБМЕН"))

    result = {
        "n_total": len(rows), "n_closed_cycle": n_closed_cycle, "n_exchange": n_exchange, "n_other": n_other,
        "usd_confirmed_real_profit_of_these_15": round(usd_confirmed_real_profit, 2),
        "usd_now_confirmed_exchange_of_these_15": round(usd_now_confirmed_exchange, 2),
        "rows": rows,
    }
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path("data/task5_bot_profit_formula_audit_contract_balance_result.json").write_text(text)
    print(f"\n[contract_balance_audit] ИТОГ (15 бывших 'НЕИЗВЕСТНО'): {n_closed_cycle} замкнутых циклов "
          f"(${usd_confirmed_real_profit:,.2f}), {n_exchange} обменов (${usd_now_confirmed_exchange:,.2f}), "
          f"{n_other} прочее")
