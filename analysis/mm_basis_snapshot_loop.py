#!/usr/bin/env python3
"""Владелец, 2026-09-03: "Базис — одновременный снимок: для 5 тикеров
одномоментно снять цену пула и марк перпа Lighter, посчитать спред в
процентах. Повторить 20 раз с интервалом 5 минут в разные режимы рынка
(открытый/закрытый). Нужно понять, реальны ли 3% по NVDA."

Важное расхождение с буквальной формулировкой -- ЧЕСТНО, не сглажено:
на момент запуска (2026-09-03 10:29 UTC, четверг) NYSE закрыт, откроется
~13:30 UTC (regular ET 09:30 = 13:30 UTC при EDT) -- это ~3 часа вперёд.
20 повторов по 5 минут = 100 минут -- этого НЕ хватает, чтобы застать
оба режима (regular откроется позже). Поэтому цикл не останавливается
ровно на 20 -- продолжает, пока не наберётся (а) >=20 снимков И
(б) хотя бы один снимок в 'regular' (открыт) И хотя бы один в
одном из закрытых режимов (pre/after/weekend/holiday/overnight) --
т.е. буквально то, что запрошено ("20 раз" + "в разные режимы"),
просто не за ровно 100 минут. Жёсткий защитный потолок по времени --
см. MAX_WALLCLOCK_S -- на случай, если что-то пойдёт не так.

ВНИМАНИЕ по времени выполнения: это НЕ разовый быстрый скрипт -- job
рассчитан на несколько часов реального времени (в основном простой,
time.sleep между снимками, RPC-нагрузка минимальна: ~15-20 вызовов на
снимок х ~40-50 снимков к моменту остановки ~ 600-1000 запросов
суммарно, растянуто на часы) -- явное расширение общего 90-минутного
правила для ЭТОЙ конкретной, явно запрошенной владельцем задачи
мониторинга через смену режима рынка (правило про 90 минут -- про
тяжёлые прогоны по логам, не про специально долгий, но лёгкий
мониторинг). Промежуточные коммиты каждые 4 снимка (~20 минут) --
прогресс не теряется при сбое.

Один снимок = fresh eth_call на v3/v4-пул каждого из 5 тикеров (НЕ из
закэшированного data/p3_guard_cache/mm_pool_verify_result.json --
тот статичен, замер 09:51:42Z, использовать его как "текущую" цену
для повторного снимка было бы неверно) + один live-запрос к Lighter
orderBookDetails (все рынки одним вызовом).

Только чтение, ключ не используется, транзакций нет.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

from alchemy_fallback import _rpc_call, topic0  # noqa: E402
from mm_common import CLOSED_REGIMES, classify_regime  # noqa: E402
from mm_liquidity_prefilter import read_v3_pool, read_v4_pool  # noqa: E402
from mm_p5_setup import sqrt_price_to_usd  # noqa: E402

JSONL_PATH = Path("data/p3_guard_cache/mm_basis_snapshots.jsonl")
SUMMARY_PATH = Path("data/p3_guard_cache/mm_basis_snapshots_summary.json")

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
USDG_DECIMALS = 6
STOCK_DECIMALS = 18  # подтверждено для всех 5 тикеров, data/p3_guard_cache/mm_p5_setup_result.json
LIGHTER_API_BASE = "https://mainnet.zklighter.elliot.ai"

# Идентификаторы -- реальные, из data/p3_guard_cache/mm_pool_verify_result.json (2026-09-03, run 33741048946)
TICKERS = {
    "NVDA": {"version": "v3", "pool_address": "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3",
              "currency0": USDG, "currency1": "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec",
              "lighter_market_id": 110},
    "QQQ": {"version": "v3", "pool_address": "0xd60a5d14db690b7afad71f76b108071d7175597d",
             "currency0": USDG, "currency1": "0xd5f3879160bc7c32ebb4dc785f8a4f505888de68",
             "lighter_market_id": 129},
    "GME": {"version": "v3", "pool_address": "0xe2b46c905e12ab8e2f864e4821a4325884c1b126",
             "currency0": "0x1b0e319c6a659f002271b69db8a7df2f911c153e", "currency1": USDG,
             "lighter_market_id": 176},
    "SPY": {"version": "v4", "pool_id": "0xfe2a80bb5618fd14984b92ca6d45bf5ba67443ddb1435e28b2e48df2fc1526cd",
             "currency0": "0x117cc2133c37b721f49de2a7a74833232b3b4c0c", "currency1": USDG,
             "lighter_market_id": 128},
    "MSTR": {"version": "v4", "pool_id": "0x319bac87e616a89e241c10aeb8afd4892a852cdd8b373cd9765ecddc40b87cfe",
              "currency0": USDG, "currency1": "0xec262a75e413fafd0df80480274532c79d42da09",
              "lighter_market_id": 122},
}

TARGET_SNAPSHOTS = 20
MAX_SNAPSHOTS_HARD_CAP = 65  # защитный потолок по числу снимков (см. MAX_WALLCLOCK_S ниже)
# GitHub Actions: жёсткий платформенный потолок на выполнение одного job --
# 6 часов (hosted runners), не настраиваемый выше на этом плане. NYSE
# открывается ~13:30 UTC (при старте в 10:29 UTC 2026-09-03 -- через ~3ч),
# закрывается ~20:00 UTC -- 5ч45м с запасом гарантированно захватывают и
# закрытый, и открытый режим при старте с утра по UTC.
MAX_WALLCLOCK_S = 5 * 3600 + 45 * 60  # 5ч45м -- запас под платформенный потолок в 6ч
INTERVAL_S = 5 * 60
COMMIT_EVERY = 4

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n


def _eth_call(to: str, data: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"[mm_basis_snapshot_loop]   eth_call {to} {data[:10]} не удался: {e}")
        return None


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def take_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    regime = classify_regime(now)
    is_open = regime == "regular"

    lighter_markets = {}
    try:
        resp = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
        resp.raise_for_status()
        for m in resp.json().get("order_book_details", []):
            lighter_markets[m.get("market_id")] = m
    except Exception as e:  # noqa: BLE001
        print(f"[mm_basis_snapshot_loop] Lighter orderBookDetails недоступен: {e}")

    entries = {}
    for sym, cfg in TICKERS.items():
        if cfg["version"] == "v3":
            state = read_v3_pool(cfg["pool_address"])
        else:
            state = read_v4_pool(POOL_MANAGER, cfg["pool_id"])
        if not state:
            entries[sym] = {"error": "не удалось прочитать состояние пула"}
            continue
        sqrt_price, liquidity = state
        stock_is_token1 = cfg["currency1"].lower() != USDG
        dec0 = USDG_DECIMALS if cfg["currency0"].lower() == USDG else STOCK_DECIMALS
        dec1 = USDG_DECIMALS if cfg["currency1"].lower() == USDG else STOCK_DECIMALS
        pool_price_usd = sqrt_price_to_usd(sqrt_price, dec0, dec1, stock_is_token1)
        market = lighter_markets.get(cfg["lighter_market_id"])
        mark_price = float(market["mark_price"]) if market and market.get("mark_price") is not None else None
        entry = {"pool_price_usd": pool_price_usd, "lighter_mark_price_usd": mark_price,
                  "liquidity_raw": liquidity}
        if mark_price:
            entry["basis_usd"] = pool_price_usd - mark_price
            entry["basis_pct"] = (pool_price_usd - mark_price) / mark_price * 100
        entries[sym] = entry

    return {"ts_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "regime": regime, "is_open": is_open,
            "basis": entries, "requests_used_cumulative": _request_count}


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True)


def _commit_and_push(msg: str) -> None:
    subprocess.run(["git", "add", str(JSONL_PATH), str(SUMMARY_PATH)], check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return
    _git("commit", "-m", msg)
    for i in range(5):
        r = subprocess.run(["git", "push"])
        if r.returncode == 0:
            return
        print(f"[mm_basis_snapshot_loop] push rejected, попытка {i+1}/5 -- pull --rebase и повтор")
        subprocess.run(["git", "pull", "--rebase"], check=False)
        time.sleep(3)
    print("[mm_basis_snapshot_loop] не удалось запушить после 5 попыток", file=sys.stderr)


def run() -> int:
    t0 = time.time()
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if JSONL_PATH.exists():
        for line in JSONL_PATH.read_text().splitlines():
            if line.strip():
                existing.append(json.loads(line))
    print(f"[mm_basis_snapshot_loop] найдено {len(existing)} уже сохранённых снимков")

    regimes_seen = {e["regime"] for e in existing}
    n = len(existing)
    i_since_commit = 0

    while True:
        have_open = any(r == "regular" for r in regimes_seen)
        have_closed = any(r in CLOSED_REGIMES for r in regimes_seen)
        if n >= TARGET_SNAPSHOTS and have_open and have_closed:
            print(f"[mm_basis_snapshot_loop] цель достигнута: {n} снимков, режимы {sorted(regimes_seen)} -- стоп")
            break
        if n >= MAX_SNAPSHOTS_HARD_CAP:
            print(f"[mm_basis_snapshot_loop] защитный потолок по числу снимков ({MAX_SNAPSHOTS_HARD_CAP}) -- стоп, "
                  f"режимы набраны не полностью: {sorted(regimes_seen)}")
            break
        if time.time() - t0 >= MAX_WALLCLOCK_S:
            print(f"[mm_basis_snapshot_loop] защитный потолок по времени ({MAX_WALLCLOCK_S}с) -- стоп")
            break

        snap = take_snapshot()
        existing.append(snap)
        regimes_seen.add(snap["regime"])
        n += 1
        i_since_commit += 1
        print(f"[mm_basis_snapshot_loop] снимок #{n} {snap['ts_utc']} режим={snap['regime']}: " +
              ", ".join(f"{sym}={e.get('basis_pct', 'н/д')}%" for sym, e in snap["basis"].items()))

        with JSONL_PATH.open("a") as f:
            f.write(json.dumps(snap, default=str, ensure_ascii=False) + "\n")
        summary = {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_snapshots": n, "regimes_seen": sorted(regimes_seen),
            "target_reached": n >= TARGET_SNAPSHOTS and "regular" in regimes_seen and bool(regimes_seen & CLOSED_REGIMES),
            "requests_used_total": _request_count, "runtime_s": time.time() - t0,
        }
        SUMMARY_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

        if i_since_commit >= COMMIT_EVERY:
            _commit_and_push(f"MM: базис-снимки {n}/{TARGET_SNAPSHOTS}+, режимы {sorted(regimes_seen)} [automated]")
            i_since_commit = 0

        have_open = any(r == "regular" for r in regimes_seen)
        have_closed = any(r in CLOSED_REGIMES for r in regimes_seen)
        if n >= TARGET_SNAPSHOTS and have_open and have_closed:
            break
        time.sleep(INTERVAL_S)

    _commit_and_push(f"MM: базис-снимки финал -- {n} снимков, режимы {sorted(regimes_seen)} [automated]")
    print(f"[mm_basis_snapshot_loop] завершено: {n} снимков, режимы {sorted(regimes_seen)}, "
          f"{_request_count} запросов, {time.time()-t0:.0f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
