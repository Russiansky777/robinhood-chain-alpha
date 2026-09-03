#!/usr/bin/env python3
"""P5 LIVE, Step 1 -- РЕАЛЬНОЕ открытие LP-позиции (Uniswap v3,
ETH/USDG, fee 0.01%, диапазон +-10%) и хеджирование её дельты коротким
ETH-перпом на Lighter (api.rh.lighter.xyz, аккаунт 22012).

Владелец, 2026-09-03: "Если хватает [маржи/баланса] -- выполнить Step 1
P5: открыть LP-позицию ... и захеджировать дельту коротким ETH-перпом
на Lighter. После открытия -- доложить: фактический размер
LP-позиции, размер и цена входа шорта, итоговую дельту (должна быть
близка к нулю), остатки на кошельке и марже."
sufficient=true подтверждено data/p3_guard_cache/p5_live_precheck_result.json
(2026-09-03T15:36:47Z, api.rh.lighter.xyz, collateral=$40.00).

**По умолчанию -- DRY-RUN** (та же дисциплина, что sc1_launcher.py):
всё считается и печатается, НИЧЕГО не подписывается и не отправляется,
пока не передан --confirm-mainnet. Реальная отправка -- 4
последовательные транзакции (wrap ETH->WETH, approve WETH, approve
USDG, mint) + 1 подписанный ордер на Lighter (SignerClient,
market_index=0 ETH, is_ask=True = шорт) -- каждый шаг ждёт реальной
квитанции/ответа перед следующим, любой сбой -- немедленный СТОП с
записью точного места сбоя (никаких автоматических повторов реальных
переводов денег).

Цена -- ИСКЛЮЧИТЕЛЬНО ончейн (sqrtPriceX96 пула P5, свежий eth_call
прямо перед отправкой) + Lighter mark price для хеджа, как в
p5_live_precheck.py. NFPM-адрес и формулы -- см. docstring
p5_live_precheck.py (то же обоснование, дословно те же функции,
переиспользованы отсюда через импорт).

ОБНОВЛЕНО 2026-09-03 (после реального инцидента на первом хосте, США:
create_market_order упал `code=20558 restricted jurisdiction` -- LP
осталась незахеджированной ~14 минут до ручного закрытия). Владелец:
"Если хедж-ордер вернёт ЛЮБУЮ ошибку -- НЕМЕДЛЕННО, автоматически, без
ожидания команды: закрыть только что открытую LP-позицию обратно...
Это не опционально." -- при ЛЮБОЙ ошибке create_market_order этот
скрипт теперь САМ вызывает p5_live_close.close_position() В ТОМ ЖЕ
процессе (никакого отдельного workflow-раунда) с реальным
только-что-полученным tokenId, сразу после детекта ошибки хеджа.

Скрипт предназначен для запуска С IP, где Lighter НЕ применяет
юрисдикционный geo-блок к торговому пути (см. docs/PROJECT_STATE.md,
"второй VPS (Нидерланды)") -- запускается по SSH на VPS, не на GH
Actions runner'е (US IP гарантированно попадает под geo-блок).

Шаги 2-4 (цикл ребаланса, почасовое логирование, safety-стопы) -- ВНЕ
этого скрипта, не запускаются без отдельной команды владельца.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

from alchemy_fallback import _rpc_call, topic0  # noqa: E402
import p5_live_precheck as pc  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_live_step1_result.json")
LOG_PATH = Path("data/p5_live_log.jsonl")

WALLET = pc.WALLET
P5_POOL = pc.P5_POOL
WETH = pc.WETH
USDG = pc.USDG
WETH_DECIMALS, USDG_DECIMALS = pc.WETH_DECIMALS, pc.USDG_DECIMALS
CHAIN_ID = 4663
NFPM = "0x73991a25c818bf1f1128deaab1492d45638de0d3"  # найден p5_live_precheck.find_nfpm_address(), 1417/1426 Mint-событий
FEE_TIER = 100  # 0.01%, подтверждено live на пуле
GAS_CEILING_USD = 8.0  # потолок суммарной стоимости газа на весь пакет из 4 транзакций
SLIPPAGE_AMOUNTS = 0.02  # 2% на amount0Min/amount1Min относительно расчётного оптимума
SLIPPAGE_HEDGE = 0.01  # 1% на worst acceptable price хедж-ордера
LIGHTER_API_KEY_INDEX = 4

MINT_SIG = "mint((address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256))"
INCREASE_LIQUIDITY_TOPIC0 = topic0("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
TRANSFER_TOPIC0 = topic0("Transfer(address,address,uint256)")


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def tick_from_price(human_price: float) -> int:
    raw_price = human_price / (10 ** (WETH_DECIMALS - USDG_DECIMALS))
    return math.floor(math.log(raw_price) / math.log(1.0001))


def build_calldata_mint(params: tuple) -> bytes:
    selector = bytes.fromhex(_selector(MINT_SIG)[2:])
    types = ["(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"]
    return selector + abi_encode(types, [params])


def build_calldata_approve(spender: str, amount: int) -> bytes:
    selector = bytes.fromhex(_selector("approve(address,uint256)")[2:])
    return selector + abi_encode(["address", "uint256"], [spender, amount])


def build_calldata_deposit() -> bytes:
    return bytes.fromhex(_selector("deposit()")[2:])


def eth_gas_price() -> int:
    return int(_rpc_call("eth_gasPrice", []), 16)


def eth_estimate_gas(to: str, data: bytes, value: int = 0) -> int:
    return int(_rpc_call("eth_estimateGas", [{"from": WALLET, "to": to, "data": "0x" + data.hex(), "value": hex(value)}]), 16)


def eth_nonce() -> int:
    return int(_rpc_call("eth_getTransactionCount", [WALLET, "pending"]), 16)


def send_tx(account, to: str, data: bytes, value: int, nonce: int, gas_limit: int, gas_price: int,
            buffer_mult: float = 1.5) -> str:
    # НАЙДЕНО (реальный прогон 33775506412, 2026-09-03): eth_account
    # требует EIP-55 checksummed 'to' (не просто валидный по длине hex) --
    # наши WETH/USDG/NFPM-константы записаны строчными буквами, чистый
    # eth_account.Account.sign_transaction() падал `TypeError: Transaction
    # had invalid fields: {'to': '0x0bd7...'}` ДО отправки (локальная
    # валидация, ничего не ушло в сеть, нонс не тронут).
    # НАЙДЕНО (реальный прогон 33780888659, 2026-09-03): фиксированного
    # запаса на gasPrice недостаточно -- base fee успевает вырасти за
    # время ожидания квитанций предыдущих tx в последовательности.
    # `buffer_mult` теперь параметризован -- см. send_with_gas_retry()
    # (эскалация запаса при повторной попытке), не жёстко зашит здесь.
    #
    # Сеть здесь принимает legacy-транзакции (поле gasPrice, БЕЗ
    # отдельных maxFeePerGas/maxPriorityFeePerGas в самой tx) -- узел
    # трактует gasPrice как неявный maxFeePerGas при сверке с текущим
    # baseFee (реальное сообщение об ошибке использует EIP-1559
    # терминологию даже для legacy-tx: `maxFeePerGas ... < baseFee ...`).
    # Отдельного maxPriorityFeePerGas в этой tx нет и не нужен.
    tx = {
        "chainId": CHAIN_ID, "nonce": nonce, "to": to_checksum_address(to), "value": value,
        "gas": int(gas_limit * 1.2), "gasPrice": int(gas_price * buffer_mult), "data": "0x" + data.hex(),
    }
    signed = Account.sign_transaction(tx, account.key)
    return _rpc_call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])


def wait_for_receipt(tx_hash: str, timeout_s: int = 300, poll_s: int = 5) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        receipt = _rpc_call("eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            return receipt
        time.sleep(poll_s)
    raise RuntimeError(f"{tx_hash} не замайнилась за {timeout_s}с -- проверить вручную, НЕ повторять отправку автоматически.")


# Эскалация запаса поверх СВЕЖЕГО eth_gasPrice на каждой попытке (владелец,
# 2026-09-03, после реального инцидента 33780888659: "добавить retry с
# эскалацией... разумный предел попыток (2-3), прежде чем останавливаться").
GAS_RETRY_BUFFERS = [1.15, 1.4, 1.75]
_GAS_TOO_LOW_MARKERS = ("max fee per gas less than block base fee", "max fee per gas too low")


def _is_gas_too_low_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(m in msg for m in _GAS_TOO_LOW_MARKERS)


def send_with_gas_retry(account, to: str, data: bytes, value: int, nonce: int, gas_limit: int, label: str) -> tuple[str, int, float]:
    """Отправляет tx с эскалирующимся запасом к gasPrice при ошибке
    "max fee per gas less than block base fee" -- каждая попытка
    заново читает eth_gasPrice (может и baseFee за это время
    подрасти, не только наш буфер недостаточен) и использует
    следующий, больший множитель из GAS_RETRY_BUFFERS. Другие ошибки
    (не gas-race) -- пробрасываются немедленно, без повторов (ретраить
    их бессмысленно, они повторятся идентично). Возвращает
    (tx_hash, gas_price_fetched, buffer_used) успешной попытки."""
    last_err: Exception | None = None
    for attempt, buf in enumerate(GAS_RETRY_BUFFERS, start=1):
        gas_price = eth_gas_price()
        try:
            tx_hash = send_tx(account, to, data, value, nonce, gas_limit, gas_price, buffer_mult=buf)
            if attempt > 1:
                print(f"[gas_retry] {label}: попытка {attempt}/{len(GAS_RETRY_BUFFERS)} прошла (buffer={buf})")
            return tx_hash, gas_price, buf
        except RuntimeError as e:
            if not _is_gas_too_low_error(e):
                raise
            last_err = e
            print(f"[gas_retry] {label}: попытка {attempt}/{len(GAS_RETRY_BUFFERS)} отклонена "
                  f"(gas price устарел, gas_price={gas_price}, buffer={buf}): {e}")
    raise RuntimeError(f"{label}: {len(GAS_RETRY_BUFFERS)} попыток отправки не прошли по gas-race -- последняя ошибка: {last_err}")


def send_and_wait(account, label: str, to: str, data: bytes, value: int, nonce: int, progress: dict) -> dict:
    print(f"[p5_live_step1] --- {label}: отправка (nonce={nonce}) ---")
    gas_est = eth_estimate_gas(to, data, value)
    tx_hash, gas_price_used, buffer_used = send_with_gas_retry(account, to, data, value, nonce, gas_est, label)
    print(f"[p5_live_step1] {label}: ОТПРАВЛЕНО {tx_hash} (gas_price={gas_price_used}, buffer={buffer_used}), жду квитанцию...")
    receipt = wait_for_receipt(tx_hash)
    status = int(receipt["status"], 16)
    effective_gas_price = int(receipt["effectiveGasPrice"], 16) if receipt.get("effectiveGasPrice") else gas_price_used
    entry = {"label": label, "tx_hash": tx_hash, "status": "success" if status == 1 else "REVERTED",
              "gas_used": int(receipt["gasUsed"], 16), "block_number": int(receipt["blockNumber"], 16),
              "gas_price_offered_wei": gas_price_used, "buffer_used": buffer_used,
              "effective_gas_price_wei": effective_gas_price}
    progress.setdefault("txs", []).append(entry)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))  # пишем ПОСЛЕ каждого шага -- восстановимо при сбое
    print(f"[p5_live_step1] {label}: {entry['status']} (gasUsed={entry['gas_used']})")
    if status != 1:
        raise RuntimeError(f"{label} REVERTED: {tx_hash} -- СТОП, не продолжаю пакет.")
    return receipt


def decode_increase_liquidity(receipt: dict) -> dict:
    for log in receipt.get("logs", []):
        if log["address"].lower() == NFPM.lower() and log["topics"][0].lower() == INCREASE_LIQUIDITY_TOPIC0.lower():
            token_id = int(log["topics"][1], 16)
            liquidity, amount0, amount1 = abi_decode(["uint128", "uint256", "uint256"], bytes.fromhex(log["data"][2:]))
            return {"token_id": token_id, "liquidity": liquidity, "amount0_wei": amount0, "amount1_wei": amount1}
    raise RuntimeError("IncreaseLiquidity событие не найдено в квитанции mint() -- разобрать логи вручную.")


def main() -> int:
    confirm = "--confirm-mainnet" in sys.argv
    t0 = time.time()
    progress: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": "REAL" if confirm else "DRY-RUN"}

    print("=== Свежий пересчёт (тот же путь, что p5_live_precheck.py) ===")
    pool = pc.read_pool_state()
    pool_price = pc.price_from_sqrt(pool["sqrt_price_x96"])
    eth_market = pc.lighter_eth_perp()
    lighter_price = float(eth_market["mark_price"]) if eth_market else None
    wb = pc.wallet_balances()
    lm = pc.lighter_margin()
    print(f"[p5_live_step1] pool_price=${pool_price:.4f} lighter_mark=${lighter_price} "
          f"wallet: ETH={wb['eth_human']} USDG={wb['usdg_human']} margin={lm}")

    p0 = pool_price
    gas_reserve_eth = pc.GAS_RESERVE_USD / p0
    usable_eth = max(0.0, wb["eth_human"] - gas_reserve_eth)
    usable_usdg = wb["usdg_human"]
    pa, pb = p0 * (1 - pc.RANGE_PCT), p0 * (1 + pc.RANGE_PCT)
    sqrt_p, sqrt_pa, sqrt_pb = p0 ** 0.5, pa ** 0.5, pb ** 0.5
    L = pc.get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, usable_eth, usable_usdg)
    expected0, expected1 = pc.v3_amounts(L, sqrt_p, sqrt_pa, sqrt_pb)
    delta_eth_expected = expected0
    hedge_notional = delta_eth_expected * (lighter_price or p0)
    required_margin = hedge_notional / pc.MAX_LEVERAGE
    margin_available = lm.get("collateral_usd", 0) if lm.get("found") else 0
    sufficient = margin_available >= required_margin and usable_eth > 0 and usable_usdg > 0

    tick_lower = tick_from_price(pa)
    tick_upper = tick_from_price(pb)
    tick_lower -= tick_lower % pool["tick_spacing"]
    tick_upper -= tick_upper % pool["tick_spacing"]
    if tick_lower >= tick_upper:
        tick_upper = tick_lower + pool["tick_spacing"]

    amount0_desired_wei = int(usable_eth * 10 ** WETH_DECIMALS)
    amount1_desired_raw = int(usable_usdg * 10 ** USDG_DECIMALS)
    amount0_min = int(expected0 * (1 - SLIPPAGE_AMOUNTS) * 10 ** WETH_DECIMALS)
    amount1_min = int(expected1 * (1 - SLIPPAGE_AMOUNTS) * 10 ** USDG_DECIMALS)
    deadline = int(time.time()) + 600

    plan = {
        "pool_price_usd": p0, "lighter_mark_price_usd": lighter_price,
        "wallet_balances": wb, "lighter_margin": lm,
        "usable_eth": usable_eth, "usable_usdg": usable_usdg,
        "range_lower_usd": pa, "range_upper_usd": pb, "tick_lower": tick_lower, "tick_upper": tick_upper,
        "tick_spacing": pool["tick_spacing"], "current_tick": pool["tick"],
        "computed_liquidity": L, "expected_amount0_eth": expected0, "expected_amount1_usdg": expected1,
        "delta_eth_expected": delta_eth_expected, "hedge_notional_usd_expected": hedge_notional,
        "required_margin_usd": required_margin, "margin_available_usd": margin_available,
        "sufficient": sufficient,
        "amount0_desired_wei": amount0_desired_wei, "amount1_desired_raw": amount1_desired_raw,
        "amount0_min_wei": amount0_min, "amount1_min_raw": amount1_min, "deadline": deadline,
        "nfpm_address": NFPM, "fee_tier": FEE_TIER,
    }
    progress["plan"] = plan
    print(json.dumps(plan, indent=2, default=str, ensure_ascii=False))

    if not sufficient:
        progress["abort_reason"] = "sufficient=False при свежем пересчёте -- СТОП, не открываю позицию частично."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] {progress['abort_reason']}")
        return 1

    if not confirm:
        progress["note"] = "DRY-RUN -- ничего не отправлялось. Запустите с --confirm-mainnet для реальной отправки."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"\n[p5_live_step1] DRY-RUN завершён. {progress['note']}")
        return 0

    # ============================= РЕАЛЬНАЯ ОТПРАВКА =============================
    chain_id = int(_rpc_call("eth_chainId", []), 16)
    if chain_id != CHAIN_ID:
        progress["abort_reason"] = f"chainId {chain_id} != {CHAIN_ID} -- СТОП"
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        return 1

    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении.")
    if priv_hex.startswith("0x"):
        priv_hex = priv_hex[2:]
    account = Account.from_key(bytes.fromhex(priv_hex))
    if account.address.lower() != WALLET.lower():
        raise RuntimeError(f"PRIVATE_KEY_NOX даёт {account.address}, ожидался {WALLET} -- СТОП.")

    gas_price = eth_gas_price()
    eth_usd = lighter_price or p0

    # Оценка суммарного газа ДО отправки -- потолок, не понижение суммы при превышении
    est_gas_total = (
        eth_estimate_gas(WETH, build_calldata_deposit(), amount0_desired_wei) +
        eth_estimate_gas(WETH, build_calldata_approve(NFPM, amount0_desired_wei), 0) +
        eth_estimate_gas(USDG, build_calldata_approve(NFPM, amount1_desired_raw), 0) +
        250_000  # консервативная надбавка на mint() (сложный вызов, оценка eth_estimateGas на NFPM ненадёжна для точного mint() до апрувов)
    )
    est_gas_cost_usd = est_gas_total * gas_price / 1e18 * eth_usd
    progress["gas_estimate"] = {"gas_units_total_est": est_gas_total, "gas_price_wei": gas_price, "est_cost_usd": est_gas_cost_usd}
    print(f"[p5_live_step1] оценка газа на весь пакет: ~{est_gas_total} units, ${est_gas_cost_usd:.4f}")
    if est_gas_cost_usd > GAS_CEILING_USD:
        progress["abort_reason"] = f"оценка газа ${est_gas_cost_usd:.4f} > потолка ${GAS_CEILING_USD} -- СТОП."
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        return 1

    nonce = eth_nonce()
    try:
        send_and_wait(account, "1_wrap_ETH_to_WETH", WETH, build_calldata_deposit(), amount0_desired_wei, nonce, progress)
        nonce += 1
        send_and_wait(account, "2_approve_WETH", WETH, build_calldata_approve(NFPM, amount0_desired_wei), 0, nonce, progress)
        nonce += 1
        send_and_wait(account, "3_approve_USDG", USDG, build_calldata_approve(NFPM, amount1_desired_raw), 0, nonce, progress)
        nonce += 1
        mint_params = (WETH, USDG, FEE_TIER, tick_lower, tick_upper, amount0_desired_wei, amount1_desired_raw,
                        amount0_min, amount1_min, WALLET, deadline)
        mint_receipt = send_and_wait(account, "4_mint", NFPM, build_calldata_mint(mint_params), 0, nonce, progress)
    except RuntimeError as e:
        progress["abort_reason"] = str(e)
        progress["CRITICAL"] = "Сбой в пакете mint -- проверить состояние вручную ПЕРЕД повтором. Если approve прошли, но mint нет -- approve остаются в силе, ETH может быть уже wrapped."
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] СТОП: {e}")
        return 1

    liq_event = decode_increase_liquidity(mint_receipt)
    real_amount0_eth = liq_event["amount0_wei"] / 10 ** WETH_DECIMALS
    real_amount1_usdg = liq_event["amount1_wei"] / 10 ** USDG_DECIMALS
    progress["lp_position"] = {
        "token_id": liq_event["token_id"], "liquidity": liq_event["liquidity"],
        "amount0_eth_actual": real_amount0_eth, "amount1_usdg_actual": real_amount1_usdg,
        "tick_lower": tick_lower, "tick_upper": tick_upper,
    }
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"[p5_live_step1] LP ОТКРЫТА: tokenId={liq_event['token_id']} liquidity={liq_event['liquidity']} "
          f"amount0(ETH)={real_amount0_eth:.6f} amount1(USDG)={real_amount1_usdg:.4f}")

    # ============================= ХЕДЖ НА LIGHTER =============================
    real_delta_eth = real_amount0_eth  # реальная ETH-экспозиция открытой LP-позиции -- размер шорта
    progress["hedge_target_delta_eth"] = real_delta_eth

    async def _place_hedge() -> dict:
        # НАЙДЕНО (p5_live_lighter_signcheck.py, реальный прогон
        # 33774374657): SignerClient требует РАБОТАЮЩИЙ event loop уже на
        # конструкторе (aiohttp внутри) -- вся работа с ним строго внутри
        # asyncio.run(). create_market_order/close -- async def, нужен await.
        import lighter
        lighter_priv = os.environ["LIGHTER_API_KEY_PRIVATE"]
        client = lighter.SignerClient(url=pc.LIGHTER_API_BASE, account_index=pc.LIGHTER_ACCOUNT_INDEX,
                                       api_private_keys={LIGHTER_API_KEY_INDEX: lighter_priv})
        try:
            check_err = client.check_client()
            if check_err is not None:
                raise RuntimeError(f"check_client() вернул ошибку: {check_err}")

            base_amount = round(real_delta_eth * 10 ** eth_market["size_decimals"])
            worst_price = (lighter_price or p0) * (1 - SLIPPAGE_HEDGE)  # is_ask=True (продажа/шорт) -- минимально приемлемая цена
            avg_execution_price = round(worst_price * 10 ** eth_market["price_decimals"])
            client_order_index = int(time.time()) % (2 ** 31)

            print(f"[p5_live_step1] ОТПРАВКА хедж-ордера: market_index=0(ETH) is_ask=True "
                  f"base_amount={base_amount} (~{real_delta_eth:.6f} ETH) worst_price=${worst_price:.2f} "
                  f"(avg_execution_price={avg_execution_price})")
            tx, tx_hash, err = await client.create_market_order(
                market_index=0, client_order_index=client_order_index, base_amount=base_amount,
                avg_execution_price=avg_execution_price, is_ask=True, reduce_only=False,
                api_key_index=LIGHTER_API_KEY_INDEX,
            )
            order_info = {
                "tx_hash": str(tx_hash), "err": str(err) if err is not None else None,
                "base_amount": base_amount, "size_eth_requested": real_delta_eth,
                "avg_execution_price_worst": avg_execution_price, "worst_price_usd": worst_price,
                "client_order_index": client_order_index,
            }
            if err is not None:
                print(f"[p5_live_step1] create_market_order вернул ошибку: {err}")
            else:
                print(f"[p5_live_step1] ХЕДЖ ОТПРАВЛЕН: tx_hash={tx_hash}")
            return order_info
        finally:
            await client.close()

    try:
        order_info = asyncio.run(_place_hedge())
        progress["hedge_order"] = order_info
        if order_info.get("err"):
            raise RuntimeError(f"create_market_order вернул ошибку: {order_info['err']}")
        worst_price = order_info["worst_price_usd"]
    except Exception as e:  # noqa: BLE001
        progress["hedge_error"] = f"{type(e).__name__}: {e}"
        progress["CRITICAL"] = (f"LP-позиция ОТКРЫТА и НЕ ЗАХЕДЖИРОВАНА (полная направленная экспозиция "
                                 f"~{real_delta_eth:.6f} ETH) -- хедж-ордер не прошёл, см. hedge_error.")
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] {progress['CRITICAL']}")

        # Владелец, 2026-09-03 (задача Step1-с-VPS-Нидерланды, п.2):
        # "Если хедж-ордер вернёт ЛЮБУЮ ошибку -- НЕМЕДЛЕННО, автоматически,
        # без ожидания команды: закрыть только что открытую LP-позицию
        # обратно... Голая экспозиция не должна оставаться открытой ни на
        # минуту дольше, чем требуется на детект ошибки. Это не опционально."
        print("[p5_live_step1] === АВТОМАТИЧЕСКОЕ ЗАКРЫТИЕ LP (хедж не прошёл) ===")
        from p5_live_close import close_position  # отложенный импорт -- p5_live_close сам импортирует из этого модуля на уровне файла (циклический импорт на верхнем уровне)
        try:
            close_position(
                liq_event["token_id"], account, progress,
                known_deposit0=real_amount0_eth, known_deposit1=real_amount1_usdg,
                prior_gas_txs=progress.get("txs", []), out_path=OUT_PATH,
            )
            progress["auto_close_succeeded"] = True
            progress["CRITICAL"] += " АВТОЗАКРЫТИЕ УСПЕШНО -- голой позиции больше нет."
            print("[p5_live_step1] АВТОЗАКРЫТИЕ УСПЕШНО -- голой позиции больше нет.")
        except Exception as close_exc:  # noqa: BLE001
            progress["auto_close_succeeded"] = False
            progress["auto_close_error"] = f"{type(close_exc).__name__}: {close_exc}"
            progress["CRITICAL"] += (f" АВТОЗАКРЫТИЕ ТОЖЕ НЕ УДАЛОСЬ: {progress['auto_close_error']} -- "
                                      "ПОЗИЦИЯ ВСЁ ЕЩЁ ГОЛАЯ, ТРЕБУЕТСЯ НЕМЕДЛЕННОЕ РУЧНОЕ ВМЕШАТЕЛЬСТВО ВЛАДЕЛЬЦА.")
            print(f"[p5_live_step1] {progress['CRITICAL']}")

        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        return 1

    # Финальные остатки (свежий пересчёт)
    wb_final = pc.wallet_balances()
    lm_final = pc.lighter_margin()
    progress["final_balances"] = {"wallet": wb_final, "lighter_margin": lm_final}
    progress["runtime_s"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({
            "event": "step1_open", "time_utc": progress["generated_at_utc"],
            "eth_price_usd": p0, "token_id": liq_event["token_id"],
            "lp_amount0_eth": real_amount0_eth, "lp_amount1_usdg": real_amount1_usdg,
            "hedge_size_eth": real_delta_eth, "hedge_entry_price_usd": worst_price,
            "tick_lower": tick_lower, "tick_upper": tick_upper,
        }, default=str, ensure_ascii=False) + "\n")

    print(f"\n[p5_live_step1] ГОТОВО. Итог: LP amount0(ETH)={real_amount0_eth:.6f} amount1(USDG)={real_amount1_usdg:.4f}, "
          f"хедж={real_delta_eth:.6f} ETH шорт @ ~${worst_price:.2f}, остатки: {wb_final}, margin={lm_final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
