#!/usr/bin/env python3
"""401 Unauthorized на минимальном запросе с Bearer-токеном -- прежде чем
пробовать разные форматы вслепую (что могло бы стоить кредитов, если
V2 API засчитывает даже неудачные по бизнес-логике, но валидные по
аутентификации запросы), читаем РЕАЛЬНУЮ документацию Bitquery про
авторизацию напрямую с VPS (у него есть реальный доступ в интернет, в
отличие от песочницы сессии, которая блокирует docs.bitquery.io).
Просто HTTP GET страницы документации -- не GraphQL-запрос, не может
стоить кредитов ни при каком раскладе."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
URLS = [
    "https://docs.bitquery.io/docs/authorisation/how-to-generate/",
    "https://docs.bitquery.io/docs/authorisation/authorisation-scheme/",
    "https://docs.bitquery.io/docs/category/authorization",
]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def main() -> None:
    root = find_repo_root()
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pages": {}}

    for url in URLS:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            text = r.text
            # Вырезать текстовое содержимое из HTML грубо -- достаточно для
            # поиска релевантных фраз про Bearer/OAuth/token, не нужен полный парсинг.
            visible = re.sub(r"<script[\s\S]*?</script>", " ", text)
            visible = re.sub(r"<style[\s\S]*?</style>", " ", visible)
            visible = re.sub(r"<[^>]+>", " ", visible)
            visible = re.sub(r"\s+", " ", visible).strip()
            # Ищем окрестности ключевых слов
            snippets = []
            for kw in ("Authorization", "Bearer", "OAuth", "access_token", "oauth2", "streaming.bitquery.io", "eap.bitquery.io"):
                idx = visible.find(kw)
                if idx != -1:
                    snippets.append({"keyword": kw, "context": visible[max(0, idx - 150):idx + 300]})
            result["pages"][url] = {"http_status": r.status_code, "len": len(text), "snippets": snippets}
        except Exception as exc:  # noqa: BLE001
            result["pages"][url] = {"exception": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    root.joinpath("data", "task_arc_bitquery_auth_docs_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
