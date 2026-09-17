#!/usr/bin/env python3
"""Продолжение бесплатного шага 3 (fomo_9wallets_field_search.py):
у "ogle" (0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143) реальный Alchemy
alchemy_getAssetTransfers показал интенсивную ERC-20 активность, но 0
строк в декодированных Uniswap v3/v4 swap-таблицах Dune (ни в
taker/maker/sender/recipient). Определяем БЕСПЛАТНО (0 кредитов Dune,
только eth_getCode + пробные view-функции), что это за 4 контракта-
контрагента, чтобы понять, ГДЕ реально происходит движение токенов:

  1. 0x0bd7d308f8e1639fab988df18a8011f41eacad73 -- получатель ОДНОЙ из
     двух транзакций, где ogle сам был tx.from (raw robinhood.transactions).
  2. 0x88ad8ddf1e3898412146a534538d418c6f8a9062 -- получатель ВТОРОЙ.
  3. 0x4cd00e387622c35bddb9b4c962c136462338bc31 -- многократный получатель
     мелких (~$0.6-1.4) исходящих платежей USDG от ogle -- похоже на
     paymaster/relayer fee.
  4. 0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f -- многократный получатель
     крупных исходящих переводов (UP, "Ponsan") от ogle."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _rpc_call as rpc_call  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_ogle_counterparty_probe_result.json")

CANDIDATES = {
    "tx_to_1_raw_txn": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
    "tx_to_2_raw_txn": "0x88ad8ddf1e3898412146a534538d418c6f8a9062",
    "repeated_small_usdg_recipient": "0x4cd00e387622c35bddb9b4c962c136462338bc31",
    "repeated_large_token_recipient": "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f",
}

CANDIDATE_SELECTORS = {
    "owner()": "0x8da5cb5b",
    "entryPoint()": "0xb0d691fe",
    "implementation()": "0x5c60da1b",
    "name()": "0x06fdde03",
    "symbol()": "0x95d89b41",
    "factory()": "0xc45a0155",
    "WETH9()": "0xad5c4648",
    "token0()": "0x0dfe1681",
    "token1()": "0xd21220a7",
}
DELEGATE_KNOWN = "0xe6cae83bde06e4c305530e199d7217f42808555b"


def decode_address_return(raw_hex: str) -> str | None:
    if not raw_hex or raw_hex in ("0x", "0x0") or len(raw_hex) < 66:
        return None
    tail = raw_hex[-40:]
    if int(tail, 16) == 0:
        return None
    return "0x" + tail


def decode_string_return(raw_hex: str) -> str | None:
    if not raw_hex or raw_hex in ("0x", "0x0"):
        return None
    try:
        data = bytes.fromhex(raw_hex[2:])
        if len(data) < 64:
            return None
        length = int.from_bytes(data[32:64], "big")
        if length == 0 or 64 + length > len(data):
            return None
        return data[64:64 + length].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    out: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "candidates": {}}

    for label, addr in CANDIDATES.items():
        entry: dict = {"address": addr}
        code = rpc_call("eth_getCode", [addr, "latest"])
        entry["has_bytecode"] = code not in (None, "0x", "0x0")
        entry["bytecode_len_hex_chars"] = len(code or "")
        entry["bytecode_first_64_hex_chars"] = (code or "")[:64]
        entry["is_eip7702_delegation"] = (code or "").startswith("0xef0100")
        if entry["is_eip7702_delegation"] and code:
            entry["eip7702_delegate_target"] = "0x" + code[8:48]
            entry["matches_known_fomo_delegate"] = entry["eip7702_delegate_target"].lower() == DELEGATE_KNOWN.lower()

        view_results = {}
        if entry["has_bytecode"] and not entry["is_eip7702_delegation"]:
            for sel_label, selector in CANDIDATE_SELECTORS.items():
                try:
                    body_result = rpc_call("eth_call", [{"to": addr, "data": selector}, "latest"])
                except RuntimeError as exc:
                    view_results[sel_label] = {"reverted_or_error": True, "detail": str(exc)[:150]}
                    continue
                if not body_result or body_result == "0x":
                    view_results[sel_label] = {"reverted_or_error": True}
                    continue
                r: dict = {"reverted_or_error": False, "raw": body_result}
                if sel_label in ("name()", "symbol()"):
                    r["decoded_string"] = decode_string_return(body_result)
                if sel_label in ("owner()", "entryPoint()", "implementation()", "factory()", "WETH9()", "token0()", "token1()"):
                    r["decoded_address"] = decode_address_return(body_result)
                view_results[sel_label] = r
        entry["view_function_probe"] = view_results
        n_answered = sum(1 for v in view_results.values() if not v.get("reverted_or_error"))
        entry["n_view_functions_answered"] = n_answered
        print(f"[ogle_counterparty] {label} ({addr}): has_code={entry['has_bytecode']}, "
              f"eip7702={entry['is_eip7702_delegation']}, n_view_ok={n_answered}")
        out["candidates"][label] = entry

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
