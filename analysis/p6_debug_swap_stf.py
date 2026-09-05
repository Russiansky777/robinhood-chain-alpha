"""
P6 -- диагностика реального revert "STF" на exactInputSingle (Aerodrome Slipstream Router, Base).

Реальный прогон (run 10, commit 4197e3c, mode=--confirm-mainnet):
  - depositV3 USDG$80 -> success, depositV3 ETH(газ) -> success, мост реально
    заполнился (USDC=79.948649, ETH=0.000248098 на Base).
  - approve(Router, 36.251385 USDC) -> success (tx 0x589ea38d...).
  - eth_estimateGas для exactInputSingle (36.251385 USDC -> cbBTC) реально
    упал ДО отправки (газ не потрачен, tx не отправлена) с кастомной ошибкой
    0x08c379a0... (Error(string)) -> строка "STF".

"STF" -- известная строка ревёрта из Uniswap V3 periphery TransferHelper.safeTransferFrom
("Safe Transfer Failed"), которую используют форки типа Aerodrome Slipstream:
  require(success && (data.length == 0 || abi.decode(data, (bool))), 'STF');
Возникает при неудачном transferFrom(payer, ..., value) -- недостаточный allowance,
недостаточный баланс, или (маловероятно для USDC) токен вернул false.

Этот скрипт реально перечитывает ТЕКУЩЕЕ состояние on-chain (не предполагает),
чтобы понять реальную причину: лаг RPC-реплики (allowance ещё не виден с
момента approve) или реальная нехватка allowance/баланса.
"""
import json
import time
from pathlib import Path

import requests

BASE_RPC = "https://mainnet.base.org"
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
ROUTER = "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "p3_guard_cache" / "p6_debug_swap_stf_result.json"

# Точный calldata реально упавшего eth_estimateGas из лога run 10 (та же
# tickSpacing=2000, тот же amountIn=36251385, min=44109, deadline истёк --
# видно ниже, deadline тогда был 0x6a9b6127, реальное время сейчас другое,
# так что для повторного estimateGas собираем calldata заново со свежим
# deadline, amountIn и min пересчитывать НЕ будем -- используем те же цифры,
# чтобы изолировать именно вопрос allowance/баланса, а не план свопа).
AMOUNT_IN_RAW = 36251385
MIN_OUT_RAW = 44109
TICK_SPACING = 2000


def rpc(method: str, params: list):
    r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    return body


def eth_call(to: str, data: str):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def pad_addr(addr: str) -> str:
    return addr[2:].rjust(64, "0").lower()


def pad_uint(v: int) -> str:
    return format(v, "064x")


def allowance(token: str, owner: str, spender: str) -> dict:
    selector = "dd62ed3e"
    data = "0x" + selector + pad_addr(owner) + pad_addr(spender)
    return eth_call(token, data)


def balance_of(token: str, holder: str) -> dict:
    selector = "70a08231"
    data = "0x" + selector + pad_addr(holder)
    return eth_call(token, data)


def to_checksum_lower(addr: str) -> str:
    return addr


def build_swap_calldata(deadline: int) -> str:
    # exactInputSingle((address,address,int24,address,uint256,uint256,uint256,uint160))
    selector = "a026383e"
    data = selector
    data += pad_addr(USDC)
    data += pad_addr(CBBTC)
    data += pad_uint(TICK_SPACING)
    data += pad_addr(WALLET)
    data += pad_uint(deadline)
    data += pad_uint(AMOUNT_IN_RAW)
    data += pad_uint(MIN_OUT_RAW)
    data += pad_uint(0)  # sqrtPriceLimitX96
    return "0x" + data


def estimate_gas(to: str, data: str):
    return rpc("eth_estimateGas", [{"from": WALLET, "to": to, "data": data, "value": "0x0"}])


def main():
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    print("=== Реальное текущее состояние on-chain (Base, только что после падения run 10) ===")
    allowance_resp = allowance(USDC, WALLET, ROUTER)
    result["allowance_usdc_wallet_to_router"] = allowance_resp
    allowance_raw = int(allowance_resp["result"], 16) if "result" in allowance_resp else None
    print(f"[debug] allowance(USDC, wallet->router) реально = {allowance_raw} (нужно >= {AMOUNT_IN_RAW})")

    usdc_bal_resp = balance_of(USDC, WALLET)
    result["usdc_balance"] = usdc_bal_resp
    usdc_bal_raw = int(usdc_bal_resp["result"], 16) if "result" in usdc_bal_resp else None
    print(f"[debug] USDC баланс реально = {usdc_bal_raw} (нужно >= {AMOUNT_IN_RAW})")

    cbbtc_bal_resp = balance_of(CBBTC, WALLET)
    result["cbbtc_balance"] = cbbtc_bal_resp
    print(f"[debug] cbBTC баланс реально = {cbbtc_bal_resp.get('result')}")

    eth_bal_resp = rpc("eth_getBalance", [WALLET, "latest"])
    result["eth_balance_base"] = eth_bal_resp
    print(f"[debug] ETH баланс на Base реально = {eth_bal_resp.get('result')}")

    print("\n=== Повторный eth_estimateGas для ТОЧНО ТОГО ЖЕ свопа (свежий deadline) ===")
    fresh_deadline = int(time.time()) + 600
    calldata = build_swap_calldata(fresh_deadline)
    result["retry_swap_calldata"] = calldata
    est_resp = estimate_gas(ROUTER, calldata)
    result["retry_estimate_gas"] = est_resp
    if "result" in est_resp:
        print(f"[debug] eth_estimateGas ТЕПЕРЬ реально успешен: {int(est_resp['result'], 16)} units "
              f"-- значит проблема была ТРАНЗИЕНТНОЙ (лаг RPC-реплики).")
        result["diagnosis"] = "TRANSIENT_RPC_LAG_LIKELY -- повторный estimateGas реально прошёл без изменений в allowance/balance"
    else:
        print(f"[debug] eth_estimateGas ВСЁ ЕЩЁ реально падает: {est_resp.get('error')}")
        if allowance_raw is not None and allowance_raw < AMOUNT_IN_RAW:
            result["diagnosis"] = f"REAL_INSUFFICIENT_ALLOWANCE -- allowance={allowance_raw} < needed={AMOUNT_IN_RAW}"
        elif usdc_bal_raw is not None and usdc_bal_raw < AMOUNT_IN_RAW:
            result["diagnosis"] = f"REAL_INSUFFICIENT_BALANCE -- balance={usdc_bal_raw} < needed={AMOUNT_IN_RAW}"
        else:
            result["diagnosis"] = "STF_PERSISTS_UNKNOWN_CAUSE -- allowance и баланс реально достаточны, revert всё равно есть -- нужна дальнейшая диагностика (например пул/токен-специфичный edge case)"
        print(f"[debug] диагноз: {result['diagnosis']}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[debug] результат записан в {OUT_PATH}")


if __name__ == "__main__":
    main()
