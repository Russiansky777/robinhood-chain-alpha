#!/usr/bin/env python3
"""Владелец (2026-09-17): сборщик 1, снимок каждые 15 минут. Читает
`data/trend_collector/pool_universe.json` (собран `trend_collector_
select_pools.py`, список ТОЛЬКО РАСТЁТ -- мёртвые пулы не выкидывать)
и снимает по каждому: feeGrowthGlobal0X128/feeGrowthGlobal1X128,
liquidity, sqrtPriceX96, номер блока, время -- через extsload для V4
(один вызов, PoolManager.Pool.State: slot0/feeGrowthGlobal0/
feeGrowthGlobal1/liquidity подряд, тот же layout, что в
task_arc_lp_fee_lvr_extsload_historical.py) и через собственные getter'ы
для V3-стиля пулов (slot0()/feeGrowthGlobal0X128()/
feeGrowthGlobal1X128()/liquidity() -- 4 отдельных eth_call, ABI-
селекторы посчитаны реальным keccak256 сигнатуры, не угаданы).

Ничего не анализируется -- только пишется RAW. Одна строка на пул на
снимок, JSONL, файл на день: `data/trend_collector/YYYY-MM-DD.jsonl`.

Каждый запуск пишет ОДНУ строку в лог (`data/trend_collector/run_log.
jsonl`) -- факт запуска + успех/ошибка -- чтобы тихая смерть сборщика
была видна (владелец, дословно: "у нас уже был случай, когда проверка
отвалилась молча")."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path  # noqa: E402

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
RPC = rpc_call_trading_path
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6
CALL_INTERVAL_S = 0.15


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


SLOT0_SELECTOR = "0x" + keccak256(b"slot0()")[:4].hex()
FEE_GROWTH_0_SELECTOR = "0x" + keccak256(b"feeGrowthGlobal0X128()")[:4].hex()
FEE_GROWTH_1_SELECTOR = "0x" + keccak256(b"feeGrowthGlobal1X128()")[:4].hex()
LIQUIDITY_SELECTOR = "0x" + keccak256(b"liquidity()")[:4].hex()
EXTSLOAD_BATCH_SELECTOR = "0x" + keccak256(b"extsload(bytes32,uint256)")[:4].hex()


def state_slot_int(pool_id_hex: str) -> int:
    return int.from_bytes(keccak256(bytes.fromhex(pool_id_hex[2:]) + POOLS_SLOT.to_bytes(32, "big")), "big")


def snapshot_v4(pool_id: str) -> dict:
    slot_int = state_slot_int(pool_id)
    calldata = EXTSLOAD_BATCH_SELECTOR + slot_int.to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex()
    try:
        raw = RPC("eth_call", [{"to": POOL_MANAGER, "data": calldata}, "latest"])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not raw or raw == "0x":
        return {"error": "empty result"}
    data = bytes.fromhex(raw[2:])
    if len(data) < 32 * 6:
        return {"error": f"result too short: {len(data)} bytes"}
    words = [int.from_bytes(data[64 + i * 32: 64 + (i + 1) * 32], "big") for i in range(4)]
    slot0_raw, fee_growth0, fee_growth1, liq_raw = words
    return {
        "sqrt_price_x96": slot0_raw & ((1 << 160) - 1),
        "fee_growth_global0_x128": fee_growth0, "fee_growth_global1_x128": fee_growth1,
        "liquidity": liq_raw & ((1 << 128) - 1),
    }


def snapshot_v3(address: str) -> dict:
    out: dict = {}
    for label, selector, parse in [
        ("slot0", SLOT0_SELECTOR, lambda h: int(h[2:66], 16)),
        ("fee_growth_global0_x128", FEE_GROWTH_0_SELECTOR, lambda h: int(h, 16)),
        ("fee_growth_global1_x128", FEE_GROWTH_1_SELECTOR, lambda h: int(h, 16)),
        ("liquidity", LIQUIDITY_SELECTOR, lambda h: int(h, 16)),
    ]:
        try:
            raw = RPC("eth_call", [{"to": address, "data": selector}, "latest"])
        except Exception as exc:  # noqa: BLE001
            out[f"{label}_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if not raw or raw == "0x":
            out[f"{label}_error"] = "empty result"
            continue
        try:
            out[label] = parse(raw)
        except Exception as exc:  # noqa: BLE001
            out[f"{label}_error"] = f"parse: {exc}"
        time.sleep(CALL_INTERVAL_S)
    if "slot0" in out:
        out["sqrt_price_x96"] = out.pop("slot0")
    return out


def run() -> int:
    root = find_repo_root()
    universe_path = root.joinpath("data/trend_collector/pool_universe.json")
    out_dir = root.joinpath("data/trend_collector")
    log_path = out_dir.joinpath("run_log.jsonl")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    today = time.strftime("%Y-%m-%d", time.gmtime())

    if not universe_path.exists():
        with open(log_path, "a") as f:
            f.write(json.dumps({"ts": now, "status": "error", "reason": "pool_universe.json не найден"}) + "\n")
        print("[snapshot] pool_universe.json не найден -- нечего снимать")
        return 1

    universe = json.loads(universe_path.read_text())
    pools = universe.get("pools", {})

    try:
        latest_raw = RPC("eth_blockNumber", [])
        latest_block = int(latest_raw, 16) if latest_raw else None
    except Exception as exc:  # noqa: BLE001
        with open(log_path, "a") as f:
            f.write(json.dumps({"ts": now, "status": "error", "reason": f"eth_blockNumber: {exc}"}) + "\n")
        print(f"[snapshot] не удалось получить latest_block: {exc}")
        return 1

    out_lines = []
    n_ok, n_err = 0, 0
    for key, p in pools.items():
        row = {"snapshot_ts_utc": now, "block": latest_block, "key": key,
               "address": p.get("address"), "pool_id": p.get("pool_id"), "kind": p.get("kind"),
               "name": p.get("name"), "dex_id": p.get("dex_id"), "fee_pips": p.get("fee_pips")}
        if p.get("kind") == "v4_pool_id":
            state = snapshot_v4(p["pool_id"])
        elif p.get("kind") == "v3_address":
            state = snapshot_v3(p["address"])
        else:
            state = {"error": f"неизвестный kind: {p.get('kind')}"}
        row.update(state)
        if "error" in state or any(k.endswith("_error") for k in state):
            n_err += 1
        else:
            n_ok += 1
        out_lines.append(json.dumps(row, ensure_ascii=False, default=str))
        time.sleep(CALL_INTERVAL_S)

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir.joinpath(f"{today}.jsonl"), "a") as f:
        for line in out_lines:
            f.write(line + "\n")

    with open(log_path, "a") as f:
        f.write(json.dumps({"ts": now, "status": "ok", "n_pools": len(pools), "n_ok": n_ok, "n_err": n_err,
                             "block": latest_block}) + "\n")
    print(f"[snapshot] block={latest_block} пулов={len(pools)} ok={n_ok} err={n_err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
