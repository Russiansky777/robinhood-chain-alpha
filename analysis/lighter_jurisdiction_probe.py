#!/usr/bin/env python3
"""Владелец, 2026-09-03, п.4 (не блокирует закрытие позиции): "выяснить
точный список заблокированных Lighter юрисдикций (документация
api.rh.lighter.xyz или ToS) -- США в частности."

Реальная ошибка хеджа (run 33776547616): `code=20558 "You are accessing
Lighter from a restricted jurisdiction. For more information, see the
https://lighter.xyz/terms"`.

Интерактивная сессия НЕ имеет egress к lighter.xyz/apidocs.rh.lighter.xyz
(домен-allowlist только github.com/raw.githubusercontent.com) -- этот
скрипт запускается на GH Actions runner'е (реальный интернет,
подтверждено весь день), не в интерактивной сессии.

Только чтение (HTTP GET), ключи не используются.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_jurisdiction_probe_result.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (robinhood-chain-alpha-p5-research/1.0)"}
TARGETS = [
    "https://lighter.xyz/terms",
    "https://apidocs.rh.lighter.xyz/docs/get-started",
    "https://apidocs.rh.lighter.xyz/",
]
KEYWORDS = ["united states", "restricted", "prohibited", "jurisdiction", "ineligible",
            "sanction", "u.s. person", "us person", "ofac", "geo-block", "geoblock", "20558"]


def strip_html(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "targets": {}}

    for url in TARGETS:
        entry: dict = {}
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
            entry["status_code"] = r.status_code
            entry["final_url"] = r.url
            text = strip_html(r.text)
            entry["text_len"] = len(text)
            found = {}
            for kw in KEYWORDS:
                ctxs = extract_context(text, kw)
                if ctxs:
                    found[kw] = ctxs
            entry["keyword_hits"] = found
            print(f"[lighter_jurisdiction_probe] {url}: status={r.status_code} len={len(text)} "
                  f"ключевых совпадений: {list(found.keys())}")
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
            print(f"[lighter_jurisdiction_probe] {url}: ошибка {e}")
        result["targets"][url] = entry

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[lighter_jurisdiction_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
