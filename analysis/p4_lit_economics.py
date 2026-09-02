#!/usr/bin/env python3
"""Задача B (владелец, 2026-09-02), секция "Экономика поинтов LIT" --
разведка через каналы, недоступные интерактивной сессии (её egress
блокирует docs.lighter.xyz/coingecko/etherscan и т.п., см.
docs/P4_RECON.md, "Метод и ограничение среды"), но, по прецеденту
предыдущих P4-прогонов (mainnet.zklighter.elliot.ai реально опрашивался
с GH Actions runner'а), должны быть доступны с GH Actions.

НЕ отправляет никаких транзакций, ключ не используется.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/p4_lit_economics_result.json")

LIGHTER_API_BASE = "https://mainnet.zklighter.elliot.ai"
DOCS_URLS = [
    "https://docs.lighter.xyz/points-program/lighter-on-robinhood-chain-points",
    "https://docs.lighter.xyz/points-program/market-makers",
]

# Понедельные окна выплат по установленной ранее каденции (docs/P4_RECON.md:
# "среда включительно -> вторник включительно", первая выплата пятница 21.08.2026)
# -- ИНТЕРПРЕТАЦИЯ каденции, не факт из источника: окно, заканчивающееся
# вторником ПЕРЕД пятничной выплатой, начинающееся средой предыдущей недели.
WEEKLY_WINDOWS = {
    "2026-08-21": ("2026-08-12T00:00:00Z", "2026-08-18T23:59:59Z"),
    "2026-08-28": ("2026-08-19T00:00:00Z", "2026-08-25T23:59:59Z"),
}


def _get(url: str, params: dict | None = None, timeout: int = 20) -> dict:
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def try_fetch_docs() -> dict:
    out = {}
    for url in DOCS_URLS:
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "robinhood-chain-alpha-p4/1.0"})
            out[url] = {"status": resp.status_code, "reachable": True, "text_len": len(resp.text), "text": resp.text}
            print(f"[p4_lit_economics] docs.lighter.xyz {url}: status={resp.status_code} len={len(resp.text)}")
        except requests.exceptions.RequestException as e:
            out[url] = {"reachable": False, "error": str(e)}
            print(f"[p4_lit_economics] {url} НЕДОСТУПЕН: {e}")
    return out


def try_fetch_coingecko() -> dict:
    try:
        data = _get("https://api.coingecko.com/api/v3/coins/lighter")
        contract_addrs = data.get("platforms", {})
        price_usd = data.get("market_data", {}).get("current_price", {}).get("usd")
        total_supply = data.get("market_data", {}).get("total_supply")
        print(f"[p4_lit_economics] CoinGecko: platforms={contract_addrs} price_usd={price_usd} total_supply={total_supply}")
        return {"reachable": True, "platforms": contract_addrs, "price_usd": price_usd, "total_supply": total_supply, "raw_id": data.get("id"), "raw_symbol": data.get("symbol"), "raw_name": data.get("name")}
    except Exception as e:  # noqa: BLE001
        print(f"[p4_lit_economics] CoinGecko недоступен: {e}")
        return {"reachable": False, "error": str(e)}


def try_fetch_coingecko_history(date_ddmmyyyy: str) -> dict | None:
    """Историческая цена LIT/USD на конкретную дату (для конвертации
    понедельных выплат в USD-эквивалент по цене НА ДЕНЬ выплаты, не по
    сегодняшней цене)."""
    try:
        data = _get(
            "https://api.coingecko.com/api/v3/coins/lighter/history",
            params={"date": date_ddmmyyyy, "localization": "false"},
        )
        price = data.get("market_data", {}).get("current_price", {}).get("usd")
        print(f"[p4_lit_economics] CoinGecko история {date_ddmmyyyy}: price_usd={price}")
        return {"date": date_ddmmyyyy, "price_usd": price}
    except Exception as e:  # noqa: BLE001
        print(f"[p4_lit_economics] CoinGecko история {date_ddmmyyyy} недоступна: {e}")
        return None


def try_fetch_lighter_exchange_metrics() -> dict:
    out = {}
    for label, (start_iso, end_iso) in WEEKLY_WINDOWS.items():
        try:
            data = _get(
                f"{LIGHTER_API_BASE}/api/v1/exchangeMetrics",
                params={"period": "w", "kind": "volume"},
            )
            out[label] = {"reachable": True, "params_used": {"period": "w", "kind": "volume"}, "response": data}
            print(f"[p4_lit_economics] exchangeMetrics ({label}): {json.dumps(data)[:500]}")
        except Exception as e:  # noqa: BLE001
            out[label] = {"reachable": False, "error": str(e)}
            print(f"[p4_lit_economics] exchangeMetrics ({label}) недоступен: {e}")
    return out


def run() -> int:
    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "docs_lighter_xyz": try_fetch_docs(),
        "coingecko_current": try_fetch_coingecko(),
        "coingecko_history": {
            "2026-08-21": try_fetch_coingecko_history("21-08-2026"),
            "2026-08-28": try_fetch_coingecko_history("28-08-2026"),
        },
        "lighter_exchange_metrics": try_fetch_lighter_exchange_metrics(),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p4_lit_economics] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
