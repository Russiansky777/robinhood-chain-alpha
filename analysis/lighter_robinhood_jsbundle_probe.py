#!/usr/bin/env python3
"""Владелец, 2026-09-03: "На счету всё есть. Возможно, потому что ты не
там смотришь? Это не сеть Lighter, а https://robinhoodchain.lighter.xyz/
и сеть соответственно robinhood." -- серьёзное расхождение с прошлой
проверкой (account 22012 на mainnet.zklighter.elliot.ai показал
collateral~=0). Разбираемся не гаданием, а по факту: сайт
robinhoodchain.lighter.xyz -- веб-фронтенд (SPA), а не API (см.
lighter_robinhood_probe.py, 2026-09-03 -- любой путь отдаёт одну и ту
же HTML). Значит РЕАЛЬНЫЙ backend, с которым разговаривает этот
фронтенд, объявлен где-то в его JS-бандле -- вытаскиваем оттуда, не
угадываем поддомены.

Только чтение (HTTP GET), ключ не используется.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_robinhood_jsbundle_result.json")
BASE = "https://robinhoodchain.lighter.xyz"

# Паттерны для поиска реального API-хоста внутри JS -- реальные URL-подобные
# строки и типичные имена переменных окружения фронтендов (Vite/Next/CRA).
URL_RE = re.compile(r"https?://[a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z]{2,}(?:/[a-zA-Z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*)?")
ENV_HINT_RE = re.compile(r"(VITE_[A-Z_]*API[A-Z_]*|NEXT_PUBLIC_[A-Z_]*API[A-Z_]*|REACT_APP_[A-Z_]*API[A-Z_]*)")


def run() -> int:
    t0 = time.time()
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "base": BASE}

    r = requests.get(BASE + "/", timeout=20)
    html = r.text
    result["root_status"] = r.status_code
    result["root_html_len"] = len(html)

    script_srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
    link_srcs = re.findall(r'<link[^>]+href=["\']([^"\']+\.js)["\']', html)
    all_assets = sorted(set(script_srcs + link_srcs))
    result["asset_paths_found_in_html"] = all_assets
    print(f"[jsbundle_probe] root HTML: {len(html)} байт, найдено {len(all_assets)} JS-ассетов: {all_assets}")

    urls_found: dict[str, list[str]] = {}
    env_hints_found: dict[str, list[str]] = {}
    fetched = []
    for path in all_assets[:15]:  # разумный потолок
        full_url = urljoin(BASE + "/", path)
        try:
            rr = requests.get(full_url, timeout=20)
            content = rr.text
            fetched.append({"url": full_url, "status": rr.status_code, "len": len(content)})
            urls = sorted(set(m for m in URL_RE.findall(content) if "lighter" in m.lower() or "api" in m.lower()
                               or "zklighter" in m.lower() or "robinhood" in m.lower()))
            if urls:
                urls_found[full_url] = urls[:40]
            hints = sorted(set(ENV_HINT_RE.findall(content)))
            if hints:
                env_hints_found[full_url] = hints
            print(f"[jsbundle_probe] {full_url}: status={rr.status_code} len={len(content)} "
                  f"URL-подобных совпадений(lighter/api/robinhood)={len(urls)} env-подсказок={len(hints)}")
        except Exception as e:  # noqa: BLE001
            fetched.append({"url": full_url, "error": str(e)})
            print(f"[jsbundle_probe] {full_url}: ошибка {e}")

    result["fetched_assets"] = fetched
    result["urls_found_per_asset"] = urls_found
    result["env_hints_found_per_asset"] = env_hints_found

    # Плоский список всех уникальных кандидатов-хостов для быстрого просмотра
    all_candidate_urls = sorted(set(u for lst in urls_found.values() for u in lst))
    result["all_candidate_api_urls"] = all_candidate_urls
    print(f"\n[jsbundle_probe] ВСЕ кандидаты API-URL, встреченные в JS: {all_candidate_urls}")

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[jsbundle_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
