#!/usr/bin/env python3
"""Дополнение к task_arc_hour_zero_audit.py: тот скрипт сохранил топ-30
пулов по объёму только как адреса currency0/currency1 -- владелец просил
"пара (символы)". Читает УЖЕ сохранённый результат (без пересканирования
логов) и делает по одному настоящему eth_call symbol()/decimals() на
не-USDC ногу каждого пула топ-30 (до 30 вызовов, дёшево)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

RPC = "https://rpc.mainnet.arc.io"
DATA_DIR = Path(__file__).parent.parent.joinpath("data")
RESULT_PATH = DATA_DIR / "task_arc_hour_zero_audit_result.json"
USDC_ADDR = "0x3600000000000000000000000000000000000000"


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
        if err and ("rate limit" in str(err.get("message", "")).lower()):
            if attempt == 4:
                return body
            time.sleep(2 * (attempt + 1))
            continue
        return body
    return {"error": {"message": "unreachable"}}


def decode_abi_string(hex_data: str) -> str | None:
    if not hex_data or hex_data == "0x":
        return None
    raw = bytes.fromhex(hex_data[2:])
    if len(raw) < 64:
        return None
    length = int.from_bytes(raw[32:64], "big")
    data = raw[64:64 + length]
    try:
        return data.decode("utf-8")
    except Exception:
        return None


def get_symbol_decimals(addr: str) -> dict:
    if addr.lower() == "0x0000000000000000000000000000000000000000":
        return {"symbol": "NATIVE(Arc)", "decimals": None, "note": "address(0) -- нативная валюта V4, не ERC20"}
    sym_body = rpc("eth_call", [{"to": addr, "data": "0x95d89b41"}, "latest"])
    dec_body = rpc("eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"])
    sym = decode_abi_string(sym_body.get("result")) if "error" not in sym_body else None
    dec_raw = dec_body.get("result") if "error" not in dec_body else None
    dec = int(dec_raw, 16) if dec_raw and dec_raw != "0x" else None
    return {"symbol": sym, "decimals": dec,
            "symbol_call_error": sym_body.get("error"), "decimals_call_error": dec_body.get("error")}


def main() -> None:
    if not RESULT_PATH.exists():
        print(json.dumps({"error": f"{RESULT_PATH} не найден на этом хосте"}))
        return
    audit = json.loads(RESULT_PATH.read_text())
    top_pools = audit.get("pools_hourly", {}).get("top_pools", [])

    cache: dict[str, dict] = {USDC_ADDR: {"symbol": "USDC", "decimals": 6}}
    enriched = []
    for p in top_pools:
        c0, c1 = p["currency0"], p["currency1"]
        for addr in (c0, c1):
            if addr and addr.lower() not in cache:
                cache[addr.lower()] = get_symbol_decimals(addr)
        enriched.append({
            "pool_id": p["pool_id"],
            "pair_symbols": f"{cache.get(c0.lower(), {}).get('symbol') or c0[:10]}/"
                             f"{cache.get(c1.lower(), {}).get('symbol') or c1[:10]}",
            "currency0": c0, "currency0_symbol": cache.get(c0.lower(), {}).get("symbol"),
            "currency1": c1, "currency1_symbol": cache.get(c1.lower(), {}).get("symbol"),
            "fee_initialize_pips": p["fee_initialize"], "hooks": p["hooks"],
            "volume_usdc_1h": p["volume_usdc"], "n_swaps": p["n_swaps"],
            "n_unique_traders_sampled": p["n_unique_tx_from_resolved"],
            "n_traders_sample_size": p["n_tx_hashes_sampled_for_from"],
            "tvl_usdc_side_estimate": p["tvl_usdc_side_estimate"],
        })

    result = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "token_symbol_cache": cache, "top_pools_enriched": enriched}
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_top_pools_symbols_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
