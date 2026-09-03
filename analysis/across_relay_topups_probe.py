#!/usr/bin/env python3
"""Владелец, 2026-09-03: "последнее пополнение и я его вижу -- с Relay
0.003948293119289604 ETH, а до этого должны были быть округлённо
0.0039 WETH и 0.00544 WETH с Across. За последний час пополнений
больше не было от меня."

Один из двух Across-переводов (~0.0039 WETH) уже разобран построчно
(data/p3_guard_cache/across_handler_probe_result.json) -- handler
сжигает WETH (Transfer -> address(0)), сам не удерживает ни WETH, ни
native ETH. Здесь ищем ВТОРОЙ Across-перевод (~0.00544 WETH) реальным
eth_getLogs (Transfer, to=кошелёк) за широкое окно, и пробуем Alchemy
`alchemy_getAssetTransfers` ОДНИМ вызовом (если поддерживается на этой
сети) для чистой сводки всех входящих (включая native ETH из Relay) --
не угадывается, либо находится реально, либо честно докладывается, что
не нашлось.

Только чтение.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

from alchemy_fallback import (  # noqa: E402
    _alchemy_direct_endpoint,
    _chunked_get_logs,
    get_block_number,
    topic0,
)

OUT_PATH = Path("data/p3_guard_cache/across_relay_topups_probe_result.json")
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
TRANSFER_TOPIC0 = topic0("Transfer(address,address,uint256)")
BLOCKS_PER_HOUR_EST = 35_000  # ~9.75 блоков/с, см. комментарии в alchemy_fallback.py -- округлено, оценка не факт
WINDOW_HOURS = 6  # с запасом -- владелец не помнит точное время второго Across-перевода


def _topic_addr(addr: str) -> str:
    return "0x" + addr[2:].lower().rjust(64, "0")


def _addr_from_topic(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:]


def try_alchemy_asset_transfers() -> dict:
    url = _alchemy_direct_endpoint()
    if not url:
        return {"available": False, "reason": "ALCHEMY_API_KEY/ALCHEMY_ROBINHOOD_RPC_URL не задан в этом окружении."}
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers",
        "params": [{
            "toAddress": WALLET,
            # НАЙДЕНО (реальный первый прогон, 2026-09-03): "internal" отклонён этой сетью --
            # "The 'internal' category is not supported for this network" -- убран.
            "category": ["external", "erc20"],
            "order": "desc",
            "maxCount": "0x19",
            "withMetadata": True,
        }],
    }
    try:
        r = requests.post(url, json=payload, headers={"User-Agent": "robinhood-chain-alpha-p5/1.0"}, timeout=30)
        body = r.json()
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"сетевая/парсинг ошибка: {e}"}
    if "error" in body:
        return {"available": False, "reason": f"JSON-RPC error: {body['error']}"}
    return {"available": True, "result": body.get("result")}


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    print("=== Попытка alchemy_getAssetTransfers (один вызов, если поддерживается) ===")
    aat = try_alchemy_asset_transfers()
    result["alchemy_getAssetTransfers"] = aat
    if aat["available"]:
        transfers = aat["result"].get("transfers", [])
        print(f"[topups_probe] alchemy_getAssetTransfers: {len(transfers)} переводов найдено")
        for t in transfers:
            print(f"  {t}")
    else:
        print(f"[topups_probe] alchemy_getAssetTransfers НЕ доступен на этой сети/ключе: {aat['reason']}")

    print(f"\n=== Реальный eth_getLogs: WETH Transfer(to={WALLET}) за последние ~{WINDOW_HOURS}ч ===")
    latest = get_block_number()
    from_block = max(1, latest - WINDOW_HOURS * BLOCKS_PER_HOUR_EST)
    print(f"[topups_probe] диапазон блоков [{from_block}, {latest}] (текущий блок {latest})")
    topics = [TRANSFER_TOPIC0, None, _topic_addr(WALLET)]
    logs = list(_chunked_get_logs(from_block, latest, topics, chunk_size=5_000, address=WETH,
                                    on_call=lambda lo, hi, n: print(f"[topups_probe]   диапазон [{lo},{hi}]: {n} логов")))
    direct_transfers = []
    for log in logs:
        frm = _addr_from_topic(log["topics"][1])
        amount_raw = int(log["data"], 16)
        direct_transfers.append({
            "block_number": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"],
            "from": frm, "amount_raw": amount_raw, "amount_human": amount_raw / 1e18,
        })
    direct_transfers.sort(key=lambda e: e["block_number"])
    result["weth_transfers_direct_to_wallet"] = direct_transfers
    print(f"[topups_probe] прямых WETH-переводов НА кошелёк за окно: {len(direct_transfers)}")
    for e in direct_transfers:
        print(f"  block={e['block_number']} tx={e['tx_hash']} from={e['from']} amount={e['amount_human']:.8f} WETH")

    # НАЙДЕНО (реальный первый прогон, 2026-09-03): ни один прямой WETH-
    # перевод НА кошелёк за окно не оказался внешним пополнением -- все
    # 10 результатов это либо наш собственный wrap (from=0x0, deposit()),
    # либо возврат из пула P5 (from=0x52e65b17...) -- согласуется с уже
    # разобранным первым Across-переводом: handler СЖИГАЕТ WETH
    # (Transfer -> address(0)), не credit'ит кошелёк напрямую. Ищем
    # ВТОРОЙ Across-перевод (~0.00544 WETH) тем же паттерном -- burn'ы
    # WETH за то же окно, без привязки к конкретному handler-адресу
    # (мог быть другой handler).
    print(f"\n=== Реальный eth_getLogs: WETH Transfer(to=0x0, burn) за последние ~{WINDOW_HOURS}ч ===")
    burn_topics = [TRANSFER_TOPIC0, None, _topic_addr("0x0000000000000000000000000000000000000000")]
    burn_logs = list(_chunked_get_logs(from_block, latest, burn_topics, chunk_size=5_000, address=WETH,
                                         on_call=lambda lo, hi, n: print(f"[topups_probe]   диапазон [{lo},{hi}]: {n} логов")))
    burns = []
    for log in burn_logs:
        frm = _addr_from_topic(log["topics"][1])  # адрес, который сжёг WETH -- это и есть handler той сделки
        amount_raw = int(log["data"], 16)
        burns.append({
            "block_number": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"],
            "burned_by": frm, "amount_raw": amount_raw, "amount_human": amount_raw / 1e18,
        })
    burns.sort(key=lambda e: e["block_number"])
    result["weth_burns"] = burns
    print(f"[topups_probe] WETH burn-событий (Transfer -> address(0)) за окно: {len(burns)}")
    for e in burns:
        flag = " <-- близко к ~0.00544" if abs(e["amount_human"] - 0.00544) < 0.0003 else \
               (" <-- уже разобран (across_handler_probe, ~0.0039)" if abs(e["amount_human"] - 0.0039) < 0.0002 else "")
        print(f"  block={e['block_number']} tx={e['tx_hash']} burned_by={e['burned_by']} "
              f"amount={e['amount_human']:.8f} WETH{flag}")

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[topups_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
