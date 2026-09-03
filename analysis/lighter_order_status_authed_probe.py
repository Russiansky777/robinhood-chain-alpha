#!/usr/bin/env python3
"""accountOrders вернул 400 "auth query param and Authorization header
are empty" -- реальный публичный account-read (GET /api/v1/account)
не требует подписи, но accountOrders требует. Генерируем реальный
auth-токен через SignerClient.create_auth_token_with_expiry() (уже
проверено безопасным и рабочим в p5_live_lighter_signcheck.py -- чистая
подпись, никакого ордера) и повторяем запрос статуса трёх неудачных
хедж-попыток с заголовком Authorization.

Только чтение (никаких SendTx/create_order), ключ подписи используется
исключительно для генерации auth-токена.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/lighter_order_status_authed_probe_result.json")
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
ACCOUNT_INDEX = 22012
API_KEY_INDEX = 4
CLIENT_ORDER_INDEXES = [1788454912, 1788456703, 1788458953]


async def _get_auth_token() -> str:
    import lighter
    priv = os.environ["LIGHTER_API_KEY_PRIVATE"]
    client = lighter.SignerClient(url=LIGHTER_API_BASE, account_index=ACCOUNT_INDEX,
                                   api_private_keys={API_KEY_INDEX: priv})
    try:
        token = client.create_auth_token_with_expiry(api_key_index=API_KEY_INDEX)
        return token
    finally:
        await client.close()


def run() -> int:
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    token = asyncio.run(_get_auth_token())
    result["auth_token_type"] = str(type(token))
    # create_auth_token_with_expiry может вернуть кортеж (token, err) или просто токен -- обработаем оба случая
    auth_value = token[0] if isinstance(token, tuple) else token
    result["auth_token_obtained"] = bool(auth_value)
    print(f"[order_status_authed] auth token получен: {bool(auth_value)}, тип={type(token)}")

    params = {"account_index": ACCOUNT_INDEX, "client_order_indexes": ",".join(str(x) for x in CLIENT_ORDER_INDEXES)}
    headers = {"Authorization": auth_value}
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/accountOrders", params=params, headers=headers, timeout=20)
    result["url"] = r.url
    result["status_code"] = r.status_code
    try:
        result["body"] = r.json()
    except Exception as e:  # noqa: BLE001
        result["body_text"] = r.text[:3000]
        result["parse_error"] = str(e)
    print(f"[order_status_authed] status={r.status_code}")
    print(json.dumps(result.get("body", result.get("body_text")), indent=2, default=str, ensure_ascii=False)[:5000])

    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[order_status_authed] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
