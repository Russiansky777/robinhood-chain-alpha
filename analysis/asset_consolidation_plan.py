#!/usr/bin/env python3
"""Консолидация активов в USDG на Robinhood Chain -- ШАГ 2: план с
котировками (владелец, 2026-09-05). ТОЛЬКО чтение/котировки, НИ ОДНОЙ
транзакции не отправляется. Реальные транзакции -- после отдельного
"да" владельца на этот план.

Шаги плана (дословно от владельца):
1. Base: cbBTC -> USDC своп (пул и fee tier -- реальные, читаются
   живьём), затем Across USDC -> USDG на Robinhood -- реальная
   котировка моста на фактическую сумму. Комиссия моста > 10% суммы
   -> шаг помечается "оставить как есть".
2. Robinhood: WETH -> USDG на пуле P5 (0x52e65B17..., тот же путь,
   что весь P5). Нативный ETH: оставить $0.50, обернуть остаток и
   свопнуть тем же путём -- если остаток сверх $0.50 дешевле двух
   транзакций (реальный gas price x реальный расход газа), не трогать.
3. Lighter -- не трогать (нет шага).

Оценка выхода свопа -- ПО СПОТ-ЦЕНЕ пула (GT, тот же снимок, что
dry-run) за вычетом fee tier, БЕЗ полной v3-симуляции проскальзывания
(суммы малы относительно типичного TVL этих пулов -- см. dry-run,
$36 и $149 -- проскальзывание пренебрежимо мало на споте, но реальное
исполнение может отличаться на несколько центов) -- явно помечено как
оценка, не гарантированное количество."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
DRYRUN_PATH = Path("data/p3_guard_cache/asset_consolidation_dryrun_result.json")
OUT_PATH = Path("data/p3_guard_cache/asset_consolidation_plan_result.json")

FEE_SELECTOR = "0xddca3f43"  # fee() -- Uniswap v3 / Aerodrome Slipstream (подтверждено в проекте)
GAS_PRICE_METHOD = "eth_gasPrice"

BASE_RPC = "https://mainnet.base.org"
BASE_CHAIN_ID = 8453
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_CHAIN_ID = 4663

P6_POOL_BASE = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"  # cbBTC/USDC, Base
P5_POOL_ROBINHOOD = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca"  # WETH/USDG, Robinhood Chain

USDC_ADDR = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
CBBTC_ADDR = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
CBBTC_DECIMALS = 8
USDG_ADDR = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
USDG_DECIMALS = 6
WETH_ADDR = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
WETH_DECIMALS = 18

BRIDGE_FEE_PCT_KILL_THRESHOLD = 10.0  # владелец: "если комиссия моста > 10% суммы -- оставить как есть"
ETH_RESERVE_USD = 0.50  # владелец: оставить $0.50 нативного ETH на Robinhood Chain
ASSUMED_GAS_PER_TX = 150_000  # консервативная оценка (wrap + swap) -- нет точного ABI-профиля здесь, честно помечено оценкой


def rpc_call(rpc_url: str, method: str, params: list):
    r = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def read_fee_bps(rpc_url: str, pool_address: str) -> int:
    raw = rpc_call(rpc_url, "eth_call", [{"to": pool_address, "data": FEE_SELECTOR}, "latest"])
    return int(raw, 16)


def across_quote(origin_chain_id: int, dest_chain_id: int, origin_token: str, amount: str,
                  origin_token_symbol: str | None = None) -> dict:
    """Первая попытка (только `token`, без проверки маршрута) реально
    провалилась на направлении Base->Robinhood USDC (run 2026-09-05,
    "Unsupported token address on given destination chain") -- в отличие
    от p6_live_step1.py, где ТОТ ЖЕ паттерн реально работал (но для
    ДРУГОГО направления/токена, Robinhood->Base). Симметрия маршрутов
    Across НЕ гарантирована -- честно проверяем `available-routes`
    ПЕРЕД suggested-fees (тот же паттерн, что p6_dry_run_entry.py::
    across_quote), не предполагаем, что раз в одну сторону сработало,
    сработает и в другую."""
    routes_r = requests.get("https://app.across.to/api/available-routes",
                             params={"originChainId": origin_chain_id, "destinationChainId": dest_chain_id}, timeout=20)
    if routes_r.status_code != 200:
        return {"error": f"available-routes HTTP {routes_r.status_code}", "raw": routes_r.text[:500]}
    routes = routes_r.json()
    match = None
    for x in routes:
        if x.get("originToken", "").lower() == origin_token.lower():
            match = x
            break
    if match is None and origin_token_symbol:
        match = next((x for x in routes if x.get("originTokenSymbol") == origin_token_symbol), None)
    if match is None:
        return {"error": f"маршрут {origin_chain_id}->{dest_chain_id} для токена {origin_token} НЕ найден в available-routes",
                "available_routes_sample": routes[:10]}
    r = requests.get("https://app.across.to/api/suggested-fees", params={
        "originChainId": origin_chain_id, "destinationChainId": dest_chain_id,
        "token": match["originToken"], "amount": amount,
    }, timeout=20)
    if r.status_code != 200:
        print(f"[plan] across_quote НЕ 200 ({r.status_code}): {r.text[:500]}")
        return {"error": f"HTTP {r.status_code}", "raw": r.text[:500], "matched_route": match}
    out = r.json()
    out["_matched_route"] = match
    return out


def run() -> int:
    if not DRYRUN_PATH.exists():
        raise SystemExit("[plan] нет dry-run результата -- сначала asset_consolidation_dryrun.py")
    dry = json.loads(DRYRUN_PATH.read_text())
    eth_usd = dry["prices"]["eth_usd"]
    btc_usd = dry["prices"]["btc_usd_for_cbbtc"]
    print(f"[plan] реальные цены из dry-run: ETH=${eth_usd} BTC(cbBTC)=${btc_usd}")

    base_chain = dry["chains"]["base"]
    rh_chain = dry["chains"]["robinhood"]
    cbbtc_amount = base_chain["tokens"]["cbBTC"]["balance"]
    usdc_amount = base_chain["tokens"]["USDC"]["balance"]
    weth_amount = rh_chain["tokens"]["WETH"]["balance"]
    native_eth_rh = rh_chain["native_eth_balance"]

    plan = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": []}

    # --- Шаг 1: Base cbBTC -> USDC ---
    print("\n=== Шаг 1: Base cbBTC -> USDC ===")
    p6_fee_bps = read_fee_bps(BASE_RPC, P6_POOL_BASE)
    p6_fee_frac = p6_fee_bps / 1_000_000
    cbbtc_usd_value = cbbtc_amount * btc_usd
    expected_usdc_from_swap = cbbtc_usd_value * (1 - p6_fee_frac)  # спот-оценка, USDC~=$1
    step1 = {
        "action": "swap cbBTC -> USDC", "chain": "base", "pool": P6_POOL_BASE, "fee_bps": p6_fee_bps,
        "fee_pct": p6_fee_bps / 100 / 100, "cbbtc_amount": cbbtc_amount, "cbbtc_usd_value": cbbtc_usd_value,
        "expected_usdc_out": expected_usdc_from_swap,
        "note": "оценка по спот-цене пула (GT) за вычетом fee tier, без полной v3-симуляции проскальзывания -- сумма мала относительно TVL пула",
    }
    plan["steps"].append(step1)
    print(f"  P6-пул fee={p6_fee_bps/100:.2f}bp, cbBTC={cbbtc_amount} (~${cbbtc_usd_value:.2f}) -> ожидаемо ~{expected_usdc_from_swap:.4f} USDC")

    total_usdc_before_bridge = usdc_amount + expected_usdc_from_swap
    print(f"  USDC после свопа + уже имеющийся: {usdc_amount:.6f} + {expected_usdc_from_swap:.6f} = {total_usdc_before_bridge:.6f}")

    # --- Шаг 2: Across USDC (Base) -> USDG (Robinhood) ---
    print("\n=== Шаг 2: Across мост USDC (Base) -> Robinhood ===")
    amount_raw = str(int(total_usdc_before_bridge * (10 ** USDC_DECIMALS)))
    quote = across_quote(BASE_CHAIN_ID, ROBINHOOD_CHAIN_ID, USDC_ADDR, amount_raw, origin_token_symbol="USDC")
    step2 = {"action": "bridge USDC Base -> Robinhood (Across)", "amount_usdc": total_usdc_before_bridge, "quote_raw": quote}
    if "error" not in quote:
        total_relay_fee_pct = None
        try:
            total_relay_fee_pct = float(quote.get("totalRelayFee", {}).get("pct", 0)) / 1e18 * 100
        except Exception:
            pass
        step2["total_relay_fee_pct"] = total_relay_fee_pct
        if total_relay_fee_pct is not None:
            kill = total_relay_fee_pct > BRIDGE_FEE_PCT_KILL_THRESHOLD
            step2["decision"] = "ОСТАВИТЬ КАК ЕСТЬ (комиссия > 10%)" if kill else "ИСПОЛНИТЬ"
            print(f"  реальная котировка Across: totalRelayFee={total_relay_fee_pct:.4f}% -> {step2['decision']}")
            if not kill:
                try:
                    output_amount_raw = int(quote.get("outputAmount", 0))
                    step2["expected_usdg_out"] = output_amount_raw / (10 ** USDG_DECIMALS)
                    print(f"  ожидаемо получить на Robinhood: ~{step2['expected_usdg_out']:.6f} USDG (уже в USDG-декодировке, комиссия моста уже вычтена)")
                except Exception as exc:  # noqa: BLE001
                    step2["expected_usdg_out_error"] = str(exc)[:200]
        else:
            step2["decision"] = "НЕ УДАЛОСЬ ОПРЕДЕЛИТЬ КОМИССИЮ -- разобрать вручную"
            print(f"  ВНИМАНИЕ: не удалось извлечь totalRelayFee.pct из ответа -- сырой ответ: {json.dumps(quote)[:500]}")
    else:
        step2["decision"] = f"ОШИБКА КОТИРОВКИ: {quote.get('error')}"
        print(f"  ОШИБКА: {quote.get('error')}")
    plan["steps"].append(step2)

    # --- Шаг 3: Robinhood WETH -> USDG ---
    print("\n=== Шаг 3: Robinhood WETH -> USDG (пул P5) ===")
    p5_fee_bps = read_fee_bps(ROBINHOOD_RPC, P5_POOL_ROBINHOOD)
    p5_fee_frac = p5_fee_bps / 1_000_000
    weth_usd_value = weth_amount * eth_usd
    expected_usdg_from_weth = weth_usd_value * (1 - p5_fee_frac)
    step3 = {
        "action": "swap WETH -> USDG", "chain": "robinhood", "pool": P5_POOL_ROBINHOOD, "fee_bps": p5_fee_bps,
        "fee_pct": p5_fee_bps / 100 / 100, "weth_amount": weth_amount, "weth_usd_value": weth_usd_value,
        "expected_usdg_out": expected_usdg_from_weth,
        "note": "оценка по спот-цене пула (GT) за вычетом fee tier",
    }
    plan["steps"].append(step3)
    print(f"  P5-пул fee={p5_fee_bps/100:.2f}bp, WETH={weth_amount} (~${weth_usd_value:.2f}) -> ожидаемо ~{expected_usdg_from_weth:.4f} USDG")

    # --- Шаг 4: нативный ETH на Robinhood -- оставить $0.50, обернуть остаток ---
    print("\n=== Шаг 4: нативный ETH на Robinhood -- оставить $0.50 ===")
    eth_reserve_amount = ETH_RESERVE_USD / eth_usd
    eth_remainder = native_eth_rh - eth_reserve_amount
    gas_price_wei = int(rpc_call(ROBINHOOD_RPC, GAS_PRICE_METHOD, []), 16)
    cost_two_tx_eth = 2 * ASSUMED_GAS_PER_TX * gas_price_wei / 1e18
    cost_two_tx_usd = cost_two_tx_eth * eth_usd
    step4 = {
        "action": "wrap remainder ETH -> WETH -> swap -> USDG", "native_eth_now": native_eth_rh,
        "reserve_eth_target": eth_reserve_amount, "remainder_eth": eth_remainder,
        "remainder_usd": eth_remainder * eth_usd, "gas_price_gwei": gas_price_wei / 1e9,
        "assumed_gas_per_tx": ASSUMED_GAS_PER_TX, "cost_two_tx_usd_estimate": cost_two_tx_usd,
    }
    if eth_remainder * eth_usd < cost_two_tx_usd:
        step4["decision"] = "НЕ ТРОГАТЬ (остаток дешевле двух транзакций)"
    else:
        step4["decision"] = "ОБЕРНУТЬ И СВОПНУТЬ"
        step4["expected_usdg_out"] = eth_remainder * eth_usd * (1 - p5_fee_frac)
    plan["steps"].append(step4)
    print(f"  остаток сверх $0.50: {eth_remainder:.8f} ETH (~${eth_remainder*eth_usd:.4f}), "
          f"оценка стоимости 2 транзакций: ~${cost_two_tx_usd:.6f} (gas_price={gas_price_wei/1e9:.4f} gwei) -> {step4['decision']}")

    # --- Шаг 5: Lighter -- не трогать ---
    plan["steps"].append({"action": "Lighter", "decision": "НЕ ТРОГАТЬ (владелец)"})

    # --- Итоговая таблица "было -> станет" ---
    print("\n=== Итог: было -> станет (оценка) ===")
    usdg_final = rh_chain["tokens"]["USDG"]["balance"] + step3["expected_usdg_out"]
    if step2.get("decision") == "ИСПОЛНИТЬ" and "expected_usdg_out" in step2:
        usdg_final += step2["expected_usdg_out"]
    if step4.get("decision") == "ОБЕРНУТЬ И СВОПНУТЬ":
        usdg_final += step4["expected_usdg_out"]
    total_cost_usd = 0.0  # издержки -- fee-компонент свопов + мост (round-trip не считаем, это одноразовая консолидация)
    fee_cost_swap1 = cbbtc_usd_value * p6_fee_frac
    fee_cost_swap2 = weth_usd_value * p5_fee_frac
    bridge_fee_usd = (total_usdc_before_bridge * (step2.get("total_relay_fee_pct") or 0) / 100) if step2.get("decision") == "ИСПОЛНИТЬ" else 0.0
    total_cost_usd = fee_cost_swap1 + fee_cost_swap2 + bridge_fee_usd + (cost_two_tx_usd if step4.get("decision") == "ОБЕРНУТЬ И СВОПНУТЬ" else 0.0)
    plan["summary"] = {
        "usdg_now": rh_chain["tokens"]["USDG"]["balance"],
        "usdg_estimated_after": usdg_final,
        "total_estimated_cost_usd": total_cost_usd,
        "total_usd_before": dry["total_usd_approx"],
    }
    print(f"  USDG сейчас: {rh_chain['tokens']['USDG']['balance']:.6f}")
    print(f"  USDG оценочно после консолидации: {usdg_final:.6f}")
    print(f"  Суммарные оценочные издержки: ~${total_cost_usd:.4f} из ~${dry['total_usd_approx']:.2f} общей стоимости активов")
    print("\n[plan] СТОП -- ждём явного 'да' владельца перед любой реальной транзакцией.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
    print(f"[plan] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
