#!/usr/bin/env python3
"""Владелец (2026-09-16), пункт Б: QuickNode/Chainstack/GetBlock -- есть
ли бесплатный тариф с поддержкой Arc, лимиты на eth_getLogs. Просто HTTP
GET публичных страниц (не регистрация, не API-вызовы) -- читаем то, что
сервер реально отдал, вырезаем текст вокруг ключевых слов. Если страница
-- SPA без серверного рендеринга, честно фиксируем пустой результат, не
выдумываем цифры лимитов."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
PAGES = {
    "quicknode_arc_chain_page": "https://www.quicknode.com/chains/arc",
    "quicknode_pricing": "https://www.quicknode.com/pricing",
    "chainstack_arc": "https://chainstack.com/build-better-with-arc/",
    "chainstack_pricing": "https://chainstack.com/pricing/",
    "getblock_arc": "https://getblock.io/nodes/arc/",
    "getblock_pricing": "https://getblock.io/pricing/",
}
KEYWORDS = ("Arc", "free", "Free", "eth_getLogs", "rate limit", "requests/s", "req/s", "credit card",
            "block range", "archive", "mainnet", "testnet", "price", "$", "/month", "compute unit", "CU")


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def fetch_and_extract(url: str) -> dict:
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"})
        text = r.text
        visible = re.sub(r"<script[\s\S]*?</script>", " ", text)
        visible = re.sub(r"<style[\s\S]*?</style>", " ", visible)
        visible = re.sub(r"<[^>]+>", " ", visible)
        visible = re.sub(r"\s+", " ", visible).strip()
        snippets = []
        seen_spans = []
        for kw in KEYWORDS:
            for m in re.finditer(re.escape(kw), visible):
                idx = m.start()
                if any(abs(idx - s) < 80 for s in seen_spans):
                    continue
                seen_spans.append(idx)
                snippets.append({"keyword": kw, "context": visible[max(0, idx - 100):idx + 200]})
        return {"http_status": r.status_code, "content_len": len(text), "visible_text_len": len(visible),
                "likely_spa_no_ssr": len(visible) < 500, "n_snippets": len(snippets), "snippets": snippets[:25]}
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    root = find_repo_root()
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pages": {}}
    for name, url in PAGES.items():
        result["pages"][name] = {"url": url, **fetch_and_extract(url)}

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    root.joinpath("data", "task_arc_alt_rpc_research_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
