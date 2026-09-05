#!/usr/bin/env python3
"""Консолидация активов -- разведка ПЕРЕД реальными транзакциями: РЕАЛЬНАЯ
проверка адреса Uniswap V3 SwapRouter02 на Base (владелец дал 'да' на
свапы, 2026-09-05). Кандидат `0x2626664c2603336E57B271c5C0b26F421741e481`
(канонический адрес SwapRouter02, тот же на многих сетях через CREATE2) --
НЕ используется вслепую: проверяем bytecode существует и `factory()`
реально возвращает канонический Uniswap V3 Factory
(`0x33128a8fC17869897dcE68Ed026d694621f6FDfD`, тоже CREATE2-детерминированный,
тот же на Base/Arbitrum/Optimism/mainnet). Расхождение -- явный СТОП,
не гадаем/не подставляем предположительный адрес в реальную транзакцию."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

BASE_RPC = "https://mainnet.base.org"
CANDIDATE_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
EXPECTED_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
FACTORY_SELECTOR = "0xc45a0155"  # factory()
OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_router_probe_result.json")


def rpc_call(method: str, params: list):
    r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def run() -> int:
    out = {"candidate_router": CANDIDATE_ROUTER}
    code = rpc_call("eth_getCode", [CANDIDATE_ROUTER, "latest"])
    out["has_bytecode"] = code not in ("0x", "0x0", None)
    out["bytecode_len_hex_chars"] = len(code) if code else 0
    print(f"[router_probe] bytecode на {CANDIDATE_ROUTER}: {'ЕСТЬ' if out['has_bytecode'] else 'НЕТ'} ({out['bytecode_len_hex_chars']} hex-символов)")

    if out["has_bytecode"]:
        factory_raw = rpc_call("eth_call", [{"to": CANDIDATE_ROUTER, "data": FACTORY_SELECTOR}, "latest"])
        factory_addr = "0x" + factory_raw[-40:]
        out["factory_call_result"] = factory_addr
        out["factory_matches_expected"] = factory_addr.lower() == EXPECTED_FACTORY.lower()
        print(f"[router_probe] factory() вернул: {factory_addr} (ожидался {EXPECTED_FACTORY}) -> "
              f"{'СОВПАДАЕТ' if out['factory_matches_expected'] else 'НЕ СОВПАДАЕТ -- СТОП'}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[router_probe] результат записан в {OUT_PATH}")
    return 0 if out.get("factory_matches_expected") else 1


if __name__ == "__main__":
    raise SystemExit(run())
