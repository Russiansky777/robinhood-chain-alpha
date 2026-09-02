#!/usr/bin/env python3
"""Задача B, продолжение (владелец, 2026-09-02): "Если распределение не
опубликовано -- искать через ончейн-переводы LIT с адресов программы,
не оценивать."

Часть 1: недостающие страницы docs.lighter.xyz (формула ретейл-поинтов,
delta-neutral, множитель Robinhood Wallet, LIT-утилити) -- их URL нашлись
внутри уже скачанного HTML предыдущего прогона (nav-ссылки), сами
страницы прошлым прогоном НЕ запрашивались.

Часть 2: разведочный скан Transfer-логов контракта LIT на Ethereum
mainnet (адрес подтверждён CoinGecko `platforms.ethereum` в прошлом
прогоне: 0x232ce3bd40fcd6f80f3d55a522d03f25df784ee2) вокруг двух
пятничных выплат (21.08 и 28.08.2026) -- ищем кластеры "один
отправитель -> много получателей за короткое окно", характерные для
дистрибьютора программы. Публичный Ethereum RPC без ключа (нигде в
проекте раньше не использовался -- эта задача первая, где нужен
контрагент-чейн вне Robinhood Chain).

ВАЖНО: даже если кластер найдётся, это НЕ автоматически "именно
Robinhood-программа" -- адрес может рассылать LIT по другим причинам
(биржевые депозиты, другие вознаграждения и т.п.). Помечаем как
"кандидат, неподтверждённая атрибуция", если не находится независимое
подтверждение (например, совпадение адреса с чем-то документированным).
Ничего не оценивается/не досочиняется сверх того, что видно в логах.

Ключ не используется, транзакций нет.
"""
from __future__ import annotations

import calendar
import json
import time
from collections import defaultdict
from pathlib import Path

import requests
from Crypto.Hash import keccak

OUT_PATH = Path("data/p3_guard_cache/p4_lit_onchain_result.json")

LIT_CONTRACT = "0x232ce3bd40fcd6f80f3d55a522d03f25df784ee2"  # CoinGecko coins/lighter -> platforms.ethereum, подтверждено прошлым прогоном

ETH_RPC_ENDPOINTS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    # cloudflare-eth.com убран -- первый реальный прогон (2026-09-02)
    # получил {"code": -32603, "message": "Internal error"} на
    # eth_getLogs с этим адресом (известное ограничение публичного
    # шлюза Cloudflare -- не поддерживает getLogs с topics для
    # произвольных контрактов), фактом, не предположением.
]

DOCS_BASE = "https://docs.lighter.xyz"
EXTRA_DOCS_PATHS = [
    "/points-program",
    "/points-program/retail",
    "/about-lighter/lit-utility",
]

# Пятничные выплаты (docs/P4_RECON.md), окно скана -- 48ч вокруг всей
# пятницы UTC (точное время выплаты в течение дня не документировано),
# т.е. с четверга 00:00 по субботу 00:00 UTC.
PAYOUT_WINDOWS = {
    "2026-08-21": ("2026-08-20T00:00:00Z", "2026-08-22T00:00:00Z"),
    "2026-08-28": ("2026-08-27T00:00:00Z", "2026-08-29T00:00:00Z"),
}


def topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


TRANSFER_TOPIC0 = topic0("Transfer(address,address,uint256)")


def _rpc_call(method: str, params: list, endpoints=None) -> dict:
    endpoints = endpoints or ETH_RPC_ENDPOINTS
    last_err = None
    for url in endpoints:
        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=25,
                    headers={"User-Agent": "robinhood-chain-alpha-p4/1.0"},
                )
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise RuntimeError(f"{url} {method}: {data['error']}")
                return data["result"]
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[p4_lit_onchain] {url} {method} попытка {attempt+1}/3 не удалась: {e}")
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"все ETH RPC эндпоинты не отвечают на {method}: {last_err}")


def _iso_to_unix(iso: str) -> int:
    # calendar.timegm трактует struct_time как UTC напрямую -- не зависит
    # от TZ раннера (в отличие от time.mktime + вычитания time.timezone,
    # которое дало бы неверный результат при DST/нестандартном TZ).
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


def find_block_by_timestamp(target_ts: int) -> int:
    """Бинарный поиск номера блока Ethereum mainnet по unix-timestamp."""
    latest_hex = _rpc_call("eth_blockNumber", [])
    hi = int(latest_hex, 16)
    lo = 0
    hi_block = _rpc_call("eth_getBlockByNumber", [hex(hi), False])
    if int(hi_block["timestamp"], 16) <= target_ts:
        return hi
    while lo < hi:
        mid = (lo + hi) // 2
        blk = _rpc_call("eth_getBlockByNumber", [hex(mid), False])
        ts = int(blk["timestamp"], 16)
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def fetch_transfer_logs(from_block: int, to_block: int, chunk_size: int = 3000) -> list[dict]:
    logs = []
    lo = from_block
    n_calls = 0
    while lo <= to_block:
        hi = min(lo + chunk_size - 1, to_block)
        n_calls += 1
        try:
            result = _rpc_call("eth_getLogs", [{
                "fromBlock": hex(lo), "toBlock": hex(hi),
                "address": LIT_CONTRACT, "topics": [TRANSFER_TOPIC0],
            }])
            logs.extend(result)
        except Exception as e:  # noqa: BLE001
            print(f"[p4_lit_onchain] getLogs [{lo},{hi}] не удался: {e}")
            # Бисекция ТОЛЬКО если ошибка похожа на "диапазон слишком
            # велик" -- на систематическую ошибку (эндпоинт в принципе
            # не отдаёт getLogs с этими параметрами) бисекция до
            # 1-блочных чанков не поможет и просто умножит число
            # безнадёжных запросов -- fail fast с диагностикой вместо
            # тихого зависания.
            msg = str(e).lower()
            size_related = any(m in msg for m in ("too large", "too many", "exceed", "limit", "range"))
            if size_related and hi > lo:
                mid = (lo + hi) // 2
                logs.extend(fetch_transfer_logs(lo, mid, max(chunk_size // 2, 200)))
                logs.extend(fetch_transfer_logs(mid + 1, hi, max(chunk_size // 2, 200)))
                lo = hi + 1
                continue
            raise
        lo = hi + 1
    print(f"[p4_lit_onchain] Transfer-логи [{from_block},{to_block}]: {n_calls} вызовов eth_getLogs, {len(logs)} событий")
    return logs


def decode_transfer(log: dict) -> dict:
    from_addr = "0x" + log["topics"][1][-40:]
    to_addr = "0x" + log["topics"][2][-40:]
    amount = int(log["data"], 16)
    return {
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"],
        "from": from_addr,
        "to": to_addr,
        "amount_wei": amount,
        "amount_lit": amount / 1e18,
    }


def analyze_window(label: str, start_iso: str, end_iso: str) -> dict:
    start_ts, end_ts = _iso_to_unix(start_iso), _iso_to_unix(end_iso)
    from_block = find_block_by_timestamp(start_ts)
    to_block = find_block_by_timestamp(end_ts)
    print(f"[p4_lit_onchain] окно {label}: {start_iso}..{end_iso} -> блоки [{from_block},{to_block}] ({to_block - from_block} блоков)")
    raw_logs = fetch_transfer_logs(from_block, to_block)
    transfers = [decode_transfer(l) for l in raw_logs]

    by_sender = defaultdict(lambda: {"n_transfers": 0, "distinct_recipients": set(), "total_lit": 0.0})
    for t in transfers:
        s = by_sender[t["from"]]
        s["n_transfers"] += 1
        s["distinct_recipients"].add(t["to"])
        s["total_lit"] += t["amount_lit"]

    candidates = sorted(
        (
            {
                "address": addr,
                "n_transfers": v["n_transfers"],
                "distinct_recipients": len(v["distinct_recipients"]),
                "total_lit": v["total_lit"],
            }
            for addr, v in by_sender.items()
            if len(v["distinct_recipients"]) >= 5  # "один -> много" паттерн дистрибьютора
        ),
        key=lambda x: -x["distinct_recipients"],
    )[:15]

    return {
        "window_start": start_iso,
        "window_end": end_iso,
        "from_block": from_block,
        "to_block": to_block,
        "n_transfer_events": len(transfers),
        "n_unique_senders": len(by_sender),
        "distributor_candidates_top15_by_distinct_recipients": candidates,
    }


def fetch_extra_docs() -> dict:
    out = {}
    for path in EXTRA_DOCS_PATHS:
        url = DOCS_BASE + path
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "robinhood-chain-alpha-p4/1.0"})
            out[url] = {"status": resp.status_code, "reachable": True, "text_len": len(resp.text), "text": resp.text}
            print(f"[p4_lit_onchain] {url}: status={resp.status_code} len={len(resp.text)}")
        except requests.exceptions.RequestException as e:
            out[url] = {"reachable": False, "error": str(e)}
            print(f"[p4_lit_onchain] {url} НЕДОСТУПЕН: {e}")
    return out


def run() -> int:
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    print("=== часть 1: недостающие страницы docs.lighter.xyz ===")
    result["extra_docs"] = fetch_extra_docs()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))  # промежуточная запись

    print("\n=== часть 2: скан Transfer-логов LIT на Ethereum mainnet ===")
    try:
        eth_block = _rpc_call("eth_blockNumber", [])
        print(f"[p4_lit_onchain] Ethereum mainnet RPC доступен, latest block = {int(eth_block, 16)}")
        onchain = {}
        for label, (start_iso, end_iso) in PAYOUT_WINDOWS.items():
            try:
                onchain[label] = analyze_window(label, start_iso, end_iso)
            except Exception as e:  # noqa: BLE001 -- одно окно не должно топить второе
                print(f"[p4_lit_onchain] окно {label} НЕ удалось: {e}")
                onchain[label] = {"reachable": False, "error": str(e)}
            OUT_PATH.write_text(json.dumps({**result, "onchain_lit_transfers": onchain}, indent=2, default=str, ensure_ascii=False))
        result["onchain_lit_transfers"] = onchain
    except Exception as e:  # noqa: BLE001
        print(f"[p4_lit_onchain] Ethereum mainnet RPC НЕДОСТУПЕН: {e}")
        result["onchain_lit_transfers"] = {"reachable": False, "error": str(e)}

    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p4_lit_onchain] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
