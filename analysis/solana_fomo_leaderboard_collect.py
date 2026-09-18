#!/usr/bin/env python3
"""Владелец, 2026-09-18: собрать кандидатов-лидеров с Fomo для сита DBot.

Владелец дал точный, подтверждённый спек (после того как первый реальный
вызов на getfomoapi.fun вернул 401 -- это была сторонняя обёртка, не тот
хост):
  Хост: https://api.fomoapi.io
  Заголовок: Authorization: Bearer <FOMO_API_KEY>
  GET /v2/leaderboard/{24h|7d|30d|all}?limit=150
  Поля строки: wallets.solana, wallets.evm, pnlUsd, volumeUsd, trades,
  followers, userId. 250 кредитов/вызов, 4 окна = 1000 кредитов.
  Ключ на userId, НЕ на handle -- /v2/users/{handle} (резолв, 2500
  кредитов) не дёргаем, адреса и так есть в рейтинге.
  Остаток кредитов -- заголовок x-credits-remaining, смотрим на первом
  вызове.

Этот проход -- сбор 4 рейтингов (сырые данные) + пересечение 7д/30д +
24ч-только отдельно + top-20/30 в сито по числу первых входов -- ЭТА
часть (прямая торговля на Solana, первые входы за неделю) считается
ОТДЕЛЬНЫМ резюмируемым шагом (solana_fomo_onchain_filter.py), дорого по
RPC на кандидата, по образцу основного конвейера 300 покупок (чекпоинт
сразу, не как там было изначально).

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

FOMO_HOST = "https://api.fomoapi.io"
WINDOWS = ["24h", "7d", "30d", "all"]
LEADER_WALLET_SOLANA = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"  # наш текущий лидер -- сверка, что он тоже виден в рейтинге

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def fomo_get(path: str, api_key: str, params: dict, credit_log: list) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        return {"exception": "ключ содержит перевод строки -- не отправляю."}
    url = f"{FOMO_HOST}{path}"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                             params=params, timeout=30)
    except Exception as exc:  # noqa: BLE001
        entry = {"ts": time.time(), "url": _scrub_all(url), "params": params, "exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
        credit_log.append(entry)
        return {"exception": entry["exception"]}
    credits_remaining = resp.headers.get("x-credits-remaining")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": _scrub_all(resp.text[:800])}
    entry = {
        "ts": time.time(), "url": _scrub_all(url), "params": params, "http_status": resp.status_code,
        "estimated_credits": 250, "x_credits_remaining": credits_remaining,
    }
    credit_log.append(entry)
    return {"http_status": resp.status_code, "body": body, "x_credits_remaining": credits_remaining}


def extract_items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "results", "leaderboard", "items", "traders"):
            if isinstance(body.get(k), list):
                return body[k]
    return []


def wallet_key(entry: dict) -> str | None:
    """Solana-адрес трейдера -- вложенный wallets.solana (подтверждённая
    схема), с фолбэком на плоское поле на случай расхождения по факту."""
    w = entry.get("wallets") or {}
    return w.get("solana") or entry.get("solana")


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

    # ---------- Первый вызов -- маленький limit, честно смотрим остаток кредитов ----------
    probe_result = fomo_get("/v2/leaderboard/24h", api_key, {"limit": 1}, credit_log)
    print(f"[fomo_collect] первый вызов: http={probe_result.get('http_status')} "
          f"x-credits-remaining={probe_result.get('x_credits_remaining')}", flush=True)
    out_raw["first_call_probe"] = probe_result
    if probe_result.get("http_status") != 200:
        out_raw["HONEST_ANSWER"] = f"Первый вызов не прошёл (http={probe_result.get('http_status')}) -- см. first_call_probe, дальше не идём."
        print("[fomo_collect] " + out_raw["HONEST_ANSWER"], flush=True)
        RAW_OUT_PATH.write_text(_scrub_all(json.dumps(out_raw, ensure_ascii=False, indent=2, default=str)))
        CREDITS_OUT_PATH.write_text(_scrub_all(json.dumps({"entries": credit_log}, ensure_ascii=False, indent=2, default=str)))
        return

    # ---------- 4 рейтинга целиком, по 150 ----------
    windows_raw = {}
    for window in WINDOWS:
        r = fomo_get(f"/v2/leaderboard/{window}", api_key, {"limit": 150}, credit_log)
        items = extract_items(r.get("body"))
        windows_raw[window] = {"http_status": r.get("http_status"), "n_items": len(items), "items": items,
                                "x_credits_remaining": r.get("x_credits_remaining")}
        print(f"[fomo_collect] {window}: http={r.get('http_status')} n_items={len(items)} "
              f"x-credits-remaining={r.get('x_credits_remaining')}", flush=True)
        time.sleep(0.3)

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

    keys_7d, keys_30d, keys_24h = (set(by_window_keys[w]) for w in ("7d", "30d", "24h"))
    intersection_7d_30d = keys_7d & keys_30d
    only_24h = keys_24h - keys_7d - keys_30d

    def build_row(key: str) -> dict:
        windows_present = [w for w in WINDOWS if key in by_window_keys[w]]
        rep = by_window_keys.get("30d", {}).get(key) or by_window_keys.get("7d", {}).get(key) or next(
            (by_window_keys[w][key] for w in WINDOWS if key in by_window_keys[w]), {})
        return {
            "user_id": rep.get("userId"),
            "solana_address": key,
            "evm_address": (rep.get("wallets") or {}).get("evm"),
            "pnl_usd_7d": (by_window_keys.get("7d", {}).get(key) or {}).get("pnlUsd"),
            "pnl_usd_30d": (by_window_keys.get("30d", {}).get(key) or {}).get("pnlUsd"),
            "volume_usd_7d": (by_window_keys.get("7d", {}).get(key) or {}).get("volumeUsd"),
            "trades_7d": (by_window_keys.get("7d", {}).get(key) or {}).get("trades"),
            "trades_30d": (by_window_keys.get("30d", {}).get(key) or {}).get("trades"),
            "followers": rep.get("followers"),
            "windows_present": windows_present,
            # Заполняется следующим шагом (solana_fomo_onchain_filter.py):
            "trades_solana_directly": None,
            "first_entries_last_week": None,
        }

    primary_candidates = [build_row(k) for k in intersection_7d_30d]
    only_24h_candidates = [build_row(k) for k in only_24h]
    primary_candidates.sort(key=lambda r: r.get("trades_7d") or 0, reverse=True)

    out_candidates = {
        "generated_at_utc": out_raw["generated_at_utc"],
        "source": f"{FOMO_HOST}/v2/leaderboard/{{window}}",
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
        "last_x_credits_remaining": credit_log[-1].get("x_credits_remaining") if credit_log else None,
        "free_tier_monthly_budget": 250_000,
    }, ensure_ascii=False, indent=2, default=str)))
    print(f"[fomo_collect] расход кредитов записан в {CREDITS_OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
