#!/usr/bin/env python3
"""Владелец: "почему в ошибке фигурирует B-1+3, а не B-1? Проверь
исторический eth_call на B-1 и B-100 через уже настроенные RPC проекта
(до 10 вызовов), простой известный контракт с непустым ожидаемым
ответом (WETH.decimals()==18 -- immutable, не зависит от состояния,
но всё равно идёт через eth_call с block tag -- честный тест архивной
глубины)." Без покупок/новых подписок/широких сканов -- только уже
сконфигурированные в analysis/config.py эндпоинты.

Метод: КАЖДЫЙ уже настроенный эндпоинт (public RPC, Alchemy напрямую,
Blockscout если есть ключ) вызывается НАПРЯМУЮ (не через
_post_with_fallback, чтобы точно знать, какой именно эндпоинт ответил
какой именно ошибкой -- предыдущий прогон шёл через общий фолбэк-путь,
поэтому нельзя было отличить "публичный RPC отказал" от "и Alchemy
тоже отказал")."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

# ЧЕСТНАЯ ПРАВКА после первого прогона этого же скрипта: забыл
# priming RPC_URL_PROVIDER->ALCHEMY_ROBINHOOD_RPC_URL, который делают
# ВСЕ остальные скрипты проекта -- в результате Alchemy оказался
# "не настроен" и проверился только public RPC. Ohio реально хранит
# ключ Alchemy под именем RPC_URL_PROVIDER (см. любой другой скрипт
# сессии) -- без этой строки config.CONFIG.alchemy_rpc_url пуст.
import os  # noqa: E402
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from config import CONFIG  # noqa: E402

B = 62_790_344
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
DECIMALS_SELECTOR = "0x313ce567"  # decimals() -- immutable, ожидаемый ответ ВСЕГДА 18, любой блок

CALL_COUNT = 0
RESULT: dict = {"block_hex_check": {"B_minus_1": hex(B - 1), "B_minus_100": hex(B - 100)},
                 "endpoints_configured": [], "probes": []}


def _endpoint_list() -> list[tuple[str, str, dict]]:
    """(label, url, extra_headers) -- ТОЛЬКО уже сконфигурированные в
    config.py, без новых ключей/подписок."""
    out = []
    if CONFIG.public_rpc_url:
        out.append(("public_rpc (без ключа)", CONFIG.public_rpc_url, {}))
    if CONFIG.alchemy_rpc_url:
        out.append(("alchemy (ALCHEMY_ROBINHOOD_RPC_URL, уже настроен)", CONFIG.alchemy_rpc_url, {}))
    elif CONFIG.alchemy_api_key:
        out.append(("alchemy (ALCHEMY_API_KEY, уже настроен)",
                     f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}", {}))
    if CONFIG.blockscout_api_key:
        out.append(("blockscout (BLOCKSCOUT_API_KEY, уже настроен)", CONFIG.blockscout_rpc_url,
                     {"Authorization": f"Bearer {CONFIG.blockscout_api_key}"}))
    return out


def raw_call(label: str, url: str, headers: dict, block_tag: str) -> dict:
    global CALL_COUNT
    if CALL_COUNT >= 10:
        return {"skipped": "10-call budget reached"}
    CALL_COUNT += 1
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
               "params": [{"to": WETH, "data": DECIMALS_SELECTOR}, block_tag]}
    t0 = time.time()
    try:
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json", **headers}, timeout=20)
        dt = time.time() - t0
        body = resp.json()
        row = {"endpoint": label, "block_tag": block_tag, "http_status": resp.status_code,
               "dt_s": round(dt, 3), "raw_response_body": body}
        if "result" in body and body["result"]:
            row["decoded_decimals"] = int(body["result"], 16)
            row["matches_expected_18"] = row["decoded_decimals"] == 18
        return row
    except Exception as exc:  # noqa: BLE001
        return {"endpoint": label, "block_tag": block_tag, "dt_s": round(time.time() - t0, 3),
                "exception": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    endpoints = _endpoint_list()
    RESULT["endpoints_configured"] = [e[0] for e in endpoints]

    # 0. Базовая проверка на "latest" -- живой sanity-тест каждого
    # эндпоинта (подтверждает, что сам вызов/контракт/селектор рабочий,
    # прежде чем списывать историческую ошибку на архив).
    for label, url, headers in endpoints:
        RESULT["probes"].append(raw_call(f"{label} @ latest", url, headers, "latest"))

    # 1-2. B-1 и B-100 на КАЖДОМ настроенном эндпоинте напрямую.
    for label, url, headers in endpoints:
        RESULT["probes"].append(raw_call(f"{label} @ B-1", url, headers, hex(B - 1)))
    for label, url, headers in endpoints:
        RESULT["probes"].append(raw_call(f"{label} @ B-100", url, headers, hex(B - 100)))

    RESULT["n_calls_used"] = CALL_COUNT
    out_path = Path(__file__).parent.parent / "data" / "task5_arb_archive_endpoint_probe_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(RESULT, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(RESULT, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
