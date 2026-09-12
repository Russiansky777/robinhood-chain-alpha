#!/usr/bin/env python3
"""Задача 5, деплой ClosedCycleExecutorV3 -- read-only оценка газа на
деплой (`eth_estimateGas` с `to=None`, `from=<ожидаемый owner>`, `data=
<bytecode+encoded constructor args>`). НЕ подписывает, НЕ отправляет --
`eth_estimateGas` не требует подписи вообще (симуляция на стороне
ноды). Читает `contracts/build/deploy_params.json` (написан этой
сессией, `deploy_tx_data_hex` уже содержит bytecode+constructor args),
дописывает туда реальный `gas_estimate` (не выдуманное число)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
PARAMS_PATH = Path(__file__).parent.parent / "contracts" / "build" / "deploy_params.json"


def rpc(method: str, params: list, url: str = RPC_URL, timeout: float = 15.0):
    r = requests.post(url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"{method} -> {d['error']}")
    return d["result"]


def main() -> int:
    params = json.loads(PARAMS_PATH.read_text())
    owner = params["expected_owner_address"]
    data = params["deploy_tx_data_hex"]

    gas_hex = rpc("eth_estimateGas", [{"from": owner, "data": data}])
    gas = int(gas_hex, 16)

    base_fee_hex = rpc("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"]
    base_fee = int(base_fee_hex, 16)
    priority = int(1e8)
    max_fee = base_fee * 2 + priority
    est_cost_wei = gas * max_fee

    params["gas_estimate"] = gas
    params["gas_estimate_note"] = (
        f"Реальный eth_estimateGas ({RPC_URL}), from={owner}, data=deploy_tx_data_hex -- "
        f"не выдумано, симуляция на ноде. base_fee на момент проверки использован ТОЛЬКО для "
        f"иллюстративной оценки стоимости в ETH ниже -- на момент реального деплоя base_fee "
        f"будет другим, брать актуальный заново."
    )
    params["gas_estimate_illustrative_cost_eth_at_check_time"] = est_cost_wei / 1e18
    params["gas_estimate_checked_at_base_fee_wei"] = base_fee

    PARAMS_PATH.write_text(json.dumps(params, indent=2))
    print(json.dumps({
        "gas_estimate": gas,
        "base_fee_wei": base_fee,
        "illustrative_max_cost_eth": est_cost_wei / 1e18,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
