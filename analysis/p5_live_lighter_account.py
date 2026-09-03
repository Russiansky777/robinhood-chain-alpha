#!/usr/bin/env python3
"""P5 LIVE, п.2 (владелец, 2026-09-03): "делай. Возьми схему подписи из
elliottech/lighter-python, реализуй и проверь только на чтении: баланс
маржи по account index 22012, список открытых позиций, доступное
плечо по ETH-перпу. Ордера не отправлять."

НАХОДКА (проверено по реальному исходнику GitHub, не угадано): для
ИМЕННО ЭТОГО чтения схема подписи НЕ НУЖНА. `AccountApi.account()`
(GET /api/v1/account?by=index&value=<idx>) в реальном SDK
(lighter/api/account_api.py, elliottech/lighter-python) объявлен с
`_auth_settings: List[str] = []` -- публичный, без авторизации,
возвращает `DetailedAccounts` (lighter/models/detailed_accounts.py:
{code, total, accounts: [DetailedAccount, ...], next_cursor}).
`DetailedAccount` (lighter/models/detailed_account.py) содержит ровно
то, что нужно: `collateral`, `available_balance`,
`cross_initial_margin_requirement`, `cross_maintenance_margin_requirement`,
`positions: List[AccountPosition]`. Максимальное плечо по рынку --
ОТДЕЛЬНО, на уровне рынка (не аккаунта): `min_initial_margin_fraction`
в `PerpsOrderBookDetail` (lighter/models/perps_order_book_detail.py),
из УЖЕ используемого весь день `GET /api/v1/orderBookDetails` --
leverage_max = 1 / min_initial_margin_fraction (единицы поля
подтверждаются эмпирически по факту ответа, не предполагаются).

Реальная схема подписи (SignerClient, api_private_keys, git-only пакет
`zklighter-perps-python`) в этом прогоне НЕ реализована и НЕ нужна --
она требуется только для ЗАПИСИ (ордера, ключи) -- явно ВНЕ этого шага
("ордера не отправлять"). Секреты LIGHTER_API_KEY_PUBLIC/
LIGHTER_API_KEY_PRIVATE в этом скрипте НЕ используются (сверено -- это
чтение только через публичный эндпоинт).

Только чтение (HTTP GET), ордеров нет.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/p5_live_lighter_account_result.json")
LIGHTER_API_BASE = "https://mainnet.zklighter.elliot.ai"
ACCOUNT_INDEX = 22012


def fetch_account() -> dict:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/account", params={"by": "index", "value": str(ACCOUNT_INDEX)},
                      timeout=20)
    entry = {"status_code": r.status_code, "url": r.url}
    try:
        body = r.json()
        entry["body"] = body
    except Exception as e:  # noqa: BLE001
        entry["error"] = f"не-JSON ответ: {e}"
        entry["body_snippet"] = r.text[:400]
    return entry


def fetch_eth_market() -> dict | None:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    r.raise_for_status()
    markets = r.json().get("order_book_details", [])
    exact = [m for m in markets if str(m.get("symbol", "")).upper() == "ETH"]
    if exact:
        return exact[0]
    cand = [m for m in markets if "ETH" in str(m.get("symbol", "")).upper()]
    return cand[0] if cand else None


def run() -> int:
    t0 = time.time()
    print("=== Аккаунт 22012 (публичный GET /api/v1/account, без подписи) ===")
    acct_resp = fetch_account()
    print(f"[p5_live_lighter_account] account: status={acct_resp['status_code']}")

    account_detail = None
    if "body" in acct_resp and isinstance(acct_resp["body"], dict):
        accounts = acct_resp["body"].get("accounts", [])
        print(f"[p5_live_lighter_account] найдено аккаунтов: {len(accounts)}")
        if accounts:
            account_detail = accounts[0]
            print(f"[p5_live_lighter_account] collateral={account_detail.get('collateral')} "
                  f"available_balance={account_detail.get('available_balance')} "
                  f"cross_initial_margin_requirement={account_detail.get('cross_initial_margin_requirement')} "
                  f"cross_maintenance_margin_requirement={account_detail.get('cross_maintenance_margin_requirement')}")
            positions = account_detail.get("positions", [])
            print(f"[p5_live_lighter_account] позиций: {len(positions)}")
            for p in positions:
                print(f"[p5_live_lighter_account]   {p.get('symbol')}: sign={p.get('sign')} "
                      f"position={p.get('position')} avg_entry_price={p.get('avg_entry_price')} "
                      f"unrealized_pnl={p.get('unrealized_pnl')} liquidation_price={p.get('liquidation_price')} "
                      f"initial_margin_fraction={p.get('initial_margin_fraction')}")
        else:
            print("[p5_live_lighter_account] аккаунт 22012 НЕ найден в ответе (пустой список accounts) -- "
                  "возможно ещё не создан на этой площадке/не сделал ни одной операции")

    print("\n=== ETH-перп: рыночные лимиты (публичный GET /api/v1/orderBookDetails) ===")
    eth_market = fetch_eth_market()
    leverage_info = None
    if eth_market:
        mimf = eth_market.get("min_initial_margin_fraction")
        dimf = eth_market.get("default_initial_margin_fraction")
        mmf = eth_market.get("maintenance_margin_fraction")
        leverage_info = {
            "market_id": eth_market.get("market_id"), "symbol": eth_market.get("symbol"),
            "mark_price": eth_market.get("mark_price"),
            "min_initial_margin_fraction_raw": mimf, "default_initial_margin_fraction_raw": dimf,
            "maintenance_margin_fraction_raw": mmf,
            "max_leverage_if_mimf_is_percent": (100 / mimf) if mimf else None,
            "max_leverage_if_mimf_is_permille": (1000 / mimf) if mimf else None,
            "note": "Единицы min_initial_margin_fraction НЕ подтверждены официальной документацией -- "
                    "два правдоподобных пересчёта плеча даны РЯДОМ с сырым значением, не выбраны как факт.",
        }
        print(f"[p5_live_lighter_account] ETH market_id={eth_market.get('market_id')} "
              f"mark_price={eth_market.get('mark_price')} min_initial_margin_fraction(raw)={mimf} "
              f"default_initial_margin_fraction(raw)={dimf} maintenance_margin_fraction(raw)={mmf}")
    else:
        print("[p5_live_lighter_account] ETH-перп не найден")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "account_index": ACCOUNT_INDEX,
        "auth_used": False,
        "auth_note": "AccountApi.account() -- публичный эндпоинт (_auth_settings=[] в реальном SDK, "
                      "elliottech/lighter-python/lighter/api/account_api.py) -- подпись/ключи НЕ использовались "
                      "для этого чтения.",
        "account_raw_response": acct_resp.get("body"), "account_detail": account_detail,
        "eth_market_raw": eth_market, "eth_leverage_info": leverage_info,
        "orders_sent": False,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p5_live_lighter_account] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
