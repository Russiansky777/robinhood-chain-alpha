#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo -- собрать историю по цепи, БЕЗ Dune.
Трассы в Dune пока не покупаем -- сначала бесплатный путь (Alchemy
alchemy_getAssetTransfers, уже подтверждён рабочим на ogle в прошлом
раунде: 100+ реальных переводов, живая активность до последнего часа).

Правило в паспорт (владелец, этот раунд): "перед тратой на данные
проверять, не упирались ли мы уже в это место раньше" -- ЭТА задача
САМА является примером: 06.09 уже установили, что торговля мем-
терминалов не отражается в decoded Uniswap-таблицах и нужны трассы;
17.09 (field_search) пришли в ту же точку ВТОРОЙ раз, потратив кредиты
Dune заново. Отмечено в PROJECT_STATE.md.

Шаг 1 из задания: полная (не 7-дневная) история ERC-20-переводов по
всем 9 адресам через alchemy_getAssetTransfers -- ОБЕ стороны
(fromAddress/toAddress), с пагинацией (pageKey), честным лимитом на
объём (чтобы не "долбить" при упоре в лимиты) и БЕЗ печати URL/ключа
Alchemy в лог (владелец: старый ключ утёк в лог 12.09, ротация не
подтверждена -- используем текущий настроенный секрет как есть, не
печатаем его нигде)."""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

from config import CONFIG  # noqa: E402
from alchemy_fallback import _rpc_call as rpc_call  # noqa: E402

OUT_JSON = Path("data/fomo_9wallets_alchemy_full_history_result.json")
OUT_CSV = Path("data/fomo_9wallets_alchemy_raw_transfers.csv")

WALLETS = {
    "SolSwizzle": "0x44fbe0006661d6d17188f1f6d42b32b5577179f7",
    "ether_monk": "0x2408ce75d217e3a70d6ca370c78c1b34d706f5a0",
    "remusofmars": "0x8ab8c0843d9738885d6273dfe3de86c56eea364c",
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}

MAX_COUNT_HEX = "0x64"  # 100/страница -- та же величина, что уже работала на ogle
MAX_PAGES_PER_DIRECTION = 30  # честный потолок -- 30 * 100 = 3000 переводов/направление/кошелёк
REQUEST_INTERVAL_S = 0.35  # мягкий троттлинг -- "не долбить" при лимитах
MAX_CONSECUTIVE_ERRORS = 3  # после стольких неудачных попыток подряд -- честная остановка, не бесконечный ретрай
BACKOFF_BASE_S = 3.0

_last_call_ts = 0.0


def _alchemy_url() -> str | None:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url
    if CONFIG.alchemy_api_key:
        return f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    return None


def _throttled_post(url: str, payload: dict) -> requests.Response:
    global _last_call_ts
    wait = _last_call_ts + REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()
    return requests.post(url, json=payload, timeout=30)


def fetch_direction(url: str, wallet_addr: str, direction_key: str) -> dict:
    """direction_key -- 'fromAddress' или 'toAddress'. Возвращает
    {"transfers": [...], "n_pages": N, "capped": bool, "consecutive_error_stop": bool,
     "last_error": str|None}."""
    transfers: list[dict] = []
    page_key = None
    n_pages = 0
    consecutive_errors = 0
    capped = False
    stop_reason = None

    while True:
        if n_pages >= MAX_PAGES_PER_DIRECTION:
            capped = True
            stop_reason = f"честный потолок {MAX_PAGES_PER_DIRECTION} страниц ({MAX_PAGES_PER_DIRECTION * 100} переводов) достигнут"
            break
        params: dict = {
            direction_key: wallet_addr,
            "category": ["erc20"],
            "withMetadata": True,
            "maxCount": MAX_COUNT_HEX,
            "order": "asc",
        }
        if page_key:
            params["pageKey"] = page_key
        payload = {"jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers", "params": [params]}
        try:
            resp = _throttled_post(url, payload)
        except Exception as exc:  # noqa: BLE001
            consecutive_errors += 1
            stop_reason = f"сетевое исключение: {type(exc).__name__}"
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                break
            time.sleep(BACKOFF_BASE_S * consecutive_errors)
            continue

        if resp.status_code == 429:
            consecutive_errors += 1
            stop_reason = "HTTP 429 (рейт-лимит Alchemy)"
            print(f"[alchemy_history] {wallet_addr[:10]}.. {direction_key}: 429, попытка {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                break
            time.sleep(BACKOFF_BASE_S * (2 ** consecutive_errors))
            continue
        if resp.status_code != 200:
            consecutive_errors += 1
            stop_reason = f"HTTP {resp.status_code}"
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                break
            time.sleep(BACKOFF_BASE_S * consecutive_errors)
            continue

        try:
            body = resp.json()
        except ValueError:
            consecutive_errors += 1
            stop_reason = "не-JSON ответ"
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                break
            continue

        if "error" in body:
            consecutive_errors += 1
            stop_reason = f"JSON-RPC ошибка: {str(body['error'])[:200]}"
            print(f"[alchemy_history] {wallet_addr[:10]}.. {direction_key}: {stop_reason}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                break
            time.sleep(BACKOFF_BASE_S * consecutive_errors)
            continue

        consecutive_errors = 0
        result = body.get("result", {})
        page_transfers = result.get("transfers", [])
        transfers.extend(page_transfers)
        n_pages += 1
        page_key = result.get("pageKey")
        if not page_key:
            stop_reason = None
            break

    return {
        "transfers": transfers, "n_pages": n_pages, "capped": capped,
        "stopped_early": stop_reason is not None and not capped,
        "stop_reason": stop_reason,
    }


def run() -> int:
    url = _alchemy_url()
    if not url:
        print("[alchemy_history] СТОП: ни ALCHEMY_ROBINHOOD_RPC_URL, ни ALCHEMY_API_KEY не настроены -- нечего вызывать.")
        return 1

    # Проверка, что ключ реально рабочий -- ОДИН лёгкий вызов, БЕЗ печати URL/ключа
    # (владелец: старый ключ утёк в лог 12.09, ротация не подтверждена).
    try:
        current_nonce = rpc_call("eth_getTransactionCount", [WALLETS["ogle"], "latest"])
        print(f"[alchemy_history] Публичный RPC живой (контрольный eth_getTransactionCount на ogle: {int(current_nonce, 16)}).")
    except Exception as exc:  # noqa: BLE001
        print(f"[alchemy_history] ПРЕДУПРЕЖДЕНИЕ: контрольный RPC-вызов не прошёл: {exc}")

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "wallets": WALLETS, "per_wallet": {}}
    all_rows: list[dict] = []
    any_capped_or_stopped = False

    for name, addr in WALLETS.items():
        wallet_summary: dict = {"address": addr}
        for direction_key, direction_label in (("fromAddress", "out"), ("toAddress", "in")):
            res = fetch_direction(url, addr, direction_key)
            n = len(res["transfers"])
            print(f"[alchemy_history] {name} ({direction_label}): {n} переводов, {res['n_pages']} стр., "
                  f"capped={res['capped']}, stopped_early={res['stopped_early']} ({res['stop_reason']})")
            wallet_summary[direction_label] = {
                "n_transfers": n, "n_pages": res["n_pages"], "capped": res["capped"],
                "stopped_early": res["stopped_early"], "stop_reason": res["stop_reason"],
            }
            if res["capped"] or res["stopped_early"]:
                any_capped_or_stopped = True
            for t in res["transfers"]:
                raw_contract = t.get("rawContract") or {}
                metadata = t.get("metadata") or {}
                all_rows.append({
                    "wallet_name": name,
                    "wallet_address": addr,
                    "direction": direction_label,
                    "block_num_hex": t.get("blockNum"),
                    "block_num": int(t["blockNum"], 16) if t.get("blockNum") else None,
                    "block_time_utc": metadata.get("blockTimestamp"),
                    "tx_hash": t.get("hash"),
                    "unique_id": t.get("uniqueId"),
                    "from_addr": t.get("from"),
                    "to_addr": t.get("to"),
                    "asset_symbol": t.get("asset"),
                    "token_address": (raw_contract.get("address") or "").lower(),
                    "token_decimal_hex": raw_contract.get("decimal"),
                    "value_human": t.get("value"),
                })
        out["per_wallet"][name] = wallet_summary

    out["total_transfers_collected"] = len(all_rows)
    out["any_capped_or_stopped"] = any_capped_or_stopped
    out["raw_csv_path"] = str(OUT_CSV)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if all_rows:
        fieldnames = list(all_rows[0].keys())
        with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
    else:
        OUT_CSV.write_text("")

    print(f"\n[alchemy_history] ИТОГО: {out['total_transfers_collected']} переводов по 9 адресам. "
          f"any_capped_or_stopped={any_capped_or_stopped}")
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
