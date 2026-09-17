#!/usr/bin/env python3
"""Владелец (2026-09-17): расхождение x1.8 между profit_usdc_net детектора
циклов ($98.63 по 267) и реальным settled Transfer на получателя ($179.45,
task_arc_arb_profit_recipients_result.json) -- не закрыто, требует
объяснения.

Гипотеза (из чтения task_arc_closed_cycle_detector.py, строки 347-393):
детектор считает profit_usdc_net = profit_usdc_gross - gas_cost_usdc, а
газ на Arc платится НАТИВНОЙ USDC-обёрткой (0xfff...ffe, 18 decimals) --
это НЕ Transfer-событие (списание газа -- протокольный механизм, не
ERC20-перевод), значит наше эмпирическое измерение (сумма Transfer-логов)
структурно НЕ включает газ вообще -- т.е. наши $179.45 это GROSS
(до газа), а $98.63 детектора -- NET (после газа). Если гипотеза верна,
detector_net + gas_cost_usdc(наш пересчёт) ≈ наш empirical_gross по тем
же tx.

Проверка -- дёшево (5 tx): eth_getTransactionReceipt (уже подтверждён
свободным от rate limit) даёт gasUsed И effectiveGasPrice напрямую в
самом receipt -- ничего дополнительно скрести не нужно."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

RPC = "https://rpc.mainnet.arc.io"
USDC_NATIVE_DECIMALS = 18
REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]

# Топ-5 расхождений detector vs empirical, взяты из уже посчитанного
# task_arc_arb_profit_recipients_result.json (per_cycle_recipient_analysis).
TARGET_TXS = [
    {"tx_hash": "0x9373fcd1ed60118fe717f00893d05f92e092cd905a857654c8e212aaf271eed0", "detector_profit_usdc_net": 8.5622},
    {"tx_hash": "0x9ad4e4ee5012e5a359d899dfcc53010d76f7614868869ee301affcb37df33150", "detector_profit_usdc_net": 8.206},
    {"tx_hash": "0x36d47847336109167386dace3e845ce1aa8379464f21eb30550b663f8d9a45f1", "detector_profit_usdc_net": 2.0093},
    {"tx_hash": "0xf1ecbcd2cbb7fdbce97a37ff5a25e4497e683aa1e204291cf47a2423ea69df0d", "detector_profit_usdc_net": 4.8906},
    {"tx_hash": "0x469f79cce642cc0321a54886e98d11d7c1d4f5b450e148bb939418f05b956c56", "detector_profit_usdc_net": 4.834},
]
EMPIRICAL_GROSS = {
    "0x9373fcd1ed60118fe717f00893d05f92e092cd905a857654c8e212aaf271eed0": 13.75,
    "0x9ad4e4ee5012e5a359d899dfcc53010d76f7614868869ee301affcb37df33150": 12.4327,
    "0x36d47847336109167386dace3e845ce1aa8379464f21eb30550b663f8d9a45f1": 5.4881,
    "0xf1ecbcd2cbb7fdbce97a37ff5a25e4497e683aa1e204291cf47a2423ea69df0d": 7.5423,
    "0x469f79cce642cc0321a54886e98d11d7c1d4f5b450e148bb939418f05b956c56": 7.2943,
}


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
        body["_http_status"] = resp.status_code
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}, "_http_status": None}


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rows": []}

    for t in TARGET_TXS:
        recv = rpc("eth_getTransactionReceipt", [t["tx_hash"]])
        receipt = recv.get("result")
        if not receipt:
            result["rows"].append({**t, "error": "no receipt"})
            continue
        gas_used = int(receipt["gasUsed"], 16)
        eff_gas_price = int(receipt.get("effectiveGasPrice", "0x0"), 16)
        gas_cost_native_wei = gas_used * eff_gas_price
        gas_cost_usdc = gas_cost_native_wei / (10 ** USDC_NATIVE_DECIMALS)
        empirical_gross = EMPIRICAL_GROSS.get(t["tx_hash"])
        detector_net = t["detector_profit_usdc_net"]
        predicted_net_from_empirical = empirical_gross - gas_cost_usdc if empirical_gross is not None else None
        result["rows"].append({
            "tx_hash": t["tx_hash"], "gas_used": gas_used, "effective_gas_price_wei": eff_gas_price,
            "gas_cost_usdc": gas_cost_usdc, "detector_profit_usdc_net": detector_net,
            "empirical_gross_usdc": empirical_gross,
            "predicted_net_from_empirical_minus_gas": predicted_net_from_empirical,
            "diff_predicted_vs_detector": (predicted_net_from_empirical - detector_net) if predicted_net_from_empirical is not None else None,
        })

    diffs = [abs(r["diff_predicted_vs_detector"]) for r in result["rows"] if r.get("diff_predicted_vs_detector") is not None]
    result["hypothesis_gas_explains_gap"] = {
        "mean_abs_diff_after_gas_correction": (sum(diffs) / len(diffs)) if diffs else None,
        "conclusion": (
            "Если mean_abs_diff мал (доли цента/центы) относительно исходного расхождения (единицы USD) -- "
            "гипотеза ПОДТВЕРЖДЕНА: наши $179.45 -- GROSS (до газа), $98.63 детектора -- NET (после газа), "
            "оба числа правильные, просто разные величины, не баг ни в одном. Если diff остаётся большим -- "
            "гипотеза НЕ объясняет расхождение полностью, есть другая причина."
        ) if diffs else "нет данных для вывода",
    }

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_arb_profit_gas_reconcile_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
