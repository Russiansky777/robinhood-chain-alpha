#!/usr/bin/env python3
"""P5 LIVE -- закрытие LP-позиции (decreaseLiquidity + collect).

Изначально написан как экстренный ручной скрипт (владелец, 2026-09-03,
"СРОЧНО: убрать голую экспозицию" -- LP tokenId=992779 была открыта, но
хедж на Lighter упал `code=20558 restricted jurisdiction`). Закрыт тем
же днём, net P&L -$1.30 (почти целиком газ короткого цикла).

Владелец, позже (тот же день, п.2 задачи Step1-с-VPS-Нидерланды): "Если
хедж-ордер вернёт ЛЮБУЮ ошибку -- НЕМЕДЛЕННО, автоматически, без
ожидания команды: закрыть только что открытую LP-позицию обратно... Это
не опционально." -- логика закрытия вынесена в переиспользуемую функцию
`close_position()`, чтобы p5_live_step1.py мог вызвать её ПРЯМО В ТОМ ЖЕ
процессе сразу после неудачного create_market_order -- без отдельного
workflow-раунда (который оставлял бы голую позицию открытой лишние
минуты на запуск нового job'а).

Формулы/сигнатуры -- ДОСЛОВНО из реального источника
(Uniswap/v3-periphery/contracts/interfaces/INonfungiblePositionManager.sol,
проверено WebFetch 2026-09-03, не по памяти):
  DecreaseLiquidityParams{tokenId,liquidity,amount0Min,amount1Min,deadline}
  CollectParams{tokenId,recipient,amount0Max,amount1Max}
  positions(tokenId) -> (nonce,operator,token0,token1,fee,tickLower,
                          tickUpper,liquidity,feeGrowthInside0LastX128,
                          feeGrowthInside1LastX128,tokensOwed0,tokensOwed1)

Правильная формула суммы из реального ончейн-liquidity (НЕ путать с
псевдо-единицами p5_live_precheck.py::v3_amounts -- та пара
самосогласована только с human-adjusted sqrt(price_usd), баг найден
реальным dry-run 33777423316, amount0 получался 78 МЛРД ETH): при
sqrtA=sqrtRatioAX96/2^96, sqrtB=sqrtRatioBX96/2^96 (СЫРЫЕ, БЕЗ
decimals-поправки), amount0=L*(1/sqrtP-1/sqrtB), amount1=L*(sqrtP-sqrtA)
-- результат СРАЗУ в raw wei/raw-USDG-unit (проверено вручную offline).

CLI (standalone): по умолчанию -- DRY-RUN (читает реальную позицию,
считает план, ничего не отправляет). --confirm-mainnet -- реальная
отправка. amount0Min/amount1Min -- НЕ ноль, но с широким допуском (5%)
-- приоритет гарантированного закрытия над защитой от slippage.

Не трогает Lighter, не размещает ордера, не запускает Шаги 2-4.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

from alchemy_fallback import _rpc_call, topic0  # noqa: E402
import p5_live_precheck as pc  # noqa: E402
from p5_live_step1 import (  # noqa: E402 -- переиспользуем уже проверенную реальными tx инфраструктуру отправки
    NFPM, WALLET, WETH, USDG, WETH_DECIMALS, USDG_DECIMALS, CHAIN_ID,
    eth_estimate_gas, eth_gas_price, eth_nonce, wait_for_receipt, send_with_gas_retry,
)

OUT_PATH = Path("data/p3_guard_cache/p5_live_close_result.json")
LOG_PATH = Path("data/p5_live_log.jsonl")
STEP1_RESULT_PATH = Path("data/p3_guard_cache/p5_live_step1_result.json")

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
    # изменится, реально вернувшиеся токены нужно читать отдельно здесь.
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


def send_and_wait(account, label: str, to: str, data: bytes, nonce: int, progress: dict, out_path: Path) -> dict:
    # Эскалирующийся retry на gas-race -- см. p5_live_step1.py::
    # send_with_gas_retry (общая реализация, реальный инцидент 33780888659).
    print(f"[p5_live_close] --- {label}: отправка (nonce={nonce}) ---")
    gas_est = eth_estimate_gas(to, data, 0)
    tx_hash, gas_price_used, buffer_used = send_with_gas_retry(account, to, data, 0, nonce, gas_est, label)
    print(f"[p5_live_close] {label}: ОТПРАВЛЕНО {tx_hash} (gas_price={gas_price_used}, buffer={buffer_used}), жду квитанцию...")
    receipt = wait_for_receipt(tx_hash)
    status = int(receipt["status"], 16)
    effective_gas_price = int(receipt["effectiveGasPrice"], 16) if receipt.get("effectiveGasPrice") else gas_price_used
    entry = {"label": label, "tx_hash": tx_hash, "status": "success" if status == 1 else "REVERTED",
              "gas_used": int(receipt["gasUsed"], 16), "block_number": int(receipt["blockNumber"], 16),
              "gas_price_offered_wei": gas_price_used, "buffer_used": buffer_used,
              "effective_gas_price_wei": effective_gas_price}
    progress.setdefault("close_txs", []).append(entry)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"[p5_live_close] {label}: {entry['status']} (gasUsed={entry['gas_used']})")
    if status != 1:
        raise RuntimeError(f"{label} REVERTED: {tx_hash} -- СТОП.")
    return receipt


def decode_event(receipt: dict, address: str, topic0_hex: str, data_types: list[str]) -> tuple | None:
    for log in receipt.get("logs", []):
        if log["address"].lower() == address.lower() and log["topics"][0].lower() == topic0_hex.lower():
            return abi_decode(data_types, bytes.fromhex(log["data"][2:]))
    return None


def close_position(
    token_id: int, account, progress: dict, *,
    known_deposit0: float | None = None, known_deposit1: float | None = None,
    prior_gas_txs: list[dict] | None = None, out_path: Path = OUT_PATH,
) -> dict:
    """Реальное закрытие (decreaseLiquidity + collect) -- используется и
    из main() (CLI) и напрямую из p5_live_step1.py при неудачном хедже
    (в ТОМ ЖЕ процессе, без отдельного workflow-раунда). Пишет прогресс
    в progress["close_plan"]/progress["close_txs"]/progress["close_result"]/
    progress["close_pnl"] по ходу (восстановимо при сбое). Поднимает
    RuntimeError, если сама отправка не удалась -- progress к этому
    моменту уже содержит всё, что было сделано."""
    print(f"=== Реальное чтение позиции tokenId={token_id} ===")
    pos = read_position(token_id)
    print(f"[p5_live_close] позиция: {pos}")
    progress["close_position_onchain"] = pos
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    if pos["liquidity"] == 0:
        progress["close_note"] = "liquidity=0 -- позиция уже закрыта (или tokensOwed ещё не собраны через collect)."
        out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_close] {progress['close_note']}")
        return {}

    pool = pc.read_pool_state()
    pool_price = pc.price_from_sqrt(pool["sqrt_price_x96"])
    tick_lower, tick_upper = pos["tickLower"], pos["tickUpper"]

    sqrt_p_raw = pool["sqrt_price_x96"] / (2 ** 96)
    sqrt_pa_raw = (1.0001 ** tick_lower) ** 0.5
    sqrt_pb_raw = (1.0001 ** tick_upper) ** 0.5
    if sqrt_pa_raw > sqrt_pb_raw:
        sqrt_pa_raw, sqrt_pb_raw = sqrt_pb_raw, sqrt_pa_raw
    sqrt_p_clamped = min(max(sqrt_p_raw, sqrt_pa_raw), sqrt_pb_raw)
    amount0_raw = max(pos["liquidity"] * (1 / sqrt_p_clamped - 1 / sqrt_pb_raw), 0.0)
    amount1_raw = max(pos["liquidity"] * (sqrt_p_clamped - sqrt_pa_raw), 0.0)
    expected0 = amount0_raw / 10 ** WETH_DECIMALS
    expected1 = amount1_raw / 10 ** USDG_DECIMALS

    amount0_min = int(expected0 * (1 - SLIPPAGE_CLOSE) * 10 ** WETH_DECIMALS)
    amount1_min = int(expected1 * (1 - SLIPPAGE_CLOSE) * 10 ** USDG_DECIMALS)
    deadline = int(time.time()) + 600

    plan = {
        "pool_price_usd": pool_price, "liquidity_to_remove": pos["liquidity"],
        "expected_amount0_eth": expected0, "expected_amount1_usdg": expected1,
        "amount0_min_wei": amount0_min, "amount1_min_raw": amount1_min, "deadline": deadline,
    }
    if known_deposit0 is not None and known_deposit1 is not None:
        sane0 = 0.7 * known_deposit0 <= expected0 <= 1.3 * known_deposit0
        sane1 = 0.7 * known_deposit1 <= expected1 <= 1.3 * known_deposit1
        plan["sanity_check_vs_known_deposit"] = {"known_deposit0_eth": known_deposit0, "known_deposit1_usdg": known_deposit1,
                                                  "sane0": sane0, "sane1": sane1}
        if not (sane0 and sane1):
            progress["close_plan"] = plan
            msg = (f"expected_amount0/1 ({expected0}, {expected1}) отклоняются от известного депозита "
                   f"({known_deposit0}, {known_deposit1}) более чем на 30% -- СТОП, не закрываю вслепую.")
            progress["close_abort_reason"] = msg
            out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
            print(f"[p5_live_close] {msg}")
            raise RuntimeError(msg)

    progress["close_plan"] = plan
    out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(json.dumps(plan, indent=2, default=str, ensure_ascii=False))

    chain_id = int(_rpc_call("eth_chainId", []), 16)
    if chain_id != CHAIN_ID:
        msg = f"chainId {chain_id} != {CHAIN_ID} -- СТОП"
        progress["close_abort_reason"] = msg
        out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        raise RuntimeError(msg)

    gas_price = eth_gas_price()
    est_gas_total = (
        eth_estimate_gas(NFPM, build_calldata_decrease(token_id, pos["liquidity"], amount0_min, amount1_min, deadline)) +
        250_000  # надбавка на collect (его точная оценка до decreaseLiquidity ненадёжна)
    )
    est_gas_cost_usd = est_gas_total * gas_price / 1e18 * pool_price
    print(f"[p5_live_close] оценка газа: ~{est_gas_total} units, ${est_gas_cost_usd:.4f}")
    if est_gas_cost_usd > GAS_CEILING_USD:
        msg = f"оценка газа ${est_gas_cost_usd:.4f} > потолка ${GAS_CEILING_USD} -- СТОП."
        progress["close_abort_reason"] = msg
        out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        raise RuntimeError(msg)

    nonce = eth_nonce()
    try:
        decrease_receipt = send_and_wait(
            account, "1_decreaseLiquidity", NFPM,
            build_calldata_decrease(token_id, pos["liquidity"], amount0_min, amount1_min, deadline),
            nonce, progress, out_path,
        )
        nonce += 1
        collect_receipt = send_and_wait(
            account, "2_collect", NFPM,
            build_calldata_collect(token_id, WALLET, MAX_UINT128, MAX_UINT128),
            nonce, progress, out_path,
        )
    except RuntimeError as e:
        progress["close_abort_reason"] = str(e)
        progress["CRITICAL_CLOSE"] = ("Сбой в закрытии позиции -- проверить состояние вручную. Если decreaseLiquidity прошла, "
                                       "но collect нет -- средства учтены в tokensOwed0/1 позиции (не потеряны), collect можно "
                                       "повторить отдельно после разбора причины.")
        out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_close] СТОП: {e}")
        raise

    dec_event = decode_event(decrease_receipt, NFPM, DECREASE_LIQUIDITY_TOPIC0, ["uint128", "uint256", "uint256"])
    col_event = decode_event(collect_receipt, NFPM, COLLECT_TOPIC0, ["address", "uint256", "uint256"])
    decrease_amount0 = dec_event[1] / 10 ** WETH_DECIMALS if dec_event else None
    decrease_amount1 = dec_event[2] / 10 ** USDG_DECIMALS if dec_event else None
    collect_amount0 = col_event[1] / 10 ** WETH_DECIMALS if col_event else None
    collect_amount1 = col_event[2] / 10 ** USDG_DECIMALS if col_event else None
    fees0 = (collect_amount0 - decrease_amount0) if (dec_event and col_event) else None
    fees1 = (collect_amount1 - decrease_amount1) if (dec_event and col_event) else None

    close_result = {
        "decrease_amount0_eth": decrease_amount0, "decrease_amount1_usdg": decrease_amount1,
        "collect_amount0_eth_total": collect_amount0, "collect_amount1_usdg_total": collect_amount1,
        "fees_earned_amount0_eth": fees0, "fees_earned_amount1_usdg": fees1,
    }
    progress["close_result"] = close_result

    total_gas_wei = sum(tx["gas_used"] * gas_price for tx in (prior_gas_txs or []))
    total_gas_wei += sum(tx["gas_used"] * gas_price for tx in progress.get("close_txs", []))
    total_gas_usd = total_gas_wei / 1e18 * pool_price

    fees_usd = (fees0 or 0) * pool_price + (fees1 or 0)
    il0 = (decrease_amount0 - known_deposit0) if (known_deposit0 is not None and decrease_amount0 is not None) else None
    il1 = (decrease_amount1 - known_deposit1) if (known_deposit1 is not None and decrease_amount1 is not None) else None
    il_usd = ((il0 or 0) * pool_price + (il1 or 0)) if (il0 is not None) else None
    net_pnl_usd = fees_usd + (il_usd or 0) - total_gas_usd

    close_pnl = {
        "fees_earned_usd": fees_usd, "impermanent_loss_vs_deposit_usd": il_usd,
        "total_gas_usd": total_gas_usd, "gas_price_used_wei": gas_price, "net_pnl_usd": net_pnl_usd,
    }
    progress["close_pnl"] = close_pnl

    wb_final = pc.wallet_balances()
    wb_final["weth_human"] = weth_balance()
    progress["close_final_wallet_balances"] = wb_final
    out_path.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({
            "event": "emergency_close", "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "token_id": token_id, "collect_amount0_eth": collect_amount0, "collect_amount1_usdg": collect_amount1,
            "fees_earned_usd": fees_usd, "net_pnl_usd": net_pnl_usd,
        }, default=str, ensure_ascii=False) + "\n")

    print(f"\n[p5_live_close] ЗАКРЫТО. collect: {collect_amount0:.6f} ETH(WETH) + {collect_amount1:.4f} USDG "
          f"(из них комиссии: {fees0:.8f} ETH + {fees1:.6f} USDG). net_pnl_usd={net_pnl_usd:.4f}")
    print(f"[p5_live_close] финальные балансы кошелька: {wb_final}")
    return {**close_result, "pnl": close_pnl, "final_wallet_balances": wb_final}


def main() -> int:
    """CLI-режим (ручной вызов): python p5_live_close.py <token_id> [--confirm-mainnet]."""
    import os

    from eth_account import Account

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Использование: python p5_live_close.py <token_id> [--confirm-mainnet]")
        return 1
    token_id = int(args[0])
    confirm = "--confirm-mainnet" in sys.argv

    progress: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "mode": "REAL" if confirm else "DRY-RUN", "token_id": token_id}

    step1_res = json.loads(STEP1_RESULT_PATH.read_text()) if STEP1_RESULT_PATH.exists() else {}
    known_deposit0 = step1_res.get("lp_position", {}).get("amount0_eth_actual")
    known_deposit1 = step1_res.get("lp_position", {}).get("amount1_usdg_actual")
    prior_gas_txs = step1_res.get("txs", []) if step1_res.get("lp_position", {}).get("token_id") == token_id else []

    if not confirm:
        pos = read_position(token_id)
        progress["close_position_onchain"] = pos
        if pos["liquidity"] == 0:
            progress["close_note"] = "liquidity=0 -- нечего закрывать."
        else:
            pool = pc.read_pool_state()
            pool_price = pc.price_from_sqrt(pool["sqrt_price_x96"])
            sqrt_p_raw = pool["sqrt_price_x96"] / (2 ** 96)
            sqrt_pa_raw = (1.0001 ** pos["tickLower"]) ** 0.5
            sqrt_pb_raw = (1.0001 ** pos["tickUpper"]) ** 0.5
            if sqrt_pa_raw > sqrt_pb_raw:
                sqrt_pa_raw, sqrt_pb_raw = sqrt_pb_raw, sqrt_pa_raw
            sqrt_p_clamped = min(max(sqrt_p_raw, sqrt_pa_raw), sqrt_pb_raw)
            expected0 = max(pos["liquidity"] * (1 / sqrt_p_clamped - 1 / sqrt_pb_raw), 0.0) / 10 ** WETH_DECIMALS
            expected1 = max(pos["liquidity"] * (sqrt_p_clamped - sqrt_pa_raw), 0.0) / 10 ** USDG_DECIMALS
            progress["close_plan"] = {"pool_price_usd": pool_price, "expected_amount0_eth": expected0,
                                       "expected_amount1_usdg": expected1}
            progress["close_note"] = "DRY-RUN -- ничего не отправлялось. Запустите с --confirm-mainnet для реального закрытия."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        return 0

    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении.")
    if priv_hex.startswith("0x"):
        priv_hex = priv_hex[2:]
    account = Account.from_key(bytes.fromhex(priv_hex))
    if account.address.lower() != WALLET.lower():
        raise RuntimeError(f"PRIVATE_KEY_NOX даёт {account.address}, ожидался {WALLET} -- СТОП.")

    try:
        close_position(token_id, account, progress, known_deposit0=known_deposit0, known_deposit1=known_deposit1,
                        prior_gas_txs=prior_gas_txs)
    except RuntimeError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
