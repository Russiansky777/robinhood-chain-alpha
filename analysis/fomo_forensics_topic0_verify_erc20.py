#!/usr/bin/env python3
"""Задача «форензика fomo» -- продолжение структурной проверки topic0.

fomo_forensics_topic0_decode_result.json показал: проверка "есть ли
реальные Transfer-логи адреса-кандидата в erc20_robinhood.evt_transfer"
вернула 0 записей для ОБОИХ кандидатов -- не доказательство отсутствия
активности, а, вероятно, ограничение самой Dune-таблицы: `evt_transfer`
-- это ДЕКОДИРОВАННАЯ (по ABI) таблица, Dune декодирует события только
для контрактов, чей ABI явно отправлен/верифицирован; пермишнлес
меметокены с фабрики почти наверняка НЕ верифицированы -- значит эта
проверка структурно не может быть доказательством, независимо от
реального topic0. Не полагаемся на неё дальше.

Реальная, ничего не предполагающая альтернатива -- прямой eth_call
стандартных ERC-20 view-функций (name/symbol/decimals/totalSupply)
на сами адреса-кандидаты. Это работает на ЛЮБОМ контракте, реализующем
интерфейс, независимо от того, знает ли о нём Dune-декодер. Бесплатно
(только RPC, без кредитов Dune).

Логика: если topic1 адреса-кандидата -- это ТОЛЬКО ЧТО созданный
токен-контракт, то у него на chain-height, отражённом в topic0-событии,
УЖЕ должны быть развёрнуты и отвечать стандартные ERC-20 view-функции
(конструктор фабрики обычно сразу проставляет name/symbol/decimals/
totalSupply). Если адрес НЕ отвечает как ERC-20 -- он не токен, и топ0,
из которого он извлечён как "topic1", видимо, не TokenLaunched (или
адрес в этом поле -- не токен, а что-то другое, например деплоер/pool)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-fomo-forensics-recon/1.0"}
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
IN_PATH = Path("data/p3_guard_cache/fomo_forensics_topic0_decode_result.json")
OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_topic0_verify_erc20_result.json")

# Стандартные 4-байтные селекторы (keccak-и общеизвестны для этих ABI-сигнатур,
# используются повсеместно, не "угадывание": name()/symbol()/decimals()/totalSupply()
SELECTORS = {
    "name": "0x06fdde03",
    "symbol": "0x95d89b41",
    "decimals": "0x313ce567",
    "totalSupply": "0x18160ddd",
}

NULL_ADDR = "0x0000000000000000000000000000000000000000"


def rpc_call(method: str, params: list):
    r = requests.post(ROBINHOOD_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                       headers=HEADERS, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        return None, body["error"]
    return body.get("result"), None


def decode_abi_string(hex_data: str) -> str | None:
    """Декодирует ABI-encoded dynamic string (offset + length + data).
    Некоторые токены (нестандартно) возвращают bytes32 вместо string --
    отдельно не пытаемся угадать, просто возвращаем None при неудаче."""
    try:
        raw = bytes.fromhex(hex_data.removeprefix("0x"))
        if len(raw) < 64:
            return None
        length = int.from_bytes(raw[32:64], "big")
        if length == 0 or 64 + length > len(raw):
            return None
        s = raw[64:64 + length]
        return s.decode("utf-8", errors="replace")
    except Exception:
        return None


def decode_uint(hex_data: str) -> int | None:
    try:
        raw = bytes.fromhex(hex_data.removeprefix("0x"))
        if len(raw) < 32:
            return None
        return int.from_bytes(raw[-32:], "big")
    except Exception:
        return None


def check_erc20_view_calls(addr: str) -> dict:
    out = {"address": addr}
    raw_results = {}
    for fn, selector in SELECTORS.items():
        result, err = rpc_call("eth_call", [{"to": addr, "data": selector}, "latest"])
        if err is not None:
            raw_results[fn] = {"error": str(err)}
            continue
        raw_results[fn] = {"raw": result}
        if result in (None, "0x"):
            continue
        if fn in ("name", "symbol"):
            out[fn] = decode_abi_string(result)
        else:
            out[fn] = decode_uint(result)
        time.sleep(0.15)
    out["raw_results"] = raw_results
    out["looks_like_erc20"] = bool(
        out.get("name") and out.get("symbol") and out.get("decimals") is not None and out.get("totalSupply") is not None
    )
    return out


def run() -> int:
    if not IN_PATH.exists():
        print(f"[verify_erc20] нет входного файла {IN_PATH} -- сначала нужен fomo_forensics_topic0_decode.py")
        return 1
    decode_result = json.loads(IN_PATH.read_text())

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "candidates": {}}

    for topic0, analysis in decode_result.get("candidates", {}).items():
        guesses = [a for a in analysis.get("sample_topic1_as_address_guess", []) if a.lower() != NULL_ADDR]
        # уникальные, сохраняя порядок
        seen = set()
        uniq_guesses = []
        for a in guesses:
            if a.lower() not in seen:
                seen.add(a.lower())
                uniq_guesses.append(a)

        print(f"\n[verify_erc20] --- topic0={topic0[:16]}..., проверяю {len(uniq_guesses)} уникальных адресов-кандидатов ---")
        checks = []
        for addr in uniq_guesses:
            r = check_erc20_view_calls(addr)
            print(f"  {addr}: looks_like_erc20={r['looks_like_erc20']} name={r.get('name')!r} symbol={r.get('symbol')!r} "
                  f"decimals={r.get('decimals')} totalSupply={r.get('totalSupply')}")
            checks.append(r)

        n_erc20 = sum(1 for c in checks if c["looks_like_erc20"])
        out["candidates"][topic0] = {
            "n_addresses_tested": len(checks),
            "n_looks_like_erc20": n_erc20,
            "fraction_erc20": (n_erc20 / len(checks)) if checks else None,
            "checks": checks,
        }
        print(f"[verify_erc20] topic0={topic0[:16]}...: {n_erc20}/{len(checks)} адресов-кандидатов ведут себя как реальный ERC-20 токен")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[verify_erc20] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
