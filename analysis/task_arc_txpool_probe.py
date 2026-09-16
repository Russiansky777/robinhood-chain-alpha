#!/usr/bin/env python3
"""Работает ли txpool на Arc публичном RPC -- реально проверить, не по
документации. rpc.mainnet.arc.io -- HTTPS/HTTP JSON-RPC endpoint (не
WebSocket) -- eth_subscribe в принципе требует WS-транспорт, честно
отметить эту оговорку транспорта отдельно от факта отказа самим узлом."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
BOT_A = "0x608750874fdcbcc21f4f9610af48e1babacf10fe"  # 225 циклов/час
BOT_B = "0xe1eee09af59990bdff17582467f5ddbef66c4bbd"  # 37 циклов
DATA_DIR = Path(__file__).parent.parent.joinpath("data")


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def rpc(method: str, params: list, timeout: int = 15) -> dict:
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        return {"http_status": resp.status_code, "body": resp.json() if resp.content else None}
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}


def addr_topic(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def bot_priority_sample(bot_addr: str, latest: int, n: int = 10) -> dict:
    """Targeted backward-скан по sender (не часовой скан) -- берёт до n
    последних Swap этого бота, для каждого сравнивает effectiveGasPrice
    receipt с baseFeePerGas его же блока."""
    found = []
    to_block = latest
    chunk = 20000
    calls = 0
    while len(found) < n and to_block > 0 and calls < 20:
        from_block = max(0, to_block - chunk)
        r = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                                  "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, None, addr_topic(bot_addr)]}])
        calls += 1
        body = r.get("body") or {}
        if "error" in body:
            err = body["error"]
            code = err.get("code")
            if code in (-32012, -32602) or "too large" in str(err.get("message", "")).lower() or "max results" in str(err.get("message", "")).lower():
                chunk = max(500, chunk // 4)
                continue  # тот же to_block, меньший диапазон -- не пропускаем блоки
            break
        logs = body.get("result", [])
        for l in logs:
            found.append(l["transactionHash"])
        to_block = from_block - 1
    found = found[:n]

    rows = []
    for txh in found:
        rec = rpc("eth_getTransactionReceipt", [txh]).get("body", {}).get("result")
        if not rec:
            continue
        blk_num = rec.get("blockNumber")
        blk = rpc("eth_getBlockByNumber", [blk_num, False]).get("body", {}).get("result")
        eff_gas_price = int(rec.get("effectiveGasPrice", "0x0"), 16)
        base_fee = int(blk.get("baseFeePerGas", "0x0"), 16) if blk and blk.get("baseFeePerGas") else None
        priority_wei = (eff_gas_price - base_fee) if base_fee is not None else None
        rows.append({
            "tx_hash": txh, "effective_gas_price_gwei": eff_gas_price / 1e9,
            "base_fee_per_gas_gwei": (base_fee / 1e9) if base_fee is not None else None,
            "priority_fee_gwei": (priority_wei / 1e9) if priority_wei is not None else None,
        })
    priorities = [r["priority_fee_gwei"] for r in rows if r["priority_fee_gwei"] is not None]
    return {"bot": bot_addr, "n_calls_used": calls, "n_tx_sampled": len(rows), "rows": rows,
            "avg_priority_fee_gwei": (sum(priorities) / len(priorities)) if priorities else None,
            "min_priority_fee_gwei": min(priorities) if priorities else None,
            "max_priority_fee_gwei": max(priorities) if priorities else None}


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}

    # 1-3. txpool_* методы
    for method in ("txpool_status", "txpool_content", "txpool_inspect"):
        result[method] = rpc(method, [])

    # 4. eth_subscribe -- честная оговорка транспорта
    result["eth_subscribe_newPendingTransactions"] = rpc("eth_subscribe", ["newPendingTransactions"])
    result["eth_subscribe_transport_caveat"] = (
        "rpc.mainnet.arc.io здесь используется как HTTPS JSON-RPC (POST), не WebSocket. "
        "eth_subscribe в принципе не поддерживается транспортом HTTP (общее правило JSON-RPC, не специфика Arc) -- "
        "поэтому ошибка ниже может отражать 'этот HTTP-эндпоинт не поддерживает подписки' наравне с "
        "'узел отклоняет подписки по документации'. Отдельный WS-эндпоинт для Arc не найден/не проверен."
    )

    # 5. Если txpool_content реально отдаёт контент -- опросить 20 раз/100мс
    content_body = (result["txpool_content"].get("body") or {})
    content_usable = "result" in content_body and isinstance(content_body.get("result"), dict) and \
                      any(content_body["result"].get(k) for k in ("pending", "queued"))
    result["txpool_content_usable"] = bool(content_usable)

    poll_summary = {"note": "txpool_content не отдал непустой pending/queued -- опрос 20x не проводился (нет смысла опрашивать пустоту)."}
    if content_usable:
        latest_block = rpc("eth_getBlockByNumber", ["latest", False])
        known_block_txs = set((latest_block.get("body", {}).get("result") or {}).get("transactions", []))
        seen_pending_hashes = set()
        samples = []
        for i in range(20):
            body = rpc("txpool_content", []).get("body", {})
            pending = (body.get("result") or {}).get("pending", {})
            hashes_now = set()
            for addr, by_nonce in pending.items():
                for nonce, txinfo in by_nonce.items():
                    hashes_now.add(txinfo.get("hash"))
            new_hashes = hashes_now - seen_pending_hashes - known_block_txs
            samples.append({"i": i, "n_pending_total": len(hashes_now), "n_new_unseen": len(new_hashes)})
            seen_pending_hashes |= hashes_now
            time.sleep(0.1)
        latest_block_2 = rpc("eth_getBlockByNumber", ["latest", False])
        later_block_txs = set((latest_block_2.get("body", {}).get("result") or {}).get("transactions", []))
        confirmed_later = seen_pending_hashes & later_block_txs
        poll_summary = {
            "samples": samples, "n_unique_pending_hashes_seen_across_20_polls": len(seen_pending_hashes),
            "n_of_those_confirmed_in_later_block": len(confirmed_later),
        }
    result["question5_poll_20x_100ms"] = poll_summary

    # 6. Приоритетная комиссия ботов
    latest = int(rpc("eth_blockNumber", []).get("body", {}).get("result", "0x0"), 16)
    result["question6_priority_fee"] = {
        "bot_a_225_per_hour": bot_priority_sample(BOT_A, latest),
        "bot_b_37_total": bot_priority_sample(BOT_B, latest),
    }

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_txpool_probe_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
