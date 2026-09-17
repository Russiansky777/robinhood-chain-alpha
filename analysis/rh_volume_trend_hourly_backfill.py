#!/usr/bin/env python3
"""Владелец (2026-09-17), Задача 3 -- этап Б: почасовая история оборота
и цены за ВЕСЬ период жизни каждого из 23 реальных пулов (data/rh_
volume_trend_pool_discovery_result.json), метод -- ΔfeeGrowthGlobal x
liquidity / fee через extsload (V4) / стандартные getter'ы (V3),
БЕЗ eth_getLogs (запрещено владельцем -- слишком дорого на этой цепи,
установлено в предыдущих раундах).

ЧЕСТНАЯ ОГОВОРКА ПО ОБЪЁМУ: самый старый из 23 пулов создан 2026-07-02
(на следующий день после генезиса цепи), то есть его полная история --
это ~1850+ часов на дату этого прогона. 23 пула x до ~1850 точек каждый
-- это НЕ помещается в один прогон ни по времени (RPC), ни по разумному
job-бюджету. Поэтому:
  - ОДИН прогон продвигает КАЖДЫЙ пул на РЕАЛЬНОЕ число часов вперёд
    (round-robin по пулам, не "сначала один пул целиком, потом
    следующий") -- если бюджет кончится, ни один пул не останется
    вообще без покрытия, у всех будет какой-то префикс истории.
  - Прогресс ЧЕКПОИНТИТСЯ в `data/rh_volume_trend_hourly_state.json` --
    повторный запуск ПРОДОЛЖАЕТ с последнего рассчитанного часа
    каждого пула, а не начинает заново. Скрипт рассчитан на несколько
    последовательных диспетчей, пока не покроет всю историю (честно
    отражено в `coverage_summary` каждого прогона).
  - Init-блок пула оценивается из `pool_created_at` (реальное поле
    GeckoTerminal) через РЕАЛЬНО измеренное время блока этой цепи
    (0.10068 с/блок, свежий сегмент -- data/fomo_short_horizon_result.
    json, `real_block_time_measurement`, НЕ 0.506 от Arc) -- это
    оценка блока по времени, не точный блок Initialize (не ищем через
    getLogs, как и просил владелец), честно помечена как approx.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path  # noqa: E402

DISCOVERY_PATH = Path("data/rh_volume_trend_pool_discovery_result.json")
STATE_PATH = Path("data/rh_volume_trend_hourly_state.json")

RPC = rpc_call_trading_path
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6
REAL_BLOCK_TIME_S = 0.10068  # свежий сегмент, см. докстринг
BLOCKS_PER_HOUR = round(3600 / REAL_BLOCK_TIME_S)
TIME_BUDGET_S = 800.0
DYNAMIC_FEE_FLAG = 0x800000


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


def read_state_v4(pool_id: str, block_hex: str) -> dict | None:
    slot_int = state_slot_int(pool_id)
    calldata = EXTSLOAD_BATCH_SELECTOR + slot_int.to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex()
    try:
        raw = RPC("eth_call", [{"to": POOL_MANAGER, "data": calldata}, block_hex])
    except Exception:  # noqa: BLE001
        return None
    if not raw or raw == "0x":
        return None
    data = bytes.fromhex(raw[2:])
    if len(data) < 32 * 6:
        return None
    words = [int.from_bytes(data[64 + i * 32: 64 + (i + 1) * 32], "big") for i in range(4)]
    slot0_raw, fg0, fg1, liq_raw = words
    return {"sqrt_price_x96": slot0_raw & ((1 << 160) - 1),
            "fee_growth_global0_x128": fg0, "fee_growth_global1_x128": fg1,
            "liquidity": liq_raw & ((1 << 128) - 1)}


def read_state_v3(address: str, block_hex: str) -> dict | None:
    out = {}
    for label, selector, parse in [
        ("sqrt_price_x96", SLOT0_SELECTOR, lambda h: int(h[2:66], 16)),
        ("fee_growth_global0_x128", FEE_GROWTH_0_SELECTOR, lambda h: int(h, 16)),
        ("fee_growth_global1_x128", FEE_GROWTH_1_SELECTOR, lambda h: int(h, 16)),
        ("liquidity", LIQUIDITY_SELECTOR, lambda h: int(h, 16)),
    ]:
        try:
            raw = RPC("eth_call", [{"to": address, "data": selector}, block_hex])
        except Exception:  # noqa: BLE001
            return None
        if not raw or raw == "0x":
            return None
        try:
            out[label] = parse(raw)
        except Exception:  # noqa: BLE001
            return None
    return out


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"pools": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, default=str))


def main() -> None:
    discovery = json.loads(DISCOVERY_PATH.read_text())
    candidates = discovery["final_candidates"]

    latest_raw = RPC("eth_blockNumber", [])
    latest_block = int(latest_raw, 16) if latest_raw else None
    latest_block_data = RPC("eth_getBlockByNumber", [hex(latest_block), False]) if latest_block else None
    latest_ts = int(latest_block_data["timestamp"], 16) if latest_block_data else None
    if latest_block is None or latest_ts is None:
        print("[backfill] не удалось получить latest_block/timestamp -- стоп")
        return

    state = load_state()
    pools_state = state["pools"]

    for c in candidates:
        fr = c["fee_resolution"]
        key = (fr.get("pool_id") or fr.get("address") or c["address"]).lower()
        if key in pools_state:
            continue
        created_ts = None
        if c.get("pool_created_at"):
            try:
                created_ts = int(datetime.fromisoformat(c["pool_created_at"].replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        if created_ts is None:
            continue
        init_block_est = max(1, latest_block - round((latest_ts - created_ts) / REAL_BLOCK_TIME_S))
        is_dynamic_fee = bool(fr.get("fee_pips") and (fr["fee_pips"] & DYNAMIC_FEE_FLAG))
        pools_state[key] = {
            "name": c.get("name"), "dex_id": c.get("dex_id"), "kind": fr.get("kind"),
            "pool_id": fr.get("pool_id"), "address": fr.get("address") or c.get("address"),
            "fee_pips": fr.get("fee_pips"), "fee_is_dynamic": is_dynamic_fee,
            "init_block_estimate": init_block_est, "init_block_source": "pool_created_at + measured block time (approx)",
            "hours": [], "next_hour_index": 0, "caught_up": False,
        }
    save_state(state)

    start_ts = time.time()
    n_calls_this_run = 0
    active_keys = [k for k, p in pools_state.items() if not p.get("caught_up")]
    idx = 0
    budget_exhausted = False
    while active_keys and not budget_exhausted:
        if time.time() - start_ts > TIME_BUDGET_S:
            budget_exhausted = True
            break
        progressed_any = False
        for key in list(active_keys):
            if time.time() - start_ts > TIME_BUDGET_S:
                budget_exhausted = True
                break
            p = pools_state[key]
            hour_idx = p["next_hour_index"]
            target_block = p["init_block_estimate"] + hour_idx * BLOCKS_PER_HOUR
            if target_block > latest_block:
                p["caught_up"] = True
                active_keys.remove(key)
                continue
            if p["kind"] == "v4_pool_id":
                st = read_state_v4(p["pool_id"], hex(target_block))
            else:
                st = read_state_v3(p["address"], hex(target_block))
            n_calls_this_run += 1
            if st is not None:
                p["hours"].append({"hour_index": hour_idx, "block": target_block, **st})
            else:
                p["hours"].append({"hour_index": hour_idx, "block": target_block, "error": True})
            p["next_hour_index"] = hour_idx + 1
            progressed_any = True
        if not progressed_any:
            break
        idx += 1
        if idx % 20 == 0:
            save_state(state)  # чекпоинт периодически, не только в конце

    save_state(state)

    coverage_summary = {}
    for key, p in pools_state.items():
        coverage_summary[key] = {
            "name": p["name"], "n_hours_computed": len(p["hours"]),
            "caught_up": p["caught_up"],
            "oldest_hour_block": p["hours"][0]["block"] if p["hours"] else None,
            "newest_hour_block": p["hours"][-1]["block"] if p["hours"] else None,
        }
    print(f"[backfill] звонков в этом прогоне: {n_calls_this_run}, бюджет исчерпан: {budget_exhausted}")
    for key, s in coverage_summary.items():
        print(f"  {s['name']}: {s['n_hours_computed']} часов, caught_up={s['caught_up']}")
    n_caught_up = sum(1 for p in pools_state.values() if p["caught_up"])
    print(f"[backfill] полностью покрыто пулов: {n_caught_up}/{len(pools_state)}")


if __name__ == "__main__":
    main()
