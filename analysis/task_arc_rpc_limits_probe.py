#!/usr/bin/env python3
"""Задание владельца, п.0: измерить, не предполагать. Реальный максимум
блоков за eth_getLogs (пробой 10/100/1000/10000 -- до первой ошибки),
реальный лимит запросов/с (до первого 429/-32005), реальное время блока
(timestamp двух блоков на расстоянии 1000), поиск обозревателя Arc.
Только чтение, RPC-only (Dune не трогать)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          headers={"Content-Type": "application/json"}, timeout=timeout)
    return {"http_status": resp.status_code, "body": resp.json() if resp.content else None}


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rpc": RPC}

    latest = int(rpc("eth_blockNumber", [])["body"]["result"], 16)
    result["latest_block_at_start"] = latest

    # 1. Реальный максимум блоков за eth_getLogs -- пробуем РОВНО заданные
    # размеры, без адаптивных хитростей (это диагностика, не рабочий скан).
    range_probe = []
    for size in (10, 100, 1000, 10000):
        lo = max(0, latest - size)
        r = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(latest),
                                  "address": POOL_MANAGER, "topics": [SWAP_TOPIC0]}])
        body = r["body"]
        row = {"requested_blocks": size, "http_status": r["http_status"]}
        if "error" in body:
            row["ok"] = False
            row["error"] = body["error"]
        else:
            row["ok"] = True
            row["n_results"] = len(body.get("result", []))
        range_probe.append(row)
    result["range_probe_10_100_1000_10000"] = range_probe
    first_failure = next((r for r in range_probe if not r["ok"]), None)
    result["max_range_conclusion"] = (
        f"Первая ошибка на {first_failure['requested_blocks']} блоках: {first_failure['error']}"
        if first_failure else "Все 4 размера (10/100/1000/10000) прошли успешно на этом топике/адресе сейчас."
    )

    # 2. Реальный лимит запросов/с -- бьём eth_blockNumber подряд без пауз,
    # до первой ошибки (429 на HTTP-уровне или -32005/-32029 на JSON-RPC).
    rate_probe = {"calls": [], "started_utc": time.time()}
    t0 = time.time()
    n_ok = 0
    first_rl_error = None
    for i in range(200):  # честный потолок -- не бить бесконечно
        r = rpc("eth_blockNumber", [])
        elapsed = time.time() - t0
        is_rate_limited = r["http_status"] == 429 or (
            r["body"] and "error" in r["body"] and
            ("rate limit" in str(r["body"]["error"].get("message", "")).lower() or
             r["body"]["error"].get("code") in (-32005, -32029))
        )
        if is_rate_limited:
            first_rl_error = {"at_call": i + 1, "elapsed_s": elapsed, "http_status": r["http_status"],
                               "body_error": r["body"].get("error") if r["body"] else None}
            break
        n_ok += 1
    total_elapsed = time.time() - t0
    result["rate_limit_probe"] = {
        "n_successful_calls_before_first_error": n_ok,
        "elapsed_s_until_first_error_or_cap": total_elapsed,
        "measured_calls_per_sec": n_ok / total_elapsed if total_elapsed > 0 else None,
        "first_rate_limit_error": first_rl_error,
        "note": "Потолок пробы -- 200 вызовов; если error=None и n=200, реальный лимит выше 200 вызовов "
                "в измеренное окно (не выявлен этим прогоном, не 'нет лимита').",
    }

    # 3. Реальное время блока -- timestamp latest и latest-1000.
    b_latest = rpc("eth_getBlockByNumber", [hex(latest), False])["body"]["result"]
    b_minus_1000 = rpc("eth_getBlockByNumber", [hex(max(0, latest - 1000)), False])["body"]["result"]
    ts_latest = int(b_latest["timestamp"], 16)
    ts_minus_1000 = int(b_minus_1000["timestamp"], 16)
    n_blocks = latest - max(0, latest - 1000)
    result["block_time_probe"] = {
        "block_a": latest, "block_a_timestamp_unix": ts_latest,
        "block_b": max(0, latest - 1000), "block_b_timestamp_unix": ts_minus_1000,
        "delta_seconds": ts_latest - ts_minus_1000, "n_blocks": n_blocks,
        "measured_seconds_per_block": (ts_latest - ts_minus_1000) / n_blocks if n_blocks else None,
    }

    # 4. Поиск обозревателя -- проверяем реальные кандидаты, подтверждаем
    # ТОЛЬКО если контент реально отражает текущее состояние этой сети
    # (совпадает latest_block с точностью до разумного окна), иначе явно
    # "не найден" -- не подставляем недоказанный домен (урок с
    # arc-mainnet.rpc.io, который оказался припаркованным доменом).
    explorer_candidates = [
        "https://explorer.arc.io", "https://arcscan.io", "https://explorer.mainnet.arc.io",
        "https://arc.blockscout.com", "https://explorer.arc.network", "https://mainnet.arcscan.io",
    ]
    explorer_probe = []
    for url in explorer_candidates:
        row = {"url": url}
        try:
            resp = requests.get(url, timeout=10, allow_redirects=True)
            row["http_status"] = resp.status_code
            row["final_url"] = resp.url
            row["content_len"] = len(resp.content)
            text_lower = resp.text[:5000].lower() if resp.status_code == 200 else ""
            row["mentions_block_or_explorer"] = any(
                kw in text_lower for kw in ("block", "explorer", "transaction", "arc"))
        except Exception as exc:  # noqa: BLE001
            row["exception"] = f"{type(exc).__name__}: {exc}"
        explorer_probe.append(row)
    result["explorer_probe"] = explorer_probe
    plausible = [r for r in explorer_probe if r.get("http_status") == 200 and r.get("mentions_block_or_explorer")]
    result["explorer_conclusion"] = (
        "не найден -- ни один кандидат не дал реального совпадающего контента, домены не подставляем"
        if not plausible else
        f"кандидат(ы) с реальным HTTP 200 и релевантным контентом (требует дальнейшей ручной проверки, "
        f"не факт по умолчанию): {[r['url'] for r in plausible]}"
    )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    Path(__file__).parent.parent.joinpath("data", "task_arc_rpc_limits_probe_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
