#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo часть 2, "попутно и бесплатно": все 9
адресов лидеров (6 fomo.family + 3 из передачи контекста) делегируют
через EIP-7702 на ОДИН И ТОТ ЖЕ контракт 0xe6cae83bde06e4c305530e199d
7217f42808555b (найдено этой же сессией, fomo_forensics_new_leaders_
crosscheck_result.json). Что это -- кошелёк платформы, снайпер-бот или
что-то третье? Определяем по реальному байткоду и реальным ответам
view-функций на Robinhood Chain, ничего не додумываем сверху.

Метод (0 кредитов Dune, только keyless публичный RPC Robinhood Chain,
тот же путь, что fomo_forensics_wallets_account_abstraction_check.py):
  1. eth_getCode -- реальная длина и первые байты байткода.
  2. Сравнение адреса с каноническими ERC-4337 EntryPoint (одинаковый
     адрес на всех сетях через CREATE2, если это официальный EntryPoint)
     -- v0.6 и v0.7, единственные, в которых уверены без сверки с
     реальным источником в этой сессии.
  3. Реальные eth_call на пригоршню общеупотребимых view-функций
     (owner/entryPoint/implementation/name/symbol) -- какие отвечают
     успешно, что возвращают."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _rpc_call  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_delegate_contract_probe_result.json")

DELEGATE = "0xe6cae83bde06e4c305530e199d7217f42808555b"

# Канонические ERC-4337 EntryPoint -- одинаковый адрес на каждой сети
# через CREATE2 деплой, если это официальный EntryPoint. Только версии,
# в которых уверены без сверки с внешним источником в этой сессии.
KNOWN_ENTRYPOINTS = {
    "0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789".lower(): "ERC-4337 EntryPoint v0.6 (канонический CREATE2-адрес)",
    "0x0000000071727De22E5E9d8BAf0edAc6f37da032".lower(): "ERC-4337 EntryPoint v0.7 (канонический CREATE2-адрес)",
}

CANDIDATE_SELECTORS = {
    "owner()": "0x8da5cb5b",
    "entryPoint()": "0xb0d691fe",
    "implementation()": "0x5c60da1b",
    "name()": "0x06fdde03",
    "symbol()": "0x95d89b41",
    "VERSION()": "0xffa1ad74",
    "version()": "0x54fd4d50",
}


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


def decode_address_return(raw_hex: str) -> str | None:
    if not raw_hex or raw_hex in ("0x", "0x0") or len(raw_hex) < 66:
        return None
    tail = raw_hex[-40:]
    if int(tail, 16) == 0:
        return None
    return "0x" + tail


def main() -> None:
    out: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "delegate_address": DELEGATE}

    known_match = KNOWN_ENTRYPOINTS.get(DELEGATE.lower())
    out["matches_known_erc4337_entrypoint"] = known_match

    code = _rpc_call("eth_getCode", [DELEGATE, "latest"])
    out["has_bytecode"] = code not in (None, "0x", "0x0")
    out["bytecode_len_hex_chars"] = len(code or "")
    out["bytecode_first_64_hex_chars"] = (code or "")[:64]
    out["bytecode_is_eip1167_prefix"] = (code or "").startswith("0x363d3d373d3d3d363d73")

    view_results = {}
    for label, selector in CANDIDATE_SELECTORS.items():
        body_result = None
        try:
            body_result = _rpc_call("eth_call", [{"to": DELEGATE, "data": selector}, "latest"])
        except RuntimeError as exc:
            view_results[label] = {"reverted_or_error": True, "detail": str(exc)[:200]}
            continue
        if not body_result or body_result == "0x":
            view_results[label] = {"reverted_or_error": True, "raw": body_result}
            continue
        entry: dict = {"reverted_or_error": False, "raw": body_result}
        if label in ("name()", "symbol()", "VERSION()", "version()"):
            entry["decoded_string"] = decode_string_return(body_result)
        if label in ("owner()", "entryPoint()", "implementation()"):
            entry["decoded_address"] = decode_address_return(body_result)
        view_results[label] = entry
        print(f"[delegate_probe] {label}: {entry}")

    out["view_function_probe"] = view_results

    honest_notes = []
    if known_match:
        honest_notes.append(f"Адрес БУКВАЛЬНО совпадает с {known_match} -- это официальный ERC-4337 EntryPoint.")
    else:
        honest_notes.append(
            "Адрес НЕ совпадает ни с одним из канонических ERC-4337 EntryPoint v0.6/v0.7 -- "
            "не официальный EntryPoint (либо неофициальный форк/другая версия, не проверено)."
        )
    if out["bytecode_is_eip1167_prefix"]:
        honest_notes.append("Байткод сам начинается с сигнатуры EIP-1167 minimal proxy -- это ещё один уровень прокси, не конечная логика.")
    n_answered = sum(1 for v in view_results.values() if not v.get("reverted_or_error"))
    honest_notes.append(f"{n_answered} из {len(CANDIDATE_SELECTORS)} общеупотребимых view-функций ответили успешно (не revert).")
    out["honest_notes"] = honest_notes

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
