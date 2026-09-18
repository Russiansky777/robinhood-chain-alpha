#!/usr/bin/env python3
"""Владелец, 2026-09-18: разведка Fomo как источника кандидатов лидеров
для сита DBot. ПЕРВЫЙ РЕАЛЬНЫЙ КОНТАКТ с API -- сеть на *.fomoapi.io и
*.fomolens.app из песочницы этой сессии заблокирована прокси (подтверждено
повторно, как ранее с dbotx.com), поэтому до этого прохода известно только
из поисковых сниппетов и README стороннего проекта на GitHub
(abstradeapi/Fomo-Auto-leaderboard-Fetcher) -- ОБА источника вторичные и
местами противоречат друг другу (см. ниже), поэтому ничего не берём на
веру: пробуем эмпирически, дампим сырые ответы.

Известно (с источниками, из диалога сессии):
  - Базовый URL по README github-проекта: https://getfomoapi.fun/api
    (НЕ https://fomoapi.io -- это, похоже, маркетинговый/док-сайт;
    getfomoapi.fun -- фактический хост API согласно клиенту).
  - Эндпоинт: GET /leaderboard/{window}, window in {24h, 7d, 30d, all}.
  - Заголовок: X-API-Key: <ключ>, Accept: application/json.
  - Поля ответа (по CSV-проекции этого клиента): rank, id, displayName,
    userHandle, pnl24h, totalVolume, numTrades, swapCount, followers,
    totalHoldings, solana, evm.
  - Бесплатный уровень (по поисковым сниппетам, НЕ проверено напрямую):
    250 000 кредитов/мес, без карты; вызов leaderboard стоит ~250
    кредитов. Один сниппет утверждает limit до 150 (окна) / 100 (all);
    ДРУГОЙ сниппет утверждает max limit=25 и что offset/page/cursor
    "тихо игнорируются, возвращая тот же первый лист" -- ПРОТИВОРЕЧИЕ,
    не разрешено без реального вызова. Проверяем оба варианта эмпирически.

Владелец: платные планы не покупать без подтверждения. Этот проход --
только разведка + (если ключ есть) попытка забрать реальные 150 позиций
на окно, если API это действительно позволяет.

БЕЗОПАСНОСТЬ: секрет никогда не подставляется в заголовок без проверки
на управляющие символы; вся печать/запись идёт через _scrub_all()."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_fomo_probe_result.json"

FOMO_HOSTS = ["https://getfomoapi.fun/api", "https://fomoapi.io/api", "https://fomoapi.io"]
FOMOLENS_HOSTS = ["https://fomolens.app/api", "https://fomolens.app"]
WINDOWS = ["24h", "7d", "30d", "all"]

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def http_get(url: str, headers: dict | None = None, params: dict | None = None) -> dict:
    for v in (headers or {}).values():
        if any(c in v for c in ("\n", "\r")):
            return {"exception": "заголовок содержит перевод строки -- не отправляю как есть."}
    try:
        resp = requests.get(url, headers=headers or {}, params=params or {}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": _scrub_all(resp.text[:800])}
    return {"http_status": resp.status_code, "body": body, "url": url}


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    api_key = os.environ.get("FOMO_API_KEY", "")
    out["fomo_api_key_present"] = bool(api_key)
    if api_key and not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)
    print(f"[fomo_probe] FOMO_API_KEY присутствует: {bool(api_key)}", flush=True)

    if not api_key:
        out["HONEST_ANSWER"] = (
            "FOMO_API_KEY нет в окружении -- реальный вызов leaderboard невозможен. "
            "По вторичным источникам (см. докстринг) бесплатный ключ создаётся без карты "
            "на fomoapi.io за секунды -- нужно, чтобы владелец завёл ключ и добавил его "
            "секретом FOMO_API_KEY в репозиторий. Ниже -- попытка достучаться до FomoLens "
            "как keyless-резерва, честно, без предположений."
        )
        print("[fomo_probe] " + out["HONEST_ANSWER"], flush=True)

    # ---------- fomoapi.io / getfomoapi.fun: leaderboard, с ключом (если есть) ----------
    # Первый реальный вызов с ключом (18:10 UTC) вернул 401 "Invalid API key" на
    # getfomoapi.fun с заголовком X-API-Key -- источники расходятся в формате
    # заголовка (один говорит X-API-Key, другой -- Authorization: Bearer), и
    # неясно, тот ли это вообще хост, под которым владелец завёл ключ на
    # fomoapi.io. Проверяем варианты заголовка x хоста эмпирически, дёшево
    # (limit=1), прежде чем тратить кредиты на полный сбор.
    HEADER_VARIANTS = [
        ("X-API-Key", lambda k: {"X-API-Key": k}),
        ("Authorization_Bearer", lambda k: {"Authorization": f"Bearer {k}"}),
        ("apiKey_param", lambda k: {}),  # ключ идёт параметром запроса, не заголовком -- см. ниже
    ]
    out["fomoapi_probes"] = {}
    if api_key:
        for host in FOMO_HOSTS:
            key_probe = {}
            for header_name, header_fn in HEADER_VARIANTS:
                params = {"limit": 1}
                headers = {"Accept": "application/json", **header_fn(api_key)}
                if header_name == "apiKey_param":
                    params["apiKey"] = api_key
                r = http_get(f"{host}/leaderboard/24h", headers=headers, params=params)
                body = r.get("body")
                key_probe[header_name] = {
                    "http_status": r.get("http_status"),
                    "body_preview": _scrub_all(json.dumps(body, default=str)[:300]) if not r.get("exception") else None,
                    "exception": r.get("exception"),
                }
                print(f"[fomo_probe] {host} header={header_name} -> http={r.get('http_status')}", flush=True)
            out["fomoapi_probes"][host] = key_probe
        print(f"[fomo_probe] сводка перебора заголовков: {out['fomoapi_probes']}", flush=True)

    # ---------- FomoLens: keyless-резерв, честно пробуем без предположений ----------
    out["fomolens_probes"] = {}
    for host in FOMOLENS_HOSTS:
        for path in ["/leaderboard/24h", "/v1/leaderboard/24h", "/api/leaderboard/24h", "/v2/leaderboard/24h", "/health", "/v1"]:
            r = http_get(f"{host}{path}")
            out["fomolens_probes"][f"{host}{path}"] = {
                "http_status": r.get("http_status"),
                "body_preview": json.dumps(r.get("body"), default=str)[:300] if r.get("body") else None,
                "exception": r.get("exception"),
            }
            print(f"[fomo_probe] fomolens {host}{path} -> http={r.get('http_status')}", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[fomo_probe] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
