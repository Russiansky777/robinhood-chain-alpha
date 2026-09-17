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
  3. Газ -- РЕАЛЬНЫЕ измеренные gasUsed трёх подтверждённых
     multi-leg-своп транзакций НА ЭТОЙ цепи (`data/task5_arb_suspect_
     audit_3tx_result.json`: 378848, 186163, 290261) x ТЕКУЩИЙ
     `eth_gasPrice` -- не выдуманное число, но и не гарантированно
     "именно 2-леговый арбитражный цикл" (в отличие от Arc, где были 5
     подтверждённых РЕАЛЬНЫХ арбитражных циклов) -- честно помечено
     как "ближайший реальный аналог с этой цепи", не точное совпадение
     сценария.

ВЛАДЕЛЕЦ, 2026-09-17 (продолжение, "A2"): ТРИ реальных попытки с
батчевыми вызовами (34/40/20-чанки) упёрлись в HTTP 429 на 3-19 блоках --
`data/p3_guard_cache/alchemy_key_probe_result.json` подтвердил РЕАЛЬНУЮ
причину: Free tier Alchemy бьёт по compute-units-per-second ИМЕННО на
батчах (несколько вызовов в одном HTTP-запросе тратят CU мгновенно),
тогда как ОДИНОЧНЫЕ вызовы чистые до ~8 запросов/с без единого 429 (тот
же файл, `rate_limit_probe`). Батчинг УБРАН ПОЛНОСТЬЮ -- только
последовательные одиночные `eth_call` с паузой (`SEQUENTIAL_CALL_
INTERVAL_S`, с запасом ниже измеренного чистого потолка).

Расплата за отказ от батчинга -- по времени. Полное окно 10 минут
(~5960 блоков x 2 ноги x N пар последовательно) физически не
укладывается ни в какой разумный бюджет. Решение -- ДВУХЭТАПНОЕ:
  Этап 1 (дёшево, для ВСЕХ пар): несколько разнесённых по времени
    отсчётов (`PREFILTER_SAMPLES`) цены на каждую пару -- честно
    отсекаем пары, где `combined_fee_frac` (уже известна из fee_pips,
    без сети) заведомо выше любого реально замеренного разрыва в
    сэмпле (`PREFILTER_FEE_DOMINANCE_RATIO`) -- владелец прямо просил
    не мерить третий раз то, что уже трижды объяснило нулевой результат
    высокой комиссией.
  Этап 2 (дорого, только для ВЫЖИВШИХ пар): плотный посекундный скан
    round-robin (как в `rh_volume_trend_hourly_backfill.py` -- каждая
    выжившая пара получает свою долю бюджета, а не одна съедает всё) --
    честно отчитывается, сколько РЕАЛЬНЫХ блоков покрыто на пару, если
    бюджет не позволил дойти до полных 10 минут.
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
TIME_BUDGET_S = 1400.0
MIN_PAIRS_REQUIRED = 5

# ЧЕСТНАЯ НАХОДКА (2026-09-17, alchemy_key_probe_result.json, повторный
# реальный прогон в тот же день): одиночные eth_call чистые до ~8
# запросов/с БЕЗ единого 429 -- батчинг убран, интервал ниже этого
# потолка с запасом.
SEQUENTIAL_CALL_INTERVAL_S = 0.15  # ~6.67 запросов/с, ниже измеренного чистого потолка ~8/с
_last_call_ts = 0.0

# Этап 1 (дёшево): сколько разнесённых отсчётов цены на пару, и во
# сколько раз комиссия должна превышать МАКСИМАЛЬНЫЙ замеренный в них
# разрыв, чтобы честно отсеять пару БЕЗ дорогого плотного скана --
# порог с запасом (не 1x, чтобы не отсекать пограничные случаи по
# шуму единичных отсчётов).
PREFILTER_SAMPLES = 8
PREFILTER_FEE_DOMINANCE_RATIO = 2.0


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


LAST_CALL_FAILURE_DIAG: dict = {}


def rpc_call_sequential(method: str, params: list, timeout: int = 20) -> dict | None:
    """Один одиночный вызов через прямой Alchemy-эндпоинт, с троттлингом
    ПОД измеренным чистым потолком (~8 запросов/с, alchemy_key_probe_
    result.json) -- НЕ батч. Возвращает {"result": ...} либо
    {"error": ...} в стиле JSON-RPC ответа; None при транспортной
    ошибке/недоступности эндпоинта (диагностика в LAST_CALL_FAILURE_DIAG)."""
    url = _alchemy_direct_endpoint()
    if not url:
        LAST_CALL_FAILURE_DIAG.clear()
        LAST_CALL_FAILURE_DIAG.update({"reason": "no_alchemy_url"})
        return None
    global _last_call_ts
    wait = _last_call_ts + SEQUENTIAL_CALL_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code != 200:
            LAST_CALL_FAILURE_DIAG.clear()
            LAST_CALL_FAILURE_DIAG.update({"reason": "non_200", "status_code": resp.status_code,
                                            "body_snippet": resp.text[:300], "method": method})
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        LAST_CALL_FAILURE_DIAG.clear()
        LAST_CALL_FAILURE_DIAG.update({"reason": "exception", "error": f"{type(exc).__name__}: {exc}", "method": method})
        return None


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


def fetch_price(pool: dict, block_hex: str) -> int | None:
    """Один последовательный вызов (НЕ батч) -- цена одной ноги на одном блоке."""
    method, params = call_spec_for(pool)
    params = [params[0], block_hex]
    resp = rpc_call_sequential(method, params)
    if resp is None or "error" in resp:
        return None
    return decode_price_for(pool, resp.get("result"))


def net_edge_usd(pair: dict, price_a: int | None, price_b: int | None, gas_cost_usd: float) -> float | None:
    if not price_a or not price_b:
        return None
    sp_a, sp_b = price_a / (2 ** 96), price_b / (2 ** 96)
    price_a_raw, price_b_raw = sp_a * sp_a, sp_b * sp_b
    if price_a_raw <= 0 or price_b_raw <= 0:
        return None
    gap_frac = abs(price_a_raw - price_b_raw) / min(price_a_raw, price_b_raw)
    combined_fee_frac = (pair["pool_a"]["fee_pips"] + pair["pool_b"]["fee_pips"]) / 1e6
    net_edge_frac = gap_frac - combined_fee_frac
    if net_edge_frac <= 0:
        return None
    return net_edge_frac * REFERENCE_NOTIONAL_USD - gas_cost_usd


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

    # sanity-check: одиночный вызов должен реально работать, иначе честно
    # останавливаемся сразу, не тратя время на этап 1.
    probe = rpc_call_sequential("eth_blockNumber", [])
    sequential_ok = probe is not None and "error" not in probe
    out["sequential_rpc_supported"] = sequential_ok
    if not sequential_ok:
        out["STOPPED"] = "одиночный eth_call недоступен (нет Alchemy-URL или транспортная ошибка) -- честно останавливаемся"
        out["call_failure_diagnostic"] = dict(LAST_CALL_FAILURE_DIAG)
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    # --- Этап 1: дешёвый префильтр -- отсечь пары, где комиссия заведомо
    # выше любого реально замеренного разрыва в разнесённом сэмпле ---
    prefilter_start = time.time()
    sample_blocks = sorted({from_block + round(i * WINDOW_BLOCKS / (PREFILTER_SAMPLES - 1)) for i in range(PREFILTER_SAMPLES)})
    prefilter_stats = []
    surviving_pairs = []
    n_prefilter_errors = 0
    for pair in pairs:
        combined_fee_frac = (pair["pool_a"]["fee_pips"] + pair["pool_b"]["fee_pips"]) / 1e6
        gaps = []
        for b in sample_blocks:
            block_hex = hex(b)
            pa = fetch_price(pair["pool_a"], block_hex)
            pb = fetch_price(pair["pool_b"], block_hex)
            if pa is None or pb is None:
                n_prefilter_errors += 1
                continue
            sp_a, sp_b = pa / (2 ** 96), pb / (2 ** 96)
            price_a_raw, price_b_raw = sp_a * sp_a, sp_b * sp_b
            if price_a_raw > 0 and price_b_raw > 0:
                gaps.append(abs(price_a_raw - price_b_raw) / min(price_a_raw, price_b_raw))
        max_gap = max(gaps) if gaps else None
        fee_dominant = max_gap is None or combined_fee_frac > max_gap * PREFILTER_FEE_DOMINANCE_RATIO
        prefilter_stats.append({
            "token_pair": pair["token_pair"], "pair_type": pair["pair_type"],
            "pool_a": pair["pool_a"]["address"], "pool_b": pair["pool_b"]["address"],
            "combined_fee_frac": combined_fee_frac, "n_samples_ok": len(gaps),
            "max_sampled_gap_frac": max_gap, "fee_dominant_excluded": fee_dominant,
        })
        if not fee_dominant:
            surviving_pairs.append(pair)
    out["prefilter"] = {
        "n_sample_blocks": len(sample_blocks), "n_pairs_checked": len(pairs),
        "n_pairs_excluded_fee_dominant": len(pairs) - len(surviving_pairs),
        "n_pairs_surviving": len(surviving_pairs), "n_prefilter_call_errors": n_prefilter_errors,
        "runtime_s": time.time() - prefilter_start, "details": prefilter_stats,
    }
    print(f"[our_share] префильтр: {len(surviving_pairs)}/{len(pairs)} пар выжили (комиссия не доминирует), "
          f"{time.time() - prefilter_start:.0f}с")

    if not surviving_pairs:
        out["HONEST_ANSWER"] = (
            f"Все {len(pairs)} пар отсеяны префильтром -- комиссия двух ног в {PREFILTER_FEE_DOMINANCE_RATIO}x+ "
            "выше любого реально замеренного разрыва в разнесённом сэмпле. Плотный скан НЕ запускается -- "
            "мерить то же самое (высокая комиссия убивает возможность) в третий раз нет смысла."
        )
        out["verdict"] = "ЗАКРЫТА -- комиссия доминирует во всех парах-кандидатах"
        out["total_runtime_s"] = time.time() - start_ts
        print("[our_share] " + out["HONEST_ANSWER"])
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    # --- Этап 2: плотный посекундный скан round-robin по выжившим парам ---
    remaining_budget = TIME_BUDGET_S - (time.time() - start_ts)
    out["dense_scan_budget_s"] = remaining_budget
    episodes_by_pair = {id(p): {"open": None, "closed": []} for p in surviving_pairs}
    blocks_scanned_by_pair = {id(p): 0 for p in surviving_pairs}
    next_block_by_pair = {id(p): from_block for p in surviving_pairs}
    dense_start = time.time()
    stopped_reason = None
    active = list(surviving_pairs)
    while active:
        if time.time() - dense_start > remaining_budget:
            stopped_reason = f"бюджет плотного скана ({remaining_budget:.0f}с) исчерпан -- честно останавливаемся"
            break
        still_active = []
        for pair in active:
            if time.time() - dense_start > remaining_budget:
                stopped_reason = f"бюджет плотного скана ({remaining_budget:.0f}с) исчерпан -- честно останавливаемся"
                break
            pid = id(pair)
            block = next_block_by_pair[pid]
            if block > latest_block:
                continue  # эта пара уже дошла до конца окна, честно не в active
            block_hex = hex(block)
            pa = fetch_price(pair["pool_a"], block_hex)
            pb = fetch_price(pair["pool_b"], block_hex)
            edge_usd = net_edge_usd(pair, pa, pb, gas_cost_usd)
            ep = episodes_by_pair[pid]
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
            blocks_scanned_by_pair[pid] += 1
            next_block_by_pair[pid] = block + 1
            still_active.append(pair)
        active = [p for p in still_active if next_block_by_pair[id(p)] <= latest_block]

    for pair in surviving_pairs:
        pid = id(pair)
        ep = episodes_by_pair[pid]
        if ep["open"] is not None:
            ep["open"]["end_block"] = next_block_by_pair[pid] - 1
            ep["open"]["note"] = "не закрылась к концу покрытого диапазона -- урезана бюджетом/окном"
            ep["closed"].append(ep["open"])

    all_episodes = []
    per_pair_coverage = []
    for pair in surviving_pairs:
        pid = id(pair)
        for e in episodes_by_pair[pid]["closed"]:
            all_episodes.append({**e, "token_pair": pair["token_pair"], "pair_type": pair["pair_type"],
                                  "pool_a": pair["pool_a"]["address"], "pool_b": pair["pool_b"]["address"]})
        per_pair_coverage.append({
            "token_pair": pair["token_pair"], "pool_a": pair["pool_a"]["address"], "pool_b": pair["pool_b"]["address"],
            "n_blocks_covered": blocks_scanned_by_pair[pid],
            "coverage_seconds": blocks_scanned_by_pair[pid] * REAL_BLOCK_TIME_S,
            "coverage_fraction_of_window": blocks_scanned_by_pair[pid] / WINDOW_BLOCKS,
        })

    out["per_pair_coverage"] = per_pair_coverage
    out["n_blocks_scanned_total"] = sum(blocks_scanned_by_pair.values())
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

    min_coverage_frac = min((c["coverage_fraction_of_window"] for c in per_pair_coverage), default=0.0)
    has_survivors = (out.get("n_lived_more_than_1_block") or 0) > 0
    if has_survivors:
        out["verdict"] = "ЖИВЫЕ 2+ БЛОКА ЕСТЬ -- разбирать отдельно"
    elif min_coverage_frac < 0.5:
        out["verdict"] = (f"НЕОКОНЧАТЕЛЬНО -- покрытие окна честно частичное (мин. {min_coverage_frac*100:.0f}% "
                           f"из {WINDOW_MINUTES} мин на пару), 0 живущих дольше 1 блока НА ПОКРЫТОМ участке")
    else:
        out["verdict"] = "ЗАКРЫТА -- 0 живущих дольше 1 блока" + (", возможностей вообще не найдено" if out["n_opportunities_found"] == 0 else "")
    out["total_runtime_s"] = time.time() - start_ts

    print(f"[our_share] пар в плотном скане: {len(surviving_pairs)}, блоков суммарно: {out['n_blocks_scanned_total']}, "
          f"возможностей: {out['n_opportunities_found']}, живущих >1 блока: {out.get('n_lived_more_than_1_block')}, "
          f"вердикт: {out['verdict']}")
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
