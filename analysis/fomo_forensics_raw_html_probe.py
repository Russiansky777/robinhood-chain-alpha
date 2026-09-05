#!/usr/bin/env python3
"""Задача «форензика fomo» -- разведка №2 (только чтение).

Первый прогон (fomo_forensics_recon.py) реально подтвердил bytecode
обоих лаунч-контрактов, но НЕ нашёл ни встроенных query_id на дашбордах
Dune (79-79.5 КБ реального HTML, не JS-shell), ни адресов в сыром HTML
robinscan.io/leaderboard (160.9 КБ реального HTML, тоже не JS-shell,
но 0 совпадений regex 0x[40 hex]). Обе страницы РЕАЛЬНО отдают контент
-- значит либо мои паттерны неверны (разная структура встраивания
данных), либо адреса на robinscan показаны усечённо в UI (частый
паттерн "0x1234...5678"). Сохраняем СЫРЫЕ фрагменты для прямого
осмотра вместо дальнейшего угадывания regex."""
from __future__ import annotations

import json
import re
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-fomo-forensics-recon/1.0"}
OUT_DIR = Path("data/p3_guard_cache")


def save_raw(name: str, url: str) -> dict:
    """НЕ пишем полный HTML отдельным файлом (79-161 КБ на страницу --
    репозиторий не место для сырых веб-дампов ради одноразовой
    диагностики) -- держим только счётчики + фрагменты в самом JSON,
    достаточные для осмотра реальной структуры страницы."""
    entry = {"url": url}
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        entry["status"] = r.status_code
        entry["content_length"] = len(r.text)
        # Реальные, но более широкие паттерны -- ищем ЛЮБОЕ упоминание "quer",
        # camelCase queryId, __NEXT_DATA__/window.__NUXT__/initial state script-теги,
        # усечённые адреса вида 0x1234...5678, и любые числа рядом со словом query.
        entry["mentions_query_lowercase"] = r.text.lower().count("queryid")
        entry["mentions_next_data"] = "__NEXT_DATA__" in r.text
        entry["script_tag_count"] = len(re.findall(r"<script", r.text, re.IGNORECASE))
        entry["truncated_address_matches"] = re.findall(r'0x[a-fA-F0-9]{4,6}\.\.\.[a-fA-F0-9]{2,6}', r.text)[:10]
        entry["full_address_matches"] = re.findall(r'0x[a-fA-F0-9]{40}', r.text)[:10]
        # Первые и последние 3000 символов -- для ручного осмотра структуры
        entry["head_3000"] = r.text[:3000]
        entry["tail_3000"] = r.text[-3000:]
    except Exception as exc:  # noqa: BLE001
        entry["error"] = str(exc)[:300]
    return entry


def run() -> int:
    out = {}
    out["dune_fomo"] = save_raw("dune_fomo", "https://dune.com/adam_tehc/fomo")
    out["dune_trenches"] = save_raw("dune_trenches", "https://dune.com/adam_tehc/the-robinhood-trenches")
    out["robinscan"] = save_raw("robinscan", "https://robinscan.io/leaderboard")

    summary_path = OUT_DIR / "fomo_forensics_raw_html_probe_result.json"
    summary_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    for k, v in out.items():
        print(f"\n=== {k}: status={v.get('status')} len={v.get('content_length')} "
              f"queryid_mentions={v.get('mentions_query_lowercase')} full_addrs={v.get('full_address_matches')} "
              f"truncated_addrs={v.get('truncated_address_matches')} ===")
        print("--- head_3000 ---")
        print(v.get("head_3000", "")[:3000])
    print(f"\n[probe] сводка (включая фрагменты HTML) записана в {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
