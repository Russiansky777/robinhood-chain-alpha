#!/usr/bin/env python3
"""Владелец, 2026-09-03: "Почему тогда я руками спокойно могу сделать
нужный ордер?" -- если бы дело было в жёстком лимите "один мейкер на
тейкер-транзакцию", ручной ордер через фронтенд должен был бы страдать
тем же самым. Аккаунт и рынок ETH показывают `can_rfq: true` /
`rfq_enabled: true` -- гипотеза: "market order" на фронтенде реально
уходит через RFQ (запрос котировки у мейкера/вендора, атомарное
исполнение на весь объём), а не через сырой CLOB market order (наш
путь, который реально матчится максимум с одним резидентным ордером
за раз, см. data/p3_guard_cache/lighter_order_history_probe_result.json).

Здесь: (a) markdown-варианты реальных доков (.md -- не JS-рендер,
в отличие от HTML) для accountOrders/trades/rfq_get/rfq_list, чтобы
узнать РЕАЛЬНЫЕ параметры; (b) llms.txt -- полный индекс API; (c) с
узнанными параметрами -- реальный запрос истории сделок аккаунта
22012, чтобы найти РУЧНУЮ сделку владельца (даже если позиция уже
закрыта -- в истории должна остаться) и посмотреть её реальный тип
транзакции (совпадает ли с нашим type=14 L2CreateOrder или это что-то
другое, напр. RFQ-related).

Только чтение, ордеров нет.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_rfq_and_docs_probe_result.json")
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
ACCOUNT_INDEX = 22012
DOCS_HEADERS = {"User-Agent": "Mozilla/5.0 (robinhood-chain-alpha-p5-research/1.0)"}

MD_TARGETS = [
    "https://apidocs.rh.lighter.xyz/llms.txt",
    "https://apidocs.rh.lighter.xyz/reference/accountorders.md",
    "https://apidocs.rh.lighter.xyz/reference/trades.md",
    "https://apidocs.rh.lighter.xyz/reference/recenttrades.md",
    "https://apidocs.rh.lighter.xyz/reference/rfq_list.md",
    "https://apidocs.rh.lighter.xyz/reference/rfq_get.md",
    "https://apidocs.rh.lighter.xyz/reference/rfq_create.md",
]


def get_text(url: str) -> dict:
    try:
        r = requests.get(url, headers=DOCS_HEADERS, timeout=20, allow_redirects=True)
        return {"status_code": r.status_code, "text": r.text}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def get_json(path: str, params: dict) -> dict:
    try:
        r = requests.get(f"{LIGHTER_API_BASE}{path}", params=params, timeout=20)
        entry = {"url": r.url, "status_code": r.status_code}
        try:
            entry["body"] = r.json()
        except Exception:  # noqa: BLE001
            entry["body_text"] = r.text[:1500]
        return entry
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "path": path, "params": params}


def extract_param_names(md_text: str) -> list[str]:
    # Грубая эвристика -- искать имена вида `word_word` рядом со словами
    # "query"/"param"/"required" в markdown-таблице параметров.
    return sorted(set(re.findall(r"`([a-z_][a-z0-9_]{2,30})`", md_text)))


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "docs_md": {}, "api": {}}

    print("=== Markdown-варианты доков (не JS-рендер) ===")
    for url in MD_TARGETS:
        entry = get_text(url)
        if "text" in entry:
            entry["len"] = len(entry["text"])
            entry["candidate_param_names"] = extract_param_names(entry["text"])[:40]
        result["docs_md"][url] = entry
        print(f"[rfq_probe] {url}: status={entry.get('status_code', entry.get('error'))} "
              f"len={entry.get('len', '?')}")

    print("\n=== Реальные попытки истории сделок с разными наборами параметров ===")
    param_variants = [
        {"account_index": ACCOUNT_INDEX, "limit": 100},
        {"account_index": ACCOUNT_INDEX, "market_id": 0, "limit": 100},
        {"index": ACCOUNT_INDEX, "limit": 100},
        {"l1_address": "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75", "limit": 100},
    ]
    for path in ["/api/v1/trades", "/api/v1/recentTrades", "/api/v1/accountOrders", "/api/v1/accountInactiveOrders"]:
        for params in param_variants:
            entry = get_json(path, params)
            key = f"{path}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
            result["api"][key] = entry
            sc = entry.get("status_code", entry.get("error"))
            print(f"[rfq_probe] {key}: {sc}")
            if entry.get("status_code") == 200:
                # Нашли рабочий набор параметров -- печатаем ответ целиком в лог сразу
                print(f"[rfq_probe]   >>> РАБОЧИЙ ЗАПРОС, тело: {json.dumps(entry.get('body'))[:2000]}")

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[rfq_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
