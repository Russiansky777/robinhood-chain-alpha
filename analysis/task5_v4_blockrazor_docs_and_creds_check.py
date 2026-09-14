#!/usr/bin/env python3
"""Подготовка третьего источника (владелец): BlockRazor Sequencer Feed
Ultra для Robinhood Chain -- https://docs.blockrazor.io/streams/
node-stream/robinhood-chain/sequencer-feed-ultra

Этот скрипт НИЧЕГО не покупает и не подключается к самому фиду --
ТОЛЬКО: (1) читает публичную документацию (обычный HTTPS GET, read-only,
egress к этому домену с локальной машины сессии заблокирован прокси,
поэтому запрос идёт с Ohio, у которого обычный интернет-доступ, как и у
любых других внешних вызовов этого проекта весь сеанс); (2) проверяет,
существует ли уже на Ohio какая-либо переменная окружения с именем,
похожим на токен доступа BlockRazor -- ТОЛЬКО имя/факт существования,
НИКОГДА не печатает и не сохраняет значение секрета."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

DOCS_URL = "https://docs.blockrazor.io/streams/node-stream/robinhood-chain/sequencer-feed-ultra"

CANDIDATE_ENV_VAR_NAMES = [
    "BLOCKRAZOR_API_KEY", "BLOCKRAZOR_TOKEN", "BLOCKRAZOR_ACCESS_TOKEN",
    "BLOCKRAZOR_SECRET", "BLOCKRAZOR_AUTH_TOKEN", "BLOCKRAZOR_CLIENT_ID",
    "BLOCKRAZOR_API_TOKEN", "BLOCKRAZOR_KEY",
]


def main() -> None:
    result: dict = {"docs_url": DOCS_URL}

    try:
        resp = requests.get(DOCS_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        result["http_status"] = resp.status_code
        result["content_length_bytes"] = len(resp.content)
        if resp.status_code == 200:
            text = resp.text
            # Сохраняем ПОЛНЫЙ текст на диск (для последующего чтения без
            # повторного запроса), а в stdout -- только компактную выжимку.
            out_path = Path(__file__).parent.parent / "data" / "task5_v4_blockrazor_docs_raw.html"
            out_path.write_text(text)
            result["saved_raw_html"] = str(out_path)
            # Грубый поиск потенциально релевантных фрагментов -- URL
            # wss://, упоминания токена/ключа/заголовка авторизации.
            wss_urls = sorted(set(re.findall(r"wss?://[^\s\"'<>]+", text)))
            result["found_ws_urls"] = wss_urls
            auth_mentions = [line.strip() for line in text.splitlines()
                              if re.search(r"(api[_-]?key|access[_-]?token|authorization|bearer|x-api-key)",
                                           line, re.IGNORECASE)]
            result["auth_related_lines_sample"] = auth_mentions[:30]
            result["mentions_free_tier"] = bool(re.search(r"free tier|free plan|no.?cost|trial", text, re.IGNORECASE))
            result["mentions_blockhash"] = "blockHash" in text
            result["mentions_sequencenumber"] = "sequenceNumber" in text or "sequence_number" in text.lower()
        else:
            result["body_prefix"] = resp.text[:2000]
    except Exception as exc:  # noqa: BLE001
        result["fetch_error"] = f"{type(exc).__name__}: {exc}"

    # ТОЛЬКО существование переменных окружения -- НИКОГДА значение.
    existing = {name: (name in os.environ) for name in CANDIDATE_ENV_VAR_NAMES}
    result["candidate_env_vars_present"] = existing
    result["any_candidate_env_var_present"] = any(existing.values())

    print(json.dumps(result, indent=2, ensure_ascii=False))
    out = Path(__file__).parent.parent / "data" / "task5_v4_blockrazor_docs_and_creds_check_result.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
