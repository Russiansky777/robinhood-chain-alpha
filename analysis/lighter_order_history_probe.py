#!/usr/bin/env python3
"""Владелец, 2026-09-03: арифметика (спред 0.012%, floor на 5% ниже
mark) не даёт mark-vs-best_bid объяснить нулевые/частичные филлы --
нужны факты от самой биржи, не рассуждения.

Два независимых, БЕЗ денег, чтения:
(1) История ордеров/сделок аккаунта 22012 для конкретных
    client_order_index трёх реальных неудачных хедж-попыток -- если
    у API есть статус/причина отмены, это и есть ответ.
(2) Плечо/режим маржи, реально настроенные для market_index=0 (ETH)
    на аккаунте 22012 -- сколько ETH покрывает $39.87 при
    ФАКТИЧЕСКОМ плече (не предполагаемом).

Домены (docs + сам api.rh.lighter.xyz) недоступны из интерактивной
сессии -- только через GH Actions runner, как и раньше.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_order_history_probe_result.json")
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
ACCOUNT_INDEX = 22012
DOCS_HEADERS = {"User-Agent": "Mozilla/5.0 (robinhood-chain-alpha-p5-research/1.0)"}

# Три реальных неудачных/частичных хедж-попытки (owner=True, is_ask=True, market_index=0)
ATTEMPTS = [
    {"label": "attempt2_0pct", "client_order_index": 1788454912,
     "tx_hash_hex": "6a2d21bb633bab778d33c4e40bda4c02bee9d435f74919b44df8694f05f469074b7a8526ba2f40aa"},
    {"label": "attempt3_27pct", "client_order_index": 1788456703,
     "tx_hash_hex": "875df81836f4c53e69a715475adda90a8bea6f9d7810dcb359fd4b67ec7b2e1669572a9ed563a4b1"},
    {"label": "attempt4_0pct_5slip", "client_order_index": 1788458953,
     "tx_hash_hex": "487b6c341d5d06ed97cab8c8de9693b81106d1fb107128b503d9eedd89788d37151542c8a77aa12a"},
]

DOCS_TARGETS = [
    "https://apidocs.rh.lighter.xyz/reference/accountorders",
    "https://apidocs.rh.lighter.xyz/reference/accountinactiveorders",
    "https://apidocs.rh.lighter.xyz/reference/accountactiveorders",
    "https://apidocs.rh.lighter.xyz/reference/trades",
    "https://apidocs.rh.lighter.xyz/reference/accountlimits",
    "https://apidocs.rh.lighter.xyz/reference/accountmetadata",
    "https://apidocs.rh.lighter.xyz/reference/tx",
]


def strip_html(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_json(path: str, params: dict) -> dict:
    try:
        r = requests.get(f"{LIGHTER_API_BASE}{path}", params=params, timeout=20)
        entry = {"url": r.url, "status_code": r.status_code}
        try:
            entry["body"] = r.json()
        except Exception:  # noqa: BLE001
            entry["body_text"] = r.text[:2000]
        return entry
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "path": path, "params": params}


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "docs": {}, "api": {}}

    print("=== Документация: реальные параметры/схема эндпоинтов ===")
    for url in DOCS_TARGETS:
        entry: dict = {}
        try:
            r = requests.get(url, headers=DOCS_HEADERS, timeout=20, allow_redirects=True)
            entry["status_code"] = r.status_code
            text = strip_html(r.text)
            entry["full_text"] = text
            print(f"[order_history_probe] {url}: status={r.status_code} len={len(text)}")
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
            print(f"[order_history_probe] {url}: ошибка {e}")
        result["docs"][url] = entry

    print("\n=== Реальные вызовы: история ордеров аккаунта 22012 ===")
    # Пробуем несколько правдоподобных наборов параметров -- реальные
    # имена подтвердятся по факту 200/400 ответа, не гадаем один вариант.
    for path in ["/api/v1/accountOrders", "/api/v1/accountInactiveOrders", "/api/v1/accountActiveOrders"]:
        for params in (
            {"account_index": ACCOUNT_INDEX, "market_id": 0, "limit": 50},
            {"by": "index", "value": str(ACCOUNT_INDEX), "market_id": 0, "limit": 50},
        ):
            entry = get_json(path, params)
            key = f"{path}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
            result["api"][key] = entry
            print(f"[order_history_probe] {key}: status={entry.get('status_code', entry.get('error'))}")

    print("\n=== Реальные вызовы: сделки (trades) аккаунта 22012 ===")
    for params in ({"account_index": ACCOUNT_INDEX, "market_id": 0, "limit": 50},
                    {"by": "index", "value": str(ACCOUNT_INDEX), "market_id": 0, "limit": 50}):
        entry = get_json("/api/v1/trades", params)
        key = f"/api/v1/trades?{'&'.join(f'{k}={v}' for k, v in params.items())}"
        result["api"][key] = entry
        print(f"[order_history_probe] {key}: status={entry.get('status_code', entry.get('error'))}")

    print("\n=== Реальные вызовы: по client_order_index / tx hash каждой попытки ===")
    for att in ATTEMPTS:
        for params in (
            {"account_index": ACCOUNT_INDEX, "client_order_index": att["client_order_index"]},
            {"account_index": ACCOUNT_INDEX, "market_id": 0, "order_index": att["client_order_index"]},
        ):
            entry = get_json("/api/v1/accountOrders", params)
            key = f"{att['label']}::accountOrders::{params}"
            result["api"][key] = entry
        entry_tx = get_json("/api/v1/tx", {"by": "hash", "value": att["tx_hash_hex"]})
        result["api"][f"{att['label']}::tx_by_hash"] = entry_tx
        print(f"[order_history_probe] {att['label']}: accountOrders + tx-by-hash запрошены")

    print("\n=== Реальные вызовы: плечо/режим маржи для account 22012 ===")
    for path in ["/api/v1/accountLimits", "/api/v1/accountMetadata"]:
        for params in ({"account_index": ACCOUNT_INDEX}, {"by": "index", "value": str(ACCOUNT_INDEX)}):
            entry = get_json(path, params)
            key = f"{path}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
            result["api"][key] = entry
            print(f"[order_history_probe] {key}: status={entry.get('status_code', entry.get('error'))}")

    # Свежий account -- ещё раз, полностью, включая positions[].initial_margin_fraction
    # (реальное текущее значение на аккаунте, не рыночный min/default)
    acct = get_json("/api/v1/account", {"by": "index", "value": str(ACCOUNT_INDEX)})
    result["api"]["/api/v1/account (full, fresh)"] = acct

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[order_history_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
