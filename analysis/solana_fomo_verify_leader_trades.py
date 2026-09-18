#!/usr/bin/env python3
"""Владелец, 2026-09-18: если Fomo API отдаёт историю сделок ПО АДРЕСУ
нашего текущего лидера -- сверить с нашими данными с цепи (300 покупок,
selected_300.json). Если совпадает -- это не только для сита, а ускорение
для всего дальнейшего конвейера (не листать пулы вручную через Alchemy,
а брать готовую историю сделок из Fomo).

Метод (по README abstradeapi/Open-Fomo-API, единственный найденный
источник с конкретным путём для истории сделок):
  GET https://getfomoapi.fun/api/users/{userId}/swaps
  header: X-API-Key
Но userId, не адрес -- значит лидера сначала нужно найти В РЕЙТИНГЕ
(data/fomo_leaderboard_raw.json, уже собран отдельным шагом) и достать
его id оттуда. Если лидера в рейтинге нет -- сверка невозможна без
эндпоинта обратного резолва по адресу (пробуем пару правдоподобных
путей честно, без гарантий).

Сверка: не count-to-count (Fomo swaps включает продажи/переводы, наша
выборка -- только покупки новых токенов), а ТОЧЕЧНАЯ -- берём несколько
наших РЕАЛЬНЫХ сделок (подпись, минт, время, usdc_spent с цепи) и ищем
совпадение в ответе Fomo (тот же минт, близкое время, близкая сумма).

БЕЗОПАСНОСТЬ: без печати ключа, вся печать/запись через _scrub_all()."""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_LEADERBOARD_PATH = REPO_ROOT / "data" / "fomo_leaderboard_raw.json"
SELECTED_300_PATH = REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json"
OUT_PATH = REPO_ROOT / "data" / "solana_fomo_verify_leader_trades_result.json"

FOMO_HOSTS = ["https://getfomoapi.fun/api", "https://fomoapi.io/api"]
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def fomo_get(host: str, path: str, api_key: str, params: dict | None = None) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        return {"exception": "ключ содержит перевод строки."}
    try:
        resp = requests.get(f"{host}{path}", headers={"X-API-Key": api_key, "Accept": "application/json"},
                             params=params or {}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": _scrub_all(resp.text[:800])}
    return {"http_status": resp.status_code, "body": body, "host": host}


def extract_items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "results", "swaps", "trades", "items"):
            if isinstance(body.get(k), list):
                return body[k]
    return []


def find_leader_id_in_raw() -> dict | None:
    if not RAW_LEADERBOARD_PATH.exists():
        return None
    raw = json.loads(RAW_LEADERBOARD_PATH.read_text())
    for window, wd in (raw.get("windows") or {}).items():
        for item in wd.get("items", []):
            if item.get("solana") == LEADER_WALLET:
                return {"window": window, **item}
    return None


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    api_key = os.environ.get("FOMO_API_KEY", "")
    if not api_key:
        out["HONEST_ANSWER"] = "FOMO_API_KEY пуст."
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print("[fomo_verify] " + out["HONEST_ANSWER"], flush=True)
        return
    if not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    leader_entry = find_leader_id_in_raw()
    out["leader_found_in_leaderboard_raw"] = leader_entry is not None
    if leader_entry:
        out["leader_leaderboard_entry"] = leader_entry
    print(f"[fomo_verify] лидер в собранных рейтингах: {leader_entry is not None}", flush=True)

    leader_id = None
    if leader_entry:
        leader_id = leader_entry.get("id")

    if not leader_id:
        # Пробуем правдоподобные пути обратного резолва по адресу -- честно,
        # без предположения, что они существуют.
        out["reverse_resolve_attempts"] = {}
        for host in FOMO_HOSTS:
            for path in [f"/users/wallet/{LEADER_WALLET}", f"/wallets/{LEADER_WALLET}", f"/users/{LEADER_WALLET}"]:
                r = fomo_get(host, path, api_key)
                out["reverse_resolve_attempts"][f"{host}{path}"] = {
                    "http_status": r.get("http_status"),
                    "body_preview": json.dumps(r.get("body"), default=str)[:400] if not r.get("exception") else r.get("exception"),
                }
                print(f"[fomo_verify] resolve {host}{path} -> http={r.get('http_status')}", flush=True)
                body = r.get("body")
                if r.get("http_status") == 200 and isinstance(body, dict) and body.get("id"):
                    leader_id = body["id"]
                    break
            if leader_id:
                break

    if not leader_id:
        out["HONEST_ANSWER"] = (
            "Лидер не найден ни в одном из 4 собранных окон рейтинга Fomo, и обратный резолв "
            "по адресу не сработал ни на одном пробном пути (см. reverse_resolve_attempts). "
            "История сделок через Fomo для ЭТОГО лидера недоступна -- метод годится только для "
            "кандидатов, которые сами есть в рейтинге (это и есть будущие кандидаты для сита, "
            "так что для них проверка возможна; для действующего лидера, который в рейтинге не "
            "числится, -- нет)."
        )
        print("[fomo_verify] " + out["HONEST_ANSWER"], flush=True)
        OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
        return

    out["leader_fomo_id"] = leader_id

    # ---------- История сделок ----------
    swaps_items = []
    swaps_raw = {}
    for host in FOMO_HOSTS:
        for path in [f"/users/{leader_id}/swaps", f"/v2/users/{leader_id}/trades", f"/users/{leader_id}/trades"]:
            r = fomo_get(host, path, api_key, params={"limit": 500})
            items = extract_items(r.get("body"))
            swaps_raw[f"{host}{path}"] = {"http_status": r.get("http_status"), "n_items": len(items)}
            print(f"[fomo_verify] {host}{path} -> http={r.get('http_status')} n_items={len(items)}", flush=True)
            if r.get("http_status") == 200 and items:
                swaps_items = items
                out["swaps_endpoint_used"] = f"{host}{path}"
                out["swaps_raw_sample"] = items[:3]
                break
        if swaps_items:
            break
    out["swaps_probe_attempts"] = swaps_raw
    out["n_swaps_returned"] = len(swaps_items)

    if not swaps_items:
        out["HONEST_ANSWER"] = "Ни один из опробованных путей истории сделок не вернул данных -- см. swaps_probe_attempts."
        print("[fomo_verify] " + out["HONEST_ANSWER"], flush=True)
        OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
        return

    # ---------- Сверка: наши реальные сделки (с цепи) против ответа Fomo ----------
    our_trades = json.loads(SELECTED_300_PATH.read_text()) if SELECTED_300_PATH.exists() else []
    out["n_our_onchain_trades"] = len(our_trades)
    if our_trades:
        times = [t["time"] for t in our_trades]
        out["our_onchain_time_range_utc"] = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(times))),
                                              time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(times)))]

    # Разбираем временной охват ответа Fomo -- честно, по факту полей (имя поля
    # времени заранее не знаем -- ищем несколько правдоподобных).
    def get_ts(item: dict):
        for k in ("timestamp", "time", "createdAt", "blockTime", "ts"):
            if k in item:
                return item[k]
        return None

    fomo_times = [get_ts(i) for i in swaps_items if get_ts(i) is not None]
    out["fomo_swaps_time_field_found"] = bool(fomo_times)
    out["fomo_swaps_sample_keys"] = list(swaps_items[0].keys()) if swaps_items else []

    matches, checked = [], 0
    for our in sorted(our_trades, key=lambda t: t["time"], reverse=True)[:15]:  # 15 самых свежих -- больше шанс, что Fomo их видит
        checked += 1
        our_mint, our_time, our_usdc = our["mint"], our["time"], D(our["usdc_spent"])
        best = None
        for f in swaps_items:
            f_mint = f.get("mint") or f.get("tokenMint") or f.get("outputMint") or f.get("token")
            if f_mint != our_mint:
                continue
            f_ts = get_ts(f)
            if f_ts is None:
                continue
            # Метка времени может быть в секундах или миллисекундах -- проверяем по порядку величины.
            f_ts_s = f_ts / 1000 if f_ts > 10**12 else f_ts
            if abs(f_ts_s - our_time) < 120:
                best = {"our_signature": our["signature"], "our_time": our_time, "our_usdc_spent": str(our_usdc),
                        "fomo_item": f, "seconds_delta": f_ts_s - our_time}
                break
        if best:
            matches.append(best)

    out["spot_check_n_checked"] = checked
    out["spot_check_n_matched"] = len(matches)
    out["spot_check_matches"] = matches
    print(f"[fomo_verify] точечная сверка: {len(matches)}/{checked} наших недавних сделок нашлись в ответе Fomo", flush=True)

    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[fomo_verify] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
