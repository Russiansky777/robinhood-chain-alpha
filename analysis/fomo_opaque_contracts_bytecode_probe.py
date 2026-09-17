#!/usr/bin/env python3
"""Владелец (2026-09-17): разобрать 2 непрозрачных контракта-контрагента
из пробы ogle -- бесплатно (0 кредитов Dune), только eth_getCode +
широкий пробинг селекторов известных DEX/bonding-curve/мем-терминал
паттернов + извлечение печатаемых ASCII-строк из байткода (та же
техника, что unix `strings`).

Адреса:
  - 0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f -- многократный получатель
    КРУПНЫХ переводов от ogle (UP, "Ponsan"), 0 из 9 проб прошлого раунда
    ответили вообще -- главный кандидат.
  - 0x4cd00e387622c35bddb9b4c962c136462338bc31 -- многократный получатель
    МЕЛКИХ (~$0.6-1.4) переводов USDG, owner() ответил, остальное нет --
    добавлен для контекста (не главный кандидат по заданию, но дёшево
    проверить заодно)."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _rpc_call as rpc_call, topic0  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_opaque_contracts_bytecode_probe_result.json")

TARGETS = {
    "repeated_large_token_recipient": "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f",
    "repeated_small_usdg_recipient": "0x4cd00e387622c35bddb9b4c962c136462338bc31",
}

# Уже пробовано прошлым раундом (fomo_ogle_counterparty_probe.py):
# owner/entryPoint/implementation/name/symbol/factory/WETH9/token0/token1.
# Здесь -- ШИРЕ: общие DEX-роутер/curve/launchpad паттерны (гипотезы, не
# подтверждённые сигнатуры -- честно отмечаем, что отсутствие ответа НЕ
# доказывает отсутствие функции, могла быть другая сигнатура).


def selector(sig: str) -> str:
    return topic0(sig)[:10]


CANDIDATE_SELECTORS = {
    "getReserves()": selector("getReserves()"),
    "reserve0()": selector("reserve0()"),
    "reserve1()": selector("reserve1()"),
    "quoteToken()": selector("quoteToken()"),
    "baseToken()": selector("baseToken()"),
    "router()": selector("router()"),
    "pool()": selector("pool()"),
    "curve()": selector("curve()"),
    "bondingCurve()": selector("bondingCurve()"),
    "graduated()": selector("graduated()"),
    "totalSupply()": selector("totalSupply()"),
    "decimals()": selector("decimals()"),
    "admin()": selector("admin()"),
    "beacon()": selector("beacon()"),
    "getAmountOut(uint256,bool)": selector("getAmountOut(uint256,bool)"),
    "buy(uint256)": selector("buy(uint256)"),
    "sell(uint256)": selector("sell(uint256)"),
    "paymaster()": selector("paymaster()"),
    "vault()": selector("vault()"),
    "treasury()": selector("treasury()"),
    "feeRecipient()": selector("feeRecipient()"),
}

MIN_STRING_LEN = 4
_PRINTABLE_RE = re.compile(rb"[\x20-\x7e]{%d,}" % MIN_STRING_LEN)


def extract_ascii_strings(raw_bytes: bytes, limit: int = 100) -> list[str]:
    found = [m.decode("ascii") for m in _PRINTABLE_RE.findall(raw_bytes)]
    # Убираем чистый hex-мусор (частые ложные совпадения на произвольных
    # байтах опкодов) -- оставляем строки, где есть хотя бы 2 буквы подряд
    # ИЛИ похоже на путь/URL/строку ошибки.
    filtered = [s for s in found if re.search(r"[A-Za-z]{2,}", s)]
    # Дедуп с сохранением порядка.
    seen = set()
    out = []
    for s in filtered:
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def decode_address_return(raw_hex: str) -> str | None:
    if not raw_hex or raw_hex in ("0x", "0x0") or len(raw_hex) < 66:
        return None
    tail = raw_hex[-40:]
    if int(tail, 16) == 0:
        return None
    return "0x" + tail


def main() -> int:
    out: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "targets": {}}

    for label, addr in TARGETS.items():
        entry: dict = {"address": addr}
        code_hex = rpc_call("eth_getCode", [addr, "latest"])
        code_hex = code_hex or "0x"
        entry["bytecode_len_bytes"] = (len(code_hex) - 2) // 2
        entry["bytecode_first_64_hex_chars"] = code_hex[:64]
        entry["bytecode_last_64_hex_chars"] = code_hex[-64:] if len(code_hex) > 64 else code_hex
        try:
            raw_bytes = bytes.fromhex(code_hex[2:])
            strings_found = extract_ascii_strings(raw_bytes)
        except ValueError:
            strings_found = []
        entry["ascii_strings_found"] = strings_found
        entry["n_ascii_strings"] = len(strings_found)
        print(f"[opaque_probe] {label} ({addr}): {entry['bytecode_len_bytes']} байт, "
              f"{len(strings_found)} ASCII-строк >= {MIN_STRING_LEN} симв.")
        if strings_found:
            print(f"[opaque_probe]   строки: {strings_found[:20]}")

        view_results = {}
        for sel_label, sel in CANDIDATE_SELECTORS.items():
            try:
                raw = rpc_call("eth_call", [{"to": addr, "data": sel}, "latest"])
            except RuntimeError as exc:
                view_results[sel_label] = {"reverted_or_error": True, "detail": str(exc)[:150]}
                continue
            if not raw or raw == "0x":
                view_results[sel_label] = {"reverted_or_error": True}
                continue
            r: dict = {"reverted_or_error": False, "raw": raw}
            if sel_label in ("router()", "pool()", "curve()", "bondingCurve()", "admin()", "beacon()",
                              "paymaster()", "vault()", "treasury()", "feeRecipient()", "quoteToken()", "baseToken()"):
                r["decoded_address"] = decode_address_return(raw)
            view_results[sel_label] = r
        n_answered = sum(1 for v in view_results.values() if not v.get("reverted_or_error"))
        entry["view_function_probe"] = view_results
        entry["n_view_functions_answered"] = n_answered
        print(f"[opaque_probe]   {n_answered}/{len(CANDIDATE_SELECTORS)} широких проб ответили")
        out["targets"][label] = entry

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
