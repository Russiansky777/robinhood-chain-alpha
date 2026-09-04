#!/usr/bin/env python3
"""Владелец, 2026-09-04, п.0 логгера спреда фандинга Lighter↔Hyperliquid:
"прочитать ToS Hyperliquid по юрисдикциям и записать в паспорт рядом со
списком Lighter -- для сбора неважно (публичный API без аккаунта), но
если линия пойдёт дальше -- знать надо сейчас".

v1 (plain requests.get, без JS) вернул честный отрицательный результат:
hyperliquid.xyz/terms -- 403 (AccessDenied), gitbook /terms-of-use --
404, остальные 200 но текст -- либо навигация доков, либо кусок JS-
бандла (React/Vite SPA, требует рендеринга). Локальный Chromium этой
интерактивной сессии тоже упёрся в тот же egress-прокси
(ERR_TUNNEL_CONNECTION_FAILED) -- прокси блокирует домен так же, как
WebFetch, обходить его (unset HTTPS_PROXY) НЕ делаем принципиально
(тот же барьер, что и с портом 22 -- см. docs/HANDOFF.md).

v2 (этот файл): рендеринг через Playwright/Chromium НА GH ACTIONS
RUNNER'е (реальный интернет, без прокси-ограничений интерактивной
сессии) -- та же логика, что все остальные *_probe.py в этом репо,
просто с рендерингом JS вместо requests.get.

Только чтение, ключи/аккаунт не используются.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT_PATH = Path("data/p3_guard_cache/hyperliquid_jurisdiction_probe_result.json")
TARGETS = [
    "https://hyperliquid.xyz/terms",
    "https://app.hyperliquid.xyz/terms",
]
KEYWORDS = ["united states", "restricted", "prohibited", "jurisdiction", "ineligible",
            "sanction", "u.s. person", "us person", "ofac", "geo-block", "geoblock",
            "canada", "united kingdom", "china", "north korea", "cuba", "iran",
            "specially designated nationals", "blocked person", "embargo"]


def extract_context(text: str, keyword: str, window: int = 300) -> list[str]:
    hits = []
    lower = text.lower()
    start = 0
    while True:
        idx = lower.find(keyword, start)
        if idx == -1:
            break
        hits.append(text[max(0, idx - window):idx + len(keyword) + window])
        start = idx + len(keyword)
        if len(hits) >= 5:
            break
    return hits


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "targets": {},
                     "method": "playwright chromium render (GH Actions runner, реальный интернет)"}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"))

        for url in TARGETS:
            entry: dict = {}
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(2000)
                text = page.evaluate("document.body ? document.body.innerText : ''")
                entry["status_code"] = resp.status if resp else None
                entry["final_url"] = page.url
                entry["text_len"] = len(text)
                found = {}
                for kw in KEYWORDS:
                    ctxs = extract_context(text, kw)
                    if ctxs:
                        found[kw] = ctxs
                entry["keyword_hits"] = found
                entry["text_preview_first_4000"] = text[:4000]
                print(f"[hyperliquid_jurisdiction_probe] {url}: status={entry['status_code']} "
                      f"len={len(text)} ключевых совпадений: {list(found.keys())}")
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
                print(f"[hyperliquid_jurisdiction_probe] {url}: ошибка {e}")
            result["targets"][url] = entry

        browser.close()

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[hyperliquid_jurisdiction_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
