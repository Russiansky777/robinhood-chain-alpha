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

CANDIDATE_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
EXPECTED_FACTORY_BASE = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
FACTORY_SELECTOR = "0xc45a0155"  # factory()
OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_router_probe_result.json")
P5_POOL_ROBINHOOD = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca"
FACTORY_SELECTOR_POOL = "0xc45a0155"  # тот же factory() -- есть и на самом пуле v3


def rpc_call(rpc_url: str, method: str, params: list):
    r = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def check_router(rpc_url: str, chain_label: str, expected_factory: str | None) -> dict:
    entry = {}
    code = rpc_call(rpc_url, "eth_getCode", [CANDIDATE_ROUTER, "latest"])
    entry["has_bytecode"] = code not in ("0x", "0x0", None)
    entry["bytecode_len_hex_chars"] = len(code) if code else 0
    print(f"[router_probe] {chain_label}: bytecode на {CANDIDATE_ROUTER}: {'ЕСТЬ' if entry['has_bytecode'] else 'НЕТ'} ({entry['bytecode_len_hex_chars']} hex-символов)")
    if entry["has_bytecode"]:
        try:
            factory_raw = rpc_call(rpc_url, "eth_call", [{"to": CANDIDATE_ROUTER, "data": FACTORY_SELECTOR}, "latest"])
            factory_addr = "0x" + factory_raw[-40:]
            entry["factory_call_result"] = factory_addr
            if expected_factory:
                entry["factory_matches_expected"] = factory_addr.lower() == expected_factory.lower()
                print(f"[router_probe] {chain_label}: factory() вернул {factory_addr} (ожидался {expected_factory}) -> "
                      f"{'СОВПАДАЕТ' if entry['factory_matches_expected'] else 'НЕ СОВПАДАЕТ'}")
            else:
                print(f"[router_probe] {chain_label}: factory() вернул {factory_addr}")
        except Exception as exc:  # noqa: BLE001
            entry["factory_call_error"] = str(exc)[:300]
            print(f"[router_probe] {chain_label}: factory() упал: {entry['factory_call_error']}")
    return entry


def run() -> int:
    out = {"candidate_router": CANDIDATE_ROUTER}
    out["base"] = check_router("https://mainnet.base.org", "Base", EXPECTED_FACTORY_BASE)

    # Robinhood Chain -- НЕТ гарантии, что тот же CREATE2-адрес используется
    # (кастомный чейн может задеплоить Uniswap v3 через другой deployer/salt).
    # Сначала узнаём РЕАЛЬНЫЙ Factory самого P5-пула (уже подтверждённого
    # v3-контракта) -- это НАШ якорь для сверки, не предположение.
    rh_rpc = "https://rpc.mainnet.chain.robinhood.com"
    p5_factory_raw = rpc_call(rh_rpc, "eth_call", [{"to": P5_POOL_ROBINHOOD, "data": FACTORY_SELECTOR_POOL}, "latest"])
    p5_factory = "0x" + p5_factory_raw[-40:]
    out["p5_pool_factory_robinhood"] = p5_factory
    print(f"[router_probe] Robinhood: P5-пул.factory() = {p5_factory} (реальный якорь для сверки router'а)")

    out["robinhood"] = check_router(rh_rpc, "Robinhood", None)
    if out["robinhood"].get("has_bytecode") and out["robinhood"].get("factory_call_result"):
        out["robinhood"]["factory_matches_p5_pool_factory"] = out["robinhood"]["factory_call_result"].lower() == p5_factory.lower()
        print(f"[router_probe] Robinhood: тот же router.factory() совпадает с P5-пул.factory()? "
              f"{out['robinhood']['factory_matches_p5_pool_factory']}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"[router_probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
