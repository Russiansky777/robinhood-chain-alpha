#!/usr/bin/env python3
"""Калибровка pool_screener_gt_recompute.py (владелец, 2026-09-05, проверка
1): прогнать СОБСТВЕННЫЙ, уже известный пул ETH/USDG (`0x52e65b17...`,
живая позиция P5, tokenId 1000756) через ТОТ ЖЕ КОД, что применяется к 23
кандидатам скринера -- как обычного кандидата, без специального пути.

Истинное значение (владелец): ratio_to_lvr в реальности 1.3-1.47 --
`real_fee_lvr_ratio_at_hist_sigma=1.4666349853049427` из
`data/p3_guard_cache/pool_screener_calibrate_p5_result.json`
(analysis/pool_screener_calibrate_p5.py, реальные собранные комиссии
позиции delёные на пуловый TVL, реальная 30-дневная sigma этого же пула
через `currency=token&token=quote` -- НЕ через `currency=usd`).

Никакого нового пути расчёта здесь не пишется -- используется
БУКВАЛЬНО:
  - `pool_screener_concentration.compute_k()` для k_pool (та же формула,
    что уже даёт k=9.9 для P6-пула, подтверждённое владельцем число);
  - `pool_screener_gt_recompute.process_one()` для fee_tier/GT-объёма/
    sigma_30d/ratio_to_lvr -- ТОТ ЖЕ путь, что для всех 21 кандидата
    скринера, синтетический candidate-словарь собран в ТОЙ ЖЕ форме,
    которую эти функции реально читают (проверено по сигнатурам, не
    предположено)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

import pool_screener_concentration as psc  # noqa: E402
import pool_screener_gt_recompute as psg  # noqa: E402

ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_CHAIN_ID = 4663
POOL_ADDRESS = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

# Робингуд-чейн не был в RPC_ENDPOINTS ни одного из двух скринерных
# скриптов (только base/arbitrum/bsc -- 23 кандидата все на них) --
# добавляем сеть в ОБА модуля, чтобы reuse-код (rpc_call/verify_rpc)
# работал без изменения самой логики.
psc.RPC_ENDPOINTS["robinhood"] = ROBINHOOD_RPC
psc.EXPECTED_CHAIN_ID["robinhood"] = ROBINHOOD_CHAIN_ID
psg.RPC_ENDPOINTS["robinhood"] = ROBINHOOD_RPC

OUT_PATH = Path("data/p3_guard_cache/pool_screener_gt_recompute_calibrate_p5_result.json")


def diagnose_close_price_domains() -> dict:
    """Прямое сравнение close-цен из currency=usd против
    currency=token&token=quote на ОДНОМ И ТОМ ЖЕ окне (последние 10 часовых
    свечей пула ETH/USDG) -- не гадаем по расхождению итогового ratio,
    смотрим на сырые данные напрямую (владелец, проверка 1: "не гадать
    дальше -- разобраться точно")."""
    params_common = {"aggregate": 1, "limit": 10, "include_empty_intervals": "true"}
    r_usd = requests.get(
        f"https://api.geckoterminal.com/api/v2/networks/robinhood/pools/{POOL_ADDRESS}/ohlcv/hour",
        params={**params_common, "currency": "usd"}, headers={"Accept": "application/json;version=20230302"}, timeout=20)
    import time as _t
    _t.sleep(2.6)
    r_quote = requests.get(
        f"https://api.geckoterminal.com/api/v2/networks/robinhood/pools/{POOL_ADDRESS}/ohlcv/hour",
        params={**params_common, "currency": "token", "token": "quote"}, headers={"Accept": "application/json;version=20230302"}, timeout=20)
    rows_usd = r_usd.json().get("data", {}).get("attributes", {}).get("ohlcv_list", []) if r_usd.status_code == 200 else []
    rows_quote = r_quote.json().get("data", {}).get("attributes", {}).get("ohlcv_list", []) if r_quote.status_code == 200 else []
    closes_usd = [(int(row[0]), float(row[4])) for row in sorted(rows_usd, key=lambda x: x[0])]
    closes_quote = [(int(row[0]), float(row[4])) for row in sorted(rows_quote, key=lambda x: x[0])]
    print(f"    currency=usd close (последние {len(closes_usd)}): {closes_usd}")
    print(f"    currency=token&token=quote close (последние {len(closes_quote)}): {closes_quote}")
    verdict = None
    if closes_usd and closes_quote:
        usd_vals = [c for _, c in closes_usd]
        quote_vals = [c for _, c in closes_quote]
        usd_range_pct = (max(usd_vals) - min(usd_vals)) / min(usd_vals) * 100 if min(usd_vals) else None
        quote_range_pct = (max(quote_vals) - min(quote_vals)) / min(quote_vals) * 100 if min(quote_vals) else None
        verdict = {
            "usd_close_min": min(usd_vals), "usd_close_max": max(usd_vals), "usd_close_range_pct": usd_range_pct,
            "quote_close_min": min(quote_vals), "quote_close_max": max(quote_vals), "quote_close_range_pct": quote_range_pct,
            "usd_close_looks_like_stable_flatline": (0.85 <= min(usd_vals) <= 1.15 and usd_range_pct is not None and usd_range_pct < 5.0),
        }
        print(f"    ВЕРДИКТ: usd close диапазон {min(usd_vals):.6g}..{max(usd_vals):.6g} ({usd_range_pct:.4f}% размах); "
              f"quote close диапазон {min(quote_vals):.6g}..{max(quote_vals):.6g} ({quote_range_pct:.4f}% размах)")
        print(f"    usd close похож на плоскую линию стейбла (~$1, размах <5%)? {verdict['usd_close_looks_like_stable_flatline']}")
    return {"closes_usd_currency": closes_usd, "closes_token_quote_currency": closes_quote, "verdict": verdict}


def run() -> int:
    print("=== 0. Подтверждение RPC Robinhood Chain (chainId) ===")
    psc.verify_rpc("robinhood")

    print("\n=== 0.5. Прямое сравнение close-цен: currency=usd vs currency=token&token=quote ===")
    domain_diag = diagnose_close_price_domains()

    candidate = {
        "pool_id": "calibration_p5_eth_usdg", "project": "uniswap-v3", "chain": "Robinhood Chain",
        "symbol": "WETH-USDG", "tvl_usd": None, "apyBase": None,
        "underlying_tokens": [WETH, USDG],
        "gt_resolution": {"network": "robinhood", "resolved_pool_address": POOL_ADDRESS},
    }

    print("\n=== 1. k_pool -- ТОТ ЖЕ compute_k(), что pool_screener_concentration.py ===")
    conc = psc.compute_k(candidate)
    print(f"    k_pool={conc.get('k')} error={conc.get('error')} tvl_usd_used={conc.get('tvl_usd_used')}")
    candidate["concentration"] = conc
    # process_one() читает c["tvl_usd"] напрямую для fee_apr -- используем
    # РЕАЛЬНЫЙ TVL, который только что употребил compute_k (реальный GT
    # reserve_in_usd, не фиктивное число), а не оставляем None.
    if conc.get("tvl_usd_used"):
        candidate["tvl_usd"] = conc["tvl_usd_used"]

    print("\n=== 2. process_one() -- ТОТ ЖЕ путь, что для 21 кандидата скринера ===")
    entry = psg.process_one(candidate)
    print(json.dumps(entry, indent=2, ensure_ascii=False, default=str))

    true_ratio_range = (1.3, 1.47)
    true_ratio_source = "data/p3_guard_cache/pool_screener_calibrate_p5_result.json::real_fee_lvr_ratio_at_hist_sigma=1.4666349853049427"
    ratio = entry.get("ratio_to_lvr_real_30d_sigma")
    out = {
        "candidate_used": candidate, "process_one_result": entry, "close_price_domain_diagnostic": domain_diag,
        "true_ratio_range_from_live_position": true_ratio_range, "true_ratio_source": true_ratio_source,
        "script_ratio": ratio,
        "within_true_range": (true_ratio_range[0] <= ratio <= true_ratio_range[1]) if ratio is not None else None,
        "multiplier_script_over_true_midpoint": (ratio / 1.383) if ratio is not None else None,
    }
    print(f"\n[calibrate_gt_recompute] script ratio_to_lvr = {ratio}")
    print(f"[calibrate_gt_recompute] истинное значение (живая позиция) = {true_ratio_range} (см. {true_ratio_source})")
    print(f"[calibrate_gt_recompute] в пределах истинного диапазона? {out['within_true_range']}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[calibrate_gt_recompute] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
