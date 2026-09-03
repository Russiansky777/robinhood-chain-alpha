#!/usr/bin/env python3
"""Владелец, 2026-09-03: "Найдена вероятная причина частичных филлов --
Volume Quota Lighter (apidocs.lighter.xyz/docs/volume-quota-program)."

Две части:
(a) Читает страницу docs про volume quota program (интерактивная
    сессия не имеет доступа к apidocs.lighter.xyz -- egress-блок,
    только через GH Actions runner, как и lighter.xyz/terms раньше).
(b) Реальный GET /api/v1/account для аккаунта 22012 -- ищет ЛЮБОЕ
    поле с "quota" в имени в сыром ответе (на случай, что текущий
    остаток квоты отдаётся оттуда, а не только в ответе SendTx, где
    он ВСЕГДА приходил volume_quota_remaining=None во всех трёх
    реальных попытках -- см. data/p3_guard_cache/p5_live_step1_result.json
    и p5_live_flatten_lighter_result.json).

Только чтение, ключи Lighter не используются для (b) -- тот же
публичный account-эндпоинт, что весь день.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_volume_quota_probe_result.json")
DOCS_HEADERS = {"User-Agent": "Mozilla/5.0 (robinhood-chain-alpha-p5-research/1.0)"}
DOCS_TARGETS = [
    "https://apidocs.lighter.xyz/docs/volume-quota-program",
    "https://apidocs.rh.lighter.xyz/docs/volume-quota-program",
    "https://apidocs.lighter.xyz/reference/changeaccounttier",
    "https://apidocs.rh.lighter.xyz/reference/changeaccounttier",
]
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
ACCOUNT_INDEX = 22012


def strip_html(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "docs": {}, "account": {}}

    for url in DOCS_TARGETS:
        entry: dict = {}
        try:
            r = requests.get(url, headers=DOCS_HEADERS, timeout=20, allow_redirects=True)
            entry["status_code"] = r.status_code
            entry["final_url"] = r.url
            text = strip_html(r.text)
            entry["text_len"] = len(text)
            entry["full_text"] = text  # небольшая страница -- забираем целиком, не только контекст ключевых слов
            print(f"[quota_probe] {url}: status={r.status_code} len={len(text)}")
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
            print(f"[quota_probe] {url}: ошибка {e}")
        result["docs"][url] = entry

    print("\n=== Реальный account 22012 -- поиск полей с 'quota' в сыром ответе ===")
    try:
        r = requests.get(f"{LIGHTER_API_BASE}/api/v1/account", params={"by": "index", "value": str(ACCOUNT_INDEX)}, timeout=20)
        body = r.json()
        raw_str = json.dumps(body)
        quota_fields_found = "quota" in raw_str.lower()
        result["account"] = {
            "status_code": r.status_code,
            "quota_keyword_found_in_raw_response": quota_fields_found,
            "raw_response": body,
        }
        print(f"[quota_probe] account: status={r.status_code} 'quota' в сыром JSON: {quota_fields_found}")
    except Exception as e:  # noqa: BLE001
        result["account"] = {"error": str(e)}
        print(f"[quota_probe] account: ошибка {e}")

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[quota_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
