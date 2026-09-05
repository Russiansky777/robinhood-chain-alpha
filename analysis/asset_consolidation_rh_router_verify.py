#!/usr/bin/env python3
"""Консолидация активов -- верификация РЕАЛЬНОГО router'а на Robinhood
Chain. Сырой список tx_to из истории сделок высокотрафикового v3-пула
(`asset_consolidation_rh_router_recon_result.json`) содержит МУСОР --
топ-1 адрес (`0x65050a9b...c40dc`, 9.09M "сделок") реально
задокументирован в паспорте как адрес САМОТОРГОВЛИ (wash-trading бот,
FOMO-форензика), не router; другие похожи на ERC-4337 EntryPoint
(`0x0000000071727De22E5E9d8BAf0edAc6f37da032`, канонический v0.7) или
MEV/aggregator-контракты с vanity-адресами (много нулей) -- НЕ
используем ни один вслепую.

Реальная проверка: для каждого кандидата -- есть ли bytecode, и
возвращает ли factory() ТОТ ЖЕ адрес, что и наши реальные v3-пулы
(0x1f7d7550b1b028f7571e69a784071f0205fd2efa) -- только тогда это
кандидат в настоящие router'ы ЭТОГО протокола, не левый контракт."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

RH_RPC = "https://rpc.mainnet.chain.robinhood.com"
EXPECTED_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
FACTORY_SELECTOR = "0xc45a0155"
RECON_PATH = Path("data/p3_guard_cache/asset_consolidation_rh_router_recon_result.json")
OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_rh_router_verify_result.json")
KNOWN_WASH_TRADING_BOT = "65050a9b7e5075a2ba5ced7b1b64ee66262c40dc"  # docs/PROJECT_STATE.md -- FOMO-форензика


def rpc_call(method: str, params: list):
    r = requests.post(RH_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def run() -> int:
    recon = json.loads(RECON_PATH.read_text())
    candidates = [r["tx_to"] for r in recon["rows"]]
    results = []
    for addr_hex in candidates:
        addr = "0x" + addr_hex
        entry = {"address": addr, "n_trades": next(r["n_trades"] for r in recon["rows"] if r["tx_to"] == addr_hex)}
        if addr_hex.lower() == KNOWN_WASH_TRADING_BOT:
            entry["skip_reason"] = "известный адрес самоторговли (FOMO-форензика, docs/PROJECT_STATE.md) -- не router"
            results.append(entry)
            print(f"  {addr}: ПРОПУСК -- {entry['skip_reason']}")
            continue
        try:
            code = rpc_call("eth_getCode", [addr, "latest"])
            entry["has_bytecode"] = code not in ("0x", "0x0", None)
            if entry["has_bytecode"]:
                try:
                    factory_raw = rpc_call("eth_call", [{"to": addr, "data": FACTORY_SELECTOR}, "latest"])
                    factory_addr = "0x" + factory_raw[-40:]
                    entry["factory_call_result"] = factory_addr
                    entry["factory_matches"] = factory_addr.lower() == EXPECTED_FACTORY.lower()
                except Exception as exc:  # noqa: BLE001
                    entry["factory_call_error"] = str(exc)[:200]
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:200]
        results.append(entry)
        print(f"  {addr}: bytecode={entry.get('has_bytecode')} factory={entry.get('factory_call_result')} "
              f"matches={entry.get('factory_matches')}")
        time.sleep(0.4)

    matching = [r for r in results if r.get("factory_matches")]
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "results": results, "n_matching_factory": len(matching), "matching_candidates": matching}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[rh_router_verify] реальных кандидатов с совпадающим factory(): {len(matching)}")
    print(f"[rh_router_verify] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
