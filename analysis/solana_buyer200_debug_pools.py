#!/usr/bin/env python3
"""Диагностика Шага 1: почему 2 пула из 5 сверяемых сигнатур дают
scanned=0/no_historical_swap в ускоренном методе, хотя у другого ИИ (через
полный blockIndex/rpc.py) для них есть реальные цены. Разовый скрипт --
не часть основного пайплайна."""
import json
import os

import requests

SUSPECT = [
    ("36SpkkrsnyUgjPC24KU993r8T3whkJaPgoEeAmbBj9uB", "sig4 leg1 (единственная нога)"),
    ("5QpDQ6ddkv1ArytJQ991kh8doeXPrt9hHFWK2HmEToDm", "sig5 leg2 (упавшая нога)"),
]
CONTROL = ("kuasvHwyBm31UjcgCQKLGQYJ5xZnsjpxWfSYZLHgduH", "sig1/2/3 leg1 (рабочий, контроль)")

REF_SIGS = {
    "36SpkkrsnyUgjPC24KU993r8T3whkJaPgoEeAmbBj9uB": "36XKYQ1DmHn9GzWw5L3AcsuCapXWoZgmgPFdCPqr8z1qE1tUDCgQcvDVXYYRm7DyoYEoKcppZwfQ4n7sqTBBEmic",
    "5QpDQ6ddkv1ArytJQ991kh8doeXPrt9hHFWK2HmEToDm": None,
}

ALCHEMY = os.environ.get("ALCHEMY_API_KEY", "")
ENDPOINTS = {"public": "https://api.mainnet-beta.solana.com"}
if ALCHEMY:
    ENDPOINTS["alchemy"] = f"https://solana-mainnet.g.alchemy.com/v2/{ALCHEMY}"


def rpc(url, method, params):
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500])


def check_address(addr, label):
    print(f"\n=== {label}: {addr} ===")
    for name, url in ENDPOINTS.items():
        status, body = rpc(url, "getSignaturesForAddress", [addr, {"limit": 5}])
        print(f"[{name}] getSignaturesForAddress status={status}")
        print(json.dumps(body, indent=2)[:1500])
        status2, body2 = rpc(url, "getAccountInfo", [addr, {"encoding": "base64"}])
        exists = bool(body2.get("result", {}).get("value")) if isinstance(body2, dict) else False
        print(f"[{name}] getAccountInfo exists={exists} owner={(body2.get('result') or {}).get('value', {}).get('owner') if isinstance(body2, dict) and body2.get('result') else None}")


if __name__ == "__main__":
    check_address(*CONTROL)
    for addr, label in SUSPECT:
        check_address(addr, label)
        ref_sig = REF_SIGS.get(addr)
        if ref_sig:
            for name, url in ENDPOINTS.items():
                status, body = rpc(url, "getTransaction", [ref_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
                found = bool(isinstance(body, dict) and body.get("result"))
                slot = (body.get("result") or {}).get("slot") if found else None
                print(f"[{name}] getTransaction({ref_sig[:12]}..) found={found} slot={slot}")
