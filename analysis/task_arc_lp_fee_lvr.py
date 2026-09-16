#!/usr/bin/env python3
"""Основное задание LP-линии (владелец, 2026-09-16), после переписи:
23 живых пула с fee>0 из топ-30 (плюс 7 fee=0 для сравнения объёма) --
пороги предрегистрации пройдены, полный скан 24567 остановлен.

Формула LVR -- ТА ЖЕ, что в task_arc_hour_zero_audit.py (паспорт,
раздел Arc): sigma = |изменение sqrtPriceX96 от первого до последнего
свопа в окне| (не годовая волатильность, честная почасовая
аппроксимация), lvr_frac_of_tvl = sigma^2/8, lvr_usdc = lvr_frac * TVL-
оценка по резервам USDC-стороны на конец окна.

LP fee -- РЕАЛЬНЫЙ fee из каждого события Swap (может отличаться от
статичного Initialize.fee при dynamic-fee хуке), не статичный тариф --
явное требование задания. fee = fee_pips/1e6 * abs(amount на входной
стороне свопа); если входная сторона не USDC, конвертируется в USD по
цене ЭТОЙ ЖЕ tx (usdc_leg/other_leg этого свопа).

Оборот окна -- через Swap-события (abs(amount на USDC-стороне) на
каждый своп), НЕ через bulk Transfer-суммы по tx -- та методология уже
шла в паспорт для 1ч-топ30 и сравнима с текущими числами. Ограничение
(честно, не молчим): на пулах с хуком-скимом это МОЖЕТ разойтись с
реально settled Transfer (см. task_arc_lp_hook_router_reconcile_result.
json -- на покупках реальный поток БОЛЬШЕ AMM-расчёта ровно на сумму
хука) -- где это дёшево проверяемо, делаем точечную сверку через
Transfer к каждому найденному hooks-адресу (не полный per-tx receipt).

Живучесть в этой конкретной выборке -- предвзята по построению (топ-30
ПО ОБЪЁМУ уже подразумевает высокую активность), честно помечено -- не
общий вывод про все 24567 пулов."""
from __future__ import annotations

import json
import time
from pathlib import Path
from collections import Counter

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDC_ERC20 = "0x3600000000000000000000000000000000000000"
NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"
USDC_DECIMALS = 6
BLOCK_TIME_S = 0.506
WINDOWS_H = [1, 3, 6, 12]
ALIVE_THRESHOLD_SWAPS = 5

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.15
_last_call_ts = [0.0]
_rpc_calls = [0]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    wait = MIN_CALL_INTERVAL_S - (time.time() - _last_call_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_call_ts[0] = time.time()
    _rpc_calls[0] += 1
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
        body["_http_status"] = resp.status_code
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}, "_http_status": None}


def is_rate_limited(body: dict) -> bool:
    err = body.get("error")
    return bool(err) and (err.get("code") == -32005 or "rate limit" in str(err.get("message", "")).lower()
                           or body.get("_http_status") == 429)


def word(data_bytes: bytes, i: int) -> bytes:
    return data_bytes[i * 32:(i + 1) * 32]


def to_int_signed(w: bytes) -> int:
    v = int.from_bytes(w, "big")
    return v - 2 ** 256 if v >= 2 ** 255 else v


def decode_swap(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1], "sender": "0x" + log["topics"][2][-40:],
        "amount0": to_int_signed(word(data, 0)), "amount1": to_int_signed(word(data, 1)),
        "sqrt_price_x96": int.from_bytes(word(data, 2), "big"),
        "liquidity": int.from_bytes(word(data, 3), "big"),
        "fee_pips": int.from_bytes(word(data, 5)[-3:], "big"),
        "block_number": int(log["blockNumber"], 16), "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
    }


def get_token_decimals(token: str, cache: dict) -> int | None:
    token = token.lower()
    if token in cache:
        return cache[token]
    if token in (USDC_ERC20.lower(), NATIVE_SENTINEL):
        cache[token] = USDC_DECIMALS if token == USDC_ERC20.lower() else 18
        return cache[token]
    body = rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
    res = body.get("result")
    dec = int(res, 16) if res and res != "0x" else 18
    cache[token] = dec
    return dec


def scan_swaps_for_pool(pool_id: str, from_block: int, to_block: int, max_calls: int = 20) -> tuple[list, dict]:
    logs = []
    block = from_block
    chunk = 20000
    calls = 0
    stopped_by_rate_limit = False
    while block <= to_block and calls < max_calls:
        end = min(block + chunk - 1, to_block)
        body = None
        for retry in range(2):
            body = rpc("eth_getLogs", [{"fromBlock": hex(block), "toBlock": hex(end),
                                         "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, pool_id]}])
            calls += 1
            if not is_rate_limited(body):
                break
            time.sleep(20)
        if is_rate_limited(body):
            stopped_by_rate_limit = True
            break
        if "error" in body:
            err = body["error"]
            msg = str(err.get("message", "")).lower()
            if "too large" in msg or "max results" in msg or err.get("code") in (-32012, -32602):
                chunk = max(500, chunk // 2)
                continue
            break
        logs.extend(body.get("result", []))
        block = end + 1
    return logs, {"n_calls": calls, "stopped_by_rate_limit": stopped_by_rate_limit, "complete": block > to_block}


def analyze_pool(pool_meta: dict, dec_cache: dict, latest: int) -> dict:
    pool_id = pool_meta["pool_id"]
    c0, c1 = pool_meta["currency0"].lower(), pool_meta["currency1"].lower()
    usdc_side = "currency0" if c0 in (USDC_ERC20.lower(), NATIVE_SENTINEL) else (
        "currency1" if c1 in (USDC_ERC20.lower(), NATIVE_SENTINEL) else None)
    if usdc_side is None:
        return {"pool_id": pool_id, "error": "ни одна сторона не USDC/native -- пропущен"}
    other_token = c1 if usdc_side == "currency0" else c0

    init_block = pool_meta["block_number"]
    window_end_12h = min(latest, init_block + int(12 * 3600 / BLOCK_TIME_S))
    logs, scan_meta = scan_swaps_for_pool(pool_id, init_block, window_end_12h)
    swaps = sorted((decode_swap(l) for l in logs), key=lambda s: (s["block_number"], s["log_index"]))

    other_dec = get_token_decimals(other_token, dec_cache)

    windows_out = {}
    for h in WINDOWS_H:
        cutoff = min(latest, init_block + int(h * 3600 / BLOCK_TIME_S))
        window_reached = cutoff <= latest and (init_block + int(h * 3600 / BLOCK_TIME_S)) <= latest
        w_swaps = [s for s in swaps if s["block_number"] <= cutoff]
        n_swaps = len(w_swaps)
        if not window_reached:
            windows_out[f"{h}h"] = {"window_not_yet_elapsed": True, "n_swaps": n_swaps}
            continue
        if n_swaps == 0:
            windows_out[f"{h}h"] = {"survived": False, "n_swaps": 0}
            continue

        volume_usdc = 0.0
        fee_usdc = 0.0
        for s in w_swaps:
            usdc_raw = s["amount0"] if usdc_side == "currency0" else s["amount1"]
            other_raw = s["amount1"] if usdc_side == "currency0" else s["amount0"]
            usdc_human = abs(usdc_raw) / (10 ** USDC_DECIMALS)
            other_human = abs(other_raw) / (10 ** other_dec) if other_dec is not None else None
            volume_usdc += usdc_human
            usdc_is_input = usdc_raw > 0
            if usdc_is_input:
                fee_usdc += s["fee_pips"] / 1_000_000 * usdc_human
            elif other_human:
                price = usdc_human / other_human if other_human else None
                fee_other = s["fee_pips"] / 1_000_000 * other_human
                if price is not None:
                    fee_usdc += fee_other * price

        first_p, last_p = w_swaps[0]["sqrt_price_x96"], w_swaps[-1]["sqrt_price_x96"]
        price_change_frac = (last_p - first_p) / first_p if first_p else None
        lvr_frac = (price_change_frac ** 2) / 8 if price_change_frac is not None else None

        last_ev = w_swaps[-1]
        sqrt_p = last_ev["sqrt_price_x96"] / (2 ** 96) if last_ev["sqrt_price_x96"] else None
        tvl_usdc_est = None
        if sqrt_p:
            reserve_raw = (last_ev["liquidity"] / sqrt_p) if usdc_side == "currency0" else (last_ev["liquidity"] * sqrt_p)
            tvl_usdc_est = reserve_raw / (10 ** USDC_DECIMALS)
        lvr_usdc = (lvr_frac * tvl_usdc_est) if (lvr_frac is not None and tvl_usdc_est) else None

        windows_out[f"{h}h"] = {
            "survived": n_swaps >= ALIVE_THRESHOLD_SWAPS, "n_swaps": n_swaps,
            "volume_usdc_swap_event_based": volume_usdc,
            "lp_fee_usdc_real_per_swap_fee": fee_usdc,
            "price_change_frac": price_change_frac,
            "lvr_frac_of_tvl": lvr_frac, "tvl_usdc_side_estimate": tvl_usdc_est,
            "lvr_usdc": lvr_usdc,
            "fee_over_lvr": (fee_usdc / lvr_usdc) if lvr_usdc else None,
        }

    return {
        "pool_id": pool_id, "pair": pool_meta.get("pair_symbols"), "fee_initialize_pips": pool_meta.get("fee_initialize_pips"),
        "hooks": pool_meta.get("hooks"), "usdc_side": usdc_side, "other_token": other_token,
        "init_block": init_block, "scan_meta": scan_meta, "n_swaps_total_up_to_12h": len(swaps),
        "windows": windows_out,
    }


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    top = json.loads(data_dir.joinpath("task_arc_top_pools_symbols_result.json").read_text())["top_pools_enriched"]
    registry = json.loads(data_dir.joinpath("task_arc_recon_pools_result.json").read_text())
    reg_by_id = {p["pool_id"]: p for p in registry.get("initialize_events", {}).get("pools", [])}

    pool_metas = []
    for p in top:
        reg = reg_by_id.get(p["pool_id"])
        if not reg:
            continue
        pool_metas.append({**p, "currency0": reg["currency0"], "currency1": reg["currency1"],
                            "block_number": reg["block_number"]})
    result["n_pools_matched_to_registry"] = len(pool_metas)
    result["n_pools_unmatched"] = len(top) - len(pool_metas)

    dynamic_fee_pools = [p for p in pool_metas if p["fee_initialize_pips"] == 8388608]
    normal_pools = [p for p in pool_metas if p["fee_initialize_pips"] != 8388608]
    result["dynamic_fee_flagged_pools_excluded_from_main"] = {
        "n": len(dynamic_fee_pools), "pool_ids": [p["pool_id"] for p in dynamic_fee_pools],
        "note": "0 найдено в топ-30 -- этот флаг встречался только в частичном скане реестра (2839/24567), не в текущей выборке" if not dynamic_fee_pools else "будет посчитана фактическая ставка по событиям Swap отдельно"
    }

    # === Осторожная проба ===
    probe_pool = normal_pools[0]
    probe = rpc("eth_getLogs", [{"fromBlock": hex(probe_pool["block_number"]), "toBlock": hex(probe_pool["block_number"] + 100),
                                  "address": POOL_MANAGER, "topics": [SWAP_TOPIC0, probe_pool["pool_id"]]}])
    result["probe"] = {"http_status": probe.get("_http_status"), "has_error": "error" in probe, "error": probe.get("error")}
    if is_rate_limited(probe):
        result["STOPPED"] = "rate limit ещё активен на пробе -- ничего больше не шлём"
        result["total_rpc_calls"] = _rpc_calls[0]
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_lp_fee_lvr_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    latest = int(rpc("eth_blockNumber", []).get("result", "0x0"), 16)
    result["latest_block"] = latest

    dec_cache: dict = {}
    checkpoint_path = data_dir.joinpath("_checkpoint_lp_fee_lvr.json")
    per_pool_results = []
    done_ids = set()
    if checkpoint_path.exists():
        cp = json.loads(checkpoint_path.read_text())
        per_pool_results = cp.get("results", [])
        done_ids = {r["pool_id"] for r in per_pool_results}
        result["resumed_from_checkpoint"] = {"n_pools_already_done": len(done_ids)}

    t_start = time.time()
    for p in normal_pools:
        if p["pool_id"] in done_ids:
            continue
        if time.time() - t_start > 15 * 60:
            result["stopped_early_wall_clock_budget"] = True
            break
        r = analyze_pool(p, dec_cache, latest)
        per_pool_results.append(r)
        checkpoint_path.write_text(json.dumps({"results": per_pool_results}, default=str))
        if r.get("scan_meta", {}).get("stopped_by_rate_limit"):
            result["stopped_by_rate_limit_mid_scan"] = p["pool_id"]
            break

    result["per_pool_results"] = per_pool_results
    complete = len(per_pool_results) >= len(normal_pools)
    result["all_pools_processed"] = complete
    if complete and checkpoint_path.exists():
        checkpoint_path.unlink()

    # === Агрегация: медиана fee/LVR по окнам, доли fee=0 vs fee>0 по обороту ===
    for h in WINDOWS_H:
        key = f"{h}h"
        # Вердикт fee/LVR -- ТОЛЬКО по 23 пулам fee>0 (задание), fee=0 пулы участвуют
        # только в volume_weighted_fee0_vs_feeN ниже, не в этих рядах.
        feeN_pools = [r for r in per_pool_results if r.get("fee_initialize_pips", 0) > 0]
        ratios = [r["windows"][key]["fee_over_lvr"] for r in feeN_pools
                  if key in r.get("windows", {}) and r["windows"][key].get("fee_over_lvr") is not None]
        survived_flags = [r["windows"][key].get("survived") for r in feeN_pools if key in r.get("windows", {})
                           and not r["windows"][key].get("window_not_yet_elapsed")]
        n_reached = len(survived_flags)
        n_survived = sum(1 for s in survived_flags if s)
        ratios_sorted = sorted(ratios)
        median = ratios_sorted[len(ratios_sorted) // 2] if ratios_sorted else None
        if ratios_sorted and len(ratios_sorted) % 2 == 0 and len(ratios_sorted) > 0:
            median = (ratios_sorted[len(ratios_sorted) // 2 - 1] + ratios_sorted[len(ratios_sorted) // 2]) / 2
        result.setdefault("window_summary", {})[key] = {
            "n_pools_with_ratio": len(ratios_sorted), "median_fee_over_lvr": median,
            "pct_gt_1": (sum(1 for r in ratios_sorted if r > 1) / len(ratios_sorted) * 100) if ratios_sorted else None,
            "n_reached_window": n_reached, "n_survived_ge_5_swaps": n_survived,
            "pct_survived": (n_survived / n_reached * 100) if n_reached else None,
        }

        vol_fee0 = sum(r["windows"][key].get("volume_usdc_swap_event_based", 0) or 0 for r in per_pool_results
                       if key in r.get("windows", {}) and r["fee_initialize_pips"] == 0)
        vol_feeN = sum(r["windows"][key].get("volume_usdc_swap_event_based", 0) or 0 for r in per_pool_results
                       if key in r.get("windows", {}) and r["fee_initialize_pips"] > 0)
        total_vol = vol_fee0 + vol_feeN
        result.setdefault("volume_weighted_fee0_vs_feeN", {})[key] = {
            "volume_fee_zero_usdc": vol_fee0, "volume_fee_nonzero_usdc": vol_feeN,
            "pct_fee_zero_of_total_volume": (vol_fee0 / total_vol * 100) if total_vol else None,
            "pct_fee_nonzero_of_total_volume": (vol_feeN / total_vol * 100) if total_vol else None,
        }

    # === Предрегистрированный вердикт ===
    medians = [result["window_summary"][f"{h}h"]["median_fee_over_lvr"] for h in WINDOWS_H
               if result["window_summary"].get(f"{h}h", {}).get("median_fee_over_lvr") is not None]
    pct_survived_list = [result["window_summary"][f"{h}h"]["pct_survived"] for h in WINDOWS_H
                          if result["window_summary"].get(f"{h}h", {}).get("pct_survived") is not None]
    any_gt_1_5_with_20pct_survival = any(
        (result["window_summary"].get(f"{h}h", {}).get("median_fee_over_lvr") or 0) > 1.5
        and (result["window_summary"].get(f"{h}h", {}).get("pct_survived") or 0) >= 20
        for h in WINDOWS_H
    )
    all_below_1 = medians and all(m < 1 for m in medians)
    if any_gt_1_5_with_20pct_survival:
        verdict = "ЖИВА"
    elif all_below_1:
        verdict = "НЕТ"
    else:
        verdict = "НЕ РЕШЕНО"
    result["preregistered_verdict"] = verdict
    result["verdict_caveat"] = (
        "Выборка -- 23 пула fee>0 (+7 fee=0 для сравнения объёма) ИЗ ТОП-30 ПО ОБЪЁМУ -- "
        "выживаемость в этой выборке предвзята по построению (уже отобраны самые активные), "
        "не репрезентативна для ВСЕХ 24567 пулов реестра. Вердикт -- для ЭТОЙ выборки, не общий."
    )

    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_lp_fee_lvr_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
