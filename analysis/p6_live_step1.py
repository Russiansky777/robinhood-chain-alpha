#!/usr/bin/env python3
"""P6 LIVE, Step 1 -- РЕАЛЬНЫЙ вход (владелец, Гейт 2, 2026-09-04):
"Да, при одном условии: сбор готов до входа... Вход, порядок из dry-run:
Across USDG -> USDC на Base -> своп половины в cbBTC -> mint +-10% ->
после подтверждения mint -- шорт BTC $73 на Lighter, плечо 2.0x уже
стоит. Хедж проверить чтением позиции (урок err=None), дельту и
свободную маржу записать как стартовые значения."

ОБНОВЛЕНО 2026-09-05 (владелец, после реального `InsufficientFunds()`
на попытке $161 -- реальный баланс USDG на кошельке был $80.282426, не
$161): "Вариант B (своп WETH->USDG на пуле P5 перед мостом) -- ТОЛЬКО
если существующий путь свопа из P5 есть; если его нет -- остановиться
и сказать, тогда идём по A на $80." Реально проверено: НИГДЕ в проекте
нет исполняемого свопа (ни на пуле P5, ни где-либо ещё на Robinhood
Chain) -- только чтение/наблюдение (across_common.py) и mint/collect/
decreaseLiquidity через NFPM. Путь B не существует -- владелец
подтвердил идти по варианту A: TARGET_TOTAL_CAPITAL_USD=$80 (реально
ликвидный баланс USDG, без довнесения). Также добавлен ОБЯЗАТЕЛЬНЫЙ
pre-flight (реальное чтение баланса + сверка с суммой) ПЕРЕД каждой
ногой: USDG перед мостом, USDC на Base перед свопом, USDC и cbBTC перед
mint, коллатерал перед шортом -- владелец, тот же урок
InsufficientFunds(), см. `preflight_balance()` ниже."

Реальные адреса -- ПОДТВЕРЖДЕНЫ отдельной разведкой (не предположены):
  - NFPM/Router на Base -- analysis/p6_entry_recon.py, совпадение
    pool.factory() с factory() кандидата (реально СОВПАЛ "initial"
    деплой Aerodrome Slipstream, НЕ "gauges_v3" -- нельзя было гадать).
  - MintParams/ExactInputSingleParams -- дословно из исходников
    github.com/aerodrome-finance/slipstream (WebFetch, 2026-09-04):
    MintParams использует tickSpacing (не fee) и sqrtPriceX96 (0 --
    пул уже существует, не создаётся); ExactInputSingleParams --
    tickSpacing вместо fee, аналогично Uniswap v3 SwapRouter иначе.
  - depositV3 -- дословно из across-protocol/contracts
    V3SpokePoolInterface.sol; exclusivityDeadline из ответа
    suggested-fees передаётся В depositV3 КАК ЕСТЬ (контракт сам
    трактует значения <=31_536_000 как relative offset -- подтверждено
    комментарием в SpokePool.sol, WebFetch 2026-09-04).

Выполняется НА VPS (SSH, тот же путь, что p5_live_step1.py) -- подпись
Lighter-транзакций из юрисдикции GH Actions отклоняется; здесь же
удобно держать ВСЮ последовательность (мост+своп+mint на Base/Robinhood
+ хедж на Lighter) в одном процессе, тот же паттерн, что P5.

Единая дисциплина со всем проектом: КАЖДЫЙ шаг ждёт реальной квитанции/
подтверждения перед следующим; любой сбой денежного шага -- НЕМЕДЛЕННЫЙ
СТОП с точной записью, где именно (мост -- НЕ отправляется повторно
автоматически, средства не теряются, просто в пути -- см. CRITICAL);
широкий допуск на проскальзывание (приоритет гарантированного
исполнения); плечо/маржа ВСЕГДА читаются с аккаунта заново (урок
canceled-margin-not-allowed); хедж подтверждается ЧТЕНИЕМ позиции, не
отсутствием ошибки в ответе (урок err=None, реальный инцидент P5
2026-09-03)."""
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

from Crypto.Hash import keccak  # noqa: E402
import requests  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p6_live_step1_result.json")
STATE_PATH = Path("data/p6_live_position_state.json")
RECON_PATH = Path("data/p3_guard_cache/p6_entry_recon_result.json")

WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"

ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_CHAIN_ID = 4663
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
SPOKE_POOL = "0xD29C85F15DF544bA632C9E25829fd29d767d7978"  # analysis/across_common.py, across-protocol/contracts

BASE_RPC = "https://mainnet.base.org"
BASE_CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
USDC_DECIMALS, CBBTC_DECIMALS = 6, 8
POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"

TARGET_TOTAL_CAPITAL_USD = 80.0  # владелец, 2026-09-05, вариант A -- $161 упёрся в реальный InsufficientFunds()
# (баланс USDG на кошельке = $80.282426, не $161; своп WETH->USDG на Robinhood Chain --
# путь B -- не существует нигде в проекте, владелец прямо сказал в этом случае идти по A).
# RESULTS.md §6.
BRIDGE_ETH_AMOUNT_ETH = 0.00025  # владелец, 2026-09-05: 0.0006 реально упёрлось в LOW_BRIDGE_LIQUIDITY
# Across ("Amount is higher than available liquidity") -- реальная нехватка ликвидности релеера
# на этом маршруте (Robinhood Chain -> Base, нативный ETH), не баг кода. minDeposit реально
# был 0.0002036 ETH (p6_debug/предыдущие котировки) -- 0.00025 ETH (~$0.61) чуть выше пола,
# Base исторически дёшев (~$0.02-0.07 за mint, см. p6_dry_run_entry_result.json), должно хватить.
RANGE_PCT = 0.10
SWAP_SLIPPAGE = 0.03  # широкий допуск -- приоритет гарантированного исполнения (тот же принцип, что P5)
MINT_SLIPPAGE = 0.05
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
LIGHTER_ACCOUNT_INDEX = 22012
LIGHTER_API_KEY_INDEX = 4
HEDGE_SLIPPAGE = 0.05
BRIDGE_FILL_TIMEOUT_S = 600
BRIDGE_FILL_POLL_S = 15


def _topic0(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


def _selector(sig: str) -> str:
    return _topic0(sig)[:10]


# --- generic RPC helpers, parametrized per-chain (rpc_url passed explicitly, no module-global chain state) ---

def rpc(rpc_url: str, method: str, params: list):
    for attempt in range(4):
        r = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        if r.status_code == 429 and attempt < 3:
            time.sleep(15)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method} {params}: {body['error']}")
        return body["result"]
    raise RuntimeError("RPC 429 после ретраев")


def eth_call(rpc_url: str, to: str, data: str) -> str:
    return rpc(rpc_url, "eth_call", [{"to": to, "data": data}, "latest"])


def erc20_balance(rpc_url: str, token: str, holder: str) -> int:
    data = "0x70a08231" + holder[2:].rjust(64, "0").lower()
    return int(eth_call(rpc_url, token, data), 16)


def erc20_allowance(rpc_url: str, token: str, owner: str, spender: str) -> int:
    selector = _selector("allowance(address,address)")[2:]
    data = "0x" + selector + owner[2:].rjust(64, "0").lower() + spender[2:].rjust(64, "0").lower()
    return int(eth_call(rpc_url, token, data), 16)


def erc20_approve_calldata(spender: str, amount: int) -> bytes:
    selector = bytes.fromhex(_selector("approve(address,uint256)")[2:])
    return selector + abi_encode(["address", "uint256"], [spender, amount])


def eth_gas_price(rpc_url: str) -> int:
    return int(rpc(rpc_url, "eth_gasPrice", []), 16)


def eth_estimate_gas(rpc_url: str, to: str, data: bytes, value: int = 0) -> int:
    return int(rpc(rpc_url, "eth_estimateGas", [{"from": WALLET, "to": to, "data": "0x" + data.hex(), "value": hex(value)}]), 16)


def eth_nonce(rpc_url: str) -> int:
    return int(rpc(rpc_url, "eth_getTransactionCount", [WALLET, "pending"]), 16)


def preflight_balance(label: str, actual_raw: int, required_raw: int, decimals: int, symbol: str) -> None:
    """Владелец, 2026-09-04 (после реального InsufficientFunds() на
    depositV3): ПЕРЕД КАЖДОЙ ногой -- реальное чтение баланса нужного
    токена и сверка с суммой, обязательный pre-flight, не полагаемся на
    ранее посчитанный план. СТОП с точной цифрой, если не хватает --
    не отправляем транзакцию вслепую."""
    actual_h, required_h = actual_raw / 10 ** decimals, required_raw / 10 ** decimals
    print(f"[p6_step1] PRE-FLIGHT {label}: реальный баланс {symbol}={actual_h}, нужно={required_h}")
    if actual_raw < required_raw:
        raise RuntimeError(f"PRE-FLIGHT {label}: недостаточно {symbol} -- реально {actual_h}, нужно {required_h} -- СТОП, не отправляю.")


def send_tx(rpc_url: str, chain_id: int, account, to: str, data: bytes, value: int, nonce: int,
            gas_limit: int, gas_price: int, buffer_mult: float = 1.5) -> str:
    tx = {"chainId": chain_id, "nonce": nonce, "to": to_checksum_address(to), "value": value,
          "gas": int(gas_limit * 1.3), "gasPrice": int(gas_price * buffer_mult), "data": "0x" + data.hex()}
    signed = Account.sign_transaction(tx, account.key)
    return rpc(rpc_url, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])


GAS_RETRY_BUFFERS = [1.15, 1.4, 1.75]
_GAS_TOO_LOW_MARKERS = ("max fee per gas less than block base fee", "max fee per gas too low")


def send_with_gas_retry(rpc_url: str, chain_id: int, account, to: str, data: bytes, value: int, nonce: int,
                         gas_limit: int, label: str) -> tuple[str, int, float]:
    last_err = None
    for buf in GAS_RETRY_BUFFERS:
        gas_price = eth_gas_price(rpc_url)
        try:
            return send_tx(rpc_url, chain_id, account, to, data, value, nonce, gas_limit, gas_price, buf), gas_price, buf
        except RuntimeError as e:
            if not any(m in str(e).lower() for m in _GAS_TOO_LOW_MARKERS):
                raise
            last_err = e
            print(f"[p6_step1] {label}: gas-race, повтор со следующим буфером ({e})")
    raise RuntimeError(f"{label}: gas-race не преодолён -- {last_err}")


def wait_for_receipt(rpc_url: str, tx_hash: str, timeout_s: int = 300, poll_s: int = 5) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        receipt = rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            return receipt
        time.sleep(poll_s)
    raise RuntimeError(f"{tx_hash} не замайнилась за {timeout_s}с -- проверить вручную, НЕ повторять отправку.")


PER_TX_GAS_CEILING_USD = 3.0  # потолок на КАЖДУЮ отдельную транзакцию (обе сети) -- P5-исторически <$1/tx, щедрый запас


def send_and_wait(rpc_url: str, chain_id: int, account, label: str, to: str, data: bytes, value: int,
                   nonce: int, progress: dict, eth_usd_price: float | None = None) -> dict:
    print(f"[p6_step1] --- {label}: отправка (nonce={nonce}, chain={chain_id}) ---")
    gas_est = eth_estimate_gas(rpc_url, to, data, value)
    if eth_usd_price:
        gas_price_now = eth_gas_price(rpc_url)
        est_cost_usd = gas_est * gas_price_now / 1e18 * eth_usd_price
        print(f"[p6_step1] {label}: оценка газа ~{gas_est} units, ~${est_cost_usd:.4f}")
        if est_cost_usd > PER_TX_GAS_CEILING_USD:
            raise RuntimeError(f"{label}: оценка газа ${est_cost_usd:.4f} > потолка ${PER_TX_GAS_CEILING_USD} -- СТОП.")
    tx_hash, gas_price_used, buffer_used = send_with_gas_retry(rpc_url, chain_id, account, to, data, value, nonce, gas_est, label)
    print(f"[p6_step1] {label}: ОТПРАВЛЕНО {tx_hash}, жду квитанцию...")
    receipt = wait_for_receipt(rpc_url, tx_hash)
    status = int(receipt["status"], 16)
    effective_gas_price = int(receipt["effectiveGasPrice"], 16) if receipt.get("effectiveGasPrice") else gas_price_used
    entry = {"label": label, "tx_hash": tx_hash, "status": "success" if status == 1 else "REVERTED",
              "gas_used": int(receipt["gasUsed"], 16), "block_number": int(receipt["blockNumber"], 16),
              "effective_gas_price_wei": effective_gas_price}
    progress.setdefault("txs", []).append(entry)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"[p6_step1] {label}: {entry['status']} (gasUsed={entry['gas_used']})")
    if status != 1:
        raise RuntimeError(f"{label} REVERTED: {tx_hash} -- СТОП.")
    return receipt


def across_quote(origin_chain_id: int, dest_chain_id: int, origin_token: str, amount: str) -> dict:
    """ПРОВЕРЕННАЯ реальными успешными прогонами схема (analysis/p6_dry_run_entry.py::across_quote,
    несколько раз реально вызвана и вернула валидные котировки) -- ТОЛЬКО
    `token` (адрес токена в ORIGIN-сети), не inputToken/outputToken --
    output-токен и его адрес API определяет сам по таблице маршрутов и
    возвращает в ответе (`outputToken.address`), не передаётся отдельно."""
    r = requests.get("https://app.across.to/api/suggested-fees", params={
        "originChainId": origin_chain_id, "destinationChainId": dest_chain_id,
        "token": origin_token, "amount": amount,
    }, timeout=20)
    if r.status_code != 200:
        # НАЙДЕНО (реальный прогон, 2026-09-05): 400 без тела ошибки в
        # логе не даёт понять причину -- печатаем/пробрасываем реальное
        # тело ответа ПЕРЕД raise_for_status(), не гадаем.
        print(f"[p6_step1] across_quote НЕ 200 ({r.status_code}): {r.text[:1000]}")
    r.raise_for_status()
    return r.json()


def build_deposit_v3_calldata(depositor: str, recipient: str, input_token: str, output_token: str,
                               input_amount: int, output_amount: int, dest_chain_id: int,
                               exclusive_relayer: str, quote_timestamp: int, fill_deadline: int,
                               exclusivity_deadline: int) -> bytes:
    sig = ("depositV3(address,address,address,address,uint256,uint256,uint256,"
           "address,uint32,uint32,uint32,bytes)")
    selector = bytes.fromhex(_selector(sig)[2:])
    types = ["address", "address", "address", "address", "uint256", "uint256", "uint256",
             "address", "uint32", "uint32", "uint32", "bytes"]
    values = [depositor, recipient, input_token, output_token, input_amount, output_amount, dest_chain_id,
              exclusive_relayer, quote_timestamp, fill_deadline, exclusivity_deadline, b""]
    return selector + abi_encode(types, values)


def read_pool_state_base() -> dict:
    slot0 = eth_call(BASE_RPC, POOL_ADDRESS, _selector("slot0()"))
    liquidity = int(eth_call(BASE_RPC, POOL_ADDRESS, _selector("liquidity()")), 16)
    tick_spacing_raw = int(eth_call(BASE_RPC, POOL_ADDRESS, _selector("tickSpacing()")), 16)
    tick_spacing = tick_spacing_raw - (1 << 256) if tick_spacing_raw >= (1 << 255) else tick_spacing_raw
    hexdata = slot0[2:]
    sqrt_price_x96 = int(hexdata[0:64], 16)
    tick_word = int(hexdata[64:128], 16)
    tick = tick_word - (1 << 256) if tick_word >= (1 << 255) else tick_word
    return {"sqrtPriceX96": sqrt_price_x96, "tick": tick, "liquidity_raw": liquidity, "tick_spacing": tick_spacing}


def price_cbbtc_usd(sqrt_price_x96: int) -> float:
    raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    price_cbbtc_per_usdc = raw * (10 ** (USDC_DECIMALS - CBBTC_DECIMALS))
    return 1.0 / price_cbbtc_per_usdc


def usd_price_to_tick(p_usd: float) -> int:
    raw = (1 / p_usd) * (10 ** (CBBTC_DECIMALS - USDC_DECIMALS))
    return math.floor(math.log(raw) / math.log(1.0001))


def get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, amount0, amount1) -> float:
    if sqrt_pa > sqrt_pb:
        sqrt_pa, sqrt_pb = sqrt_pb, sqrt_pa
    if sqrt_p <= sqrt_pa:
        return amount0 * (sqrt_pa * sqrt_pb) / (sqrt_pb - sqrt_pa)
    elif sqrt_p < sqrt_pb:
        l0 = amount0 * (sqrt_p * sqrt_pb) / (sqrt_pb - sqrt_p)
        l1 = amount1 / (sqrt_p - sqrt_pa)
        return min(l0, l1)
    else:
        return amount1 / (sqrt_pb - sqrt_pa)


def v3_amounts(liquidity, sqrt_p, sqrt_pa, sqrt_pb) -> tuple[float, float]:
    sqrt_p = min(max(sqrt_p, sqrt_pa), sqrt_pb)
    amount0 = liquidity * (1 / sqrt_p - 1 / sqrt_pb)
    amount1 = liquidity * (sqrt_p - sqrt_pa)
    return max(amount0, 0.0), max(amount1, 0.0)


def lighter_account_full() -> dict | None:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/account", params={"by": "index", "value": str(LIGHTER_ACCOUNT_INDEX)}, timeout=20)
    r.raise_for_status()
    accounts = r.json().get("accounts", [])
    return accounts[0] if accounts else None


def lighter_btc_market() -> dict | None:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    r.raise_for_status()
    markets = r.json().get("order_book_details", [])
    exact = [m for m in markets if str(m.get("symbol", "")).upper() == "BTC"]
    return exact[0] if exact else None


def real_btc_leverage(account_full: dict) -> dict:
    pos = next((p for p in account_full.get("positions", []) if str(p.get("symbol", "")).upper() == "BTC"), None)
    if pos is None:
        return {"found": False}
    imf_pct = float(pos["initial_margin_fraction"])
    return {"found": True, "initial_margin_fraction_pct": imf_pct, "leverage": 100.0 / imf_pct if imf_pct else None,
            "margin_mode": pos.get("margin_mode")}


def main() -> int:
    confirm = "--confirm-mainnet" in sys.argv
    t0 = time.time()
    progress: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": "REAL" if confirm else "DRY-RUN"}

    if not RECON_PATH.exists():
        print("[p6_step1] СТОП: analysis/p6_entry_recon.py ещё не запускался -- реальный NFPM/Router не подтверждён.")
        return 1
    recon = json.loads(RECON_PATH.read_text())
    if recon.get("matched_deployment") is None:
        print(f"[p6_step1] СТОП: разведка не нашла совпадения NFPM/factory -- {recon.get('abort_reason')}")
        return 1
    NFPM = recon["confirmed_nfpm"]
    ROUTER = recon["confirmed_router"]
    print(f"[p6_step1] реальный NFPM={NFPM} Router={ROUTER} (подтверждено разведкой, {recon['matched_deployment']})")

    print("=== П.0: реальное текущее плечо BTC (не предположение) ===")
    account_full = lighter_account_full()
    btc_market = lighter_btc_market()
    btc_leverage = real_btc_leverage(account_full) if account_full else {"found": False}
    print(f"[p6_step1] плечо BTC сейчас: {btc_leverage}")
    if not btc_leverage.get("found") or not btc_leverage.get("leverage"):
        print("[p6_step1] СТОП: реальное плечо BTC не подтверждено -- запустите analysis/p6_set_btc_leverage.py.")
        return 1
    existing_btc_pos = next((p for p in account_full.get("positions", [])
                              if str(p.get("symbol", "")).upper() == "BTC" and abs(float(p.get("position", 0))) > 1e-9), None)
    if existing_btc_pos:
        print(f"[p6_step1] СТОП: на аккаунте УЖЕ есть открытая BTC-позиция ({existing_btc_pos}) -- не открываю поверх.")
        return 1

    progress["recon_used"] = {"nfpm": NFPM, "router": ROUTER}
    progress["btc_leverage_confirmed"] = btc_leverage

    if not confirm:
        # DRY-RUN расширенный -- реально запрашивает котировки Across (то
        # же самое чтение, без отправки) и печатает план, вместо того
        # чтобы останавливаться сразу после проверки плеча -- ловит
        # проблемы схемы API/сети ДО реальных денег.
        usdc_before_dry = erc20_balance(BASE_RPC, USDC, WALLET)
        eth_before_dry = int(rpc(BASE_RPC, "eth_getBalance", [WALLET, "latest"]), 16)
        usdg_amount_wei_dry = int(TARGET_TOTAL_CAPITAL_USD * 10 ** 6)
        quote_usdg_dry = across_quote(ROBINHOOD_CHAIN_ID, BASE_CHAIN_ID, USDG, str(usdg_amount_wei_dry))
        eth_bridge_wei_dry = int(BRIDGE_ETH_AMOUNT_ETH * 1e18)
        quote_eth_dry = across_quote(ROBINHOOD_CHAIN_ID, BASE_CHAIN_ID,
                                      "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", str(eth_bridge_wei_dry))
        pool_dry = read_pool_state_base()
        p0_dry = price_cbbtc_usd(pool_dry["sqrtPriceX96"])
        progress["dry_run_plan"] = {
            "base_balances_now": {"usdc_human": usdc_before_dry / 10 ** USDC_DECIMALS, "eth_human": eth_before_dry / 1e18},
            "across_quote_usdg": quote_usdg_dry, "across_quote_eth": quote_eth_dry,
            "pool_price_cbbtc_usd_now": p0_dry, "pool_tick_now": pool_dry["tick"], "tick_spacing": pool_dry["tick_spacing"],
        }
        progress["note"] = "DRY-RUN -- ничего не отправлено (котировки реально запрошены, план выше). Запустите с --confirm-mainnet для реального входа."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p6_step1] {progress['note']}")
        print(json.dumps(progress["dry_run_plan"], indent=2, default=str, ensure_ascii=False))
        return 0

    eth_usd_price = None
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "ethereum", "vs_currencies": "usd"},
                          headers={"User-Agent": "robinhood-chain-alpha-p6/1.0"}, timeout=20)
        eth_usd_price = float(r.json()["ethereum"]["usd"])
        print(f"[p6_step1] ETH/USD (для потолка газа на обе сети): ${eth_usd_price}")
    except Exception as exc:  # noqa: BLE001
        print(f"[p6_step1] ETH/USD недоступен ({exc}) -- потолок газа на транзакцию пропущен для этого прогона.")

    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении.")
    if priv_hex.startswith("0x"):
        priv_hex = priv_hex[2:]
    account = Account.from_key(bytes.fromhex(priv_hex))
    if account.address.lower() != WALLET.lower():
        raise RuntimeError(f"PRIVATE_KEY_NOX даёт {account.address}, ожидался {WALLET} -- СТОП.")

    # ============================= ШАГ 1: Across USDG -> USDC на Base =============================
    print(f"\n=== ШАГ 1: Across -- бридж USDG->USDC (${TARGET_TOTAL_CAPITAL_USD}) и ETH->ETH (газ на Base) ===")
    chain_id_check = int(rpc(ROBINHOOD_RPC, "eth_chainId", []), 16)
    if chain_id_check != ROBINHOOD_CHAIN_ID:
        raise RuntimeError(f"chainId Robinhood {chain_id_check} != {ROBINHOOD_CHAIN_ID} -- СТОП")

    usdc_before_bridge = erc20_balance(BASE_RPC, USDC, WALLET)
    eth_before_bridge = int(rpc(BASE_RPC, "eth_getBalance", [WALLET, "latest"]), 16)
    print(f"[p6_step1] Base ДО моста: USDC={usdc_before_bridge / 10**USDC_DECIMALS} ETH={eth_before_bridge / 1e18}")

    usdg_amount_wei = int(TARGET_TOTAL_CAPITAL_USD * 10 ** 6)
    quote_usdg = across_quote(ROBINHOOD_CHAIN_ID, BASE_CHAIN_ID, USDG, str(usdg_amount_wei))
    progress["across_quote_usdg"] = quote_usdg
    output_token_usdg = quote_usdg["outputToken"]["address"]  # из ответа, не предположено -- должен быть USDC на Base
    if output_token_usdg.lower() != USDC.lower():
        raise RuntimeError(f"котировка USDG вернула неожиданный outputToken={output_token_usdg}, ожидался USDC={USDC} -- СТОП.")
    print(f"[p6_step1] котировка USDG->USDC: outputAmount={quote_usdg.get('outputAmount')} outputToken={output_token_usdg}")

    eth_bridge_wei = int(BRIDGE_ETH_AMOUNT_ETH * 1e18)
    # ETH -- нативный маршрут (isNative:true, подтверждено p6_entry_recon.py) --
    # origin_token в запросе -- WETH-адрес-плейсхолдер на Robinhood chain
    # (тот же, что реально вернулся в available-routes для isNative:true записи),
    # outputToken -- из ответа (должен быть нативный ETH-сентинел на Base).
    quote_eth = across_quote(ROBINHOOD_CHAIN_ID, BASE_CHAIN_ID,
                              "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", str(eth_bridge_wei))
    progress["across_quote_eth"] = quote_eth
    output_token_eth = quote_eth["outputToken"]["address"]
    print(f"[p6_step1] котировка ETH->ETH: outputAmount={quote_eth.get('outputAmount')} outputToken={output_token_eth}")
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    print("\n--- PRE-FLIGHT (обязательно перед мостом): реальный баланс USDG/ETH на Robinhood Chain ---")
    usdg_balance_now = erc20_balance(ROBINHOOD_RPC, USDG, WALLET)
    eth_balance_robinhood_now = int(rpc(ROBINHOOD_RPC, "eth_getBalance", [WALLET, "latest"]), 16)
    preflight_balance("USDG перед мостом", usdg_balance_now, usdg_amount_wei, 6, "USDG")
    preflight_balance("ETH(Robinhood) перед мостом", eth_balance_robinhood_now, eth_bridge_wei, 18, "ETH")
    progress["preflight_bridge"] = {"usdg_balance_now": usdg_balance_now / 1e6, "eth_balance_robinhood_now": eth_balance_robinhood_now / 1e18,
                                     "usdg_required": usdg_amount_wei / 1e6, "eth_required": eth_bridge_wei / 1e18}
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    nonce = eth_nonce(ROBINHOOD_RPC)
    # Владелец, 2026-09-05: "Approve не трогать, allowance останется" --
    # реально уже есть allowance $161 (прошлая попытка, tx=0xe3ab9448...),
    # этого достаточно для нового меньшего $80 -- реально ПЕРЕЧИТАНО, не
    # предположено; approve отправляется, только если реально не хватает.
    existing_allowance = erc20_allowance(ROBINHOOD_RPC, USDG, WALLET, SPOKE_POOL)
    print(f"[p6_step1] реальный текущий allowance USDG->SpokePool: {existing_allowance / 1e6} (нужно {usdg_amount_wei / 1e6})")
    if existing_allowance < usdg_amount_wei:
        send_and_wait(ROBINHOOD_RPC, ROBINHOOD_CHAIN_ID, account, "1_approve_USDG_to_SpokePool", USDG,
                      erc20_approve_calldata(SPOKE_POOL, usdg_amount_wei), 0, nonce, progress, eth_usd_price)
        nonce += 1
    else:
        print("[p6_step1] 1_approve_USDG_to_SpokePool: ПРОПУЩЕН -- существующего allowance уже достаточно.")

    deposit_usdg_calldata = build_deposit_v3_calldata(
        WALLET, WALLET, USDG, USDC, usdg_amount_wei, int(quote_usdg["outputAmount"]), BASE_CHAIN_ID,
        quote_usdg["exclusiveRelayer"], int(quote_usdg["timestamp"]), int(quote_usdg["fillDeadline"]),
        int(quote_usdg["exclusivityDeadline"]),
    )
    send_and_wait(ROBINHOOD_RPC, ROBINHOOD_CHAIN_ID, account, "2_depositV3_USDG", SPOKE_POOL,
                  deposit_usdg_calldata, 0, nonce, progress, eth_usd_price)
    nonce += 1

    deposit_eth_calldata = build_deposit_v3_calldata(
        WALLET, WALLET, "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", output_token_eth,
        eth_bridge_wei, int(quote_eth["outputAmount"]), BASE_CHAIN_ID,
        quote_eth["exclusiveRelayer"], int(quote_eth["timestamp"]), int(quote_eth["fillDeadline"]),
        int(quote_eth["exclusivityDeadline"]),
    )
    send_and_wait(ROBINHOOD_RPC, ROBINHOOD_CHAIN_ID, account, "3_depositV3_ETH_native", SPOKE_POOL,
                  deposit_eth_calldata, eth_bridge_wei, nonce, progress, eth_usd_price)

    print("\n=== Ожидание заполнения моста на Base (poll реального баланса, до 10 минут) ===")
    expected_usdc_min = usdc_before_bridge + int(int(quote_usdg["outputAmount"]) * 0.9)  # запас на возможное отклонение котировки
    expected_eth_min = eth_before_bridge + int(int(quote_eth["outputAmount"]) * 0.5)
    deadline = time.time() + BRIDGE_FILL_TIMEOUT_S
    filled = False
    while time.time() < deadline:
        usdc_now = erc20_balance(BASE_RPC, USDC, WALLET)
        eth_now = int(rpc(BASE_RPC, "eth_getBalance", [WALLET, "latest"]), 16)
        print(f"[p6_step1] Base сейчас: USDC={usdc_now / 10**USDC_DECIMALS} ETH={eth_now / 1e18}")
        if usdc_now >= expected_usdc_min and eth_now >= expected_eth_min:
            filled = True
            break
        time.sleep(BRIDGE_FILL_POLL_S)
    progress["bridge_fill_check"] = {"filled": filled, "usdc_before": usdc_before_bridge, "eth_before": eth_before_bridge}
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    if not filled:
        progress["CRITICAL"] = ("Мост НЕ подтвердил заполнение за отведённое время -- средства В ПУТИ на "
                                 "SpokePool (Robinhood), НЕ отправляю повторно, НЕ считаю потерянными. "
                                 "Проверить вручную: FundsDeposited на Robinhood, FilledV3Relay на Base "
                                 "(analysis/across_common.py) для реальных depositId выше.")
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p6_step1] СТОП: {progress['CRITICAL']}")
        return 1
    usdc_balance_now = erc20_balance(BASE_RPC, USDC, WALLET)
    print(f"[p6_step1] МОСТ ЗАПОЛНЕН: USDC={usdc_balance_now / 10**USDC_DECIMALS}")

    # ============================= ШАГ 2: своп половины в cbBTC =============================
    print("\n=== ШАГ 2: exactInputSingle -- своп части USDC в cbBTC (Aerodrome Slipstream Router) ===")
    chain_id_base_check = int(rpc(BASE_RPC, "eth_chainId", []), 16)
    if chain_id_base_check != BASE_CHAIN_ID:
        raise RuntimeError(f"chainId Base {chain_id_base_check} != {BASE_CHAIN_ID} -- СТОП")

    pool = read_pool_state_base()
    p0 = price_cbbtc_usd(pool["sqrtPriceX96"])
    pa_usd, pb_usd = p0 * (1 - RANGE_PCT), p0 * (1 + RANGE_PCT)
    usdc_total_human = usdc_balance_now / 10 ** USDC_DECIMALS

    def usd_to_domain(p_usd: float) -> float:
        return 1.0 / p_usd

    sqrt_p = usd_to_domain(p0) ** 0.5
    sqrt_pa, sqrt_pb = usd_to_domain(pb_usd) ** 0.5, usd_to_domain(pa_usd) ** 0.5
    amount0_target = usdc_total_human / 2
    amount1_target = (usdc_total_human / 2) / p0
    L_target = get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, amount0_target, amount1_target)
    amount0_at_L, amount1_at_L = v3_amounts(L_target, sqrt_p, sqrt_pa, sqrt_pb)
    usdc_to_swap_human = amount1_at_L * p0  # $-стоимость нужной cbBTC-ноги -- именно столько USDC меняем
    usdc_to_swap_raw = int(usdc_to_swap_human * 10 ** USDC_DECIMALS)
    expected_cbbtc_out = usdc_to_swap_human / p0
    min_cbbtc_out_raw = int(expected_cbbtc_out * (1 - SWAP_SLIPPAGE) * 10 ** CBBTC_DECIMALS)
    print(f"[p6_step1] price=${p0:.2f}, USDC всего={usdc_total_human:.4f}, своп в cbBTC={usdc_to_swap_human:.4f} USDC "
          f"-> ожидаемо {expected_cbbtc_out:.8f} cbBTC (min={min_cbbtc_out_raw})")
    progress["swap_plan"] = {"pool_price_usd": p0, "usdc_total_human": usdc_total_human,
                              "usdc_to_swap_human": usdc_to_swap_human, "expected_cbbtc_out": expected_cbbtc_out}
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    print("\n--- PRE-FLIGHT (обязательно перед свопом): реальный баланс USDC на Base ---")
    usdc_balance_preswap = erc20_balance(BASE_RPC, USDC, WALLET)
    preflight_balance("USDC перед свопом", usdc_balance_preswap, usdc_to_swap_raw, USDC_DECIMALS, "USDC")

    nonce_base = eth_nonce(BASE_RPC)
    send_and_wait(BASE_RPC, BASE_CHAIN_ID, account, "4_approve_USDC_to_Router", USDC,
                  erc20_approve_calldata(ROUTER, usdc_to_swap_raw), 0, nonce_base, progress, eth_usd_price)
    nonce_base += 1

    swap_selector = bytes.fromhex(_selector(
        "exactInputSingle((address,address,int24,address,uint256,uint256,uint256,uint160))")[2:])
    swap_deadline = int(time.time()) + 600
    swap_params = (to_checksum_address(USDC), to_checksum_address(CBBTC), pool["tick_spacing"],
                   to_checksum_address(WALLET), swap_deadline, usdc_to_swap_raw, min_cbbtc_out_raw, 0)
    swap_calldata = swap_selector + abi_encode(
        ["(address,address,int24,address,uint256,uint256,uint256,uint160)"], [swap_params])
    send_and_wait(BASE_RPC, BASE_CHAIN_ID, account, "5_exactInputSingle_swap", ROUTER, swap_calldata, 0, nonce_base, progress, eth_usd_price)
    nonce_base += 1

    usdc_after_swap = erc20_balance(BASE_RPC, USDC, WALLET)
    cbbtc_after_swap = erc20_balance(BASE_RPC, CBBTC, WALLET)
    print(f"[p6_step1] после свопа: USDC={usdc_after_swap / 10**USDC_DECIMALS} cbBTC={cbbtc_after_swap / 10**CBBTC_DECIMALS}")

    # ============================= ШАГ 3: mint LP +-10% =============================
    print("\n=== ШАГ 3: mint LP на Aerodrome Slipstream (тик-диапазон пересчитан СВЕЖЕ) ===")
    pool_fresh = read_pool_state_base()
    p0_fresh = price_cbbtc_usd(pool_fresh["sqrtPriceX96"])
    pa_fresh, pb_fresh = p0_fresh * (1 - RANGE_PCT), p0_fresh * (1 + RANGE_PCT)
    ts = pool_fresh["tick_spacing"]
    tick_at_pb = usd_price_to_tick(pb_fresh)
    tick_at_pa = usd_price_to_tick(pa_fresh)
    tick_lower = math.floor(tick_at_pb / ts) * ts
    tick_upper = math.ceil(tick_at_pa / ts) * ts
    if tick_lower >= tick_upper:
        tick_upper = tick_lower + ts

    amount0_desired = usdc_after_swap
    amount1_desired = cbbtc_after_swap
    amount0_min = int(amount0_desired * (1 - MINT_SLIPPAGE))
    amount1_min = int(amount1_desired * (1 - MINT_SLIPPAGE))
    mint_deadline = int(time.time()) + 600

    print(f"[p6_step1] mint: tick_lower={tick_lower} tick_upper={tick_upper} tickSpacing={ts} "
          f"amount0(USDC)={amount0_desired / 10**USDC_DECIMALS} amount1(cbBTC)={amount1_desired / 10**CBBTC_DECIMALS}")

    print("\n--- PRE-FLIGHT (обязательно перед mint): реальные балансы USDC и cbBTC на Base ---")
    usdc_balance_premint = erc20_balance(BASE_RPC, USDC, WALLET)
    cbbtc_balance_premint = erc20_balance(BASE_RPC, CBBTC, WALLET)
    preflight_balance("USDC перед mint", usdc_balance_premint, amount0_desired, USDC_DECIMALS, "USDC")
    preflight_balance("cbBTC перед mint", cbbtc_balance_premint, amount1_desired, CBBTC_DECIMALS, "cbBTC")

    send_and_wait(BASE_RPC, BASE_CHAIN_ID, account, "6_approve_USDC_to_NFPM", USDC,
                  erc20_approve_calldata(NFPM, amount0_desired), 0, nonce_base, progress, eth_usd_price)
    nonce_base += 1
    send_and_wait(BASE_RPC, BASE_CHAIN_ID, account, "7_approve_CBBTC_to_NFPM", CBBTC,
                  erc20_approve_calldata(NFPM, amount1_desired), 0, nonce_base, progress, eth_usd_price)
    nonce_base += 1

    mint_selector = bytes.fromhex(_selector(
        "mint((address,address,int24,int24,int24,uint256,uint256,uint256,uint256,address,uint256,uint160))")[2:])
    mint_params = (to_checksum_address(USDC), to_checksum_address(CBBTC), ts, tick_lower, tick_upper,
                   amount0_desired, amount1_desired, amount0_min, amount1_min, to_checksum_address(WALLET),
                   mint_deadline, 0)
    mint_calldata = mint_selector + abi_encode(
        ["(address,address,int24,int24,int24,uint256,uint256,uint256,uint256,address,uint256,uint160)"], [mint_params])
    mint_receipt = send_and_wait(BASE_RPC, BASE_CHAIN_ID, account, "8_mint", NFPM, mint_calldata, 0, nonce_base, progress, eth_usd_price)

    increase_liq_topic0 = _topic0("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
    liq_event = None
    for log in mint_receipt.get("logs", []):
        if log["address"].lower() == NFPM.lower() and log["topics"][0].lower() == increase_liq_topic0.lower():
            token_id = int(log["topics"][1], 16)
            liquidity, amt0, amt1 = abi_decode(["uint128", "uint256", "uint256"], bytes.fromhex(log["data"][2:]))
            liq_event = {"token_id": token_id, "liquidity": liquidity, "amount0_wei": amt0, "amount1_wei": amt1}
            break
    if liq_event is None:
        progress["CRITICAL"] = "mint() прошёл (status=success), но IncreaseLiquidity событие НЕ найдено в логах -- разобрать вручную, tokenId неизвестен."
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p6_step1] {progress['CRITICAL']}")
        return 1

    real_amount0_usdc = liq_event["amount0_wei"] / 10 ** USDC_DECIMALS
    real_amount1_cbbtc = liq_event["amount1_wei"] / 10 ** CBBTC_DECIMALS
    progress["lp_position"] = {"token_id": liq_event["token_id"], "liquidity": liq_event["liquidity"],
                                "amount0_usdc_actual": real_amount0_usdc, "amount1_cbbtc_actual": real_amount1_cbbtc,
                                "tick_lower": tick_lower, "tick_upper": tick_upper}
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"[p6_step1] LP ОТКРЫТА: tokenId={liq_event['token_id']} amount0(USDC)={real_amount0_usdc:.4f} "
          f"amount1(cbBTC)={real_amount1_cbbtc:.8f}")

    # ============================= ШАГ 4: хедж на Lighter (после подтверждения mint) =============================
    print("\n=== ШАГ 4: шорт BTC на Lighter (плечо уже 2.0x, размер = реальная cbBTC-нога) ===")
    real_delta_btc = real_amount1_cbbtc
    progress["hedge_target_delta_btc"] = real_delta_btc

    print("\n--- PRE-FLIGHT (обязательно перед шортом): реальный коллатерал на Lighter, свежий (не из П.0) ---")
    btc_market = lighter_btc_market()  # перечитано СВЕЖЕ -- с П.0 прошло время (мост+своп+mint)
    account_full_prehedge = lighter_account_full()
    collateral_prehedge = float(account_full_prehedge.get("collateral", 0)) if account_full_prehedge else 0.0
    mark_price_prehedge = float(btc_market["mark_price"]) if btc_market else None
    short_notional_prehedge = real_delta_btc * mark_price_prehedge if mark_price_prehedge else None
    required_margin_prehedge = short_notional_prehedge / btc_leverage["leverage"] if short_notional_prehedge else None
    print(f"[p6_step1] PRE-FLIGHT шорт: collateral реально=${collateral_prehedge:.4f}, notional=${short_notional_prehedge:.4f}, "
          f"требуемая маржа=${required_margin_prehedge:.4f}")
    if required_margin_prehedge is None or collateral_prehedge < required_margin_prehedge:
        progress["CRITICAL"] = (f"LP-позиция (tokenId={liq_event['token_id']}) ОТКРЫТА, но PRE-FLIGHT перед шортом провален -- "
                                 f"collateral=${collateral_prehedge} < требуемая маржа=${required_margin_prehedge}. "
                                 "ТРЕБУЕТСЯ НЕМЕДЛЕННОЕ РУЧНОЕ ВМЕШАТЕЛЬСТВО ВЛАДЕЛЬЦА (LP без хеджа).")
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p6_step1] {progress['CRITICAL']}")
        return 1
    progress["preflight_hedge"] = {"collateral_usd": collateral_prehedge, "short_notional_usd": short_notional_prehedge,
                                    "required_margin_usd": required_margin_prehedge, "mark_price_usd": mark_price_prehedge}
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    async def _place_hedge() -> dict:
        import lighter
        lighter_priv = os.environ["LIGHTER_API_KEY_PRIVATE"]
        client = lighter.SignerClient(url=LIGHTER_API_BASE, account_index=LIGHTER_ACCOUNT_INDEX,
                                       api_private_keys={LIGHTER_API_KEY_INDEX: lighter_priv})
        try:
            base_amount = round(real_delta_btc * 10 ** btc_market["size_decimals"])
            client_order_index = int(time.time()) % (2 ** 31)
            tx, resp, err = await client.create_market_order_limited_slippage(
                market_index=btc_market["market_id"], client_order_index=client_order_index, base_amount=base_amount,
                max_slippage=HEDGE_SLIPPAGE, is_ask=True, reduce_only=False, api_key_index=LIGHTER_API_KEY_INDEX,
            )
            return {"tx_hash": resp.tx_hash if resp else None, "resp_code": resp.code if resp else None,
                    "resp_message": resp.message if resp else None, "err": str(err) if err is not None else None,
                    "base_amount": base_amount, "size_btc_requested": real_delta_btc}
        finally:
            await client.close()

    def _verify_hedge_filled(expected_size_btc: float, attempts: int = 4, delay_s: float = 3.0) -> dict:
        last_positions = []
        for i in range(attempts):
            if i > 0:
                time.sleep(delay_s)
            acc = lighter_account_full()
            last_positions = acc.get("positions", []) if acc else []
            btc_pos = next((p for p in last_positions if str(p.get("symbol", "")).upper() == "BTC"), None)
            if btc_pos is not None and abs(float(btc_pos.get("position", 0))) >= expected_size_btc * 0.85:
                return {"filled": True, "position": btc_pos, "attempts_used": i + 1}
            print(f"[p6_step1] проверка филла: попытка {i + 1}/{attempts} -- {last_positions}")
        return {"filled": False, "position": None, "attempts_used": attempts, "last_positions_seen": last_positions}

    try:
        order_info = asyncio.run(_place_hedge())
        progress["hedge_order"] = order_info
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        if order_info.get("err") is not None:
            raise RuntimeError(f"create_market_order вернул ошибку: {order_info['err']}")
        fill_check = _verify_hedge_filled(real_delta_btc)
        progress["hedge_fill_check"] = fill_check
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        if not fill_check["filled"]:
            raise RuntimeError(f"err=None, но реальная BTC-позиция НЕ появилась после {fill_check['attempts_used']} проверок.")
    except Exception as e:  # noqa: BLE001
        progress["hedge_error"] = f"{type(e).__name__}: {e}"
        progress["CRITICAL"] = (f"LP-позиция (tokenId={liq_event['token_id']}) ОТКРЫТА и НЕ ЗАХЕДЖИРОВАНА "
                                 f"(~{real_delta_btc:.8f} cbBTC экспозиции) -- хедж не прошёл. Реальное закрытие LP "
                                 f"НЕ выполнено автоматически (P6 -- новый протокол, автозакрытие для него не "
                                 f"писалось и не тестировалось) -- ТРЕБУЕТСЯ РУЧНОЕ ВМЕШАТЕЛЬСТВО ВЛАДЕЛЬЦА.")
        OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
        print(f"[p6_step1] {progress['CRITICAL']}")
        return 1

    real_hedge_size_btc = abs(float(fill_check["position"].get("position", 0)))
    avg_entry_price = float(fill_check["position"].get("avg_entry_price", 0))
    liq_price_exchange = float(fill_check["position"]["liquidation_price"]) if fill_check["position"].get("liquidation_price") not in (None, "") else None
    account_post_hedge = lighter_account_full()
    collateral_post = float(account_post_hedge.get("collateral", 0))
    available_post = float(account_post_hedge.get("available_balance", 0))
    free_margin_pct_post = (available_post / collateral_post * 100) if collateral_post else None
    mmf = float(btc_market["maintenance_margin_fraction"]) / 10000
    p_liq_formula = (collateral_post + real_hedge_size_btc * avg_entry_price) / (real_hedge_size_btc * (1 + mmf))
    net_delta_btc_final = real_amount1_cbbtc - real_hedge_size_btc

    progress["hedge_confirmed"] = {
        "size_btc": real_hedge_size_btc, "avg_entry_price_usd": avg_entry_price,
        "liquidation_price_exchange_usd": liq_price_exchange, "liquidation_price_formula_usd": p_liq_formula,
        "collateral_usd": collateral_post, "available_balance_usd": available_post, "free_margin_pct": free_margin_pct_post,
        "net_delta_btc": net_delta_btc_final,
    }
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))
    print(f"[p6_step1] ХЕДЖ ПОДТВЕРЖДЁН: size={real_hedge_size_btc} BTC @ ${avg_entry_price} "
          f"liq(биржа)=${liq_price_exchange} liq(формула)=${p_liq_formula} free_margin={free_margin_pct_post}% "
          f"дельта={net_delta_btc_final}")

    total_gas_wei = sum(tx["gas_used"] * tx["effective_gas_price_wei"] for tx in progress.get("txs", []))
    progress["runtime_s"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(progress, indent=2, default=str, ensure_ascii=False))

    position_state = {
        "opened_at_utc": progress["generated_at_utc"], "token_id": liq_event["token_id"],
        "lp_amount0_usdc_entry": real_amount0_usdc, "lp_amount1_cbbtc_entry": real_amount1_cbbtc,
        "tick_lower": tick_lower, "tick_upper": tick_upper, "pool_price_usd_entry": p0_fresh,
        "hedge_size_btc_entry": real_hedge_size_btc, "hedge_entry_price_usd": avg_entry_price,
        "leverage_entry": btc_leverage["leverage"], "margin_available_usd_entry": collateral_post,
        "capital_at_risk_usd_entry": real_amount0_usdc + real_amount1_cbbtc * p0_fresh + collateral_post,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(position_state, indent=2, default=str, ensure_ascii=False))

    print(f"\n[p6_step1] ГОТОВО. tokenId={liq_event['token_id']} LP=({real_amount0_usdc:.4f} USDC, "
          f"{real_amount1_cbbtc:.8f} cbBTC) хедж={real_hedge_size_btc:.8f} BTC @ ${avg_entry_price} "
          f"дельта={net_delta_btc_final:.8f} свободная_маржа={free_margin_pct_post:.2f}% "
          f"газ_wei={total_gas_wei}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
