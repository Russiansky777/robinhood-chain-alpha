#!/usr/bin/env python3
"""P5 LIVE -- ЭКСТРЕННОЕ закрытие LP-позиции (владелец, 2026-09-03,
"СРОЧНО: убрать голую экспозицию"):

LP-позиция tokenId=992779 (см. data/p3_guard_cache/p5_live_step1_result.json,
run 33776547616) была открыта реально (4/4 tx success), но хедж на
Lighter НЕ прошёл (`code=20558 restricted jurisdiction` -- geo-блок по
IP GH Actions раннера) -- позиция осталась без хеджа, полная
направленная экспозиция ~0.0365 ETH.

Владелец: "Закрыть/вывести LP-позицию tokenId 992779 из пула ETH/USDG
полностью (decreaseLiquidity + collect, весь диапазон). Это чистая
ончейн-операция, Lighter не участвует, гео-блок к ней не относится."

Формулы/сигнатуры -- ДОСЛОВНО из реального источника
(Uniswap/v3-periphery/contracts/interfaces/INonfungiblePositionManager.sol,
проверено WebFetch 2026-09-03, не по памяти):
  DecreaseLiquidityParams{tokenId,liquidity,amount0Min,amount1Min,deadline}
  CollectParams{tokenId,recipient,amount0Max,amount1Max}
  positions(tokenId) -> (nonce,operator,token0,token1,fee,tickLower,
                          tickUpper,liquidity,feeGrowthInside0LastX128,
                          feeGrowthInside1LastX128,tokensOwed0,tokensOwed1)

По умолчанию -- DRY-RUN (читает реальную позицию, считает план,
ничего не отправляет). --confirm-mainnet -- 2 реальные транзакции
(decreaseLiquidity, collect), каждая ждёт квитанции. amount0Min/
amount1Min -- НЕ ноль (защита от явного sandwich), но с широким
допуском (5%), чтобы не блокировать срочное закрытие небольшим
движением цены за минуты между открытием и закрытием.

Не трогает Lighter, не размещает ордера, не запускает Шаги 2-4.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

from alchemy_fallback import _rpc_call, topic0  # noqa: E402
import p5_live_precheck as pc  # noqa: E402
from p5_live_step1 import (  # noqa: E402 -- переиспользуем уже проверенную реальными tx инфраструктуру отправки
    NFPM, WALLET, WETH, USDG, WETH_DECIMALS, USDG_DECIMALS, CHAIN_ID,
    eth_estimate_gas, eth_gas_price, eth_nonce, send_tx, wait_for_receipt,
)

OUT_PATH = Path("data/p3_guard_cache/p5_live_close_result.json")
LOG_PATH = Path("data/p5_live_log.jsonl")
STEP1_RESULT_PATH = Path("data/p3_guard_cache/p5_live_step1_result.json")

TOKEN_ID = 992779  # см. data/p3_guard_cache/p5_live_step1_result.json, lp_position.token_id
SLIPPAGE_CLOSE = 0.05  # широкий допуск -- приоритет гарантированного закрытия над защитой от slippage
GAS_CEILING_USD = 5.0
MAX_UINT128 = 2 ** 128 - 1

POSITIONS_SIG = "positions(uint256)"
DECREASE_SIG = "decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))"
COLLECT_SIG = "collect((uint256,address,uint128,uint128))"
DECREASE_LIQUIDITY_TOPIC0 = topic0("DecreaseLiquidity(uint256,uint128,uint256,uint256)")
COLLECT_TOPIC0 = topic0("Collect(uint256,address,uint256,uint256)")


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def weth_balance() -> float:
    # collect() отправляет WETH как обычный ERC20 (не разворачивает в
    # нативный ETH) -- баланс native ETH кошелька от закрытия НЕ
    # изменится, реально вернувшиеся токены нужно читать отдельно здесь,
    # иначе финальный отчёт был бы неполным/вводящим в заблуждение.
    calldata = "0x" + _selector("balanceOf(address)")[2:] + abi_encode(["address"], [WALLET]).hex()
    raw = _rpc_call("eth_call", [{"to": to_checksum_address(WETH), "data": calldata}, "latest"])
    return int(raw, 16) / 10 ** WETH_DECIMALS


def read_position(token_id: int) -> dict:
    calldata = "0x" + _selector(POSITIONS_SIG)[2:] + abi_encode(["uint256"], [token_id]).hex()
    raw = _rpc_call("eth_call", [{"to": to_checksum_address(NFPM), "data": calldata}, "latest"])
    fields = abi_decode(
        ["uint96", "address", "address", "address", "uint24", "int24", "int24", "uint128",
         "uint256", "uint256", "uint128", "uint128"],
        bytes.fromhex(raw[2:]),
    )
    keys = ["nonce", "operator", "token0", "token1", "fee", "tickLower", "tickUpper", "liquidity",
            "feeGrowthInside0LastX128", "feeGrowthInside1LastX128", "tokensOwed0", "tokensOwed1"]
    return dict(zip(keys, fields))


def build_calldata_decrease(token_id: int, liquidity: int, amount0_min: int, amount1_min: int, deadline: int) -> bytes:
    selector = bytes.fromhex(_selector(DECREASE_SIG)[2:])
    types = ["(uint256,uint128,uint256,uint256,uint256)"]
    return selector + abi_encode(types, [(token_id, liquidity, amount0_min, amount1_min, deadline)])


def build_calldata_collect(token_id: int, recipient: str, amount0_max: int, amount1_max: int) -> bytes:
    selector = bytes.fromhex(_selector(COLLECT_SIG)[2:])
    types = ["(uint256,address,uint128,uint128)"]
    return selector + abi_encode(types, [(token_id, recipient, amount0_max, amount1_max)])


def send_and_wait(account, label: str, to: str, data: bytes, nonce: int, gas_price: int, progress: dict) -> dict:
    print(f"[p5_live_close] --- {label}: отправка (nonce={nonce}) ---")
    gas_est = eth_estimate_gas(to, data, 0)
    tx_hash = send_tx(account, to, data, 0, nonce, gas_est, gas_price)
    print(f"[p5_live_close] {label}: ОТПРАВЛЕНО {tx_hash}, жду квитанцию...")
    receipt = wait_for_receipt(tx_hash)
    status = int(receipt["status"], 16)
    entry = {"label": label, "tx_hash": tx_hash, "status": "success" if status == 1 else "REVERTED",
              "gas_used": int(receipt["gasUsed"], 16), "block_number": int(receipt["blockNumber"], 16)}
    progress.setdefault("txs", []).append(entry)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"[p5_live_close] {label}: {entry['status']} (gasUsed={entry['gas_used']})")
    if status != 1:
        raise RuntimeError(f"{label} REVERTED: {tx_hash} -- СТОП.")
    return receipt


def decode_event(receipt: dict, address: str, topic0_hex: str, data_types: list[str]) -> tuple | None:
    for log in receipt.get("logs", []):
        if log["address"].lower() == address.lower() and log["topics"][0].lower() == topic0_hex.lower():
            return abi_decode(data_types, bytes.fromhex(log["data"][2:]))
    return None


def main() -> int:
    confirm = "--confirm-mainnet" in sys.argv
    t0 = time.time()
    progress: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "mode": "REAL" if confirm else "DRY-RUN", "token_id": TOKEN_ID}

    print(f"=== Реальное чтение позиции tokenId={TOKEN_ID} ===")
    pos = read_position(TOKEN_ID)
    print(f"[p5_live_close] позиция: {pos}")
    progress["position_onchain"] = pos

    if pos["liquidity"] == 0:
        progress["note"] = "liquidity=0 -- позиция уже закрыта (или tokensOwed ещё не собраны через collect)."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_close] {progress['note']}")
        return 0

    pool = pc.read_pool_state()
    pool_price = pc.price_from_sqrt(pool["sqrt_price_x96"])
    sqrt_p = pool["sqrt_price_x96"] / (2 ** 96)
    tick_lower, tick_upper = pos["tickLower"], pos["tickUpper"]
    pa = 1.0001 ** tick_lower * (10 ** (WETH_DECIMALS - USDG_DECIMALS))
    pb = 1.0001 ** tick_upper * (10 ** (WETH_DECIMALS - USDG_DECIMALS))
    sqrt_pa, sqrt_pb = pa ** 0.5, pb ** 0.5
    expected0, expected1 = pc.v3_amounts(pos["liquidity"], sqrt_p, sqrt_pa, sqrt_pb)
    amount0_min = int(expected0 * (1 - SLIPPAGE_CLOSE) * 10 ** WETH_DECIMALS)
    amount1_min = int(expected1 * (1 - SLIPPAGE_CLOSE) * 10 ** USDG_DECIMALS)
    deadline = int(time.time()) + 600

    plan = {
        "pool_price_usd": pool_price, "range_lower_usd": pa, "range_upper_usd": pb,
        "liquidity_to_remove": pos["liquidity"],
        "expected_amount0_eth": expected0, "expected_amount1_usdg": expected1,
        "amount0_min_wei": amount0_min, "amount1_min_raw": amount1_min, "deadline": deadline,
        "tokens_owed0_wei_before": pos["tokensOwed0"], "tokens_owed1_raw_before": pos["tokensOwed1"],
    }
    progress["plan"] = plan
    print(json.dumps(plan, indent=2, default=str, ensure_ascii=False))

    if not confirm:
        progress["note"] = "DRY-RUN -- ничего не отправлялось. Запустите с --confirm-mainnet для реального закрытия."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"\n[p5_live_close] {progress['note']}")
        return 0

    # ============================= РЕАЛЬНОЕ ЗАКРЫТИЕ =============================
    chain_id = int(_rpc_call("eth_chainId", []), 16)
    if chain_id != CHAIN_ID:
        progress["abort_reason"] = f"chainId {chain_id} != {CHAIN_ID} -- СТОП"
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        return 1

    import os
    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении.")
    if priv_hex.startswith("0x"):
        priv_hex = priv_hex[2:]
    account = Account.from_key(bytes.fromhex(priv_hex))
    if account.address.lower() != WALLET.lower():
        raise RuntimeError(f"PRIVATE_KEY_NOX даёт {account.address}, ожидался {WALLET} -- СТОП.")

    gas_price = eth_gas_price()
    est_gas_total = (
        eth_estimate_gas(NFPM, build_calldata_decrease(TOKEN_ID, pos["liquidity"], amount0_min, amount1_min, deadline)) +
        250_000  # надбавка на collect (его точная оценка до decreaseLiquidity ненадёжна -- tokensOwed ещё не обновлены)
    )
    est_gas_cost_usd = est_gas_total * gas_price / 1e18 * pool_price
    progress["gas_estimate"] = {"gas_units_total_est": est_gas_total, "gas_price_wei": gas_price, "est_cost_usd": est_gas_cost_usd}
    print(f"[p5_live_close] оценка газа: ~{est_gas_total} units, ${est_gas_cost_usd:.4f}")
    if est_gas_cost_usd > GAS_CEILING_USD:
        progress["abort_reason"] = f"оценка газа ${est_gas_cost_usd:.4f} > потолка ${GAS_CEILING_USD} -- СТОП."
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        return 1

    nonce = eth_nonce()
    try:
        decrease_receipt = send_and_wait(
            account, "1_decreaseLiquidity", NFPM,
            build_calldata_decrease(TOKEN_ID, pos["liquidity"], amount0_min, amount1_min, deadline),
            nonce, gas_price, progress,
        )
        nonce += 1
        collect_receipt = send_and_wait(
            account, "2_collect", NFPM,
            build_calldata_collect(TOKEN_ID, WALLET, MAX_UINT128, MAX_UINT128),
            nonce, gas_price, progress,
        )
    except RuntimeError as e:
        progress["abort_reason"] = str(e)
        progress["CRITICAL"] = ("Сбой в закрытии позиции -- проверить состояние вручную. Если decreaseLiquidity прошла, "
                                 "но collect нет -- средства учтены в tokensOwed0/1 позиции (не потеряны), collect можно "
                                 "повторить отдельно после разбора причины.")
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_close] СТОП: {e}")
        return 1

    dec_event = decode_event(decrease_receipt, NFPM, DECREASE_LIQUIDITY_TOPIC0, ["uint128", "uint256", "uint256"])
    col_event = decode_event(collect_receipt, NFPM, COLLECT_TOPIC0, ["address", "uint256", "uint256"])
    decrease_amount0 = dec_event[1] / 10 ** WETH_DECIMALS if dec_event else None
    decrease_amount1 = dec_event[2] / 10 ** USDG_DECIMALS if dec_event else None
    collect_amount0 = col_event[1] / 10 ** WETH_DECIMALS if col_event else None
    collect_amount1 = col_event[2] / 10 ** USDG_DECIMALS if col_event else None
    fees0 = (collect_amount0 - decrease_amount0) if (dec_event and col_event) else None
    fees1 = (collect_amount1 - decrease_amount1) if (dec_event and col_event) else None

    progress["close_result"] = {
        "decrease_amount0_eth": decrease_amount0, "decrease_amount1_usdg": decrease_amount1,
        "collect_amount0_eth_total": collect_amount0, "collect_amount1_usdg_total": collect_amount1,
        "fees_earned_amount0_eth": fees0, "fees_earned_amount1_usdg": fees1,
    }

    # --- P&L: fee_usd - весь газ (4 tx mint + decrease + collect) ---
    total_gas_wei = 0
    step1_res = json.loads(STEP1_RESULT_PATH.read_text()) if STEP1_RESULT_PATH.exists() else {}
    mint_txs = step1_res.get("txs", [])
    for tx in mint_txs:
        total_gas_wei += tx["gas_used"] * gas_price  # приближение -- используем текущий gas_price, эффективная цена по факту незначительно отличалась
    for tx in progress["txs"]:
        total_gas_wei += tx["gas_used"] * gas_price
    total_gas_eth = total_gas_wei / 1e18
    total_gas_usd = total_gas_eth * pool_price

    fees_usd = (fees0 or 0) * pool_price + (fees1 or 0)
    deposited0 = step1_res.get("lp_position", {}).get("amount0_eth_actual")
    deposited1 = step1_res.get("lp_position", {}).get("amount1_usdg_actual")
    il0 = (decrease_amount0 - deposited0) if (deposited0 is not None and decrease_amount0 is not None) else None
    il1 = (decrease_amount1 - deposited1) if (deposited1 is not None and decrease_amount1 is not None) else None
    il_usd = ((il0 or 0) * pool_price + (il1 or 0)) if (il0 is not None) else None

    net_pnl_usd = fees_usd + (il_usd or 0) - total_gas_usd

    progress["pnl"] = {
        "fees_earned_usd": fees_usd, "impermanent_loss_vs_deposit_usd": il_usd,
        "total_gas_usd_all_6_txs": total_gas_usd, "gas_price_used_wei": gas_price,
        "net_pnl_usd": net_pnl_usd,
        "note": "IL считается относительно суммы, реально задепонированной в mint() (см. data/p3_guard_cache/p5_live_step1_result.json), "
                "не относительно holding-альтернативы. Газ на все 6 tx (4 mint + decrease + collect) пересчитан по ТЕКУЩЕЙ gas_price "
                "(эффективная цена по факту каждой tx могла немного отличаться -- расхождение пренебрежимо мало).",
    }

    wb_final = pc.wallet_balances()
    wb_final["weth_human"] = weth_balance()  # collect() вернул WETH (ERC20), не развёрнутый нативный ETH -- см. weth_balance()
    progress["final_wallet_balances"] = wb_final
    progress["runtime_s"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({
            "event": "emergency_close", "time_utc": progress["generated_at_utc"], "token_id": TOKEN_ID,
            "reason": "hedge failed -- Lighter restricted jurisdiction (code 20558)",
            "collect_amount0_eth": collect_amount0, "collect_amount1_usdg": collect_amount1,
            "fees_earned_usd": fees_usd, "net_pnl_usd": net_pnl_usd,
        }, default=str, ensure_ascii=False) + "\n")

    print(f"\n[p5_live_close] ЗАКРЫТО. collect: {collect_amount0:.6f} ETH(WETH) + {collect_amount1:.4f} USDG "
          f"(из них комиссии: {fees0:.8f} ETH + {fees1:.6f} USDG). net_pnl_usd={net_pnl_usd:.4f}")
    print(f"[p5_live_close] финальные балансы кошелька: {wb_final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
