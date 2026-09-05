"""P6 -- сверка капитала (владелец, 2026-09-05): реальные балансы ДО входа
(после закрытия P5) -> каждая реальная транзакция -> реальные балансы
СЕЙЧАС, на трёх площадках (Robinhood Chain, Base, Lighter). Только чтение.

Проверяет, в частности: был ли реально исполнен своп WETH->USDG (владелец
спрашивает явно) -- если WETH-баланс СЕЙЧАС равен балансу ПОСЛЕ закрытия
P5 (0.060744817, RESULTS.md §4) с точностью до газа (WETH сам по себе не
тратится на газ, только native ETH) -- значит своп НЕ происходил, реально,
а не по памяти."""
import json
import time
from pathlib import Path

import requests

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "p3_guard_cache" / "p6_capital_reconciliation_result.json"

WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"

ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH_ROBINHOOD = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"

BASE_RPC = "https://mainnet.base.org"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
LIGHTER_ACCOUNT_INDEX = 22012

# Реальные балансы ПОСЛЕ закрытия P5 (RESULTS.md §4, п.4) -- задокументированный
# факт, не переоценивается здесь заново (тот момент времени уже прошёл).
BASELINE_AFTER_P5_CLOSE = {
    "robinhood_eth_native": 0.003660874,
    "robinhood_weth": 0.060744817,
    "robinhood_usdg": 80.282426,
    "lighter_collateral_usd": 61.883369,
}


def rpc(url: str, method: str, params: list):
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def erc20_balance(rpc_url: str, token: str, holder: str) -> int:
    selector = "70a08231"
    data = "0x" + selector + holder[2:].rjust(64, "0").lower()
    return int(rpc(rpc_url, "eth_call", [{"to": token, "data": data}, "latest"]), 16)


def native_balance(rpc_url: str, holder: str) -> int:
    return int(rpc(rpc_url, "eth_getBalance", [holder, "latest"]), 16)


def lighter_account() -> dict | None:
    try:
        r = requests.get(f"{LIGHTER_API_BASE}/api/v1/account", params={"by": "index", "value": str(LIGHTER_ACCOUNT_INDEX)}, timeout=20)
        r.raise_for_status()
        accounts = r.json().get("accounts", [])
        return accounts[0] if accounts else None
    except Exception as e:  # noqa: BLE001
        print(f"[recon] Lighter account недоступен: {e}")
        return None


def main():
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "baseline_after_p5_close": BASELINE_AFTER_P5_CLOSE}

    print("=== Реальные балансы СЕЙЧАС ===")
    robinhood_usdg_raw = erc20_balance(ROBINHOOD_RPC, USDG, WALLET)
    robinhood_weth_raw = erc20_balance(ROBINHOOD_RPC, WETH_ROBINHOOD, WALLET)
    robinhood_eth_raw = native_balance(ROBINHOOD_RPC, WALLET)
    base_usdc_raw = erc20_balance(BASE_RPC, USDC, WALLET)
    base_cbbtc_raw = erc20_balance(BASE_RPC, CBBTC, WALLET)
    base_eth_raw = native_balance(BASE_RPC, WALLET)

    robinhood_usdg = robinhood_usdg_raw / 1e6
    robinhood_weth = robinhood_weth_raw / 1e18
    robinhood_eth = robinhood_eth_raw / 1e18
    base_usdc = base_usdc_raw / 1e6
    base_cbbtc = base_cbbtc_raw / 1e8
    base_eth = base_eth_raw / 1e18

    print(f"[recon] Robinhood Chain: USDG={robinhood_usdg} WETH={robinhood_weth} ETH={robinhood_eth}")
    print(f"[recon] Base: USDC={base_usdc} cbBTC={base_cbbtc} ETH={base_eth}")

    lighter = lighter_account()
    lighter_collateral = float(lighter["collateral"]) if lighter else None
    lighter_available = float(lighter["available_balance"]) if lighter else None
    print(f"[recon] Lighter: collateral={lighter_collateral} available_balance={lighter_available}")

    weth_delta = robinhood_weth - BASELINE_AFTER_P5_CLOSE["robinhood_weth"]
    swap_weth_to_usdg_happened = abs(weth_delta) > 1e-9
    print(f"[recon] WETH дельта с момента закрытия P5: {weth_delta} -- "
          f"{'РЕАЛЬНО ИЗМЕНИЛСЯ (своп/перевод был)' if swap_weth_to_usdg_happened else 'НЕ ИЗМЕНИЛСЯ -- свопа WETH->USDG НЕ было'}")

    result["balances_now"] = {
        "robinhood_chain": {"usdg": robinhood_usdg, "weth": robinhood_weth, "eth_native": robinhood_eth},
        "base": {"usdc": base_usdc, "cbbtc": base_cbbtc, "eth_native": base_eth},
        "lighter": {"collateral_usd": lighter_collateral, "available_balance_usd": lighter_available},
    }
    result["weth_swap_check"] = {
        "weth_now": robinhood_weth, "weth_baseline_after_p5_close": BASELINE_AFTER_P5_CLOSE["robinhood_weth"],
        "weth_delta": weth_delta, "swap_weth_to_usdg_happened": swap_weth_to_usdg_happened,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[recon] результат записан в {OUT_PATH}")


if __name__ == "__main__":
    main()
