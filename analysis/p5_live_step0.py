#!/usr/bin/env python3
"""P5 LIVE — Шаг 0 (владелец, 2026-09-03): "живой бот P5... Реальные
деньги... Шаг 0 — проверка перед деньгами. Прочитай фактические
балансы кошелька (ETH, USDG) и баланс маржи на Lighter. Доложи цифры и
остановись, если чего-то не хватает. Подтверди, что ETH-перп доступен,
и сними его спред и глубину."

ЭТОТ скрипт -- ТОЛЬКО чтение, ключ (PRIVATE_KEY_NOX) используется
ИСКЛЮЧИТЕЛЬНО чтобы вывести из него публичный адрес (Account.from_key)
и свериться с ожидаемым кошельком -- тот же приём, что уже проверен в
analysis/sc1_launcher.py (send_one(), строка ~267) -- НИКОГДА не
печатается сам ключ, транзакций НЕТ.

Часть "баланс маржи на Lighter" НЕ реализована в этом прогоне --
требует аутентифицированного API Lighter (не публичный REST), а
реальная схема подписи запросов НЕ проверена в этом репозитории (см.
docstring analysis/p4_lighter_markets.py: "никакого SDK-пакета
lighter-python в этом репозитории на самом деле [не установлено]" --
она использовала только ПУБЛИЧНЫЕ эндпоинты). Секреты
LIGHTER_API_KEY_PUBLIC/LIGHTER_API_KEY_PRIVATE тоже пока не заданы
(отдельный блокер, требует действия владельца — эта сессия не имеет
инструмента для установки GitHub Secrets, см. докладную часть в чате).
Часть "ETH-перп доступен, спред и глубина" -- ПУБЛИЧНЫЙ Lighter REST
(та же связка, что analysis/p4_lighter_markets.py/mm_p5_setup.py),
ключа не требует, выполнена здесь полностью.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
from eth_account import Account  # noqa: E402

from alchemy_fallback import _rpc_call, topic0  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_live_step0_result.json")
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
USDG_DECIMALS = 6
P5_POOL = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca".lower()
LIGHTER_API_BASE = "https://robinhoodchain.lighter.xyz"
GAS_RESERVE_USD = 10.0

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n


def _eth_call(to: str, data: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"[p5_live_step0]   eth_call {to} {data[:10]} не удался: {e}")
        return None


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def check_key() -> dict:
    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        return {"key_present": False, "verdict": "PRIVATE_KEY_NOX не задан в окружении job'а -- СТОП"}
    try:
        account = Account.from_key(priv_hex)
    except Exception as e:  # noqa: BLE001
        return {"key_present": True, "verdict": f"PRIVATE_KEY_NOX не парсится как приватный ключ: {e} -- СТОП"}
    matches = account.address.lower() == WALLET.lower()
    return {"key_present": True, "derived_address": account.address, "expected_wallet": WALLET,
            "matches": matches,
            "verdict": "OK -- ключ соответствует ожидаемому кошельку" if matches else
                       "РАСХОЖДЕНИЕ -- ключ выводит ДРУГОЙ адрес, не тот, что дан владельцем -- СТОП"}


def check_wallet_balances() -> dict:
    eth_balance_raw = None
    try:
        _count()
        eth_balance_raw = int(_rpc_call("eth_getBalance", [WALLET, "latest"]), 16)
    except Exception as e:  # noqa: BLE001
        print(f"[p5_live_step0] eth_getBalance не удался: {e}")

    # calldata = selector(balanceOf(address)) + адрес, дополненный нулями до 32 байт
    balance_of_calldata = "0x" + _selector("balanceOf(address)")[2:] + WALLET[2:].rjust(64, "0").lower()
    usdg_raw_hex = _eth_call(USDG, balance_of_calldata)
    usdg_raw = int(usdg_raw_hex, 16) if usdg_raw_hex else None

    return {
        "eth_balance_raw_wei": eth_balance_raw,
        "eth_balance_human": eth_balance_raw / 1e18 if eth_balance_raw is not None else None,
        "usdg_balance_raw": usdg_raw,
        "usdg_balance_human": usdg_raw / (10 ** USDG_DECIMALS) if usdg_raw is not None else None,
    }


def find_eth_perp_market() -> dict | None:
    try:
        resp = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
        resp.raise_for_status()
        markets = resp.json().get("order_book_details", [])
    except Exception as e:  # noqa: BLE001
        print(f"[p5_live_step0] orderBookDetails недоступен на {LIGHTER_API_BASE}: {e}")
        return None
    exact = [m for m in markets if str(m.get("symbol", "")).upper() == "ETH"]
    if exact:
        return exact[0]
    cand = [m for m in markets if "ETH" in str(m.get("symbol", "")).upper()]
    return cand[0] if cand else None


def fetch_eth_perp_depth(market_id: int, mid: float) -> dict:
    try:
        r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookOrders",
                          params={"market_id": market_id, "limit": 200}, timeout=20)
        r.raise_for_status()
        body = r.json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    bids = sorted(body.get("bids", []), key=lambda o: float(o["price"]), reverse=True)
    asks = sorted(body.get("asks", []), key=lambda o: float(o["price"]))
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    spread_pct = (best_ask - best_bid) / mid * 100 if (best_bid and best_ask and mid) else None

    def depth_usd(orders, pct, is_bid):
        bound = mid * (1 - pct) if is_bid else mid * (1 + pct)
        total = 0.0
        for o in orders:
            price = float(o["price"])
            size = float(o.get("remaining_base_amount", o.get("initial_base_amount", 0)))
            within = price >= bound if is_bid else price <= bound
            if within:
                total += price * size
        return total

    return {
        "best_bid": best_bid, "best_ask": best_ask, "mid": mid, "spread_pct": spread_pct,
        "n_bids": len(bids), "n_asks": len(asks),
        "bid_depth_usd_0.5pct": depth_usd(bids, 0.005, True),
        "ask_depth_usd_0.5pct": depth_usd(asks, 0.005, False),
    }


def check_lighter_margin() -> dict:
    pub = os.environ.get("LIGHTER_API_KEY_PUBLIC", "")
    priv = os.environ.get("LIGHTER_API_KEY_PRIVATE", "")
    if not pub or not priv:
        return {"configured": False,
                "verdict": "LIGHTER_API_KEY_PUBLIC/LIGHTER_API_KEY_PRIVATE не заданы -- проверка маржи "
                           "НЕ выполнена, требуется секрет + верификация реальной схемы подписи Lighter "
                           "(не публичный REST) -- см. докладную часть, не додумано."}
    return {"configured": True,
            "verdict": "Секреты заданы, но аутентифицированный вызов ЕЩЁ НЕ РЕАЛИЗОВАН в этом прогоне -- "
                       "схема подписи Lighter требует проверки по реальному источнику (elliottech/lighter-python), "
                       "не догадки, до реализации."}


def run() -> int:
    t0 = time.time()
    key_check = check_key()
    print(f"[p5_live_step0] Ключ: {key_check['verdict']}")

    balances = check_wallet_balances()
    print(f"[p5_live_step0] ETH: {balances['eth_balance_human']}, USDG: {balances['usdg_balance_human']}")

    eth_market = find_eth_perp_market()
    perp_info = None
    if eth_market:
        mid = float(eth_market.get("mark_price") or 0)
        perp_info = fetch_eth_perp_depth(eth_market["market_id"], mid)
        perp_info["market_id"] = eth_market["market_id"]
        perp_info["symbol"] = eth_market.get("symbol")
        print(f"[p5_live_step0] ETH-перп на Lighter: market_id={eth_market['market_id']} mid={mid} "
              f"spread%={perp_info.get('spread_pct')}")
    else:
        print("[p5_live_step0] ETH-перп на Lighter НЕ найден через публичный API")

    margin_check = check_lighter_margin()
    print(f"[p5_live_step0] Маржа Lighter: {margin_check['verdict']}")

    blockers = []
    if not key_check.get("matches"):
        blockers.append("PRIVATE_KEY_NOX не подтверждён/не совпадает")
    if balances.get("eth_balance_human") is None or balances.get("usdg_balance_human") is None:
        blockers.append("не удалось прочитать баланс кошелька")
    if eth_market is None:
        blockers.append("ETH-перп на Lighter не найден")
    if not margin_check.get("configured"):
        blockers.append("маржа Lighter не проверена (нет секретов/реализации)")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wallet": WALLET, "key_check": key_check, "wallet_balances": balances,
        "eth_perp_market": eth_market, "eth_perp_depth": perp_info, "lighter_margin_check": margin_check,
        "gas_reserve_usd": GAS_RESERVE_USD,
        "blockers": blockers,
        "ready_for_step1": len(blockers) == 0,
        "requests_used": _request_count, "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p5_live_step0] записано {OUT_PATH}")
    print(f"[p5_live_step0] БЛОКЕРЫ: {blockers if blockers else 'нет'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
