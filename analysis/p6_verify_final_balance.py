"""P6 -- реальная перепроверка финального баланса USDC на Base после
закрытия (result.json показал usdc=9e-06 сразу после collect() на
43.839 USDC -- подозрение на тот же лаг RPC-реплики mainnet.base.org,
что уже реально был найден и задокументирован при входе, docs/
PROJECT_STATE.md #11). Только чтение."""
import json
import time
from pathlib import Path

import requests

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "p3_guard_cache" / "p6_verify_final_balance_result.json"
BASE_RPC = "https://mainnet.base.org"
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"


def rpc(method, params):
    r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    return r.json()["result"]


def balance(token):
    data = "0x70a08231" + WALLET[2:].rjust(64, "0").lower()
    return int(rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def main():
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reads": []}
    for i in range(3):
        usdc_raw = balance(USDC)
        cbbtc_raw = balance(CBBTC)
        tx_receipt = rpc("eth_getTransactionReceipt", ["0x00015359fc30814582c750b56894f1c5d720e74fb96a218ff013cf8c219f9a49"])
        row = {"attempt": i + 1, "usdc": usdc_raw / 1e6, "cbbtc": cbbtc_raw / 1e8,
               "collect_tx_status": int(tx_receipt["status"], 16) if tx_receipt else None,
               "collect_tx_block": int(tx_receipt["blockNumber"], 16) if tx_receipt else None}
        latest_block = int(rpc("eth_blockNumber", []), 16)
        row["latest_block_now"] = latest_block
        print(f"[verify] попытка {i+1}: USDC={row['usdc']} cbBTC={row['cbbtc']} collect_tx_status={row['collect_tx_status']} "
              f"collect_tx_block={row['collect_tx_block']} latest_block_now={latest_block}")
        result["reads"].append(row)
        if i < 2:
            time.sleep(10)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[verify] результат записан в {OUT_PATH}")


if __name__ == "__main__":
    main()
