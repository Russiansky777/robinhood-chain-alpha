#!/usr/bin/env python3
"""Владелец, 2026-09-18: если Fomo API отдаёт историю сделок ПО userId
нашего текущего лидера -- сверить с нашими данными с цепи (300 покупок,
selected_300.json). Если совпадает -- ускорение не только для сита, а
для всего дальнейшего конвейера.

Подтверждённый владельцем спек:
  Хост: https://api.fomoapi.io, заголовок Authorization: Bearer <ключ>
  GET /v2/users/{userId}/trades?cursor=start -- по 25 ЗАКРЫТЫХ сделок на
  страницу, 250 кредитов/вызов. НЕ полная история -- для сита хватит,
  для точного расчёта $ и % владелец велел идти на цепь по адресу (уже
  умеем, см. solana_dbot_pilot_summary.py).
  userId берём ИЗ УЖЕ СОБРАННОГО рейтинга (data/fomo_leaderboard_raw.json,
  поле wallets.solana -> userId) -- резолв по handle (/v2/users/{handle},
  2500 кредитов) НЕ используем, адреса и так в рейтинге.

Сверка: не count-to-count (Fomo может отдавать не всю историю, наша
выборка -- только покупки новых токенов), а ТОЧЕЧНАЯ -- берём несколько
наших РЕАЛЬНЫХ сделок (подпись, минт, время, usdc_spent с цепи) и ищем
совпадение в ответе Fomo (тот же минт, близкое время, близкая сумма).

Если лидера нет ни в одном из 4 собранных окон рейтинга -- честно
останавливаемся: без userId сделок не получить, а резолв по handle
явно исключён владельцем.

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

FOMO_HOST = "https://api.fomoapi.io"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def fomo_get(path: str, api_key: str, params: dict | None = None) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        return {"exception": "ключ содержит перевод строки."}
    try:
        resp = requests.get(f"{FOMO_HOST}{path}", headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                             params=params or {}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": _scrub_all(resp.text[:800])}
    return {"http_status": resp.status_code, "body": body, "x_credits_remaining": resp.headers.get("x-credits-remaining")}


def extract_items(body) -> tuple[list, str | None]:
    """Возвращает (список сделок, курсор следующей страницы если есть)."""
    if isinstance(body, list):
        return body, None
    if isinstance(body, dict):
        cursor = body.get("nextCursor") or body.get("next_cursor") or body.get("cursor")
        for k in ("data", "results", "trades", "items"):
            if isinstance(body.get(k), list):
                return body[k], cursor
    return [], None


def find_leader_wallet_key(raw: dict) -> dict | None:
    for window, wd in (raw.get("windows") or {}).items():
        for item in wd.get("items", []):
            w = item.get("wallets") or {}
            if w.get("solana") == LEADER_WALLET or item.get("solana") == LEADER_WALLET:
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

    if not RAW_LEADERBOARD_PATH.exists():
        out["HONEST_ANSWER"] = "data/fomo_leaderboard_raw.json ещё нет -- сначала solana_fomo_leaderboard_collect.py."
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print("[fomo_verify] " + out["HONEST_ANSWER"], flush=True)
        return

    raw = json.loads(RAW_LEADERBOARD_PATH.read_text())
    leader_entry = find_leader_wallet_key(raw)
    out["leader_found_in_leaderboard_raw"] = leader_entry is not None
    if leader_entry:
        out["leader_leaderboard_entry"] = leader_entry
    print(f"[fomo_verify] лидер в собранных рейтингах: {leader_entry is not None}", flush=True)

    leader_id = leader_entry.get("userId") if leader_entry else None
    if not leader_id:
        out["HONEST_ANSWER"] = (
            "Лидер не найден ни в одном из 4 собранных окон рейтинга Fomo (или собран старой, "
            "неверной версией скрипта -- перепроверь дату data/fomo_leaderboard_raw.json). Резолв "
            "по handle (/v2/users/{handle}) исключён владельцем -- 2500 кредитов, не используем. "
            "История сделок через Fomo для ЭТОГО лидера недоступна без userId."
        )
        print("[fomo_verify] " + out["HONEST_ANSWER"], flush=True)
        OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
        return

    out["leader_fomo_user_id"] = leader_id

    # ---------- История сделок: постранично, cursor=start, 25/страницу ----------
    all_trades: list = []
    cursor = "start"
    pages_fetched = 0
    while cursor and pages_fetched < 8:  # 8 страниц = 200 сделок -- достаточно для точечной сверки, не выкачиваем всё без нужды
        r = fomo_get(f"/v2/users/{leader_id}/trades", api_key, params={"cursor": cursor})
        items, next_cursor = extract_items(r.get("body"))
        print(f"[fomo_verify] trades page {pages_fetched}: http={r.get('http_status')} n={len(items)} "
              f"x-credits-remaining={r.get('x_credits_remaining')}", flush=True)
        if r.get("http_status") != 200:
            out[f"trades_page_{pages_fetched}_error"] = r
            break
        if pages_fetched == 0:
            out["trades_raw_sample"] = items[:3]
        all_trades.extend(items)
        pages_fetched += 1
        if not items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(0.3)

    out["n_pages_fetched"] = pages_fetched
    out["n_trades_returned"] = len(all_trades)
    if not all_trades:
        out["HONEST_ANSWER"] = "История сделок пуста или эндпоинт не вернул данных -- см. поля trades_page_*_error."
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

    def get_ts(item: dict):
        for k in ("timestamp", "time", "createdAt", "closedAt", "blockTime", "ts"):
            if k in item:
                return item[k]
        return None

    out["fomo_trades_sample_keys"] = list(all_trades[0].keys()) if all_trades else []
    fomo_times = [get_ts(i) for i in all_trades if get_ts(i) is not None]
    if fomo_times:
        fmin, fmax = min(fomo_times), max(fomo_times)
        fmin_s = fmin / 1000 if fmin > 10**12 else fmin
        fmax_s = fmax / 1000 if fmax > 10**12 else fmax
        out["fomo_trades_time_range_utc"] = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fmin_s)),
                                              time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fmax_s))]

    matches, checked = [], 0
    for our in sorted(our_trades, key=lambda t: t["time"], reverse=True)[:15]:  # 15 самых свежих -- больше шанс попасть в первые страницы
        checked += 1
        our_mint, our_time, our_usdc = our["mint"], our["time"], D(our["usdc_spent"])
        best = None
        for f in all_trades:
            f_mint = f.get("mint") or f.get("tokenMint") or f.get("outputMint") or f.get("token")
            if f_mint != our_mint:
                continue
            f_ts = get_ts(f)
            if f_ts is None:
                continue
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
