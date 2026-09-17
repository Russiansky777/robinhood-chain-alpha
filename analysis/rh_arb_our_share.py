#!/usr/bin/env python3
"""Владелец (2026-09-17), Задача 2 -- Робинхуд, наша доля, метод с Arc
(`task_arc_arb_our_share.py`, часть А): не чужой заработок, а сколько
незакрытых возможностей остаётся -- v3<->v4 по одному токену
(кросс-версионный арбитраж) + v4 с разными fee.

ОТЛИЧИЯ ОТ ARC, ЧЕСТНО:
  1. Источник пар -- GeckoTerminal (нет готового полного реестра
     Initialize-событий для Robinhood Chain, как был `task_arc_recon_
     pools_result.json` для Arc) -- тот же метод открытия пулов, что
     Задача 3 (`rh_volume_trend_pool_discovery.py`), TVL>=$10000.
     "V3-стиль" / "V4" -- по РЕАЛЬНОМУ ончейн-типу (`resolve_real_fee`:
     v3_address имеет свой контракт, v4_pool_id живёт в PoolManager),
     не по названию декса (имена вроде "alandale-cl" неоднозначны).
  2. "Реальная торговля в измеряемом часе" -- ПРЯМОЕ поле GeckoTerminal
     `volume_usd.h1` (реальный почасовой объём, не гипотеза) -- не
     нужен отдельный OHLCV-запрос, как в Задаче 3.
  3. Окно детекции по блокам -- 10 МИНУТ, не 1 час, как у Arc.
     Причина честно посчитана: реальное время блока Robinhood Chain
     ~0.10068с (data/fomo_short_horizon_result.json, свежий сегмент) --
     это В 5 РАЗ БЫСТРЕЕ Arc (0.506с). Час на Robinhood -- это ~35750
     блоков против ~7115 у Arc; посл едовательное сканирование блок-за-
     блоком (нужно для точного числа блоков жизни возможности) на
     35750 блоках x N батч-вызовов не укладывается ни в какой разумный
     бюджет одного job'а. 10 минут (~5960 блоков) даёт СОПОСТАВИМОЕ по
     размеру Arc количество блок-наблюдений (~5960 против ~7115) --
     тот же порядок статистической выборки, при честно меньшем
     покрытии по календарному времени (отражено в выводе явно).
  4. Газ -- РЕАЛЬНЫЕ измеренные gasUsed трёх подтверждённых
     multi-leg-своп транзакций НА ЭТОЙ цепи (`data/task5_arb_suspect_
     audit_3tx_result.json`: 378848, 186163, 290261) x ТЕКУЩИЙ
     `eth_gasPrice` -- не выдуманное число, но и не гарантированно
     "именно 2-леговый арбитражный цикл" (в отличие от Arc, где были 5
     подтверждённых РЕАЛЬНЫХ арбитражных циклов) -- честно помечено
     как "ближайший реальный аналог с этой цепи", не точное совпадение
     сценария.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path, _alchemy_direct_endpoint  # noqa: E402
from rh_volume_trend_pool_discovery import gt_get, GT_NETWORK, resolve_real_fee, MAX_PAGES, GT_REQUEST_INTERVAL_S  # noqa: E402

OUT_PATH = Path("data/rh_arb_our_share_result.json")
RPC = rpc_call_trading_path
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6
DYNAMIC_FEE_FLAG = 0x800000

TVL_MIN_USD = 10000.0
REAL_BLOCK_TIME_S = 0.10068  # свежий сегмент, data/fomo_short_horizon_result.json
WINDOW_MINUTES = 10.0
WINDOW_BLOCKS = round(WINDOW_MINUTES * 60 / REAL_BLOCK_TIME_S)
REFERENCE_NOTIONAL_USD = 200.0
# Реальные gasUsed 3 подтверждённых multi-leg-своп tx на Robinhood Chain
# (data/task5_arb_suspect_audit_3tx_result.json) -- ближайший реальный
# аналог 2-легового цикла на ЭТОЙ цепи, не выдумано.
REAL_CYCLE_GAS_USED_SAMPLES = [378848, 186163, 290261]
TIME_BUDGET_S = 800.0
MIN_PAIRS_REQUIRED = 5


def keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


SLOT0_V3_SELECTOR = "0x" + keccak256(b"slot0()")[:4].hex()
EXTSLOAD_1SLOT_SELECTOR = "0x" + keccak256(b"extsload(bytes32)")[:4].hex()


def state_slot_int(pool_id_hex: str) -> int:
    return int.from_bytes(keccak256(bytes.fromhex(pool_id_hex[2:]) + POOLS_SLOT.to_bytes(32, "big")), "big")


def decode_v3_slot0_price(raw_hex: str | None) -> int | None:
    if not raw_hex or raw_hex == "0x" or len(raw_hex) < 66:
        return None
    return int(raw_hex[2:66], 16)


def decode_v4_slot0_price(raw_hex: str | None) -> int | None:
    if not raw_hex or raw_hex == "0x":
        return None
    data = bytes.fromhex(raw_hex[2:])
    if len(data) < 32:
        return None
    return int.from_bytes(data[0:32], "big") & ((1 << 160) - 1)


def rpc_batch(requests_list: list[tuple[str, list]], timeout: int = 25) -> list[dict] | None:
    url = _alchemy_direct_endpoint()
    if not url:
        return None
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(requests_list)]
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code != 200:
            return None
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, list) or len(body) != len(requests_list):
        return None
    by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
    ordered = []
    for i in range(len(requests_list)):
        if i not in by_id:
            return None
        ordered.append(by_id[i])
    return ordered


def discover_pools() -> list[dict]:
    all_pools = []
    for page in range(1, MAX_PAGES + 1):
        status, body = gt_get(f"/networks/{GT_NETWORK}/pools", params={"page": page})
        if status != 200 or not body or not body.get("data"):
            break
        all_pools.extend(body["data"])
        time.sleep(GT_REQUEST_INTERVAL_S)
    out = []
    for p in all_pools:
        attrs = p.get("attributes", {})
        try:
            reserve_f = float(attrs.get("reserve_in_usd")) if attrs.get("reserve_in_usd") is not None else None
        except (TypeError, ValueError):
            reserve_f = None
        if reserve_f is None or reserve_f < TVL_MIN_USD:
            continue
        vol_h1_raw = (attrs.get("volume_usd") or {}).get("h1")
        try:
            vol_h1 = float(vol_h1_raw) if vol_h1_raw is not None else 0.0
        except (TypeError, ValueError):
            vol_h1 = 0.0
        if vol_h1 <= 0:
            continue
        addr = attrs.get("address")
        if not addr:
            continue
        rel = p.get("relationships", {})
        out.append({
            "address": addr, "name": attrs.get("name"), "reserve_in_usd": reserve_f, "volume_usd_h1": vol_h1,
            "dex_id": (rel.get("dex", {}).get("data", {}) or {}).get("id"),
            "base_token_id": (rel.get("base_token", {}).get("data", {}) or {}).get("id"),
            "quote_token_id": (rel.get("quote_token", {}).get("data", {}) or {}).get("id"),
        })
    return out


def build_pairs(pools: list[dict], latest_block: int, budget_deadline: float) -> tuple[list[dict], dict]:
    resolved = []
    for p in pools:
        if time.time() > budget_deadline:
            break
        fee_info = resolve_real_fee(p["address"], latest_block)
        kind = fee_info.get("kind")
        fee_pips = fee_info.get("fee_pips")
        if kind is None or fee_pips is None:
            continue
        is_dynamic = bool(fee_pips & DYNAMIC_FEE_FLAG)
        resolved.append({**p, "kind": kind, "fee_pips": fee_pips, "fee_is_dynamic": is_dynamic,
                          "pool_id": fee_info.get("pool_id")})

    groups: dict[tuple, list[dict]] = {}
    for p in resolved:
        if not p["base_token_id"] or not p["quote_token_id"]:
            continue
        key = tuple(sorted([p["base_token_id"], p["quote_token_id"]]))
        groups.setdefault(key, []).append(p)

    cross_version_pairs, same_version_diff_fee_pairs = [], []
    for key, members in groups.items():
        v3_members = [m for m in members if m["kind"] == "v3_address"]
        v4_members = [m for m in members if m["kind"] == "v4_pool_id" and not m["fee_is_dynamic"]]
        for a in v3_members:
            for b in v4_members:
                cross_version_pairs.append({"token_pair": key, "pool_a": a, "pool_b": b, "pair_type": "v3_vs_v4"})
        for i in range(len(v4_members)):
            for j in range(i + 1, len(v4_members)):
                a, b = v4_members[i], v4_members[j]
                if a["fee_pips"] != b["fee_pips"]:
                    same_version_diff_fee_pairs.append({"token_pair": key, "pool_a": a, "pool_b": b, "pair_type": "v4_diff_fee"})

    meta = {"n_resolved_pools": len(resolved), "n_token_groups": len(groups),
            "n_cross_version_pairs": len(cross_version_pairs), "n_same_version_diff_fee_pairs": len(same_version_diff_fee_pairs)}
    return cross_version_pairs + same_version_diff_fee_pairs, meta


def call_spec_for(pool: dict) -> tuple[str, list]:
    if pool["kind"] == "v3_address":
        return "eth_call", [{"to": pool["address"], "data": SLOT0_V3_SELECTOR}, None]
    slot_int = state_slot_int(pool["pool_id"])
    calldata = EXTSLOAD_1SLOT_SELECTOR + slot_int.to_bytes(32, "big").hex()
    return "eth_call", [{"to": POOL_MANAGER, "data": calldata}, None]


def decode_price_for(pool: dict, raw_hex: str | None) -> int | None:
    if pool["kind"] == "v3_address":
        return decode_v3_slot0_price(raw_hex)
    return decode_v4_slot0_price(raw_hex)


def main() -> None:
    start_ts = time.time()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    pools = discover_pools()
    out["n_pools_tvl_and_h1_active"] = len(pools)
    print(f"[our_share] TVL>=${TVL_MIN_USD:.0f} + реальный h1-объём: {len(pools)} пулов")

    latest_raw = RPC("eth_blockNumber", [])
    latest_block = int(latest_raw, 16) if latest_raw else None
    if latest_block is None:
        out["STOPPED"] = "не удалось получить latest_block"
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return
    out["latest_block"] = latest_block

    pairs, pair_meta = build_pairs(pools, latest_block, start_ts + 300.0)
    out["pair_discovery"] = pair_meta
    out["n_pairs_total"] = len(pairs)
    print(f"[our_share] пар кандидатов: {len(pairs)} ({pair_meta})")

    if len(pairs) < MIN_PAIRS_REQUIRED:
        out["HONEST_ANSWER"] = (
            f"Пар кандидатов после TVL>=${TVL_MIN_USD:.0f} + реальный h1-объём с обеих сторон: {len(pairs)} "
            f"-- МЕНЬШЕ {MIN_PAIRS_REQUIRED}, это само по себе ответ по правилу владельца. Блочный скан НЕ запускается."
        )
        print("[our_share] " + out["HONEST_ANSWER"])
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    gas_price_raw = RPC("eth_gasPrice", [])
    real_gas_price_wei = int(gas_price_raw, 16) if gas_price_raw else None
    median_gas_used = statistics.median(REAL_CYCLE_GAS_USED_SAMPLES)
    native_usd = None
    try:
        from fomo_short_horizon_wave_and_entry import native_usd_price_at_block  # noqa: E402
        native_usd = native_usd_price_at_block(latest_block, {})
    except Exception:  # noqa: BLE001
        pass
    gas_cost_usd = (real_gas_price_wei * median_gas_used / 1e18 * native_usd) if (real_gas_price_wei and native_usd) else None
    out["gas_assumption"] = {
        "real_eth_gasPrice_wei": real_gas_price_wei, "real_cycle_gas_used_samples": REAL_CYCLE_GAS_USED_SAMPLES,
        "median_gas_used": median_gas_used, "native_usd_price": native_usd, "gas_cost_usd_per_cycle": gas_cost_usd,
        "source": "data/task5_arb_suspect_audit_3tx_result.json (реальные multi-leg-своп tx на этой цепи) + текущий eth_gasPrice",
    }
    if gas_cost_usd is None:
        out["STOPPED"] = "не удалось получить gas_cost_usd (gasPrice или native_usd)"
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    from_block = latest_block - WINDOW_BLOCKS
    out["window"] = {"from_block": from_block, "to_block": latest_block, "window_blocks_intended": WINDOW_BLOCKS,
                      "window_minutes_intended": WINDOW_MINUTES}

    batching_ok = rpc_batch([("eth_blockNumber", [])]) is not None
    out["batching_supported"] = batching_ok
    if not batching_ok:
        out["STOPPED"] = "JSON-RPC batching недоступен (нет Alchemy-URL или транспортная ошибка) -- честно останавливаемся, без него скан 6000 блоков не укладывается в бюджет"
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    episodes_by_pair = [{"open": None, "closed": []} for _ in pairs]
    n_blocks_scanned = 0
    scan_start = time.time()
    block = from_block
    stopped_reason = None
    while block <= latest_block:
        if time.time() - scan_start > TIME_BUDGET_S:
            stopped_reason = f"бюджет скана ({TIME_BUDGET_S}с) исчерпан -- честно останавливаемся"
            break
        block_hex = hex(block)
        reqs = []
        req_index_map = []
        for pi, pair in enumerate(pairs):
            for side in ("pool_a", "pool_b"):
                method, params = call_spec_for(pair[side])
                params = [params[0], block_hex]
                reqs.append((method, params))
                req_index_map.append((pi, side))
        batch_res = rpc_batch(reqs)
        if batch_res is None:
            stopped_reason = f"batching отказал на блоке {block} -- честно останавливаемся, не переходим на sequential (было бы на порядки медленнее)"
            break
        prices_by_pair: dict[int, dict] = {}
        for (pi, side), r in zip(req_index_map, batch_res):
            raw = r.get("result") if "error" not in r else None
            price = decode_price_for(pairs[pi][side], raw)
            prices_by_pair.setdefault(pi, {})[side] = price

        for pi, pair in enumerate(pairs):
            pa_price = prices_by_pair.get(pi, {}).get("pool_a")
            pb_price = prices_by_pair.get(pi, {}).get("pool_b")
            edge_usd = None
            if pa_price and pb_price:
                sp_a, sp_b = pa_price / (2 ** 96), pb_price / (2 ** 96)
                price_a_raw, price_b_raw = sp_a * sp_a, sp_b * sp_b
                if price_a_raw > 0 and price_b_raw > 0:
                    gap_frac = abs(price_a_raw - price_b_raw) / min(price_a_raw, price_b_raw)
                    combined_fee_frac = (pair["pool_a"]["fee_pips"] + pair["pool_b"]["fee_pips"]) / 1e6
                    net_edge_frac = gap_frac - combined_fee_frac
                    if net_edge_frac > 0:
                        edge_usd = net_edge_frac * REFERENCE_NOTIONAL_USD - gas_cost_usd
            ep = episodes_by_pair[pi]
            if edge_usd is not None and edge_usd > 0:
                if ep["open"] is None:
                    ep["open"] = {"start_block": block, "size_usd_at_start": edge_usd, "n_blocks": 1, "peak_usd": edge_usd}
                else:
                    ep["open"]["n_blocks"] += 1
                    ep["open"]["peak_usd"] = max(ep["open"]["peak_usd"], edge_usd)
            else:
                if ep["open"] is not None:
                    ep["open"]["end_block"] = block - 1
                    ep["closed"].append(ep["open"])
                    ep["open"] = None
        n_blocks_scanned += 1
        block += 1

    for pi, pair in enumerate(pairs):
        ep = episodes_by_pair[pi]
        if ep["open"] is not None:
            ep["open"]["end_block"] = block - 1
            ep["open"]["note"] = "не закрылась к концу окна -- урезана окном"
            ep["closed"].append(ep["open"])

    all_episodes = []
    for pi, pair in enumerate(pairs):
        for e in episodes_by_pair[pi]["closed"]:
            all_episodes.append({**e, "token_pair": pair["token_pair"], "pair_type": pair["pair_type"],
                                  "pool_a": pair["pool_a"]["address"], "pool_b": pair["pool_b"]["address"]})

    out["n_blocks_scanned"] = n_blocks_scanned
    if stopped_reason:
        out["partial_coverage_reason"] = stopped_reason
    out["n_opportunities_found"] = len(all_episodes)
    if all_episodes:
        sizes = [e["size_usd_at_start"] for e in all_episodes]
        lifetimes = [e["n_blocks"] for e in all_episodes]
        out["median_size_usd"] = statistics.median(sizes)
        out["median_lifetime_blocks"] = statistics.median(lifetimes)
        out["n_lived_more_than_1_block"] = sum(1 for lt in lifetimes if lt > 1)
        out["max_size_usd"] = max(sizes)
        out["max_lifetime_blocks"] = max(lifetimes)
    else:
        out["median_size_usd"] = None
        out["n_lived_more_than_1_block"] = 0
    out["episodes_sample_first_30"] = all_episodes[:30]

    has_survivors = (out.get("n_lived_more_than_1_block") or 0) > 0
    if not has_survivors and (out.get("n_opportunities_found") or 0) >= 0:
        out["verdict"] = "ЗАКРЫТА -- 0 живущих дольше 1 блока" + (", возможностей вообще не найдено" if out["n_opportunities_found"] == 0 else "")
    else:
        out["verdict"] = "ЖИВЫЕ 2+ БЛОКА ЕСТЬ -- разбирать отдельно"
    out["total_runtime_s"] = time.time() - start_ts

    print(f"[our_share] блоков просканировано: {n_blocks_scanned}/{WINDOW_BLOCKS}, возможностей: {out['n_opportunities_found']}, "
          f"живущих >1 блока: {out.get('n_lived_more_than_1_block')}, вердикт: {out['verdict']}")
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
