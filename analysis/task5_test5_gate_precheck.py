#!/usr/bin/env python3
"""Задача 5, $5-тест -- ДОПОЛНИТЕЛЬНАЯ, отдельная от scripts/test5_revert.sh
проверка ворот: `eth_call` с `calldata_hex` из `data/task5_test_params.json`
ОТ АДРЕСА ВЛАДЕЛЬЦА, честно докладывает ПОЛНЫЕ данные отката -- не
только 4-байтный селектор, но и сырой hex целиком, ABI-декодированные
аргументы (если селектор известен), и явную проверку на стандартный
`Error(string)`/`Panic(uint256)` (если бы откат был текстовым от пула
Uniswap -- "SPL"/"LOK" -- это видно было бы именно здесь). Read-only,
БЕСПЛАТНО (симуляция на ноде, не транзакция), НИКАКОЙ подписи/отправки.

Владелец, 2026-09-12: "приоритет -- селектор из gate-проверки: какой
именно вернулся, полные revert-данные, и на каком шаге цикла это
случилось. Если текстовый revert от пула (SPL/LOK) -- приложи сырые
байты." Этот скрипт делает СЫРОЙ JSON-RPC eth_call
(`provider.make_request`, в обход авто-парсинга исключений web3.py) --
чтобы получить `error.data` НОДЫ буквально, без риска потерять/
исказить байты при парсинге строкового представления исключения."""
from __future__ import annotations

import json
import os

from eth_abi import decode as abi_decode
from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
OWNER = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"

# Полный набор известных custom error селекторов КОНТРАКТА (см.
# CUSTOM_ERROR_SELECTORS в task5_bot_executor.py / REVERT_SELECTORS в
# task5_bot_sender.py -- независимо перепроверены keccak256), плюс
# стандартные Solidity-селекторы (Error(string), Panic(uint256)) --
# ИМЕННО ими ревертит сам Uniswap V3 пул (require(..., "SPL")/"LOK"
# и т.п. -- это Error(string), не наш custom error).
CONTRACT_SELECTORS = {
    "0xc39ba758": ("InsufficientProfit", ["uint256", "uint256", "uint256"]),
    "0xc2221189": ("UnexpectedCallback", ["address"]),
    "0x37ed32e8": ("ReentrantCall", []),
    "0x30cd7471": ("NotOwner", []),
    "0xb95380e9": ("RepayShortfall", ["uint256", "uint256"]),
}
STANDARD_SELECTORS = {
    "0x08c379a0": "Error(string)",   # обычный require(cond, "текст") -- ТАК ревертят Uniswap V3 пулы (SPL/LOK/...)
    "0x4e487b71": "Panic(uint256)",  # arithmetic overflow/underflow, division by zero и т.п.
}
EXPECTED = "0xc39ba758"  # InsufficientProfit -- то, что должен вернуть ворота-eth_call


def main() -> None:
    params = json.load(open(os.path.join(REPO_ROOT, "data", "task5_test_params.json")))
    contract = Web3.to_checksum_address(params["contract_address"])
    calldata = params["calldata_hex"]
    if not calldata.startswith("0x"):
        calldata = "0x" + calldata

    rpc = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    call = {"from": Web3.to_checksum_address(OWNER), "to": contract, "data": calldata}

    # Сырой JSON-RPC запрос -- в обход авто-парсинга/исключений web3.py,
    # чтобы увидеть error.data НОДЫ буквально, как он есть.
    raw_response = rpc.provider.make_request("eth_call", [call, "latest"])

    result = {
        "contract_address": contract,
        "from": OWNER,
        "calldata_hex": calldata,
        "rpc_url": RPC_URL,
        "raw_json_rpc_response": raw_response,
    }

    error = raw_response.get("error")
    if error is None:
        result["reverted"] = False
        result["gate_would_pass"] = False
        result["note"] = "НЕ откатилось вообще -- result = " + str(raw_response.get("result")) + \
                          " -- ворота НЕ пройдены (ожидался revert InsufficientProfit)"
        print(json.dumps(result, indent=2))
        return

    result["reverted"] = True
    result["rpc_error_message"] = error.get("message")
    error_data = error.get("data")
    result["error_data_raw"] = error_data  # ПОЛНЫЙ, СЫРОЙ hex -- ничего не обрезано

    if not error_data or not isinstance(error_data, str) or not error_data.startswith("0x") or len(error_data) < 10:
        result["selector"] = None
        result["gate_would_pass"] = False
        result["note"] = "error.data отсутствует/не hex -- нода не вернула структурированную причину отката " \
                          f"(rpc_error_message={result['rpc_error_message']!r})"
        print(json.dumps(result, indent=2))
        return

    selector = error_data[:10].lower()
    body_hex = error_data[10:]
    result["selector"] = selector

    if selector in CONTRACT_SELECTORS:
        name, types = CONTRACT_SELECTORS[selector]
        result["revert_kind"] = "contract_custom_error"
        result["revert_name"] = name
        if types and body_hex:
            try:
                decoded = abi_decode(types, bytes.fromhex(body_hex))
                result["decoded_args"] = [str(v) for v in decoded]
            except Exception as exc:
                result["decode_error"] = str(exc)
        result["gate_would_pass"] = (selector == EXPECTED)
        result["note"] = (
            "ворота ПРОЙДЕНЫ бы -- ожидаемый InsufficientProfit" if selector == EXPECTED
            else f"ворота НЕ пройдены -- получен custom error контракта {name} "
                 f"(НЕ текстовый revert пула -- это наш собственный именованный error, "
                 f"селектор -- keccak256 сигнатуры {name}(...), совпадение случайным не бывает)"
        )
    elif selector in STANDARD_SELECTORS:
        std_name = STANDARD_SELECTORS[selector]
        result["revert_kind"] = "standard_solidity_error"
        result["revert_name"] = std_name
        result["gate_would_pass"] = False
        if selector == "0x08c379a0" and body_hex:
            try:
                (text,) = abi_decode(["string"], bytes.fromhex(body_hex))
                result["decoded_string"] = text
                result["note"] = f"ТЕКСТОВЫЙ revert (Error(string)) -- скорее всего от Uniswap V3 пула " \
                                  f"(require(cond, \"...\")), НЕ от нашего контракта: \"{text}\""
            except Exception as exc:
                result["decode_error"] = str(exc)
                result["note"] = "Error(string), но не удалось ABI-декодировать тело -- см. error_data_raw"
        elif selector == "0x4e487b71" and body_hex:
            try:
                (code,) = abi_decode(["uint256"], bytes.fromhex(body_hex))
                result["decoded_panic_code"] = hex(code)
                result["note"] = f"Panic(uint256) code={hex(code)} -- арифметика/массив/etc, не наш custom error"
            except Exception as exc:
                result["decode_error"] = str(exc)
    else:
        result["revert_kind"] = "unknown"
        result["revert_name"] = None
        result["gate_would_pass"] = False
        result["note"] = f"НЕИЗВЕСТНЫЙ селектор {selector} -- ни один из известных custom errors контракта, " \
                          f"ни Error(string)/Panic(uint256) -- см. error_data_raw целиком для ручного разбора"

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
