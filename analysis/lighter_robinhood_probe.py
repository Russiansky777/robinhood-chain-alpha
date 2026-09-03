#!/usr/bin/env python3
"""Диагностика: владелец дал `https://robinhoodchain.lighter.xyz/` как
инстанс Lighter для Robinhood Chain -- но `/api/v1/orderBookDetails`
(тот же путь, что реально работает на `mainnet.zklighter.elliot.ai`,
см. analysis/p4_lighter_markets.py/mm_p5_setup.py) вернул НЕ-JSON
("Expecting value: line 1 column 1", т.е. пустое тело или HTML/редирект,
не JSON) -- см. data/p3_guard_cache/p5_live_step0_result.json,
eth_perp_market=null. Не гадаем про правильный путь/хост -- смотрим,
что реально отдаёт сервер (статус, content-type, тело) на нескольких
вероятных вариантах, и докладываем фактами.

Только чтение (HTTP GET), ключ не используется.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_robinhood_probe_result.json")

CANDIDATES = [
    ("root", "https://robinhoodchain.lighter.xyz/", {}),
    ("orderBookDetails (тот же путь, что рабочий на mainnet.zklighter.elliot.ai)",
     "https://robinhoodchain.lighter.xyz/api/v1/orderBookDetails", {"filter": "all"}),
    ("orderBookDetails без параметров", "https://robinhoodchain.lighter.xyz/api/v1/orderBookDetails", {}),
    ("api/v1 (корень секции)", "https://robinhoodchain.lighter.xyz/api/v1", {}),
    ("api root", "https://robinhoodchain.lighter.xyz/api", {}),
    ("swagger/openapi (частый паттерн у REST-API)", "https://robinhoodchain.lighter.xyz/docs", {}),
    ("mainnet.zklighter.elliot.ai (известно рабочий, для сравнения)",
     "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails", {"filter": "all"}),
]


def run() -> int:
    t0 = time.time()
    results = []
    for label, url, params in CANDIDATES:
        entry = {"label": label, "url": url, "params": params}
        try:
            r = requests.get(url, params=params, timeout=15, allow_redirects=True)
            entry["status_code"] = r.status_code
            entry["final_url"] = r.url
            entry["content_type"] = r.headers.get("content-type")
            entry["body_len"] = len(r.content)
            entry["body_snippet"] = r.text[:400]
            try:
                body = r.json()
                entry["is_json"] = True
                entry["json_top_level_keys"] = list(body.keys()) if isinstance(body, dict) else f"list[{len(body)}]"
            except Exception:  # noqa: BLE001
                entry["is_json"] = False
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
        results.append(entry)
        print(f"[lighter_robinhood_probe] {label}: {entry.get('status_code', entry.get('error'))} "
              f"content-type={entry.get('content_type')} is_json={entry.get('is_json')}")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results, "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[lighter_robinhood_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
