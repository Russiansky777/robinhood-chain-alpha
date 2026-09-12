#!/usr/bin/env python3
"""Задача 5, $5-тест -- ДОПОЛНИТЕЛЬНАЯ, отдельная от scripts/test5_revert.sh
проверка ворот: `eth_call` с `calldata_hex` из `data/task5_test_params.json`
ОТ АДРЕСА ВЛАДЕЛЬЦА, честно докладывает селектор отката. Read-only,
БЕСПЛАТНО (симуляция на ноде, не транзакция), НИКАКОЙ подписи/отправки.

Скрипт владельца (`scripts/test5_revert.sh`) делает точно такую же
проверку САМ первым шагом перед реальной отправкой -- это НЕ замена
того шага, а более ранняя, отдельная проверка (без ключа, без венва
web3 на боевом хосте) -- узнать заранее, пройдут ли ворота, ничего не
тратя и не рискуя."""
from __future__ import annotations

import json
import os

from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
OWNER = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"

# Полный набор известных custom error селекторов контракта (см.
# CUSTOM_ERROR_SELECTORS в task5_bot_executor.py / REVERT_SELECTORS в
# task5_bot_sender.py -- те же значения, независимо перепроверены keccak256).
SELECTORS = {
    "0xc39ba758": "InsufficientProfit",
    "0xc2221189": "UnexpectedCallback",
    "0x37ed32e8": "ReentrantCall",
    "0x30cd7471": "NotOwner",
    "0xb95380e9": "RepayShortfall",
}
EXPECTED = "0xc39ba758"  # InsufficientProfit -- то, что должен вернуть ворота-eth_call


def _revert_selector(exc: Exception) -> str | None:
    data = getattr(exc, "data", None) or str(exc)
    if isinstance(data, dict):
        data = data.get("data", "") or ""
    data = str(data)
    i = data.find("0x")
    return data[i:i + 10].lower() if i >= 0 else None


def main() -> None:
    params = json.load(open(os.path.join(REPO_ROOT, "data", "task5_test_params.json")))
    contract = Web3.to_checksum_address(params["contract_address"])
    calldata = params["calldata_hex"]
    if not calldata.startswith("0x"):
        calldata = "0x" + calldata

    rpc = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    call = {"from": Web3.to_checksum_address(OWNER), "to": contract, "data": calldata, "gas": 900_000}

    result = {"contract_address": contract, "from": OWNER, "rpc_url": RPC_URL}
    try:
        rpc.eth.call(call)
        result["reverted"] = False
        result["selector"] = None
        result["revert_name"] = None
        result["gate_would_pass"] = False
        result["note"] = "НЕ откатилось вообще -- ворота НЕ пройдены (ожидался revert InsufficientProfit)"
    except Exception as exc:
        sel = _revert_selector(exc)
        result["reverted"] = True
        result["selector"] = sel
        result["revert_name"] = SELECTORS.get(sel, f"unknown:{sel}") if sel else None
        result["gate_would_pass"] = (sel == EXPECTED)
        result["note"] = (
            "ворота ПРОЙДЕНЫ бы -- ожидаемый InsufficientProfit" if sel == EXPECTED
            else f"ворота НЕ пройдены -- получен {result['revert_name']}, ожидался InsufficientProfit"
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
