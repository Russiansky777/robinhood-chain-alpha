#!/usr/bin/env python3
"""Владелец, 2026-09-18: собрать кандидатов-лидеров с Fomo для сита DBot.
Ключ FOMO_API_KEY теперь в секретах -- первый реальный проход с реальными
данными (до этого был только keyless-разведочный пробник, см.
solana_fomo_probe.py и data/solana_fomo_probe_result.json).

Метод (по README реального клиента abstradeapi/Fomo-Auto-leaderboard-Fetcher,
единственный источник с конкретным путём и заголовком, но лимит/пагинация
там расходились с другим сниппетом -- поэтому здесь всё ещё проверяем
факт ответа, не берём числа на веру):
  GET https://getfomoapi.fun/api/leaderboard/{window}
  header: X-API-Key: <ключ>, Accept: application/json
  window in {24h, 7d, 30d, all}, params: limit

Этот проход -- ТОЛЬКО сбор четырёх рейтингов (сырые данные) + пересечение
7д/30д + разметка 24ч-только/всё время. Он НЕ включает шаги 2-3 (прямая
торговля на Solana по факту цепи, число первых входов за неделю) -- это
для КАЖДОГО кандидата отдельное сканирование истории кошелька на 7 дней,
дорого по RPC-вызовам и времени; сделано отдельным резюмируемым проходом
(solana_fomo_onchain_filter.py, следующий шаг) по образцу основного
конвейера 300 покупок -- ТАМ уже был баг с потерей прогресса при
перезапуске без чекпоинта, здесь сразу с чекпоинтом.

Расход кредитов: пишем в data/credits_spent_fomo.json -- по каждому
вызову: оценка (250 кредитов/вызов leaderboard, по вторичному источнику,
НЕ подтверждено) + реальный остаток, если API его отдаёт (заголовки
ответа или поле в теле -- смотрим по факту первого вызова, не гадаем
имя поля заранее).

БЕЗОПАСНОСТЬ: секрет не подставляется в заголовок без проверки на
переводы строк; вся печать/запись -- через _scrub_all()."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_OUT_PATH = REPO_ROOT / "data" / "fomo_leaderboard_raw.json"
CANDIDATES_OUT_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"
CREDITS_OUT_PATH = REPO_ROOT / "data" / "credits_spent_fomo.json"

FOMO_HOSTS = ["https://getfomoapi.fun/api", "https://fomoapi.io/api"]
WINDOWS = ["24h", "7d", "30d", "all"]
LEADER_WALLET_SOLANA = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"  # наш текущий лидер -- сверка, что он тоже виден в рейтинге

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def _find_credit_hints(resp: requests.Response) -> dict:
    """Ищем в заголовках/теле ответа что-то похожее на остаток кредитов --
    ИМЯ поля заранее не знаем (в доках не нашли), поэтому просто собираем
    все заголовки, где встречается credit/remaining/quota/limit, честно."""
    hints = {}
    for k, v in resp.headers.items():
        lk = k.lower()
        if any(w in lk for w in ("credit", "remaining", "quota", "ratelimit", "rate-limit")):
            hints[k] = v
    return hints


def fomo_get(host: str, path: str, api_key: str, params: dict, credit_log: list) -> tuple[dict, dict]:
    if any(c in api_key for c in ("\n", "\r")):
        return {"exception": "ключ содержит перевод строки -- не отправляю."}, {}
    url = f"{host}{path}"
    try:
        resp = requests.get(url, headers={"X-API-Key": api_key, "Accept": "application/json"}, params=params, timeout=30)
    except Exception as exc:  # noqa: BLE001
        entry = {"ts": time.time(), "url": _scrub_all(url), "params": params, "exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
        credit_log.append(entry)
        return {"exception": entry["exception"]}, {}
    credit_hints = _find_credit_hints(resp)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": _scrub_all(resp.text[:800])}
    entry = {
        "ts": time.time(), "url": _scrub_all(url), "params": params, "http_status": resp.status_code,
        "estimated_credits": 250, "credit_hints_from_response": credit_hints,
    }
    credit_log.append(entry)
    return {"http_status": resp.status_code, "body": body}, credit_hints


def extract_items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "results", "leaderboard", "items", "traders"):
            if isinstance(body.get(k), list):
                return body[k]
    return []


def wallet_key(entry: dict) -> str | None:
    """Уникальный идентификатор трейдера для пересечения окон -- Solana-адрес,
    если есть (это то, что нам реально нужно для DBot); иначе id/handle."""
    return entry.get("solana") or entry.get("solanaAddress") or entry.get("solana_address")


def main() -> None:
    out_raw: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    credit_log: list = []
    api_key = os.environ.get("FOMO_API_KEY", "")
    if not api_key:
        CANDIDATES_OUT_PATH.write_text(json.dumps({"HONEST_ANSWER": "FOMO_API_KEY пуст в окружении."}, ensure_ascii=False, indent=2))
        print("[fomo_collect] FOMO_API_KEY пуст -- нечего собирать.", flush=True)
        return
    if not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    host = FOMO_HOSTS[0]

    # ---------- Первый вызов -- маленький limit, только чтобы честно увидеть остаток кредитов ----------
    probe_result, probe_hints = fomo_get(host, "/leaderboard/24h", api_key, {"limit": 1}, credit_log)
    print(f"[fomo_collect] первый вызов (проверка остатка): http={probe_result.get('http_status')} "
          f"credit_hints={probe_hints} body_keys={list((probe_result.get('body') or {}).keys()) if isinstance(probe_result.get('body'), dict) else 'list'}",
          flush=True)
    out_raw["first_call_probe"] = {"result": probe_result, "credit_hints": probe_hints}
    if probe_result.get("http_status") != 200:
        out_raw["HONEST_ANSWER"] = f"Первый вызов не прошёл (http={probe_result.get('http_status')}) -- см. first_call_probe, дальше не идём."
        print("[fomo_collect] " + out_raw["HONEST_ANSWER"], flush=True)
        RAW_OUT_PATH.write_text(_scrub_all(json.dumps(out_raw, ensure_ascii=False, indent=2, default=str)))
        CREDITS_OUT_PATH.write_text(_scrub_all(json.dumps({"entries": credit_log}, ensure_ascii=False, indent=2, default=str)))
        return

    # ---------- 4 рейтинга целиком, по 150 (100 для all) ----------
    windows_raw = {}
    for window in WINDOWS:
        limit = 100 if window == "all" else 150
        r, hints = fomo_get(host, f"/leaderboard/{window}", api_key, {"limit": limit}, credit_log)
        items = extract_items(r.get("body"))
        windows_raw[window] = {"http_status": r.get("http_status"), "n_items": len(items), "items": items,
                                "credit_hints": hints, "requested_limit": limit}
        print(f"[fomo_collect] {window}: http={r.get('http_status')} n_items={len(items)} (запрошено {limit})", flush=True)
        time.sleep(0.3)  # не долбим API без нужды

    out_raw["windows"] = windows_raw
    RAW_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_OUT_PATH.write_text(_scrub_all(json.dumps(out_raw, ensure_ascii=False, indent=2, default=str)))
    print(f"[fomo_collect] сырые данные записаны в {RAW_OUT_PATH}", flush=True)

    # ---------- Пересечение 7д/30д, отметка 24ч-только, all-time справочно ----------
    by_window_keys: dict[str, dict[str, dict]] = {}
    for window in WINDOWS:
        d = {}
        for item in windows_raw[window]["items"]:
            k = wallet_key(item)
            if k:
                d[k] = item
        by_window_keys[window] = d
        print(f"[fomo_collect] {window}: {len(d)}/{windows_raw[window]['n_items']} записей имеют solana-адрес", flush=True)

    keys_7d, keys_30d, keys_24h, keys_all = (set(by_window_keys[w]) for w in WINDOWS)
    intersection_7d_30d = keys_7d & keys_30d
    only_24h = keys_24h - keys_7d - keys_30d

    def build_row(key: str) -> dict:
        windows_present = [w for w in WINDOWS if key in by_window_keys[w]]
        rep = by_window_keys.get("30d", {}).get(key) or by_window_keys.get("7d", {}).get(key) or next(
            (by_window_keys[w][key] for w in WINDOWS if key in by_window_keys[w]), {})
        return {
            "nickname": rep.get("displayName") or rep.get("userHandle") or rep.get("id"),
            "solana_address": key,
            "evm_address": rep.get("evm"),
            "pnl_7d": (by_window_keys.get("7d", {}).get(key) or {}).get("pnl24h") or (by_window_keys.get("7d", {}).get(key) or {}).get("pnl"),
            "pnl_30d": (by_window_keys.get("30d", {}).get(key) or {}).get("pnl24h") or (by_window_keys.get("30d", {}).get(key) or {}).get("pnl"),
            "windows_present": windows_present,
            "followers": rep.get("followers"),
            "rank_7d": (by_window_keys.get("7d", {}).get(key) or {}).get("rank"),
            "rank_30d": (by_window_keys.get("30d", {}).get(key) or {}).get("rank"),
            # Заполняется следующим шагом (solana_fomo_onchain_filter.py):
            "trades_solana_directly": None,
            "first_entries_last_week": None,
        }

    primary_candidates = [build_row(k) for k in intersection_7d_30d]
    only_24h_candidates = [build_row(k) for k in only_24h]

    out_candidates = {
        "generated_at_utc": out_raw["generated_at_utc"],
        "source": f"{host}/leaderboard/{{window}}",
        "n_by_window": {w: windows_raw[w]["n_items"] for w in WINDOWS},
        "n_with_solana_address_by_window": {w: len(by_window_keys[w]) for w in WINDOWS},
        "n_intersection_7d_30d": len(primary_candidates),
        "n_only_24h": len(only_24h_candidates),
        "leader_wallet_in_leaderboard": {w: (LEADER_WALLET_SOLANA in by_window_keys[w]) for w in WINDOWS},
        "primary_candidates": primary_candidates,
        "only_24h_candidates_flagged_separately": only_24h_candidates,
        "all_time_reference_top20": windows_raw["all"]["items"][:20],
        "NEXT_STEP": "solana_fomo_onchain_filter.py -- для primary_candidates: прямая торговля на Solana + первые входы за неделю (шаги 2-3), резюмируемо.",
    }
    CANDIDATES_OUT_PATH.write_text(_scrub_all(json.dumps(out_candidates, ensure_ascii=False, indent=2, default=str)))
    print(f"[fomo_collect] {len(primary_candidates)} кандидатов (пересечение 7д/30д) записаны в {CANDIDATES_OUT_PATH}", flush=True)
    print(f"[fomo_collect] наш текущий лидер в рейтинге: {out_candidates['leader_wallet_in_leaderboard']}", flush=True)

    CREDITS_OUT_PATH.write_text(_scrub_all(json.dumps({
        "entries": credit_log,
        "total_calls": len(credit_log),
        "total_estimated_credits": sum(e.get("estimated_credits", 0) for e in credit_log),
        "free_tier_monthly_budget_estimated": 250_000,
        "note": "estimated_credits -- оценка 250/вызов по вторичному источнику, НЕ подтверждена API; "
                "credit_hints_from_response в каждой записи -- то, что реально нашли в заголовках ответа (если нашли).",
    }, ensure_ascii=False, indent=2, default=str)))
    print(f"[fomo_collect] расход кредитов записан в {CREDITS_OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
