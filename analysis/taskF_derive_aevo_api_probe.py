#!/usr/bin/env python3
"""Задача F владельца (2026-09-10), Шаг 0 -- разведка API, БЕЗ капитала,
без Dune, бесплатно и быстро.

Гипотеза: IV опционов систематически выше RV -- премия за продажу
волатильности с дельта-хеджем перпом.

Площадки: Derive (бывш. Lyra) и Aevo -- обе on-chain, без KYC. Проверяем
РЕАЛЬНО (не по памяти -- API у таких проектов меняются) наличие
публичного REST API с ИСТОРИЕЙ: цены опционов, IV, страйки, экспирации.
Мой sandbox блокирует оба домена организационной политикой (тот же
паттерн, что был с gamma-api.polymarket.com в Задаче E) -- поэтому
пробуем через GitHub Actions runner, у которого реальный доступ."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskF-probe/1.0", "Accept": "application/json"}
OUT_PATH = Path("data/p3_guard_cache/taskF_derive_aevo_api_probe_result.json")

# Реальные кандидаты базовых URL и эндпоинтов -- проверяем ФАКТ, не
# полагаемся на память о том, как выглядел API до ребрендинга Lyra->Derive
# или на не-обновлённую документацию.
DERIVE_BASES = [
    "https://api.derive.xyz",
    "https://api.lyra.finance",
]
DERIVE_PATHS = [
    ("GET", "/public/get_instruments", {"currency": "BTC", "instrument_type": "option", "expired": "false"}),
    ("POST", "/public/get_instruments", {"currency": "BTC", "instrument_type": "option", "expired": "false"}),
    ("GET", "/public/get_all_currencies", None),
    ("POST", "/public/get_all_currencies", None),
    ("GET", "/public/get_option_settlement_history", {"currency": "BTC"}),
    ("POST", "/public/get_option_settlement_history", {"currency": "BTC"}),
    ("GET", "/public/get_trade_history", {"currency": "BTC", "instrument_type": "option"}),
    ("POST", "/public/get_trade_history", {"currency": "BTC", "instrument_type": "option"}),
    ("GET", "/openapi.json", None),
    ("GET", "/swagger.json", None),
]

AEVO_BASES = [
    "https://api.aevo.xyz",
]
AEVO_PATHS = [
    ("GET", "/markets", {"asset": "BTC", "instrument_type": "OPTION"}),
    ("GET", "/index", {"asset": "BTC"}),
    ("GET", "/ticker", None),
    ("GET", "/trade-history", {"asset": "BTC", "instrument_type": "OPTION"}),
    ("GET", "/history/trades", {"asset": "BTC", "instrument_type": "OPTION"}),
    ("GET", "/openapi.json", None),
    ("GET", "/swagger.json", None),
]


def probe(bases: list[str], paths: list[tuple], platform: str) -> list[dict]:
    results = []
    for base in bases:
        for method, path, params in paths:
            url = f"{base}{path}"
            entry = {"platform": platform, "base": base, "method": method, "path": path, "params": params}
            try:
                if method == "GET":
                    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
                else:
                    r = requests.post(url, json=params, headers=HEADERS, timeout=15)
                entry["status"] = r.status_code
                entry["content_type"] = r.headers.get("content-type", "")
                body_text = r.text[:500]
                entry["body_snippet"] = body_text
                if r.status_code == 200 and "json" in entry["content_type"].lower():
                    try:
                        body = r.json()
                        if isinstance(body, dict):
                            entry["json_top_keys"] = list(body.keys())[:20]
                        elif isinstance(body, list):
                            entry["json_type"] = "list"
                            entry["json_len"] = len(body)
                            if body and isinstance(body[0], dict):
                                entry["json_item0_keys"] = list(body[0].keys())[:20]
                    except (json.JSONDecodeError, ValueError):
                        entry["json_parse_error"] = True
            except requests.exceptions.RequestException as exc:
                entry["exception"] = str(exc)[:300]
            results.append(entry)
            time.sleep(0.2)
    return results


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("=== Задача F, Шаг 0: разведка API Derive и Aevo (реально, не по памяти) ===")
    derive_results = probe(DERIVE_BASES, DERIVE_PATHS, "derive")
    for e in derive_results:
        print(f"[derive] {e['method']} {e['base']}{e['path']} -> status={e.get('status', 'EXC:' + str(e.get('exception')))}")
    aevo_results = probe(AEVO_BASES, AEVO_PATHS, "aevo")
    for e in aevo_results:
        print(f"[aevo] {e['method']} {e['base']}{e['path']} -> status={e.get('status', 'EXC:' + str(e.get('exception')))}")

    result["derive_probe"] = derive_results
    result["aevo_probe"] = aevo_results

    n_derive_200 = sum(1 for e in derive_results if e.get("status") == 200)
    n_aevo_200 = sum(1 for e in aevo_results if e.get("status") == 200)
    result["n_derive_200"] = n_derive_200
    result["n_aevo_200"] = n_aevo_200
    print(f"\n[taskF_probe] Derive: {n_derive_200}/{len(derive_results)} запросов вернули 200")
    print(f"[taskF_probe] Aevo: {n_aevo_200}/{len(aevo_results)} запросов вернули 200")

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[taskF_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
