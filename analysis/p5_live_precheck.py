#!/usr/bin/env python3
"""P5 LIVE, Step 1 -- ПРЕДВАРИТЕЛЬНАЯ ПРОВЕРКА (владелец, 2026-09-03):
"пересчитать требуемые суммы под LP+хедж, используя цену ETH
ИСКЛЮЧИТЕЛЬНО с ончейн-источников... Если по этим данным текущего
баланса не хватает — остановиться и доложить точную недостающую сумму,
не открывать позицию частично."

ТОЛЬКО ЧТЕНИЕ. Транзакций нет. Цель -- дать однозначный ответ
достаточно/недостаточно ДО того, как строится сама mint-транзакция.

Источники цены -- ИСКЛЮЧИТЕЛЬНО ончейн: (а) sqrtPriceX96 пула P5
(прямой eth_call), (б) mark_price ETH-перпа на Lighter (публичный
REST mainnet.zklighter.elliot.ai -- это цена ПЕРП-РЫНКА, определяется
ончейн-подобным оракулом/книгой заявок биржи, не веб-поиском).

Также находит РЕАЛЬНЫЙ адрес NonfungiblePositionManager на Robinhood
Chain -- НЕ предполагается по стандартному адресу Uniswap Labs
(периферийные контракты почти наверняка задеплоены отдельно на этой
сети, т.к. Factory уже оказался нестандартным, см. docs/
PROJECT_STATE.md) -- ищется по факту: сканируются реальные Mint-события
на пуле P5, `owner` в событии -- это и есть адрес, через который
реально проходят чужие mint()-вызовы (сам виджет владельца никогда не
использовался напрямую с pool.mint(), это periphery-паттерн).

Формула getLiquidityForAmounts -- ДОСЛОВНО из реального источника
(Uniswap/v3-periphery/contracts/libraries/LiquidityAmounts.sol),
проверено WebFetch, не по памяти.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
from eth_abi import decode as abi_decode  # noqa: E402
from eth_account import Account  # noqa: E402

from alchemy_fallback import _chunked_get_logs, _rpc_call, get_block_number, topic0  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_live_precheck_result.json")
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
P5_POOL = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca".lower()
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
LIGHTER_API_BASE = "https://mainnet.zklighter.elliot.ai"
LIGHTER_ACCOUNT_INDEX = 22012
GAS_RESERVE_USD = 10.0
RANGE_PCT = 0.10
MAX_LEVERAGE = 3.0
MINT_TOPIC0 = topic0("Mint(address,address,int24,int24,uint128,uint256,uint256)")

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n


def _eth_call(to: str, data: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"[p5_live_precheck]   eth_call {to} {data[:10]} не удался: {e}")
        return None


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def read_pool_state() -> dict:
    liq = _eth_call(P5_POOL, _selector("liquidity()"))
    slot0 = _eth_call(P5_POOL, _selector("slot0()"))
    tick_spacing_raw = _eth_call(P5_POOL, _selector("tickSpacing()"))
    token0 = _eth_call(P5_POOL, _selector("token0()"))
    token1 = _eth_call(P5_POOL, _selector("token1()"))
    liquidity = abi_decode(["uint128"], bytes.fromhex(liq[2:]))[0]
    sqrt_price_x96, tick, *_ = abi_decode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], bytes.fromhex(slot0[2:]))
    tick_spacing = abi_decode(["int24"], bytes.fromhex(tick_spacing_raw[2:]))[0]
    return {
        "liquidity_raw": liquidity, "sqrt_price_x96": sqrt_price_x96, "tick": tick,
        "tick_spacing": tick_spacing, "token0": "0x" + token0[-40:], "token1": "0x" + token1[-40:],
    }


def price_from_sqrt(sqrt_price_x96: int) -> float:
    raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    return raw * (10 ** (WETH_DECIMALS - USDG_DECIMALS))


def find_nfpm_address() -> dict:
    latest = get_block_number()
    _count()
    window = 500_000  # ~14ч при ~9.75 блоков/с -- достаточно широкое окно для реальных mint'ов
    from_block = max(1, latest - window)
    logs = list(_chunked_get_logs(
        from_block, latest, [MINT_TOPIC0], chunk_size=5_000, address=P5_POOL,
        on_call=lambda lo, hi, n: _count(1),
    ))
    owners: dict[str, int] = {}
    for log in logs:
        owner = "0x" + str(log["topics"][1])[-40:]
        owners[owner] = owners.get(owner, 0) + 1
    ranked = sorted(owners.items(), key=lambda x: x[1], reverse=True)
    return {"window_blocks": window, "n_mint_events_found": len(logs),
            "owners_ranked": ranked[:5], "most_common_owner": ranked[0][0] if ranked else None}


def wallet_balances() -> dict:
    _count()
    eth_raw = int(_rpc_call("eth_getBalance", [WALLET, "latest"]), 16)
    calldata = "0x" + _selector("balanceOf(address)")[2:] + WALLET[2:].rjust(64, "0").lower()
    usdg_raw = int(_eth_call(USDG, calldata), 16)
    return {"eth_raw_wei": eth_raw, "eth_human": eth_raw / 1e18,
            "usdg_raw": usdg_raw, "usdg_human": usdg_raw / 1e6}


def lighter_eth_perp() -> dict | None:
    resp = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    resp.raise_for_status()
    markets = resp.json().get("order_book_details", [])
    exact = [m for m in markets if str(m.get("symbol", "")).upper() == "ETH"]
    return exact[0] if exact else None


def lighter_margin() -> dict:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/account",
                      params={"by": "index", "value": str(LIGHTER_ACCOUNT_INDEX)}, timeout=20)
    r.raise_for_status()
    accounts = r.json().get("accounts", [])
    if not accounts:
        return {"found": False}
    acct = accounts[0]
    return {"found": True, "collateral_usd": float(acct.get("collateral", 0)),
            "available_balance_usd": float(acct.get("available_balance", 0))}


def get_liquidity_for_amounts(sqrt_p: float, sqrt_pa: float, sqrt_pb: float, amount0: float, amount1: float) -> float:
    """Дословно Uniswap/v3-periphery LiquidityAmounts.sol::getLiquidityForAmounts
    (работает во float, не в Q96-фиксированной точке -- для оценочного
    расчёта перед реальной транзакцией этого достаточно; в саму
    транзакцию идут amount0Desired/amount1Desired напрямую, не L)."""
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


def v3_amounts(liquidity: float, sqrt_p: float, sqrt_pa: float, sqrt_pb: float) -> tuple[float, float]:
    sqrt_p = min(max(sqrt_p, sqrt_pa), sqrt_pb)
    amount0 = liquidity * (1 / sqrt_p - 1 / sqrt_pb)
    amount1 = liquidity * (sqrt_p - sqrt_pa)
    return max(amount0, 0.0), max(amount1, 0.0)


def run() -> int:
    t0 = time.time()

    print("=== Ончейн-цена пула P5 (fresh) ===")
    pool = read_pool_state()
    pool_price = price_from_sqrt(pool["sqrt_price_x96"])
    print(f"[p5_live_precheck] pool: price=${pool_price:.4f} tick={pool['tick']} "
          f"tickSpacing={pool['tick_spacing']} token0={pool['token0']} token1={pool['token1']} "
          f"liquidity_raw={pool['liquidity_raw']}")

    print("\n=== Lighter ETH-перп mark price (fresh) ===")
    eth_market = lighter_eth_perp()
    lighter_price = float(eth_market["mark_price"]) if eth_market else None
    print(f"[p5_live_precheck] Lighter mark_price=${lighter_price}")

    print("\n=== NonfungiblePositionManager -- реальный адрес (по Mint-событиям) ===")
    nfpm = find_nfpm_address()
    print(f"[p5_live_precheck] найдено Mint-событий за {nfpm['window_blocks']} блоков: "
          f"{nfpm['n_mint_events_found']}, топ владельцы: {nfpm['owners_ranked']}")

    print("\n=== Балансы (fresh) ===")
    wb = wallet_balances()
    lm = lighter_margin()
    print(f"[p5_live_precheck] кошелёк: ETH={wb['eth_human']} USDG={wb['usdg_human']}")
    print(f"[p5_live_precheck] Lighter margin: {lm}")

    # --- Расчёт: используем ОНЧЕЙН-цену пула как основную (это сам актив
    # хеджа/позиции), Lighter mark -- для сверки и для хеджа. ---
    p0 = pool_price
    gas_reserve_eth = GAS_RESERVE_USD / p0
    usable_eth = max(0.0, wb["eth_human"] - gas_reserve_eth)
    usable_usdg = wb["usdg_human"]

    pa, pb = p0 * (1 - RANGE_PCT), p0 * (1 + RANGE_PCT)
    sqrt_p, sqrt_pa, sqrt_pb = p0 ** 0.5, pa ** 0.5, pb ** 0.5
    # token0=WETH, token1=USDG (подтверждено read_pool_state) -- amount0=ETH, amount1=USDG
    L = get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, usable_eth, usable_usdg)
    amount0_used, amount1_used = v3_amounts(L, sqrt_p, sqrt_pa, sqrt_pb)
    delta_eth = amount0_used  # ETH-экспозиция позиции = хедж, который нужен

    hedge_price = lighter_price or p0
    hedge_notional_usd = delta_eth * hedge_price
    required_margin_at_3x = hedge_notional_usd / MAX_LEVERAGE
    margin_available = lm.get("collateral_usd", 0) if lm.get("found") else 0

    sufficient = margin_available >= required_margin_at_3x and usable_eth > 0 and usable_usdg > 0
    shortfall_margin = max(0.0, required_margin_at_3x - margin_available)

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pool_state": pool, "pool_price_usd": pool_price,
        "lighter_eth_market": eth_market, "lighter_mark_price_usd": lighter_price,
        "nfpm_discovery": nfpm,
        "wallet_balances": wb, "lighter_margin": lm,
        "gas_reserve_usd": GAS_RESERVE_USD, "gas_reserve_eth": gas_reserve_eth,
        "usable_eth": usable_eth, "usable_usdg": usable_usdg,
        "range_pct": RANGE_PCT, "range_lower_usd": pa, "range_upper_usd": pb,
        "computed_liquidity": L, "computed_amount0_eth": amount0_used, "computed_amount1_usdg": amount1_used,
        "computed_delta_eth": delta_eth, "hedge_notional_usd": hedge_notional_usd,
        "max_leverage": MAX_LEVERAGE, "required_margin_usd_at_max_leverage": required_margin_at_3x,
        "margin_available_usd": margin_available, "shortfall_margin_usd": shortfall_margin,
        "sufficient": sufficient,
        "requests_used": _request_count, "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p5_live_precheck] pool_price=${p0:.4f} usable_eth={usable_eth:.6f} usable_usdg={usable_usdg:.4f}")
    print(f"[p5_live_precheck] диапазон [{pa:.2f}, {pb:.2f}], L={L:.2f}, "
          f"amount0(ETH)={amount0_used:.6f} amount1(USDG)={amount1_used:.4f}")
    print(f"[p5_live_precheck] delta_eth={delta_eth:.6f} hedge_notional=${hedge_notional_usd:.2f} "
          f"требуемая маржа (<={MAX_LEVERAGE}x)=${required_margin_at_3x:.2f} доступно=${margin_available:.4f}")
    print(f"[p5_live_precheck] ДОСТАТОЧНО: {sufficient}" + (f", НЕХВАТКА МАРЖИ: ${shortfall_margin:.4f}" if not sufficient else ""))
    print(f"[p5_live_precheck] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
