"""Одна попытка (не задача, владелец, 2026-09-01): дёрнуть публичный
REST Blockscout v2 БЕЗ ключа с GH Actions runner'а (egress
интерактивной сессии блокирует этот домен -- см. docs/P3_GUARD.md) --

  GET https://robinhoodchain.blockscout.com/api/v2/addresses/{addr}/token-transfers
  GET https://robinhoodchain.blockscout.com/api/v2/addresses/{addr}/transactions

Если ОБА без 403 -- это бесплатный путь ко всему, что раньше упиралось
в ключ (SC1-срез analysis/sc1_wash_slice.py, P3-гард analysis/
p3_dislocation_guard.py) -- доложить и переключить скрипты на него.
Если 403 -- доложить, дальше НЕ пробовать (ни повтор, ни другие
эндпоинты v2 в этом проходе).

Ранее УЖЕ подтверждено 403 на другом эндпоинте этого домена
(`/api/eth-rpc`, JSON-RPC proxy, docs/P3_GUARD.md) -- это отдельная,
явно другая проверка: сам официальный `robinhood-api.mdx` говорит про
"PRO API гейтвей" именно для JSON-RPC, но НЕ обязательно означает то
же самое для REST v2 `/api/v2/addresses/...` -- владелец прямо просит
проверить именно этот путь отдельно, не выводить по аналогии.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://robinhoodchain.blockscout.com"
ADDRESS = "0x8F62A08537cede87D511AcA6436274Ab4Ca080a3"
ENDPOINTS = ["token-transfers", "transactions"]
HEADERS = {"User-Agent": "robinhood-chain-alpha-blockscout-probe/1.0"}
OUT_PATH = Path("data/p3_guard_cache/blockscout_addr_probe_result.json")


def run() -> int:
    results = {}
    any_403 = False
    for ep in ENDPOINTS:
        url = f"{BASE_URL}/api/v2/addresses/{ADDRESS}/{ep}"
        print(f"[blockscout_probe] GET {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            body_snippet = resp.text[:500]
            print(f"[blockscout_probe] status={resp.status_code} body[:500]={body_snippet!r}")
            results[ep] = {
                "url": url,
                "status_code": resp.status_code,
                "body_snippet": body_snippet,
                "n_items_if_json": None,
            }
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    items = data.get("items")
                    results[ep]["n_items_if_json"] = len(items) if isinstance(items, list) else None
                except ValueError:
                    pass
            if resp.status_code == 403:
                any_403 = True
        except requests.exceptions.RequestException as e:
            print(f"[blockscout_probe] ОШИБКА СЕТИ на {url}: {e}")
            results[ep] = {"url": url, "error": str(e)}

    verdict = (
        "403 хотя бы на одном эндпоинте -- ключ по-прежнему нужен, дальше НЕ пробовать"
        if any_403 else
        "ни одного 403 -- см. статус-коды по отдельности, возможен бесплатный путь"
    )
    print(f"[blockscout_probe] ВЕРДИКТ: {verdict}")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "address": ADDRESS,
        "results": results,
        "verdict": verdict,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, default=str))
    print(f"[blockscout_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
