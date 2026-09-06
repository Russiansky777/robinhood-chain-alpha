#!/usr/bin/env python3
"""Форензика fomo, продолжение п.2: реальный пул найден по частоте
Transfer-контрагентов -- 0x8366a39cc670b4001a1121b8f6a443a643e40951
(топ-1 и для BUDDY 17/33, и для PICKAZO 93/119 -- совпадение по двум
независимым токенам, не случайность). Перед тем как искать схему на
Dune (платно), пробуем определить тип контракта БЕСПЛАТНО через
eth_call стандартных ABI-функций популярных DEX-паттернов (Uniswap
V2 Pair: token0/token1/factory; Uniswap V3 Pool: token0/token1/fee;
свой роутер/curve: probe getReserves) -- если отвечает как
известный стандарт, дальше можно искать НЕ по 100 схемам, а по
конкретной decoded-схеме этого стандарта (uniswap_v2_robinhood и т.п.,
уже видели в реальном списке схем)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _rpc_call, topic0  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_probe_pool_contract_result.json")

POOL_ADDR = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

# 4-байтные селекторы общеизвестных ABI-сигнатур (считаем, не хардкодим память)
def selector(sig: str) -> str:
    return topic0(sig)[:10]


PROBES = {
    "token0()": selector("token0()"),
    "token1()": selector("token1()"),
    "factory()": selector("factory()"),
    "fee()": selector("fee()"),
    "getReserves()": selector("getReserves()"),
    "name()": selector("name()"),
    "symbol()": selector("symbol()"),
}


def decode_address(hex_result: str) -> str | None:
    if not hex_result or hex_result == "0x":
        return None
    raw = hex_result.removeprefix("0x")
    if len(raw) < 64:
        return None
    return "0x" + raw[-40:]


def decode_string_or_bytes32(hex_result: str) -> str | None:
    if not hex_result or hex_result == "0x":
        return None
    raw = bytes.fromhex(hex_result.removeprefix("0x"))
    try:
        if len(raw) >= 64:
            length = int.from_bytes(raw[32:64], "big")
            if 0 < length <= len(raw) - 64:
                return raw[64:64 + length].decode("utf-8", errors="replace")
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace") or None
    except Exception:
        return None


def eth_call(to: str, data: str) -> str | None:
    result, err = None, None
    try:
        result = _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    return result if err is None else None


def run() -> int:
    has_code_result = _rpc_call("eth_getCode", [POOL_ADDR, "latest"])
    has_code = has_code_result not in (None, "0x", "0x0")
    print(f"[probe] {POOL_ADDR}: bytecode present={has_code} (len={len(has_code_result or '')})")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "pool_address": POOL_ADDR, "has_bytecode": has_code, "probes": {}}

    for name, sel in PROBES.items():
        raw = eth_call(POOL_ADDR, sel)
        decoded = None
        if raw and raw != "0x":
            if name in ("token0()", "token1()", "factory()"):
                decoded = decode_address(raw)
            elif name in ("name()", "symbol()"):
                decoded = decode_string_or_bytes32(raw)
            elif name == "fee()":
                decoded = int(raw, 16) if raw != "0x" else None
            elif name == "getReserves()":
                decoded = raw  # составной, просто печатаем сырые байты
        print(f"[probe]   {name}: raw={raw!r} decoded={decoded!r}")
        out["probes"][name] = {"raw": raw, "decoded": decoded}
        time.sleep(0.15)

    is_uniswap_v2_like = bool(out["probes"]["token0()"]["decoded"] and out["probes"]["token1()"]["decoded"]
                               and out["probes"]["getReserves()"]["raw"] not in (None, "0x"))
    is_uniswap_v3_like = bool(out["probes"]["token0()"]["decoded"] and out["probes"]["token1()"]["decoded"]
                               and out["probes"]["fee()"]["decoded"] is not None)
    out["classification"] = {
        "looks_like_uniswap_v2_pair": is_uniswap_v2_like,
        "looks_like_uniswap_v3_pool": is_uniswap_v3_like,
    }
    print(f"\n[probe] классификация: {out['classification']}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[probe] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
