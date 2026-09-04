#!/usr/bin/env python3
"""Диагностика (только чтение, ничего не отправляет): реальный
depositV3(USDG->USDC) на реальном SpokePool Robinhood Chain упал при
eth_estimateGas с кастомной ошибкой `0x356680b7` -- ни один известный
error из V3SpokePoolInterface.sol/SpokePool.sol (35 проверено) не
совпал. Ищем реальный источник: publичная база сигнатур (4byte/openchain)
+ верифицированный исходник на Blockscout (Robinhood Chain), плюс
повторный eth_call с ТЕМИ ЖЕ параметрами для подтверждения (не
переисполняем -- то же самое чтение)."""
import json
import requests

SELECTOR = "0x356680b7"


def try_get(url, params=None, headers=None):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        return r.status_code, (r.json() if "json" in r.headers.get("content-type", "") else r.text[:2000])
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:300]


def main():
    result = {}

    print("=== 1. 4byte.directory ===")
    status, body = try_get("https://www.4byte.directory/api/v1/signatures/", {"hex_signature": SELECTOR})
    print(status, json.dumps(body)[:1000])
    result["4byte"] = {"status": status, "body": body}

    print("\n=== 2. openchain.xyz ===")
    status, body = try_get("https://api.openchain.xyz/signature-database/v1/lookup",
                            {"function": SELECTOR, "filter": "true"})
    print(status, json.dumps(body)[:1000])
    result["openchain"] = {"status": status, "body": body}

    print("\n=== 3. Blockscout Robinhood Chain -- verified source (getsourcecode) ===")
    status, body = try_get("https://robinhoodchain.blockscout.com/api",
                            {"module": "contract", "action": "getsourcecode",
                             "address": "0xD29C85F15DF544bA632C9E25829fd29d767d7978"})
    print(status, json.dumps(body)[:3000] if isinstance(body, dict) else str(body)[:2000])
    result["blockscout_source"] = {"status": status, "body_truncated": (json.dumps(body)[:5000] if isinstance(body, dict) else str(body)[:2000])}

    print("\n=== 4. Реальный повторный eth_call с ТЕМИ ЖЕ параметрами (подтверждение, не переисполнение) ===")
    # Реальные значения -- РАСКОДИРОВАНЫ из фактического traceback реального
    # прогона (GH Actions run 33930344312), не набраны вручную заново --
    # abi_encode здесь просто ВОСПРОИЗВОДИТ байт-в-байт ту же calldata,
    # что реально ушла в eth_estimateGas и получила этот revert (ручная
    # пересборка hex-строки уже один раз дала опечатку, обнаружено локально).
    from eth_abi import encode as abi_encode
    from Crypto.Hash import keccak as _keccak
    def _sel(sig: str) -> str:
        k = _keccak.new(digest_bits=256); k.update(sig.encode()); return "0x" + k.hexdigest()[:8]
    depositv3_sig = ("depositV3(address,address,address,address,uint256,uint256,uint256,"
                      "address,uint32,uint32,uint32,bytes)")
    types = ["address", "address", "address", "address", "uint256", "uint256", "uint256",
             "address", "uint32", "uint32", "uint32", "bytes"]
    values = [
        "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75",  # depositor
        "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75",  # recipient
        "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",  # inputToken (USDG)
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # outputToken (USDC)
        161000000, 160900058, 8453,                     # inputAmount, outputAmount, destinationChainId
        "0xfd03aBCAdAF3F930Fa4E37eb2f6Ea3A44a41b7F0",  # exclusiveRelayer (реальный из ответа котировки)
        1788564947, 1788572147, 3,                       # quoteTimestamp, fillDeadline, exclusivityDeadline
        b"",
    ]
    calldata = _sel(depositv3_sig) + abi_encode(types, values).hex()
    r = requests.post("https://rpc.mainnet.chain.robinhood.com", json={
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"from": "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75",
                     "to": "0xD29C85F15DF544bA632C9E25829fd29d767d7978", "data": calldata}, "latest"],
    }, timeout=30)
    print(r.status_code, r.text[:1500])
    result["eth_call_repro"] = {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:1500]}

    # чейн-статус SpokePool -- реальные view-геттеры (paused, chainId), если есть
    print("\n=== 5. Реальные view-геттеры SpokePool (pausedDeposits, chainId) ===")
    from Crypto.Hash import keccak
    def sel(sig):
        k = keccak.new(digest_bits=256); k.update(sig.encode()); return "0x" + k.hexdigest()[:8]
    for sig in ["pausedDeposits()", "chainId()"]:
        try:
            r2 = requests.post("https://rpc.mainnet.chain.robinhood.com", json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": "0xD29C85F15DF544bA632C9E25829fd29d767d7978", "data": sel(sig)}, "latest"],
            }, timeout=20)
            print(sig, "->", r2.json())
            result[sig] = r2.json()
        except Exception as e:  # noqa: BLE001
            print(sig, "error", e)

    with open("data/p3_guard_cache/p6_debug_deposit_revert_result.json", "w") as f:
        json.dump(result, f, indent=2, default=str, ensure_ascii=False)


if __name__ == "__main__":
    main()
