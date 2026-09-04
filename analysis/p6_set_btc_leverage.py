#!/usr/bin/env python3
"""P6 -- владелец, 2026-09-04: "Плечо BTC не брать по дефолту, а
выставить аутентифицированным вызовом (не ордер) и перечитать."

Это ЕДИНСТВЕННЫЙ мутирующий вызов во всей цепочке P6-dry-run: реальная
подписанная транзакция `update_leverage` на Lighter (аккаунт 22012,
рынок BTC). НЕ ордер, НЕ открывает позицию/экспозицию -- это только
настройка плеча/margin_mode для рынка на аккаунте (реальный метод
`lighter.SignerClient.update_leverage(market_index, margin_mode,
leverage)`, elliottech/lighter-python, подтверждено чтением исходника
2026-09-04). Явно разрешено этим сообщением владельца отдельно от
"да" на реальный вход в P6 (тот остаётся отдельным гейтом).

margin_mode=0 -- то же значение, что у реальной ETH-позиции аккаунта
(`market_margin_mode` в orderBookDetails ETH и `margin_mode` самой
позиции -- оба 0, cross), для консистентности с уже существующим
cross-margin режимом аккаунта (P5). leverage=2.0 -- то же плечо, что
реально подтверждено для ETH (imf=50% -> 100/50=2.0), не дефолт биржи.

Запускается на VPS (тот же путь, что p5_live_flatten_lighter.py) --
подпись Lighter-транзакций работает только оттуда (юрисдикционное
ограничение, реальный `code=20558 restricted jurisdiction` с адресов
GH Actions, docs/PROJECT_STATE.md).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import p5_live_precheck as pc  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p6_set_btc_leverage_result.json")
LIGHTER_API_KEY_INDEX = 4
TARGET_LEVERAGE = 2.0
TARGET_MARGIN_MODE = 0  # cross -- то же значение, что реальная ETH-позиция аккаунта


def find_btc_market() -> dict | None:
    import requests
    resp = requests.get(f"{pc.LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    resp.raise_for_status()
    markets = resp.json().get("order_book_details", [])
    exact = [m for m in markets if str(m.get("symbol", "")).upper() == "BTC"]
    return exact[0] if exact else None


async def _set_leverage(market_index: int, margin_mode: int, leverage: float) -> dict:
    import lighter
    lighter_priv = os.environ["LIGHTER_API_KEY_PRIVATE"]
    client = lighter.SignerClient(url=pc.LIGHTER_API_BASE, account_index=pc.LIGHTER_ACCOUNT_INDEX,
                                   api_private_keys={LIGHTER_API_KEY_INDEX: lighter_priv})
    try:
        tx, resp, err = await client.update_leverage(
            market_index=market_index, margin_mode=margin_mode, leverage=leverage,
            api_key_index=LIGHTER_API_KEY_INDEX,
        )
        return {
            "tx_hash": resp.tx_hash if resp is not None else None,
            "resp_code": resp.code if resp is not None else None,
            "resp_message": resp.message if resp is not None else None,
            "err": str(err) if err is not None else None,
        }
    finally:
        await client.close()


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "note": "ЕДИНСТВЕННЫЙ мутирующий вызов в цепочке P6 -- update_leverage (не ордер, не позиция), явно запрошено владельцем."}

    btc_market = find_btc_market()
    if btc_market is None:
        result["abort_reason"] = "рынок BTC не найден в orderBookDetails -- стоп."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return 1

    market_index = btc_market["market_id"]
    result["btc_market_id_real"] = market_index
    result["default_initial_margin_fraction_bps"] = btc_market.get("default_initial_margin_fraction")

    print("=== ДО: реальное плечо BTC на аккаунте 22012 (до вызова) ===")
    account_before = pc.lighter_account_full()
    leverage_before = pc.real_eth_leverage(account_before, "BTC")
    result["leverage_before"] = leverage_before
    print(f"[p6_set_btc_leverage] до: {leverage_before}")

    print(f"\n=== Отправка update_leverage(market_index={market_index}, margin_mode={TARGET_MARGIN_MODE}, leverage={TARGET_LEVERAGE}) ===")
    tx_result = asyncio.run(_set_leverage(market_index, TARGET_MARGIN_MODE, TARGET_LEVERAGE))
    result["update_leverage_tx"] = tx_result
    print(f"[p6_set_btc_leverage] tx: {tx_result}")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))

    if tx_result.get("err") is not None:
        result["abort_reason"] = f"update_leverage вернул ошибку: {tx_result['err']}"
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[p6_set_btc_leverage] СТОП: {result['abort_reason']}")
        return 1

    time.sleep(5)  # дать транзакции попасть в состояние аккаунта перед повторным чтением

    print("\n=== ПОСЛЕ: реальное плечо BTC на аккаунте 22012 (перечитано) ===")
    account_after = pc.lighter_account_full()
    leverage_after = pc.real_eth_leverage(account_after, "BTC")
    result["leverage_after"] = leverage_after
    result["confirmed_by_account_reread"] = (leverage_after.get("found") is True and
                                              leverage_after.get("leverage") is not None and
                                              abs(leverage_after["leverage"] - TARGET_LEVERAGE) < 0.01)
    print(f"[p6_set_btc_leverage] после: {leverage_after}")
    print(f"[p6_set_btc_leverage] подтверждено перечитыванием: {result['confirmed_by_account_reread']}")

    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0 if result["confirmed_by_account_reread"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
