#!/usr/bin/env python3
"""Узкая проверка личности доминирующего currency0 (0x3600...0000, ~74%
пулов из 24567 найденных Initialize-событий). Ровно то, что просили:
один раунд eth_call (symbol/name/decimals) -- НЕ предположение по
паттерну, а реальный ABI-вызов на сам адрес. Заодно проверяем
0x0000...0000 для контраста (ожидание: это placeholder нативной валюты
V4, а не ERC20-контракт -- eth_getCode должен быть пустым)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

RPC = "https://rpc.mainnet.arc.io"
CANDIDATES = {
    "currency0_dominant": "0x3600000000000000000000000000000000000000",
    "native_placeholder_control": "0x0000000000000000000000000000000000000000",
}

SELECTORS = {"name": "0x06fdde03", "symbol": "0x95d89b41", "decimals": "0x313ce567"}


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          headers={"Content-Type": "application/json"}, timeout=timeout)
    return resp.json()


def decode_abi_string(hex_data: str) -> str | None:
    """Декодирует стандартный ABI-encoded string return (offset+len+bytes)."""
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


def eth_call(address: str, selector: str) -> dict:
    body = rpc("eth_call", [{"to": address, "data": selector}, "latest"])
    return body


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC,
                     "note": "Реальный eth_call на сам адрес -- не предположение по паттерну использования."}

    for label, addr in CANDIDATES.items():
        entry: dict = {"address": addr}
        try:
            code = rpc("eth_getCode", [addr, "latest"])["result"]
            entry["code_len_bytes"] = (len(code) - 2) // 2 if code else 0
            entry["has_code"] = bool(code) and code != "0x"
        except Exception as exc:  # noqa: BLE001
            entry["code_check_error"] = str(exc)

        for fn, selector in SELECTORS.items():
            try:
                body = eth_call(addr, selector)
                if "error" in body:
                    entry[fn] = {"error": body["error"]}
                    continue
                raw = body.get("result")
                if fn == "decimals":
                    entry[fn] = {"raw": raw, "decoded": int(raw, 16) if raw and raw != "0x" else None}
                else:
                    entry[fn] = {"raw": raw, "decoded": decode_abi_string(raw)}
            except Exception as exc:  # noqa: BLE001
                entry[fn] = {"exception": str(exc)}

        result[label] = entry

    dom = result.get("currency0_dominant", {})
    result["conclusion"] = {
        "is_confirmed_usdc": (dom.get("symbol", {}).get("decoded") == "USDC"
                               and dom.get("decimals", {}).get("decoded") == 6),
        "basis": "symbol()=='USDC' AND decimals()==6, оба из реального eth_call на 0x3600...0000",
    }

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_currency0_identity_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
