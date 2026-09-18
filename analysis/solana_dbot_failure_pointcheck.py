#!/usr/bin/env python3
"""Владелец, 2026-09-18: разбор двух конкретных событий с дашборда DBot,
не путая их.

1) 16:35 UTC, токен G44sp7...DQfe, "Copy Buy Failed (Unknown)" -- РЕАЛЬНЫЙ
   провал (потерянный сигнал). Ищем сделку лидера на цепи (какой токен,
   сколько купил, первый вход или нет) + пробуем вытащить причину отказа
   через DBot API, если она где-то логируется отдельно от follow_orders
   (тот эндпоинт даёт только агрегаты, не пофайловый лог).

2) 16:16 и 16:18 UTC, токен HcRLc9, "Copy Sell Failed (Only take profit /
   stop loss)" -- ПО ЗАДАНИЮ владельца это НЕ провал (осознанный эффект
   mode=only_pnl), но проверяем факт: покупал ли пилот этот токен вообще
   (если нет -- лидер продавал то, чего у нас не было, событие можно
   игнорировать).

Только чтение -- ни одного торгового вызова."""
from __future__ import annotations

import calendar
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_buyer200_select_extend import classify  # noqa: E402
import solana_buyer200_fast_price as fp  # noqa: E402

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_failure_pointcheck_result.json"

DBOT_HOSTS = ["https://api-bot-v1.dbotx.com", "https://servapi.dbotx.com"]
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
PILOT_WALLET = "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS"
TASK_ID = "mu746r8f05f9ic"

# Заявленные владельцем времена/префиксы (UTC, 18.09.2026) -- ищем в
# окне +/-3 минуты вокруг каждого, префикс+суффикс минта для сопоставления.
EVENT_G44SP7 = {"time_str": "2026-09-18T16:35:00Z", "mint_prefix": "G44sp7", "mint_suffix": "DQfe"}
EVENTS_HCRLC9 = [
    {"time_str": "2026-09-18T16:16:00Z", "mint_prefix": "HcRLc9"},
    {"time_str": "2026-09-18T16:18:00Z", "mint_prefix": "HcRLc9"},
]

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


def dbot_get(path: str, api_key: str, params: dict | None = None) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        raise RuntimeError("DBOT_API_KEY содержит перевод строки -- не отправляю как есть.")
    header_variants = [{"x-api-key": api_key}, {"token": api_key}]
    last: dict = {}
    for host in DBOT_HOSTS:
        for headers in header_variants:
            try:
                resp = requests.get(f"{host}{path}", params=params or {}, headers=headers, timeout=30)
            except Exception as exc:  # noqa: BLE001
                last = {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
                continue
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {"non_json_body": _scrub_all(resp.text[:1000])}
            last = {"http_status": resp.status_code, "body": body, "host": host, "auth_tried": list(headers.keys())}
            if resp.status_code == 200:
                return last
    return last


def find_leader_tx_near(target_time: int, window_s: int, mint_prefix: str, mint_suffix: str | None = None) -> list[dict]:
    """Листаем подписи ЛИДЕРА вокруг target_time, классифицируем каждую
    (тот же classify(), что основной прогон), возвращаем все, чей минт
    подходит по префиксу(+суффиксу). НЕ ограничиваемся первой находкой --
    возможно несколько сделок в узком окне."""
    hi = target_time + window_s
    lo = target_time - window_s
    before = None
    matches = []
    for _ in range(15):
        batch = fp.get_signatures_for_address(LEADER_WALLET, before=before, limit=1000)
        if not batch:
            break
        stop = False
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            if bt > hi:
                continue
            if bt < lo:
                stop = True
                break
            if s.get("err") is not None:
                continue
            tx = fp.get_transaction(s["signature"])
            if tx is None:
                continue
            row, check = classify(tx, {"transactionIndex": None})
            mint = (row or {}).get("mint") or ""
            if mint.startswith(mint_prefix) and (mint_suffix is None or mint.endswith(mint_suffix)):
                matches.append({"row": row, "check": check, "signature": s["signature"], "block_time": bt})
            elif row is None and check.get("positive_mints"):
                # Тоже сверяем "неотобранные" (напр. spent<=500) кандидаты по префиксу --
                # честно логируем, даже если это не была бы "покупка" по критерию select.py.
                for pm in check["positive_mints"]:
                    if pm.startswith(mint_prefix) and (mint_suffix is None or pm.endswith(mint_suffix)):
                        matches.append({"row": None, "check": check, "signature": s["signature"], "block_time": bt,
                                        "note": "минт совпал, но НЕ прошёл критерий select.py (см. check.status)"})
        if stop:
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return matches


def check_pilot_ever_held(mint_prefix: str) -> dict:
    """getTokenAccountsByOwner(pilot) без фильтра по минту -- сканируем ВСЕ
    токен-аккаунты пилота, ищем совпадение по префиксу. Если ни одного --
    честный ответ 'никогда не держал' (включая нулевой текущий баланс,
    закрытые ATA видно тоже, пока аккаунт не закрыт полностью -- см. оговорку)."""
    result = fp.rpc_call("getTokenAccountsByOwner", [PILOT_WALLET, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}])
    accounts = (result or {}).get("value", [])
    matching = [a for a in accounts if a["account"]["data"]["parsed"]["info"]["mint"].startswith(mint_prefix)]
    return {
        "n_total_token_accounts_pilot": len(accounts),
        "matching_mint_prefix": matching,
        "ever_held_this_mint": bool(matching),
        "caveat": ("Если ATA этого минта был закрыт целиком (продано всё, аккаунт закрыт) ПОСЛЕ покупки, "
                   "getTokenAccountsByOwner его больше не покажет -- это НЕ доказывает отсутствие покупки, "
                   "только что сейчас открытого аккаунта нет. Для окончательного ответа при пустом результате "
                   "здесь нужна ещё проверка истории подписей пилотного кошелька в это же окно (см. ниже)."),
    }


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    api_key = os.environ.get("DBOT_API_KEY", "")
    if api_key and not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    print(f"[dbot_fail] alchemy_available={fp.alchemy_available()}", flush=True)

    # ---------- Событие 1: G44sp7...DQfe, Copy Buy Failed, 16:35 UTC ----------
    t1 = calendar.timegm(time.strptime(EVENT_G44SP7["time_str"], "%Y-%m-%dT%H:%M:%SZ"))
    matches1 = find_leader_tx_near(t1, 180, EVENT_G44SP7["mint_prefix"], EVENT_G44SP7["mint_suffix"])
    out["event1_G44sp7_leader_matches"] = matches1
    print(f"[dbot_fail] Событие 1 (G44sp7...DQfe, 16:35): найдено совпадений на цепи: {len(matches1)}", flush=True)
    for m in matches1:
        r = m.get("row")
        if r:
            print(f"  mint={r['mint']} spent={r['usdc_spent']} zero_balance={r['zero_balance']} sig={m['signature'][:20]}..", flush=True)

    if api_key:
        # Пробуем вытащить причину отказа отдельно от follow_orders --
        # несколько новых кандидатов, раз swap_orders/follow_orders её не дают.
        reason_candidates = {}
        for path in ["/automation/follow_orders/records", "/automation/copy_logs", "/automation/task_logs",
                      "/automation/error_logs", "/automation/follow_records", "/automation/notifications"]:
            r = dbot_get(path, api_key, params={"chain": "solana", "id": TASK_ID})
            reason_candidates[path] = {"http_status": r.get("http_status"), "host": r.get("host")}
            if r.get("http_status") == 200:
                reason_candidates[path]["full_body"] = r.get("body")
            print(f"[dbot_fail] причина отказа -- {path}: http={r.get('http_status')}", flush=True)
        out["event1_dbot_reason_endpoint_search"] = reason_candidates

        r_swap = dbot_get("/automation/swap_orders", api_key, params={"chain": "solana", "state": "fail"})
        out["event1_swap_orders_state_fail"] = r_swap
        print(f"[dbot_fail] swap_orders?state=fail: http={r_swap.get('http_status')} "
              f"n={len((r_swap.get('body') or {}).get('res', []))}", flush=True)

    # ---------- Событие 2: HcRLc9, Copy Sell Failed, 16:16 и 16:18 UTC ----------
    pilot_check = check_pilot_ever_held(EVENTS_HCRLC9[0]["mint_prefix"])
    out["event2_pilot_ever_held_HcRLc9"] = pilot_check
    print(f"[dbot_fail] Событие 2 (HcRLc9): пилот когда-либо держал этот минт (по текущим ATA)? "
          f"{pilot_check['ever_held_this_mint']}", flush=True)

    # Прямая проверка: подписи пилотного кошелька вообще (не лидера) --
    # честно: если их 0 за всё время, пилот вообще ни разу не торговал,
    # и вопрос "покупали ли мы HcRLc9" закрывается тривиально для ЛЮБОГО минта.
    pilot_sigs = fp.get_signatures_for_address(PILOT_WALLET, limit=50)
    out["event2_pilot_recent_signatures_count"] = len(pilot_sigs or [])
    out["event2_pilot_recent_signatures_sample"] = [
        {"signature": s["signature"], "block_time": s.get("blockTime"), "err": s.get("err")}
        for s in (pilot_sigs or [])[:10]
    ]
    print(f"[dbot_fail] Подписей у пилотного кошелька вообще: {len(pilot_sigs or [])}", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[dbot_fail] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
