#!/usr/bin/env python3
"""Владелец скорректировал прошлую поправку: 0x47e7936ae9891e61c5123db
720593c05de7120cc -- НЕ касса бота, а сборщик комиссии хука/площадки
(~2.5% от выхода USDC из PoolManager). Реальная ссылочная tx (обычная
розничная продажа, не цикл): 0xc06934c034d507921ba9fe5b59d378047ab0ddb
811c1a75ff200ca018b5ed6c1 -- проверить самостоятельно перед записью в
паспорт, не доверять описанию без проверки. Плюс: 20 входящих переводов
на 0x47e79...20cc вразброс по 24ч-окну -- посчитать долю от общего
выхода USDC из PoolManager в каждой их tx, проверить постоянство ставки."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

from arc_units import USDC_ERC20, USDC_NATIVE_WRAP, to_human, merge_mirrored_transfers

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PROFIT_RECEIVER = "0x47e7936ae9891e61c5123db720593c05de7120cc"
REFERENCE_TX = "0xc06934c034d507921ba9fe5b59d378047ab0ddb811c1a75ff200ca018b5ed6c1"
DATA_DIR = Path(__file__).parent.parent.joinpath("data")
BLOCK_TIME_S = 0.506


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")


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


def addr_topic(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:]


def decode_transfer(log: dict) -> dict | None:
    if len(log.get("topics", [])) < 3:
        return None
    return {"token": log["address"].lower(), "from": topic_to_addr(log["topics"][1]).lower(),
            "to": topic_to_addr(log["topics"][2]).lower(),
            "value": int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0}


def usdc_equiv(token: str, raw: int) -> float | None:
    return to_human(token, raw)


def analyze_tx_fee_rate(tx_hash: str) -> dict:
    receipt = rpc("eth_getTransactionReceipt", [tx_hash]).get("result")
    if not receipt:
        return {"tx_hash": tx_hash, "error": "receipt не получен"}
    transfers_raw = [t for t in (decode_transfer(l) for l in receipt.get("logs", [])
                                  if l.get("topics", [None])[0] == TRANSFER_TOPIC0) if t]
    transfers = merge_mirrored_transfers(transfers_raw)

    usdc_tokens = {USDC_ERC20.lower(), USDC_NATIVE_WRAP.lower()}
    out_from_pm = [t for t in transfers if t["from"] == POOL_MANAGER.lower() and t["token"] in usdc_tokens]
    total_out_usdc = sum(usdc_equiv(t["token"], t["value"]) or 0 for t in out_from_pm)
    to_receiver = [t for t in transfers if t["to"] == PROFIT_RECEIVER.lower() and t["token"] in usdc_tokens]
    amount_to_receiver = sum(usdc_equiv(t["token"], t["value"]) or 0 for t in to_receiver)

    gas_used = int(receipt.get("gasUsed", "0x0"), 16)
    eff_gas_price = int(receipt.get("effectiveGasPrice", "0x0"), 16)
    blk = rpc("eth_getBlockByNumber", [receipt.get("blockNumber"), False]).get("result")
    base_fee = int(blk.get("baseFeePerGas", "0x0"), 16) if blk and blk.get("baseFeePerGas") else None

    return {
        "tx_hash": tx_hash, "block_number": int(receipt["blockNumber"], 16),
        "total_usdc_out_of_pool_manager": total_out_usdc,
        "amount_to_receiver_usdc": amount_to_receiver,
        "ratio_to_receiver": (amount_to_receiver / total_out_usdc) if total_out_usdc else None,
        "effective_gas_price_gwei": eff_gas_price / 1e9,
        "base_fee_per_gas_gwei": (base_fee / 1e9) if base_fee is not None else None,
        "priority_fee_gwei": ((eff_gas_price - base_fee) / 1e9) if base_fee is not None else None,
        "priority_multiplier_vs_base": (eff_gas_price / base_fee) if base_fee else None,
        "all_usdc_transfers_deduped": [{"from": t["from"], "to": t["to"], "token": t["token"],
                                         "usdc_equiv": usdc_equiv(t["token"], t["value"])}
                                        for t in transfers if t["token"] in usdc_tokens],
    }


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # === Проверка ссылочной tx владельца (не доверять описанию без проверки) ===
    result["reference_tx_verification"] = analyze_tx_fee_rate(REFERENCE_TX)

    # === 20 входящих переводов вразброс по 24ч-окну ===
    latest = int(rpc("eth_blockNumber", [])["result"], 16)
    window_blocks = int(24 * 3600 / BLOCK_TIME_S)
    from_block_24h = max(0, latest - window_blocks)

    all_incoming = []
    to_block = latest
    chunk = 20000
    n_calls_scan = 0
    while to_block > from_block_24h and n_calls_scan < 40:
        fb = max(from_block_24h, to_block - chunk)
        body = rpc("eth_getLogs", [{"fromBlock": hex(fb), "toBlock": hex(to_block),
                                     "topics": [TRANSFER_TOPIC0, None, addr_topic(PROFIT_RECEIVER)]}])
        n_calls_scan += 1
        if "error" in body:
            err = body["error"]
            if err.get("code") in (-32012, -32602) or "too large" in str(err.get("message", "")).lower() or "max results" in str(err.get("message", "")).lower():
                chunk = max(500, chunk // 3)
                continue
            break
        for l in body.get("result", []):
            all_incoming.append({"tx_hash": l["transactionHash"], "block": int(l["blockNumber"], 16)})
        to_block = fb - 1

    # Равномерная выборка 20 ПО БЛОКАМ (вразброс, не подряд) из уникальных tx
    unique_by_tx = {}
    for r in all_incoming:
        unique_by_tx.setdefault(r["tx_hash"], r["block"])
    unique_list = sorted(unique_by_tx.items(), key=lambda kv: kv[1])  # по блоку, по возрастанию
    n_unique = len(unique_list)
    sample_idx = sorted({int(i * n_unique / 20) for i in range(20)}) if n_unique >= 20 else list(range(n_unique))
    sample_txs = [unique_list[i][0] for i in sample_idx]

    rate_rows = []
    for txh in sample_txs:
        rate_rows.append(analyze_tx_fee_rate(txh))

    ratios = [r["ratio_to_receiver"] for r in rate_rows if r.get("ratio_to_receiver") is not None]
    n_at_2_5pct = sum(1 for r in ratios if abs(r - 0.025) <= 0.0005)
    other_values = sorted({round(r, 4) for r in ratios if abs(r - 0.025) > 0.0005})

    result["fee_rate_sample_20_spread"] = {
        "n_calls_backward_scan": n_calls_scan, "n_unique_incoming_tx_in_24h_window": n_unique,
        "n_sampled": len(sample_txs), "rows": rate_rows,
        "n_at_2_5_percent": n_at_2_5pct, "other_ratio_values": other_values,
        "summary_line": f"ставка 2.5% в {n_at_2_5pct} из {len(ratios)}, иные значения: {other_values}",
    }

    if n_at_2_5pct == len(ratios) and ratios:
        daily_inflow_usd = 94608.50949651619  # из data/task_arc_speed_probe_result.json, часть 4, подтверждена ранее делением на 1e18
        result["platform_daily_volume_estimate"] = {
            "assumption": "ставка ПОСТОЯННА и равна 2.5% (подтверждено на всей выборке 20/20)",
            "daily_inflow_to_receiver_usd": daily_inflow_usd,
            "estimated_daily_platform_volume_usd": daily_inflow_usd / 0.025,
            "caveat": "оценка при допущении постоянной ставки на ВСЕ 24267 tx за 24ч, а не только на 20-выборку",
        }
    else:
        result["platform_daily_volume_estimate"] = "ставка не постоянна на всей выборке -- оценка суточного оборота не считается (была бы недостоверной)"

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_fee_rate_verify_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
