#!/usr/bin/env python3
"""P6 LIVE -- реальное закрытие (владелец, 2026-09-05, "Закрыть P6. Да."):
1) закрыть BTC-шорт на Lighter (reduce_only, market_id=1)
2) decreaseLiquidity(ALL)+collect на tokenId=76445294 (Base, NFPM)
3) финальный PnL по обеим ногам, реальные tx-хэши

Порядок, формулы и дисциплина -- ДОСЛОВНО те же, что реальное закрытие P5
(RESULTS.md §1-3, analysis/p5_live_close.py): хедж закрывается ПЕРВЫМ
(reduce_only), коллатерал до/после -- основной метод расчёта PnL хеджа
(метод 1), unrealized_pnl+funding на момент закрытия -- сверочный (метод
2). LP закрывается decreaseLiquidity(ALL)+collect(MAX), fees=collect-decrease,
IL относительно реального депозита на входе (data/p6_live_position_state.json).
Хедж подтверждается ЧТЕНИЕМ позиции (урок err=None), не отсутствием ошибки.

Дребезг cbBTC (~$17, накопленный на кошельке ещё с реального входа,
RESULTS.md §8) -- НЕ трогается, остаётся на Base (владелец: "оставить как
есть, не гонять обратно ради центов").
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402
from eth_account import Account  # noqa: E402

from p6_live_step1 import (  # noqa: E402 -- переиспользуем уже проверенную реальными tx инфраструктуру
    WALLET, BASE_RPC, BASE_CHAIN_ID, USDC, CBBTC, USDC_DECIMALS, CBBTC_DECIMALS, POOL_ADDRESS,
    LIGHTER_API_BASE, LIGHTER_ACCOUNT_INDEX, LIGHTER_API_KEY_INDEX, HEDGE_SLIPPAGE,
    _selector, rpc, eth_call, erc20_balance, eth_gas_price, eth_nonce,
    send_and_wait, read_pool_state_base, price_cbbtc_usd, tick_to_usd_price,
    lighter_account_full, lighter_btc_market,
)

OUT_PATH = Path("data/p3_guard_cache/p6_live_close_result.json")
STATE_PATH = Path("data/p6_live_position_state.json")

NFPM = "0x827922686190790b37229fd06084350E74485b72"  # подтверждено разведкой (p6_entry_recon.py), тот же на входе
TOKEN_ID = 76445294
MINT_SLIPPAGE_CLOSE = 0.10  # тот же широкий допуск, что вход (docs/PROJECT_STATE.md #13) -- приоритет гарантированного закрытия
MAX_UINT128 = 2 ** 128 - 1


def nfpm_position(token_id: int) -> dict:
    calldata = "0x" + _selector("positions(uint256)")[2:] + hex(token_id)[2:].rjust(64, "0")
    raw = eth_call(BASE_RPC, NFPM, calldata)
    fields = abi_decode(
        ["uint96", "address", "address", "address", "int24", "int24", "int24",
         "uint128", "uint256", "uint256", "uint128", "uint128"],
        bytes.fromhex(raw[2:]),
    )
    keys = ["nonce", "operator", "token0", "token1", "tick_spacing", "tick_lower", "tick_upper",
            "liquidity", "fee_growth0", "fee_growth1", "tokens_owed0", "tokens_owed1"]
    return dict(zip(keys, fields))


def decode_event(receipt: dict, address: str, topic0_hex: str, data_types: list[str]):
    for log in receipt.get("logs", []):
        if log["address"].lower() == address.lower() and log["topics"][0].lower() == topic0_hex.lower():
            return abi_decode(data_types, bytes.fromhex(log["data"][2:]))
    return None


def _topic0(sig: str) -> str:
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


DECREASE_LIQUIDITY_TOPIC0 = _topic0("DecreaseLiquidity(uint256,uint128,uint256,uint256)")
COLLECT_TOPIC0 = _topic0("Collect(uint256,address,uint256,uint256)")


def main() -> int:
    t0 = time.time()
    progress: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": "REAL"}

    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении.")
    if priv_hex.startswith("0x"):
        priv_hex = priv_hex[2:]
    account = Account.from_key(bytes.fromhex(priv_hex))
    if account.address.lower() != WALLET.lower():
        raise RuntimeError(f"PRIVATE_KEY_NOX даёт {account.address}, ожидался {WALLET} -- СТОП.")

    eth_usd_price = None
    try:
        import requests
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "ethereum", "vs_currencies": "usd"},
                          headers={"User-Agent": "robinhood-chain-alpha-p6/1.0"}, timeout=20)
        eth_usd_price = float(r.json()["ethereum"]["usd"])
    except Exception as exc:  # noqa: BLE001
        print(f"[p6_close] ETH/USD недоступен ({exc}) -- потолок газа пропущен.")

    # ============================= ШАГ 1: закрыть BTC-шорт на Lighter (ПЕРВЫМ) =============================
    print("=== ШАГ 1: закрытие BTC-шорта на Lighter (reduce_only, market_id=1) -- ПЕРЕД LP ===")
    account_full_before = lighter_account_full()
    if not account_full_before:
        raise RuntimeError("Lighter account недоступен -- СТОП, не закрываю вслепую.")
    btc_pos_before = next((p for p in account_full_before.get("positions", [])
                            if str(p.get("symbol", "")).upper() == "BTC" and abs(float(p.get("position", 0))) > 1e-9), None)
    collateral_before = float(account_full_before.get("collateral", 0))
    print(f"[p6_close] реальный коллатерал ДО закрытия хеджа = ${collateral_before}")
    progress["hedge_before"] = {"position": btc_pos_before, "collateral_usd": collateral_before}

    hedge_close_result = None
    if btc_pos_before is None:
        print("[p6_close] реальной BTC-позиции нет -- шаг хеджа пропущен (нечего закрывать).")
        progress["hedge_close_note"] = "позиция уже 0 -- ничего не отправлено"
    else:
        size_btc = abs(float(btc_pos_before["position"]))
        is_short = int(btc_pos_before.get("sign", -1)) < 0 or float(btc_pos_before["position"]) < 0
        avg_entry_orig = float(btc_pos_before.get("avg_entry_price", 0))
        unrealized_pnl_at_close = float(btc_pos_before.get("unrealized_pnl", 0))
        realized_pnl_before = float(btc_pos_before.get("realized_pnl", 0))
        funding_received_before = float(btc_pos_before.get("total_funding_paid_out", 0))
        market = lighter_btc_market()
        if not market:
            raise RuntimeError("Lighter BTC market недоступен -- СТОП.")

        async def _close_hedge() -> dict:
            import lighter
            lighter_priv = os.environ["LIGHTER_API_KEY_PRIVATE"]
            client = lighter.SignerClient(url=LIGHTER_API_BASE, account_index=LIGHTER_ACCOUNT_INDEX,
                                           api_private_keys={LIGHTER_API_KEY_INDEX: lighter_priv})
            try:
                base_amount = round(size_btc * 10 ** market["size_decimals"])
                client_order_index = int(time.time() * 1000) % (2 ** 31)
                tx, resp, err = await client.create_market_order_limited_slippage(
                    market_index=market["market_id"], client_order_index=client_order_index, base_amount=base_amount,
                    max_slippage=HEDGE_SLIPPAGE, is_ask=not is_short, reduce_only=True, api_key_index=LIGHTER_API_KEY_INDEX,
                )
                return {"tx_hash": resp.tx_hash if resp else None, "resp_code": resp.code if resp else None,
                        "resp_message": resp.message if resp else None, "err": str(err) if err is not None else None,
                        "base_amount": base_amount, "is_ask": not is_short, "size_btc_requested": size_btc}
            finally:
                await client.close()

        print(f"[p6_close] реально ДО: шорт={size_btc} BTC avg_entry=${avg_entry_orig} unrealized_pnl=${unrealized_pnl_at_close} "
              f"realized_pnl_cum=${realized_pnl_before} funding_cum=${funding_received_before}")
        order_info = asyncio.run(_close_hedge())
        progress["hedge_close_order"] = order_info
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        if order_info.get("err") is not None:
            raise RuntimeError(f"закрытие хеджа вернуло ошибку: {order_info['err']} -- СТОП, LP НЕ трогаю.")

        # Подтверждение ЧТЕНИЕМ позиции (урок err=None) -- не доверяем ответу без проверки.
        filled = False
        last_pos = None
        for i in range(6):
            if i > 0:
                time.sleep(3)
            acc = lighter_account_full()
            last_pos = next((p for p in (acc.get("positions", []) if acc else [])
                              if str(p.get("symbol", "")).upper() == "BTC"), None)
            if last_pos is None or abs(float(last_pos.get("position", 0))) < 1e-9:
                filled = True
                break
            print(f"[p6_close] проверка закрытия хеджа: попытка {i + 1}/6 -- ещё видно {last_pos.get('position')}")
        if not filled:
            progress["CRITICAL"] = (f"err=None на закрытии хеджа, но реальная BTC-позиция НЕ обнулилась после 6 проверок "
                                     f"(последнее чтение: {last_pos}). LP-позиция НЕ закрыта автоматически -- "
                                     "ТРЕБУЕТСЯ РУЧНОЕ ВМЕШАТЕЛЬСТВО ВЛАДЕЛЬЦА.")
            OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
            print(f"[p6_close] {progress['CRITICAL']}")
            return 1

        account_full_after = lighter_account_full()
        collateral_after = float(account_full_after.get("collateral", 0))
        method1_pnl = collateral_after - collateral_before
        method2_pnl = unrealized_pnl_at_close + funding_received_before
        print(f"[p6_close] РЕАЛЬНО закрыт хедж. collateral: ${collateral_before} -> ${collateral_after} "
              f"(метод1={method1_pnl}, метод2(сверка)={method2_pnl})")
        hedge_close_result = {
            "size_btc": size_btc, "avg_entry_price_usd": avg_entry_orig,
            "unrealized_pnl_at_close_usd": unrealized_pnl_at_close, "realized_pnl_cum_usd": realized_pnl_before,
            "funding_received_cum_usd": funding_received_before,
            "collateral_before_usd": collateral_before, "collateral_after_usd": collateral_after,
            "method1_pnl_usd": method1_pnl, "method2_pnl_usd": method2_pnl,
        }
        progress["hedge_close_result"] = hedge_close_result
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    # ============================= ШАГ 2: decreaseLiquidity(ALL) + collect на 76445294 =============================
    print(f"\n=== ШАГ 2: реальное закрытие LP tokenId={TOKEN_ID} (decreaseLiquidity(ALL) + collect(MAX)) ===")
    pos = nfpm_position(TOKEN_ID)
    print(f"[p6_close] реальная позиция: liquidity={pos['liquidity']} tick_lower={pos['tick_lower']} tick_upper={pos['tick_upper']}")
    progress["lp_position_before_close"] = {k: v for k, v in pos.items()}
    if pos["liquidity"] == 0:
        progress["lp_close_note"] = "liquidity=0 -- позиция уже закрыта."
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p6_close] {progress['lp_close_note']}")
    else:
        pool = read_pool_state_base()
        p0 = price_cbbtc_usd(pool["sqrtPriceX96"])
        # НАЙДЕНО (реальный прогон, run 1, revert "PS" = NFPM.decreaseLiquidity
        # "Price Slippage", исходник подтверждён WebFetch): РЕАЛЬНАЯ ончейн
        # `liquidity` (raw, из positions()) НЕЛЬЗЯ комбинировать со sqrt в
        # "человеческом" (1/p_usd) домене (тем, что использует get_liquidity_
        # for_amounts/v3_amounts выше по файлу для расчёта ЖЕЛАЕМЫХ сумм при
        # mint) -- это ДВА РАЗНЫХ домена. РЕАЛЬНАЯ формула (тот же урок, что
        # уже задокументирован в analysis/p5_live_close.py после реального
        # инцидента 33777423316, amount0 получался 78 МЛРД ETH): raw
        # liquidity нужно комбинировать ТОЛЬКО с СЫРЫМ sqrtPriceX96/2^96 (без
        # какой-либо decimals/price-адаptации) -- результат сразу в raw wei
        # (token0/token1 minimal units), делим на 10**decimals В КОНЦЕ.
        sqrt_p_raw = pool["sqrtPriceX96"] / (2 ** 96)
        sqrt_pa_raw = (1.0001 ** pos["tick_lower"]) ** 0.5
        sqrt_pb_raw = (1.0001 ** pos["tick_upper"]) ** 0.5
        if sqrt_pa_raw > sqrt_pb_raw:
            sqrt_pa_raw, sqrt_pb_raw = sqrt_pb_raw, sqrt_pa_raw
        sqrt_p_clamped = min(max(sqrt_p_raw, sqrt_pa_raw), sqrt_pb_raw)
        expected0 = max(pos["liquidity"] * (1 / sqrt_p_clamped - 1 / sqrt_pb_raw), 0.0)
        expected1 = max(pos["liquidity"] * (sqrt_p_clamped - sqrt_pa_raw), 0.0)
        amount0_min = int(expected0 * (1 - MINT_SLIPPAGE_CLOSE))
        amount1_min = int(expected1 * (1 - MINT_SLIPPAGE_CLOSE))
        deadline = int(time.time()) + 600
        print(f"[p6_close] реальный ожидаемый возврат: amount0(USDC)~={expected0/10**USDC_DECIMALS:.6f} "
              f"amount1(cbBTC)~={expected1/10**CBBTC_DECIMALS:.8f}")
        progress["lp_close_plan"] = {"pool_price_usd": p0, "expected_amount0_usdc": expected0 / 10 ** USDC_DECIMALS,
                                      "expected_amount1_cbbtc": expected1 / 10 ** CBBTC_DECIMALS}
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

        decrease_selector = bytes.fromhex(_selector("decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))")[2:])
        decrease_calldata = decrease_selector + abi_encode(
            ["(uint256,uint128,uint256,uint256,uint256)"],
            [(TOKEN_ID, pos["liquidity"], int(amount0_min), int(amount1_min), deadline)],
        )
        nonce_base = eth_nonce(BASE_RPC)
        decrease_receipt = send_and_wait(BASE_RPC, BASE_CHAIN_ID, account, "1_decreaseLiquidity", NFPM,
                                          decrease_calldata, 0, nonce_base, progress, eth_usd_price)
        nonce_base += 1

        collect_selector = bytes.fromhex(_selector("collect((uint256,address,uint128,uint128))")[2:])
        collect_calldata = collect_selector + abi_encode(
            ["(uint256,address,uint128,uint128)"], [(TOKEN_ID, to_checksum_address(WALLET), MAX_UINT128, MAX_UINT128)],
        )
        collect_receipt = send_and_wait(BASE_RPC, BASE_CHAIN_ID, account, "2_collect", NFPM,
                                         collect_calldata, 0, nonce_base, progress, eth_usd_price)

        dec_event = decode_event(decrease_receipt, NFPM, DECREASE_LIQUIDITY_TOPIC0, ["uint128", "uint256", "uint256"])
        col_event = decode_event(collect_receipt, NFPM, COLLECT_TOPIC0, ["address", "uint256", "uint256"])
        decrease_amount0 = dec_event[1] / 10 ** USDC_DECIMALS if dec_event else None
        decrease_amount1 = dec_event[2] / 10 ** CBBTC_DECIMALS if dec_event else None
        collect_amount0 = col_event[1] / 10 ** USDC_DECIMALS if col_event else None
        collect_amount1 = col_event[2] / 10 ** CBBTC_DECIMALS if col_event else None
        fees0 = (collect_amount0 - decrease_amount0) if (dec_event and col_event) else None
        fees1 = (collect_amount1 - decrease_amount1) if (dec_event and col_event) else None
        print(f"[p6_close] РЕАЛЬНО: decrease amount0(USDC)={decrease_amount0} amount1(cbBTC)={decrease_amount1}")
        print(f"[p6_close] РЕАЛЬНО: collect amount0(USDC)={collect_amount0} amount1(cbBTC)={collect_amount1} "
              f"(комиссии: {fees0} USDC + {fees1} cbBTC)")

        lp_close_result = {
            "decrease_amount0_usdc": decrease_amount0, "decrease_amount1_cbbtc": decrease_amount1,
            "collect_amount0_usdc_total": collect_amount0, "collect_amount1_cbbtc_total": collect_amount1,
            "fees_earned_amount0_usdc": fees0, "fees_earned_amount1_cbbtc": fees1,
        }
        progress["lp_close_result"] = lp_close_result
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    # ============================= Финальные балансы =============================
    print("\n=== Реальные финальные балансы кошелька на Base ===")
    final_usdc = erc20_balance(BASE_RPC, USDC, WALLET) / 10 ** USDC_DECIMALS
    final_cbbtc = erc20_balance(BASE_RPC, CBBTC, WALLET) / 10 ** CBBTC_DECIMALS
    final_eth_base = int(rpc(BASE_RPC, "eth_getBalance", [WALLET, "latest"]), 16) / 1e18
    print(f"[p6_close] Base финально: USDC={final_usdc} cbBTC={final_cbbtc} ETH={final_eth_base}")
    progress["final_base_balances"] = {"usdc": final_usdc, "cbbtc": final_cbbtc, "eth_native": final_eth_base}

    final_account = lighter_account_full()
    progress["final_lighter"] = {"collateral_usd": float(final_account.get("collateral", 0)) if final_account else None,
                                  "available_balance_usd": float(final_account.get("available_balance", 0)) if final_account else None,
                                  "positions_open": len(final_account.get("positions", [])) if final_account else None}

    progress["runtime_s"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p6_close] ЗАВЕРШЕНО. Результат: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
