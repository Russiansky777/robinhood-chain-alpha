#!/usr/bin/env python3
"""«Расхождение площадок» → проверка 4 владельца (2026-09-06, дословно):
"Кто такой xyz. Деплойер HIP-3, источник оракула, комиссии на его
рынках (тейкер/мейкер), правила делистинга и что с открытыми
позициями. Совпадает ли источник цены с оракулом Lighter. Всё -- из
документации HL и параметров рынка через API, не по памяти."

Часть 1 (реальные API-параметры рынка, уже частично есть, но
собираем в одном месте + добираем недостающее): `deployer`,
`oracleUpdater`, `feeRecipient`, `subDeployers` (кто может делать
`setOracle`/`setDeployerFees`/`haltTrading`) -- уже реально в
`hyperliquid_hip3_stock_perp_discovery_result.json::perpDexs`, здесь
переиспользуем, не переисполняем запрос. НОВОЕ: `{"type":
"perpDexLimits"}` и/или `{"type": "meta", "dex": "xyz"}` С ПОЛНЫМ
телом (не только universe) -- ищем поля margin/fee, если реально
есть.

Часть 2 (реальные доки HL, JS-рендерится -- тот же метод Playwright
на GH Actions раннере, что `hyperliquid_jurisdiction_probe.py`):
страницы про HIP-3 (деплойер/оракул/делистинг) и про комиссии
(тейкер/мейкер, доля деплойера)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0", "Content-Type": "application/json"}
OUT_PATH = Path("data/p3_guard_cache/sleeping_refs_xyz_deployer_probe_result.json")

DOC_TARGETS = [
    "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
    "https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals",
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/perpetuals",
]
API_INFO_TYPES = ["perpDexLimits", "perpDeployAuctionStatus"]


def retry_post(body: dict) -> requests.Response:
    for i in range(4):
        try:
            r = requests.post(f"{HYPERLIQUID_API_BASE}/info", headers=HEADERS, data=json.dumps(body), timeout=20)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(2 ** i)
    return r  # noqa: F821 -- последний реальный ответ, даже если не 200


def run() -> int:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "api": {}, "docs": {}}

    print("=== Реальные API-типы (проверяем, есть ли доп. поля про комиссии/лимиты) ===")
    for info_type in API_INFO_TYPES:
        r = retry_post({"type": info_type})
        print(f"{info_type}: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            body = r.text[:1000]
        print(json.dumps(body, ensure_ascii=False)[:2000])
        out["api"][info_type] = {"status_code": r.status_code, "body": body}

    # Полное тело meta(dex=xyz) -- ищем margin/fee поля за пределами universe
    r = retry_post({"type": "meta", "dex": "xyz"})
    meta_body = r.json() if r.status_code == 200 else None
    meta_keys = list(meta_body.keys()) if isinstance(meta_body, dict) else None
    print(f"\nmeta(dex=xyz) реальные ключи верхнего уровня: {meta_keys}")
    out["api"]["meta_xyz_top_level_keys"] = meta_keys
    if isinstance(meta_body, dict):
        non_universe = {k: v for k, v in meta_body.items() if k != "universe"}
        print(json.dumps(non_universe, ensure_ascii=False)[:3000])
        out["api"]["meta_xyz_non_universe_fields"] = non_universe

    print("\n=== Реальные доки HL (Playwright-рендер, JS SPA) ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"))
        for url in DOC_TARGETS:
            entry: dict = {}
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(2000)
                text = page.evaluate("document.body ? document.body.innerText : ''")
                entry["status_code"] = resp.status if resp else None
                entry["final_url"] = page.url
                entry["text_len"] = len(text)
                entry["text"] = text
                print(f"\n--- {url} -- HTTP {entry['status_code']}, {entry['text_len']} симв. ---")
                print(text[:3000])
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
                print(f"\n--- {url} -- ОШИБКА: {e} ---")
            out["docs"][url] = entry
        browser.close()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[xyz_deployer] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
