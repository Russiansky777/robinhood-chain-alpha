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

НАЙДЕНО (реальный инцидент 2026-09-03, run 33782052511, VPS-NL):
`create_market_order` вернул `err=None` (200 OK, реальный tx_hash) БЕЗ
фактического исполнения ордера -- ответ содержал `"ratelimit": "didn't
use volume quota"`, не ошибку, но и не подтверждение филла. Реальных
позиций на Lighter не появилось (LP осталась голой). `err is None` САМ
ПО СЕБЕ теперь НЕ считается доказательством хеджа -- добавлена
`_verify_hedge_filled()`: свежее чтение реальных positions() (до 4
попыток, 3с пауза) ПОСЛЕ отправки ордера; если реальная позиция не
появилась -- это трактуется как сбой хеджа наравне с ошибкой API, тот
же автозакрытие срабатывает.

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
SLIPPAGE_HEDGE = 0.05  # 5% на worst acceptable price хедж-ордера -- НАЙДЕНО (владелец, 2026-09-03):
# три реальные попытки с 1% дали 0%/0%/27% филл, при том что реальная глубина книги на
# порядки больше нашего размера (412 ETH на 0.10% против запрошенных 0.0365 ETH, см.
# data/p3_guard_cache/lighter_depth_fine_probe_result.json) -- гипотезы про volume quota,
# post_only/limit и тонкую книгу опровергнуты по отдельности реальными данными. Владелец
# верно заметил: то, что мы шлём -- IOC-ордер с ЦЕНОВЫМ ЛИМИТОМ (avg_execution_price), не
# безусловный market (который обязан исполниться полностью, если не съедает весь стакан) --
# реальная семантика этого поля на бэкенде Lighter не до конца ясна, поэтому расширяем запас
# с большим избытком (5% при глубине книги на порядки больше размера ордера ничего не стоит
# по факту исполнения), чтобы полностью исключить цену как переменную и изолировать проблему.
LIGHTER_API_KEY_INDEX = 4

# ПЯТАЯ ПОПЫТКА (владелец, 2026-09-03, после реального `canceled-margin-
# not-allowed` на всех 3 прошлых попытках -- см. data/p3_guard_cache/
# lighter_order_status_authed_probe_result.json): "Владелец выбрал плечо
# 3×. Пятая попытка Step 1 разрешена, при условии что все проверки ниже
# выполнены и показаны" -- update_leverage(3x) реально + верификация
# ЧТЕНИЕМ, расчёт по прочитанному (не ожидаемому) плечу, flat-аккаунт как
# ПОСТОЯННАЯ предпосылка хеджа, MAX_LEVERAGE-допущение убрано из
# precheck.py (см. real_eth_leverage() там).
TARGET_LEVERAGE = 3.0
LEVERAGE_MARGIN_MODE = 0  # CROSS_MARGIN_MODE=0 (elliottech/lighter-python/lighter/signer_client.py) --
# совпадает с реальным margin_mode=0 на позиции ETH аккаунта 22012 (см. p5_live_lighter_account_result.json)
LEVERAGE_VERIFY_TOLERANCE = 0.05  # 5% допуск при сверке -- imf_raw=int(10_000/leverage) целочисленно
# округляет (для 3x: int(10000/3)=3333 => фактическое плечо 10000/3333=3.0003x, не ровно 3.0) --
# не ошибка проверки, а механическое усечение при отправке; допуск покрывает его, не более.

# ЧИСТЫЙ ЛИСТ (владелец, 2026-09-03, после ручного закрытия LP+хеджа и
# довнесения маржи +20%): "Если по расчёту свободной маржи остаётся
# меньше 25% от collateral -- уменьшить размер позиции до того, при
# котором остаётся, и сказать какой он." Иначе ребалансы (будущий
# демон) будут упираться в тот же `canceled-margin-not-allowed`,
# который стоил 4 попыток ранее -- нужен запас, не 100% использование
# маржи под один вход.
MARGIN_FREE_BUFFER_PCT = 0.25
# Реальный, ранее УЖЕ установленный владельцем kill-порог (не новое
# число -- см. analysis/p5_backtest_10d.py::KILL_THRESHOLD_ANNUAL,
# "поднят с 25% до 30%"), сюда просто зеркалится для стартовой записи
# состояния позиции -- будущий демон (p5_live_bot.py) читает её отсюда.
KILL_THRESHOLD_ANNUAL = 0.30
# Старая LP-позиция (создана предыдущей попыткой этого же скрипта,
# закрыта владельцем ВРУЧНУЮ) -- проверяем реальным чтением, что она
# действительно пуста, не полагаемся на слова "закрыл всё".
PRIOR_LP_TOKEN_ID_TO_VERIFY = 999556
POSITION_STATE_PATH = Path("data/p5_live_position_state.json")

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


def _ensure_account_flat() -> dict:
    """п.4 (владелец, 2026-09-03): "убедиться, что аккаунт flat... Сделать
    это постоянной предпосылкой хеджа, не разовой проверкой." Реальное
    чтение позиций; если что-то открыто -- реально закрывает
    (p5_live_flatten_lighter.flatten(), reduce_only рыночными ордерами на
    ФАКТИЧЕСКИЙ текущий размер) ПЕРЕД тем, как продолжать. Вызывается на
    каждом реальном прогоне Step 1, не только при обнаруженной проблеме."""
    from p5_live_flatten_lighter import flatten  # отложенный импорт -- см. p5_live_close выше по той же причине
    positions_before = pc.lighter_positions()
    if not positions_before:
        return {"was_flat": True, "flatten_result": None, "flat": True, "positions_before": []}
    print(f"[p5_live_step1] === П.4: аккаунт НЕ flat перед хеджем ({positions_before}) -- закрываю реально перед продолжением ===")
    flatten_result = flatten()
    return {"was_flat": False, "flatten_result": flatten_result, "flat": flatten_result["flattened"],
             "positions_before": positions_before}


async def _update_leverage_and_verify(target_leverage: float, market_index: int = 0,
                                       margin_mode: int = LEVERAGE_MARGIN_MODE,
                                       verify_attempts: int = 4, verify_delay_s: float = 3.0) -> dict:
    """п.1-2 (владелец, 2026-09-03): реальный update_leverage(3x) +
    ПРОВЕРКА ЧТЕНИЕМ фактического initial_margin_fraction ПОСЛЕ вызова --
    "Не полагаться на отсутствие ошибки в ответе update_leverage (урок
    err=None)." err=None здесь тоже НЕ считается доказательством --
    только свежее чтение pc.real_eth_leverage() после вызова."""
    import lighter
    lighter_priv = os.environ["LIGHTER_API_KEY_PRIVATE"]
    client = lighter.SignerClient(url=pc.LIGHTER_API_BASE, account_index=pc.LIGHTER_ACCOUNT_INDEX,
                                   api_private_keys={LIGHTER_API_KEY_INDEX: lighter_priv})
    try:
        before = pc.real_eth_leverage()
        print(f"[p5_live_step1] update_leverage: плечо ДО вызова (прочитано): {before}")
        # Реальная формула SDK: imf = int(10_000/leverage) -- см. комментарий у TARGET_LEVERAGE.
        tx_info, resp, err = await client.update_leverage(
            market_index=market_index, margin_mode=margin_mode, leverage=target_leverage,
            api_key_index=LIGHTER_API_KEY_INDEX,
        )
        call_info = {
            "tx_hash": resp.tx_hash if resp is not None else None,
            "resp_code": resp.code if resp is not None else None,
            "resp_message": resp.message if resp is not None else None,
            "err": str(err) if err is not None else None,
        }
        print(f"[p5_live_step1] update_leverage: ответ API: {call_info}")
        if err is not None:
            return {"verified": False, "reason": f"update_leverage вернул ошибку: {err}",
                     "before": before, "call": call_info}

        for i in range(verify_attempts):
            if i > 0:
                time.sleep(verify_delay_s)
            after = pc.real_eth_leverage()
            actual = after.get("leverage")
            print(f"[p5_live_step1] update_leverage: проверка чтением {i + 1}/{verify_attempts}: {after}")
            if actual is not None and abs(actual - target_leverage) <= target_leverage * LEVERAGE_VERIFY_TOLERANCE:
                return {"verified": True, "before": before, "after": after, "call": call_info,
                         "actual_leverage": actual, "attempts_used": i + 1}
        return {"verified": False, "reason": f"после {verify_attempts} проверок чтением плечо НЕ подтвердилось "
                                              f"на уровне {target_leverage}x (допуск {LEVERAGE_VERIFY_TOLERANCE:.0%}) -- "
                                              f"err=None в ответе API НЕ считается доказательством.",
                 "before": before, "after": pc.real_eth_leverage(), "call": call_info}
    finally:
        await client.close()


def liquidation_distance_pct(collateral_usd: float, size_eth: float, entry_price_usd: float,
                              maintenance_margin_fraction: float) -> float:
    """Оценка % роста цены ETH до ликвидации КОРОТКОЙ позиции в CROSS-
    маржинальном режиме, при условии, что это ЕДИНСТВЕННАЯ открытая
    позиция в пуле обеспечения (гарантируется п.4 -- flat перед хеджем)
    и весь `collateral_usd` пула обеспечения стоит за этой позицией
    (не только формально требуемая initial margin -- у cross-margin
    избыточное обеспечение тоже служит буфером).

    Вывод (линейный перп, не инверсный): equity(P) = collateral - size*(P-P0)
    (для шорта цена растёт -> убыток). Ликвидация при equity(P) = P*size*mmf
    (maintenance margin по ТЕКУЩЕЙ цене). Решая относительно P:
        P = (collateral + size*P0) / (size*(1+mmf))
    % роста до ликвидации = P/P0 - 1.

    ПРИМЕЧАНИЕ: это расчётная оценка по официально задокументированным
    сырым риск-параметрам рынка (maintenance_margin_fraction), а не
    официальная формула ликвидации самой Lighter (публичный API её не
    публикует по этому эндпоинту) -- используется только как оценка
    запаса, не как гарантия точной цены ликвидации."""
    if size_eth <= 0 or entry_price_usd <= 0:
        return float("nan")
    p_liq = (collateral_usd + size_eth * entry_price_usd) / (size_eth * (1 + maintenance_margin_fraction))
    return (p_liq / entry_price_usd - 1) * 100


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
    account_full = pc.lighter_account_full()
    current_leverage_info = pc.real_eth_leverage(account_full)
    positions_open = pc.lighter_positions()
    print(f"[p5_live_step1] pool_price=${pool_price:.4f} lighter_mark=${lighter_price} "
          f"wallet: ETH={wb['eth_human']} WETH={wb['weth_human']} USDG={wb['usdg_human']} margin={lm}")
    print(f"[p5_live_step1] п.5 -- РЕАЛЬНОЕ текущее плечо ETH (прочитано с аккаунта, "
          f"допущение MAX_LEVERAGE=3 из precheck.py убрано): {current_leverage_info}")
    print(f"[p5_live_step1] п.4 -- открытые позиции на Lighter сейчас (постоянная предпосылка -- должно стать "
          f"пусто до хеджа): {positions_open}")

    # ЧИСТЫЙ ЛИСТ -- п.1, последний пункт: реально подтвердить, что старая
    # LP (та же, что открывал этот скрипт ранее) закрыта, не со слов.
    prior_lp_state = pc.nfpm_position(PRIOR_LP_TOKEN_ID_TO_VERIFY, NFPM)
    progress["prior_lp_check"] = prior_lp_state
    print(f"[p5_live_step1] п.1 -- старая LP tokenId={PRIOR_LP_TOKEN_ID_TO_VERIFY}: {prior_lp_state}")
    if prior_lp_state.get("found") and not prior_lp_state.get("fully_closed"):
        progress["abort_reason"] = (f"Старая LP tokenId={PRIOR_LP_TOKEN_ID_TO_VERIFY} НЕ закрыта полностью "
                                     f"({prior_lp_state}) -- СТОП, не открываю новую позицию поверх незакрытой старой.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] {progress['abort_reason']}")
        return 1

    if not current_leverage_info.get("found") or not current_leverage_info.get("leverage"):
        progress["abort_reason"] = "Не удалось прочитать реальное плечо с аккаунта -- СТОП, не считаю по допущению."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] {progress['abort_reason']}")
        return 1
    current_leverage = current_leverage_info["leverage"]

    p0 = pool_price
    gas_reserve_eth = pc.GAS_RESERVE_USD / p0
    usable_native_eth = max(0.0, wb["eth_human"] - gas_reserve_eth)
    # П.2 -- "WETH считать доступным ETH, mint его всё равно оборачивает":
    # уже имеющийся WETH-остаток (от предыдущего close) -- тот же капитал.
    usable_eth_total_full = usable_native_eth + wb["weth_human"]
    usable_usdg_full = wb["usdg_human"]
    pa, pb = p0 * (1 - pc.RANGE_PCT), p0 * (1 + pc.RANGE_PCT)
    sqrt_p, sqrt_pa, sqrt_pb = p0 ** 0.5, pa ** 0.5, pb ** 0.5

    def _plan_at(usable_eth: float, usable_usdg: float) -> dict:
        L = pc.get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, usable_eth, usable_usdg)
        e0, e1 = pc.v3_amounts(L, sqrt_p, sqrt_pa, sqrt_pb)
        notional = e0 * (lighter_price or p0)
        return {"usable_eth": usable_eth, "usable_usdg": usable_usdg, "L": L,
                "expected0": e0, "expected1": e1, "delta_eth": e0, "hedge_notional": notional}

    margin_available = lm.get("collateral_usd", 0) if lm.get("found") else 0
    full = _plan_at(usable_eth_total_full, usable_usdg_full)
    required_margin_full = full["hedge_notional"] / current_leverage

    # П.2 -- буфер 25% свободной маржи: если required_margin_full не оставляет
    # хотя бы MARGIN_FREE_BUFFER_PCT от collateral свободными, УМЕНЬШИТЬ размер
    # (не просто отказать) -- формулы LiquidityAmounts/v3_amounts линейны по
    # входным (amount0,amount1) (проверено: L, expected0, expected1 все line-
    # арны по usable_eth/usable_usdg при фиксированном диапазоне/цене), так что
    # масштаб k считается аналитически, без подбора.
    max_required_margin = margin_available * (1 - MARGIN_FREE_BUFFER_PCT)
    scale_k = 1.0 if required_margin_full <= 0 else min(1.0, max_required_margin / required_margin_full)
    scaled = _plan_at(usable_eth_total_full * scale_k, usable_usdg_full * scale_k) if scale_k < 1.0 else full
    usable_eth, usable_usdg = scaled["usable_eth"], scaled["usable_usdg"]
    L, expected0, expected1 = scaled["L"], scaled["expected0"], scaled["expected1"]
    delta_eth_expected, hedge_notional = scaled["delta_eth"], scaled["hedge_notional"]
    required_margin_at_current_leverage = hedge_notional / current_leverage
    free_margin_after_at_current_leverage = margin_available - required_margin_at_current_leverage
    free_margin_pct_at_current_leverage = (free_margin_after_at_current_leverage / margin_available
                                            if margin_available > 0 else 0.0)
    sufficient_at_current_leverage = margin_available >= required_margin_at_current_leverage
    print(f"[p5_live_step1] п.2 -- размер БЕЗ ограничения: delta_eth={full['delta_eth']:.6f} "
          f"notional=${full['hedge_notional']:.2f} margin@{current_leverage:.4f}x="
          f"${full['hedge_notional']/current_leverage:.4f}")
    if scale_k < 1.0:
        print(f"[p5_live_step1] п.2 -- УМЕНЬШЕНО (scale_k={scale_k:.6f}) чтобы оставить >={MARGIN_FREE_BUFFER_PCT:.0%} "
              f"свободной маржи: delta_eth={delta_eth_expected:.6f} notional=${hedge_notional:.2f} "
              f"required_margin=${required_margin_at_current_leverage:.4f} свободно после="
              f"${free_margin_after_at_current_leverage:.4f} ({free_margin_pct_at_current_leverage:.2%})")
    else:
        print(f"[p5_live_step1] п.2 -- полный размер уже укладывается в буфер {MARGIN_FREE_BUFFER_PCT:.0%}: "
              f"свободно после=${free_margin_after_at_current_leverage:.4f} ({free_margin_pct_at_current_leverage:.2%})")
    # Предпросмотр -- ТОЛЬКО информационно (для dry-run отчёта): плечо, утверждённое владельцем
    # как целевое для ЭТОЙ попытки, но ЕЩЁ НЕ применённое -- реальный gate ниже, в REAL-ветке,
    # использует плечо, ПОДТВЕРЖДЁННОЕ чтением ПОСЛЕ настоящего update_leverage(), не эту цифру.
    required_margin_at_target_leverage = hedge_notional / TARGET_LEVERAGE
    sufficient_at_target_leverage = margin_available >= required_margin_at_target_leverage
    has_funds = usable_eth > 0 and usable_usdg > 0

    # П.2 -- расстояние до ликвидации (предпросмотр, по текущему прочитанному плечу и цене).
    mmf_preview = float(eth_market["maintenance_margin_fraction"]) / 10_000
    liq_move_pct_preview = liquidation_distance_pct(margin_available, delta_eth_expected, lighter_price or p0, mmf_preview)

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
    # Сколько реально нужно ОБЕРНУТЬ (native ETH -> WETH) -- уже имеющийся WETH идёт в дело
    # первым, wrap покрывает только недостачу (см. докстринг у 1_wrap ниже).
    existing_weth_wei = wb["weth_raw"]
    wrap_amount_wei = max(0, amount0_desired_wei - existing_weth_wei)

    plan = {
        "pool_price_usd": p0, "lighter_mark_price_usd": lighter_price,
        "wallet_balances": wb, "lighter_margin": lm,
        "current_leverage": current_leverage_info, "positions_open_now": positions_open,
        "target_leverage_for_this_attempt": TARGET_LEVERAGE,
        "usable_eth_total_full_no_scale": usable_eth_total_full, "usable_usdg_full_no_scale": usable_usdg_full,
        "margin_buffer_scale_k": scale_k, "margin_free_buffer_pct_target": MARGIN_FREE_BUFFER_PCT,
        "usable_eth": usable_eth, "usable_usdg": usable_usdg,
        "existing_weth_wei": existing_weth_wei, "wrap_amount_wei": wrap_amount_wei,
        "range_lower_usd": pa, "range_upper_usd": pb, "tick_lower": tick_lower, "tick_upper": tick_upper,
        "tick_spacing": pool["tick_spacing"], "current_tick": pool["tick"],
        "computed_liquidity": L, "expected_amount0_eth": expected0, "expected_amount1_usdg": expected1,
        "delta_eth_expected": delta_eth_expected, "hedge_notional_usd_expected": hedge_notional,
        "margin_available_usd": margin_available,
        "required_margin_usd_at_current_leverage": required_margin_at_current_leverage,
        "free_margin_after_usd_at_current_leverage": free_margin_after_at_current_leverage,
        "free_margin_pct_at_current_leverage": free_margin_pct_at_current_leverage,
        "sufficient_at_current_leverage": sufficient_at_current_leverage,
        "required_margin_usd_at_target_leverage_PREVIEW": required_margin_at_target_leverage,
        "sufficient_at_target_leverage_PREVIEW": sufficient_at_target_leverage,
        "estimated_liquidation_move_pct_PREVIEW": liq_move_pct_preview,
        "amount0_desired_wei": amount0_desired_wei, "amount1_desired_raw": amount1_desired_raw,
        "amount0_min_wei": amount0_min, "amount1_min_raw": amount1_min, "deadline": deadline,
        "nfpm_address": NFPM, "fee_tier": FEE_TIER,
    }
    progress["plan"] = plan
    print(json.dumps(plan, indent=2, default=str, ensure_ascii=False))

    if not has_funds:
        progress["abort_reason"] = "usable_eth<=0 или usable_usdg<=0 при свежем пересчёте -- СТОП, не открываю позицию частично."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] {progress['abort_reason']}")
        return 1

    if not confirm:
        # П.5: НЕ считаем dry-run проваленным только из-за sufficient_at_current_leverage=False --
        # это ОЖИДАЕМО, пока update_leverage(3x) реально не вызван (текущее реальное плечо
        # {current_leverage}x, см. plan.current_leverage) -- сам gate по марже происходит В
        # REAL-ветке ниже, ПОСЛЕ подтверждённого чтением update_leverage, не здесь.
        progress["note"] = ("DRY-RUN -- ничего не отправлялось (плечо НЕ менялось). Запустите с "
                             "--confirm-mainnet для реальной отправки: сначала flat-precondition (п.4), "
                             f"затем update_leverage({TARGET_LEVERAGE}x) + верификация чтением (п.1-2), "
                             "затем пересчёт маржи по подтверждённому плечу (п.3) и только потом деньги (п.6).")
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

    # === П.4: аккаунт на Lighter ДОЛЖЕН быть flat перед хеджем -- ПОСТОЯННАЯ предпосылка,
    # проверяется и, если нужно, реально исправляется на КАЖДОМ реальном прогоне, не разово. ===
    print("[p5_live_step1] === П.4: flat-precondition перед хеджем ===")
    flat_check = _ensure_account_flat()
    progress["pre_hedge_flat_check"] = flat_check
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    if not flat_check["flat"]:
        progress["abort_reason"] = f"П.4: не удалось привести аккаунт на Lighter к flat перед хеджем -- СТОП. {flat_check}"
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] {progress['abort_reason']}")
        return 1
    print(f"[p5_live_step1] П.4 подтверждено: аккаунт flat. {flat_check}")

    # === П.1-2: update_leverage(TARGET_LEVERAGE) реально + верификация ЧТЕНИЕМ. ===
    print(f"[p5_live_step1] === П.1-2: update_leverage({TARGET_LEVERAGE}x) + верификация чтением ===")
    leverage_update = asyncio.run(_update_leverage_and_verify(TARGET_LEVERAGE, market_index=0))
    progress["leverage_update"] = leverage_update
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    if not leverage_update["verified"]:
        progress["abort_reason"] = f"П.1-2: update_leverage не подтверждено чтением -- СТОП, не полагаюсь на err=None. {leverage_update}"
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] {progress['abort_reason']}")
        return 1
    real_leverage_confirmed = leverage_update["actual_leverage"]
    print(f"[p5_live_step1] П.1-2 подтверждено ЧТЕНИЕМ: реальное плечо теперь {real_leverage_confirmed}x "
          f"(цель {TARGET_LEVERAGE}x, допуск {LEVERAGE_VERIFY_TOLERANCE:.0%}).")

    # === П.3: пересчёт требуемой маржи по ФАКТИЧЕСКОМУ (прочитанному) плечу, не по ожиданию. ===
    lm_post_leverage = pc.lighter_margin()  # маржа могла чуть измениться (комиссия tx update_leverage)
    margin_available_confirmed = lm_post_leverage.get("collateral_usd", 0) if lm_post_leverage.get("found") else 0
    required_margin_confirmed = hedge_notional / real_leverage_confirmed
    free_margin_after_hedge = margin_available_confirmed - required_margin_confirmed
    sufficient_confirmed = margin_available_confirmed >= required_margin_confirmed and has_funds
    mmf_fraction = float(eth_market["maintenance_margin_fraction"]) / 10_000  # см. комментарий в precheck.py
    liq_move_pct_estimate = liquidation_distance_pct(
        margin_available_confirmed, delta_eth_expected, lighter_price or p0, mmf_fraction)
    post_leverage_recheck = {
        "real_leverage_confirmed": real_leverage_confirmed,
        "margin_available_usd": margin_available_confirmed,
        "hedge_notional_usd": hedge_notional,
        "required_margin_usd": required_margin_confirmed,
        "free_margin_after_hedge_usd": free_margin_after_hedge,
        "sufficient": sufficient_confirmed,
        "maintenance_margin_fraction": mmf_fraction,
        "estimated_liquidation_move_pct": liq_move_pct_estimate,
    }
    progress["post_leverage_recheck"] = post_leverage_recheck
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"[p5_live_step1] П.3: {json.dumps(post_leverage_recheck, indent=2, default=str, ensure_ascii=False)}")
    if not sufficient_confirmed:
        progress["abort_reason"] = (f"П.3: даже после подтверждённого {real_leverage_confirmed}x маржи "
                                     f"недостаточно (требуется ${required_margin_confirmed:.4f}, доступно "
                                     f"${margin_available_confirmed:.4f}) -- СТОП, не открываю частично.")
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_step1] {progress['abort_reason']}")
        return 1

    gas_price = eth_gas_price()
    eth_usd = lighter_price or p0

    # Оценка суммарного газа ДО отправки -- потолок, не понижение суммы при превышении.
    # wrap_amount_wei (не amount0_desired_wei) -- уже имеющийся WETH используется без
    # повторного wrap (см. plan.wrap_amount_wei выше); если == 0, тратим 0 газа на этот шаг вообще.
    est_gas_total = (
        (eth_estimate_gas(WETH, build_calldata_deposit(), wrap_amount_wei) if wrap_amount_wei > 0 else 0) +
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
        # П.2 -- "WETH считать доступным ETH": уже имеющийся WETH-остаток идёт в mint как есть,
        # wrap отправляется ТОЛЬКО на недостачу (wrap_amount_wei), а не всегда на полную сумму
        # amount0_desired_wei -- пропускается целиком, если существующего WETH уже достаточно.
        if wrap_amount_wei > 0:
            send_and_wait(account, "1_wrap_ETH_to_WETH", WETH, build_calldata_deposit(), wrap_amount_wei, nonce, progress)
            nonce += 1
        else:
            print(f"[p5_live_step1] 1_wrap_ETH_to_WETH: ПРОПУЩЕН -- существующего WETH ({wb['weth_human']:.6f}) "
                  f"уже достаточно для amount0_desired ({amount0_desired_wei / 10**WETH_DECIMALS:.6f} ETH).")
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
            client_order_index = int(time.time()) % (2 ** 31)

            # НАЙДЕНО (владелец, 2026-09-03, после 4 реальных попыток с
            # sell-паттерном 0%/27%/0% филла против 100% на buy): наша
            # ручная формула avg_execution_price = mark_price*(1-slippage)
            # использовала MARK PRICE как референс -- знак совпадал с
            # формулой самого SDK, но реальный "ideal_price" в
            # create_market_order_limited_slippage() (см. реальный
            # исходник, lighter/signer_client.py) берётся из ЖИВОГО
            # get_best_price() -- лучший бид/аск ИЗ САМОЙ КНИГИ на момент
            # отправки, не mark. Если mark хоть немного расходится с
            # реальным best_bid в моменте, наш floor мог оказаться выше
            # реального best_bid -- ордеру не с чем матчиться. Переходим
            # на сам SDK-хелпер (ideal_price=None -> получает live
            # best_bid сам), а не на собственный расчёт с mark_price.
            print(f"[p5_live_step1] ОТПРАВКА хедж-ордера (create_market_order_limited_slippage, "
                  f"живой best_bid из книги заявок, не mark_price): market_index=0(ETH) is_ask=True "
                  f"base_amount={base_amount} (~{real_delta_eth:.6f} ETH) max_slippage={SLIPPAGE_HEDGE}")
            tx, resp, err = await client.create_market_order_limited_slippage(
                market_index=0, client_order_index=client_order_index, base_amount=base_amount,
                max_slippage=SLIPPAGE_HEDGE, is_ask=True, reduce_only=False,
                api_key_index=LIGHTER_API_KEY_INDEX,
            )
            # НАЙДЕНО (проверка реального исходника signer_client.py,
            # 2026-09-03): create_order/create_market_order_limited_slippage
            # возвращают (tx_object, RespSendTx, error) -- второй элемент
            # НЕ строка-хэш, это pydantic-модель (code/message/tx_hash/...).
            # Прежний `"tx_hash": str(resp)` писал ВЕСЬ repr модели в это
            # поле (реально найдено в старом p5_live_step1_result.json:
            # "tx_hash": "code=200 message=... tx_hash='487b6c...' ...") --
            # достаём реальный хэш и код явно, не полагаемся на str().
            real_tx_hash = resp.tx_hash if resp is not None else None
            order_info = {
                "tx_hash": real_tx_hash,
                "resp_code": resp.code if resp is not None else None,
                "resp_message": resp.message if resp is not None else None,
                "err": str(err) if err is not None else None,
                "base_amount": base_amount, "size_eth_requested": real_delta_eth,
                "max_slippage": SLIPPAGE_HEDGE, "client_order_index": client_order_index,
            }
            if err is not None:
                print(f"[p5_live_step1] create_market_order вернул ошибку: {err}")
            else:
                print(f"[p5_live_step1] ХЕДЖ ОТПРАВЛЕН: tx_hash={real_tx_hash} resp_code={order_info['resp_code']} resp_message={order_info['resp_message']}")
            return order_info
        finally:
            await client.close()

    def _verify_hedge_filled(expected_size_eth: float, attempts: int = 4, delay_s: float = 3.0) -> dict:
        # НАЙДЕНО (реальный инцидент 2026-09-03, run 33782052511):
        # create_market_order вернул err=None (200 OK, реальный tx_hash),
        # но реальной позиции на Lighter НЕ появилось (ответ содержал
        # `"ratelimit": "didn't use volume quota"` -- не ошибка, но и не
        # подтверждение филла). err=None САМ ПО СЕБЕ НЕ ДОКАЗЫВАЕТ хедж --
        # только свежее чтение реальных positions() доказывает.
        #
        # Порог 85% (не 50%) -- владелец, 2026-09-03, ревью логики:
        # частичный филл 50-85% всё ещё оставляет существенную голую
        # экспозицию и НЕ должен молча считаться успехом без автозакрытия
        # -- только близкий к полному филл (>=85% запрошенного размера)
        # трактуется как success, иначе -- та же ветка автозакрытия, что
        # и полное отсутствие позиции. sign/direction поля позиции
        # НЕ проверяются здесь напрямую (семантика API не подтверждена
        # официальной документацией) -- сырой объект позиции целиком
        # попадает в hedge_fill_check для ручной проверки при докладе.
        last_positions: list[dict] = []
        for i in range(attempts):
            if i > 0:
                time.sleep(delay_s)
            last_positions = pc.lighter_positions()
            eth_pos = next((p for p in last_positions if str(p.get("symbol", "")).upper() == "ETH"), None)
            if eth_pos is not None and abs(float(eth_pos.get("position", 0))) >= expected_size_eth * 0.85:
                return {"filled": True, "position": eth_pos, "attempts_used": i + 1}
            print(f"[p5_live_step1] проверка филла хеджа: попытка {i + 1}/{attempts} -- ETH-позиция не найдена "
                  f"или недостаточного размера (positions={last_positions})")
        return {"filled": False, "position": None, "attempts_used": attempts, "last_positions_seen": last_positions}

    try:
        order_info = asyncio.run(_place_hedge())
        progress["hedge_order"] = order_info
        # `is not None`, не truthy-проверка -- пустая строка "" тоже
        # означала бы реальную ошибку API и не должна молча проскочить
        # как "нет ошибки" (ревью логики, владелец, 2026-09-03).
        if order_info.get("err") is not None:
            raise RuntimeError(f"create_market_order вернул ошибку: {order_info['err']}")

        fill_check = _verify_hedge_filled(real_delta_eth)
        progress["hedge_fill_check"] = fill_check
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        if not fill_check["filled"]:
            raise RuntimeError(
                f"create_market_order вернул err=None (принят API), но реальная позиция на Lighter НЕ появилась "
                f"после {fill_check['attempts_used']} проверок -- ордер не исполнился (см. hedge_order.tx_hash "
                f"для сырого ответа API)."
            )
        # Реальная цена входа -- из подтверждённой позиции (avg_entry_price),
        # не из нашей предторговой оценки (которой теперь и нет -- цену
        # определяет сам SDK через live best_bid, см. _place_hedge()).
        worst_price = float(fill_check["position"].get("avg_entry_price", 0))
        real_hedge_size_eth = abs(float(fill_check["position"].get("position", 0)))
        print(f"[p5_live_step1] ХЕДЖ ПОДТВЕРЖДЁН реальной позицией: {fill_check['position']}")

        # П.7: расстояние до ликвидации в % -- по РЕАЛЬНЫМ пост-филл цифрам
        # (свежая маржа, реальный размер и реальная цена входа), не по предторговой оценке.
        lm_post_hedge = pc.lighter_margin()
        margin_post_hedge = lm_post_hedge.get("collateral_usd", 0) if lm_post_hedge.get("found") else 0
        mmf_fraction_final = float(eth_market["maintenance_margin_fraction"]) / 10_000
        liq_move_pct_final = liquidation_distance_pct(margin_post_hedge, real_hedge_size_eth, worst_price, mmf_fraction_final)
        progress["post_hedge_liquidation_estimate"] = {
            "margin_usd": margin_post_hedge, "size_eth": real_hedge_size_eth, "entry_price_usd": worst_price,
            "maintenance_margin_fraction": mmf_fraction_final, "estimated_liquidation_move_pct": liq_move_pct_final,
        }
        print(f"[p5_live_step1] п.7: оценка расстояния до ликвидации (рост цены ETH): "
              f"{liq_move_pct_final:.2f}% (маржа=${margin_post_hedge:.4f}, размер={real_hedge_size_eth:.6f} ETH, "
              f"вход=${worst_price:.2f}, mmf={mmf_fraction_final:.4%})")
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

    # П.6 -- потраченный газ: сумма по РЕАЛЬНЫМ квитанциям (gas_used * effective_gas_price),
    # не по предторговой оценке.
    total_gas_wei = sum(tx["gas_used"] * tx["effective_gas_price_wei"] for tx in progress.get("txs", []))
    total_gas_eth = total_gas_wei / 1e18
    total_gas_usd = total_gas_eth * (lighter_price or p0)
    progress["total_gas_spent"] = {"wei": total_gas_wei, "eth": total_gas_eth, "usd_est": total_gas_usd}

    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({
            "event": "step1_open", "time_utc": progress["generated_at_utc"],
            "eth_price_usd": p0, "token_id": liq_event["token_id"],
            "lp_amount0_eth": real_amount0_eth, "lp_amount1_usdg": real_amount1_usdg,
            "hedge_size_eth": real_delta_eth, "hedge_entry_price_usd": worst_price,
            "tick_lower": tick_lower, "tick_upper": tick_upper,
            "real_leverage_confirmed": real_leverage_confirmed,
            "estimated_liquidation_move_pct": liq_move_pct_final,
            "total_gas_spent_usd_est": total_gas_usd,
        }, default=str, ensure_ascii=False) + "\n")

    # П.5 -- зафиксировать время открытия и стартовые значения для расчёта доходности
    # ("первый полноценный прогон, с которого считается kill-порог 30% годовых") --
    # отдельный state-файл для будущего демона (p5_live_bot.py), не полагаемся на
    # разбор хвоста p5_live_log.jsonl.
    position_state = {
        "opened_at_utc": progress["generated_at_utc"],
        "token_id": liq_event["token_id"],
        "lp_amount0_eth_entry": real_amount0_eth, "lp_amount1_usdg_entry": real_amount1_usdg,
        "tick_lower": tick_lower, "tick_upper": tick_upper,
        "pool_price_usd_entry": p0,
        "hedge_size_eth_entry": real_hedge_size_eth, "hedge_entry_price_usd": worst_price,
        "leverage_entry": real_leverage_confirmed,
        "margin_available_usd_entry": margin_post_hedge,
        "total_gas_spent_usd_est": total_gas_usd,
        "kill_threshold_annual": KILL_THRESHOLD_ANNUAL,
        "capital_at_risk_usd_entry": real_amount0_eth * p0 + real_amount1_usdg + margin_post_hedge,
    }
    POSITION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSITION_STATE_PATH.write_text(json.dumps(position_state, indent=2, default=str, ensure_ascii=False))
    print(f"[p5_live_step1] п.5: состояние позиции для расчёта доходности записано в {POSITION_STATE_PATH}")

    # П.6 -- итоговый доклад: фактическое плечо, tokenId LP, обе ноги, размер и цена
    # входа шорта, итоговая дельта, свободная маржа, расстояние до ликвидации, газ.
    delta_final = real_amount0_eth - real_hedge_size_eth
    print(f"\n[p5_live_step1] ГОТОВО. Итог:")
    print(f"  фактическое плечо (подтверждено чтением): {real_leverage_confirmed}x")
    print(f"  LP tokenId={liq_event['token_id']} amount0(ETH)={real_amount0_eth:.6f} amount1(USDG)={real_amount1_usdg:.4f}")
    print(f"  хедж: {real_hedge_size_eth:.6f} ETH шорт @ ${worst_price:.2f}")
    print(f"  итоговая дельта (LP ETH - хедж ETH, должна быть близка к нулю): {delta_final:.6f} ETH")
    print(f"  остатки на кошельке: {wb_final}")
    print(f"  остатки/свободная маржа на Lighter: {lm_final}")
    print(f"  расстояние до ликвидации (оценка, рост цены ETH): {liq_move_pct_final:.2f}%")
    print(f"  потраченный газ: {total_gas_eth:.8f} ETH (~${total_gas_usd:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
