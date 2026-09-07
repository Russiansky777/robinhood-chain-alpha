#!/usr/bin/env python3
"""Скринер пулов, ПЕРЕОТКРЫТИЕ через эмиссию (владелец, 2026-09-07):
"Скринер считал только apyBase и закрыл линию на том, что комиссии ≈
LVR. Но эмиссия — сверху, и её мы не смотрели." Хеджированное LP было
закрыто (docs/PROJECT_STATE.md, "Хеджированное LP... ЗАКРЫТ") на 0/21
по критерию apyBase/LVR≥2 — здесь добавляется apyReward (эмиссия
gauge/кампании), ПРОВЕРЕННАЯ НА ЦЕПИ, а не взятая с доверием у DefiLlama.

Реальные факты этой сессии (WebSearch + прямое чтение официальных
исходников aerodrome-finance/contracts и aerodrome-finance/slipstream,
НЕ по памяти):
  - Voter (Base): 0x16613524e02ad97eDfeF371bC883F2F5d6C480A5,
    `gauges(address pool) returns (address)`.
  - CLGauge (aerodrome-slipstream, Slipstream/CL): `rewardRate()`,
    `rewardToken()` (ВСЕГДА AERO, зашито в конструкторе через Minter),
    `periodFinish()`. `totalSupply()` НЕ СУЩЕСТВУЕТ на CLGauge (стейк —
    NFT-позиции NonfungiblePositionManager, не фунгибельный баланс) —
    вместо этого берём `stakedLiquidity()`/`liquidity()` С САМОГО ПУЛА
    (уже читается в pool_screener_gt_recompute.get_gauge_correction(),
    переиспользуется здесь).
  - Классический Gauge (aerodrome-v1): `rewardRate()`, `periodFinish()`,
    `totalSupply()` (фунгибельный застейканный объём LP-токена) —
    ПРИСУТСТВУЮТ (Synthetix StakingRewards-паттерн), доля застейканного
    объёма = gauge.totalSupply() / pool.totalSupply() (у v1-пула тоже
    ERC-20 LP-токен).
  - AERO (Base): 0x940181a94A35A4569E4529A3CDfB74e38FD98631.
  - Цена AERO — DefiLlama `coins.llama.fi/prices/current/base:<addr>`
    (реальный, бесплатный, без ключа эндпоинт, подтверждено WebSearch).

Формула (владелец, дословно): total = apyBase − LVR + apyReward ×
(1 − дисконт на дневную продажу награды). LVR — та же честная 30-
дневная σ-формула, что уже посчитана в pool_screener_gt_recompute.py
(`lvr_at_real_30d_sigma_frac`), переиспользуется, не пересчитывается.
Дисконт на продажу — ЧЕСТНАЯ, явно обозначенная АППРОКСИМАЦИЯ
constant-product (`price_impact ≈ sell_usd/(reserve_usd+sell_usd)`) на
реальной глубине AERO/квота-пула (GT `reserve_in_usd`), не точная
котировка агрегатора (на Base/Arbitrum/BSC такого готового источника
в проекте пока нет, в отличие от Jupiter на Solana) — метод отдельно
обозначен, число не выдаётся за биржевую котировку.

Aerodrome/Velodrome — ОБЯЗАТЕЛЬНО два варианта раздельно (владелец):
  - НЕЗАСТЕЙКАННАЯ: apyBase_corrected (комиссии, уже gauge-
    скорректированные для CL — pool_screener_gt_recompute.py) − LVR,
    БЕЗ apyReward.
  - ЗАСТЕЙКАННАЯ: apyReward_onchain (эмиссия) − LVR, БЕЗ apyBase (та
    же механика: застейканная ликвидность не получает комиссии — уже
    задокументировано в docs/PROJECT_STATE.md, "механика Aerodrome").

Обязательное условие (владелец): для волатильной ноги есть перп на
Lighter — реальная проверка через `orderBookDetails` (тот же метод,
что funding_pairs_select.py::fetch_lighter_markets()).

Порог: ≥40% на полный капитал при исторической σ, ПЛЮС стресс-тест
"цена награды −50%" (тот же порог, на стрессовом apyReward) — оба
должны пройти, иначе честно "не проходит порог с запасом"."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import pool_screener_gt_recompute as _psg  # noqa: E402 -- rpc_call/_selector/RPC_ENDPOINTS/get_gauge_correction

IN_GT_RECOMPUTE = Path("data/p3_guard_cache/pool_screener_gt_recompute_result.json")
IN_DEFILLAMA = Path("data/p3_guard_cache/pool_screener_defillama_result.json")
IN_CONCENTRATION = Path("data/p3_guard_cache/pool_screener_concentration_result.json")  # источник gt_resolution.resolved_pool_address -- gt_recompute_result.json его НЕ хранит напрямую
OUT_PATH = Path("data/p3_guard_cache/pool_screener_emission_apy_result.json")

VOTER_BASE = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"  # реальный, WebSearch+GitHub-исходник этой сессии
AERO_BASE = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"  # реальный, тот же источник
GT_BASE = "https://api.geckoterminal.com/api/v2"
LIGHTER_API_BASE = "https://mainnet.zklighter.elliot.ai"  # тот же, что funding_pairs_select.py

SECONDS_PER_YEAR = 365 * 24 * 3600
THRESHOLD_PCT = 40.0
REWARD_PRICE_STRESS = 0.5  # -50% цены токена награды

_last_gt_call = 0.0


def _throttle_gt() -> None:
    global _last_gt_call
    wait = _last_gt_call + 2.6 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_gt_call = time.monotonic()


def erc20_decimals(network: str, token_address: str) -> int:
    try:
        raw = _psg.rpc_call(network, token_address, _psg._selector("decimals()"))
        return int(raw, 16)
    except Exception:  # noqa: BLE001
        return 18  # честный дефолт для ERC-20 (AERO подтверждена как обычный ERC-20, но decimals() читаем явно, не считаем 18 по умолчанию без попытки)


def get_gauge_address(network: str, pool_address: str) -> str | None:
    if network != "base":
        return None  # Voter только на Base -- реальные 9 кандидатов с apyReward все на Base (проверено по данным)
    raw = _psg.rpc_call(network, VOTER_BASE, _psg._selector("gauges(address)") + pool_address[2:].rjust(64, "0").lower())
    addr = "0x" + raw[-40:]
    if int(addr, 16) == 0:
        return None
    return addr


def aero_price_usd() -> float | None:
    try:
        r = requests.get(f"https://coins.llama.fi/prices/current/base:{AERO_BASE}", timeout=20)
        r.raise_for_status()
        body = r.json()
        return body.get("coins", {}).get(f"base:{AERO_BASE}", {}).get("price")
    except Exception as exc:  # noqa: BLE001
        print(f"    AERO price error: {exc}")
        return None


def gt_get(url: str, params: dict) -> tuple[int, dict | str]:
    _throttle_gt()
    r = requests.get(url, params=params, headers={"Accept": "application/json;version=20230302"}, timeout=20)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def find_most_liquid_pool_for_token(network: str, token_address: str) -> dict | None:
    status, body = gt_get(f"{GT_BASE}/networks/{network}/tokens/{token_address}/pools", {})
    if status != 200 or not isinstance(body, dict):
        return None
    best = None
    for p in body.get("data", []):
        attrs = p.get("attributes", {})
        reserve = attrs.get("reserve_in_usd")
        if reserve is None:
            continue
        reserve = float(reserve)
        if best is None or reserve > best["reserve_usd"]:
            best = {"pool_address": attrs.get("address"), "reserve_usd": reserve, "name": attrs.get("name")}
    return best


def fetch_lighter_markets() -> set[str]:
    """Реальные символы Lighter (тот же метод, что funding_pairs_select.py::
    fetch_lighter_markets(), не дублируем импортом -- он у нас в другом,
    несвязанном скрипте без экспортируемых констант, копия минимальна)."""
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    r.raise_for_status()
    body = r.json()
    markets = body.get("order_book_details", body.get("markets", body if isinstance(body, list) else []))
    out = set()
    for m in markets:
        sym = m.get("symbol")
        if not sym:
            continue
        s = sym.upper()
        for suffix in ("-PERP", "-USD", "-USDT", "PERP"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        out.add(s.strip("-_"))
    return out


VOLATILE_LEG_ALIASES = {"WETH": "ETH", "WBTC": "BTC", "CBBTC": "BTC", "WBNB": "BNB", "BTCB": "BTC"}
STABLE_LEGS = {"USDC", "USDT", "USDG", "USD1", "DAI", "USDE", "USD"}


def volatile_legs(symbol: str) -> list[str]:
    legs = symbol.split("-")
    return [VOLATILE_LEG_ALIASES.get(leg, leg) for leg in legs if leg not in STABLE_LEGS]


def check_lighter_hedge(symbol: str, lighter_symbols: set[str]) -> dict:
    legs = volatile_legs(symbol)
    return {"volatile_legs": legs, "n_volatile_legs": len(legs),
            "each_leg_has_lighter_perp": {leg: (leg in lighter_symbols) for leg in legs},
            "all_legs_hedgeable": all(leg in lighter_symbols for leg in legs) if legs else None,
            "dual_leg_warning": "оба конца пары волатильны -- полный хедж требует ДВА перпа в нужной пропорции, не один шорт как в P5 (ETH/USDG)" if len(legs) > 1 else None}


def compute_cl_emission(entry: dict, network: str, pool_address: str, gauge_address: str, tvl_usd: float, aero_price: float,
                          existing_gauge_correction: dict | None) -> dict:
    out: dict = {"gauge_address": gauge_address, "reward_token_verified": None}
    try:
        reward_token_raw = _psg.rpc_call(network, gauge_address, _psg._selector("rewardToken()"))
        reward_token = "0x" + reward_token_raw[-40:]
        out["reward_token_onchain"] = reward_token
        out["reward_token_verified"] = reward_token.lower() == AERO_BASE.lower()

        reward_rate_raw = int(_psg.rpc_call(network, gauge_address, _psg._selector("rewardRate()")), 16)
        period_finish = int(_psg.rpc_call(network, gauge_address, _psg._selector("periodFinish()")), 16)
        decimals = erc20_decimals(network, reward_token)
        reward_rate_tokens_per_s = reward_rate_raw / (10 ** decimals)
        out["reward_rate_raw"] = reward_rate_raw
        out["reward_rate_tokens_per_s"] = reward_rate_tokens_per_s
        out["period_finish_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(period_finish))
        out["campaign_active_now"] = period_finish > time.time()
        if not out["campaign_active_now"]:
            out["warning"] = "periodFinish в прошлом -- реальная текущая эмиссия скорее всего 0, даже если rewardRate хранит старое ненулевое значение (Synthetix-паттерн: не считаем валидной после periodFinish)"

        # staked/total liquidity УЖЕ реально прочитаны живьём в этом же
        # прогоне для fee-стороны (pool_screener_gt_recompute.py::
        # get_gauge_correction) -- переиспользуем, не дублируем eth_call.
        if existing_gauge_correction and existing_gauge_correction.get("applicable") and not existing_gauge_correction.get("error"):
            staked_liq_raw = existing_gauge_correction["staked_liquidity_raw"]
            total_liq_raw = existing_gauge_correction["total_liquidity_raw"]
            staked_share = 1 - existing_gauge_correction["unstaked_share"]
            out["reused_from_gt_recompute"] = True
        else:
            total_liq_raw = int(_psg.rpc_call(network, pool_address, _psg._selector("liquidity()")), 16)
            staked_liq_raw = int(_psg.rpc_call(network, pool_address, _psg._selector("stakedLiquidity()")), 16)
            staked_share = (staked_liq_raw / total_liq_raw) if total_liq_raw else None
            out["reused_from_gt_recompute"] = False
        out["staked_liquidity_raw"] = staked_liq_raw
        out["total_liquidity_raw"] = total_liq_raw
        out["staked_share"] = staked_share

        if staked_share is not None and staked_share > 0 and out["campaign_active_now"] and aero_price is not None:
            staked_tvl_usd = tvl_usd * staked_share
            annual_reward_usd = reward_rate_tokens_per_s * SECONDS_PER_YEAR * aero_price
            apy_reward_onchain_pct = annual_reward_usd / staked_tvl_usd * 100
            out["staked_tvl_usd"] = staked_tvl_usd
            out["annual_reward_usd"] = annual_reward_usd
            out["apy_reward_onchain_pct"] = apy_reward_onchain_pct
        else:
            out["error"] = "staked_share=0/None, кампания неактивна или нет цены AERO -- apyReward_onchain не считается"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"eth_call упал: {str(exc)[:400]}"
    return out


def compute_v1_emission(network: str, pool_address: str, gauge_address: str, tvl_usd: float, aero_price: float) -> dict:
    out: dict = {"gauge_address": gauge_address}
    try:
        reward_rate_raw = int(_psg.rpc_call(network, gauge_address, _psg._selector("rewardRate()")), 16)
        period_finish = int(_psg.rpc_call(network, gauge_address, _psg._selector("periodFinish()")), 16)
        gauge_supply_raw = int(_psg.rpc_call(network, gauge_address, _psg._selector("totalSupply()")), 16)
        pool_supply_raw = int(_psg.rpc_call(network, pool_address, _psg._selector("totalSupply()")), 16)
        decimals = erc20_decimals(network, AERO_BASE)  # rewardToken() на v1 Gauge -- internal, не публичный геттер (реально подтверждено) -- используем AERO напрямую (тот же токен, подтверждено исходником для обоих типов gauge)
        reward_rate_tokens_per_s = reward_rate_raw / (10 ** decimals)
        staked_share = (gauge_supply_raw / pool_supply_raw) if pool_supply_raw else None
        out.update({
            "reward_rate_raw": reward_rate_raw, "reward_rate_tokens_per_s": reward_rate_tokens_per_s,
            "period_finish_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(period_finish)),
            "campaign_active_now": period_finish > time.time(),
            "gauge_total_supply_raw": gauge_supply_raw, "pool_total_supply_raw": pool_supply_raw,
            "staked_share": staked_share,
            "note_reward_token": "rewardToken() на классическом Gauge -- internal (не публичный геттер), используем AERO напрямую -- подтверждено исходником Gauge.sol (тот же IVotingEscrow(_ve).token())",
        })
        if staked_share and out["campaign_active_now"] and aero_price is not None:
            staked_tvl_usd = tvl_usd * staked_share
            annual_reward_usd = reward_rate_tokens_per_s * SECONDS_PER_YEAR * aero_price
            out["staked_tvl_usd"] = staked_tvl_usd
            out["annual_reward_usd"] = annual_reward_usd
            out["apy_reward_onchain_pct"] = annual_reward_usd / staked_tvl_usd * 100
        else:
            out["error"] = "staked_share=0/None, кампания неактивна или нет цены AERO"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"eth_call упал: {str(exc)[:400]}"
    return out


def run() -> int:
    gt_recompute = json.loads(IN_GT_RECOMPUTE.read_text())
    defillama = json.loads(IN_DEFILLAMA.read_text())
    concentration = json.loads(IN_CONCENTRATION.read_text())
    apy_reward_by_pool_id = {c["pool_id"]: c.get("apyReward") for c in defillama["candidates"]}
    gt_resolution_by_pool_id = {c["pool_id"]: c.get("gt_resolution") for c in concentration["candidates"]}

    print("=== Получение реальной цены AERO (DefiLlama coins API) ===")
    aero_price = aero_price_usd()
    print(f"AERO price (реальная, сейчас): ${aero_price}")

    print("\n=== Реальные символы Lighter (для проверки хеджа) ===")
    lighter_symbols = fetch_lighter_markets()
    print(f"реальных символов на Lighter: {len(lighter_symbols)}")

    print("\n=== Пул AERO для дисконта на продажу (GT, самый ликвидный) ===")
    aero_pool = find_most_liquid_pool_for_token("base", AERO_BASE)
    print(f"AERO самый ликвидный пул (GT): {aero_pool}")

    results = []
    for c in gt_recompute["candidates"]:
        apy_reward_defillama = apy_reward_by_pool_id.get(c["pool_id"])
        if not apy_reward_defillama or c.get("error"):
            continue  # честно пропускаем -- нет эмиссии по DefiLlama или уже была ошибка в предыдущем шаге
        if c["project"] not in ("aerodrome-slipstream", "aerodrome-v1"):
            continue  # только gauge-форки -- у остальных 21 (uniswap-v3/v4) эмиссии нет по конструкции протокола

        entry = {"chain": c["chain"], "project": c["project"], "symbol": c["symbol"], "pool_id": c["pool_id"],
                  "apy_reward_defillama_pct": apy_reward_defillama, "tvl_usd": c["tvl_usd"]}
        print(f"\n=== {c['chain']} {c['project']} {c['symbol']} (apyReward DefiLlama={apy_reward_defillama:.2f}%) ===")

        gt_res = gt_resolution_by_pool_id.get(c["pool_id"]) or {}
        network = gt_res.get("network")
        pool_address = gt_res.get("resolved_pool_address")
        if not network or not pool_address:
            entry["error"] = "нет адреса пула в исходных данных (concentration-результат) -- пропущено"
            results.append(entry)
            continue

        gauge_address = get_gauge_address(network, pool_address)
        entry["gauge_address_from_voter"] = gauge_address
        if gauge_address is None:
            entry["error"] = "Voter.gauges(pool) вернул нулевой адрес -- реально НЕТ активного gauge для этого пула (apyReward DefiLlama, если есть, не подтверждён этим методом)"
            print(f"    {entry['error']}")
            results.append(entry)
            continue

        if c["project"] == "aerodrome-slipstream":
            emission = compute_cl_emission(entry, network, pool_address, gauge_address, c["tvl_usd"], aero_price, c.get("gauge_correction"))
        else:
            emission = compute_v1_emission(network, pool_address, gauge_address, c["tvl_usd"], aero_price)
        entry["emission_onchain"] = emission
        print(f"    apyReward on-chain: {emission.get('apy_reward_onchain_pct')}, staked_share={emission.get('staked_share')}, error={emission.get('error')}")

        apy_reward_onchain = emission.get("apy_reward_onchain_pct")
        if apy_reward_onchain is not None and apy_reward_defillama:
            entry["onchain_vs_defillama_ratio"] = apy_reward_onchain / apy_reward_defillama

        # Дисконт на дневную продажу -- constant-product аппроксимация на реальной глубине AERO-пула
        if apy_reward_onchain is not None and aero_pool is not None and emission.get("staked_tvl_usd"):
            daily_reward_usd = emission["staked_tvl_usd"] * (apy_reward_onchain / 100) / 365
            discount_frac = daily_reward_usd / (aero_pool["reserve_usd"] + daily_reward_usd)
            entry["daily_reward_usd"] = daily_reward_usd
            entry["sell_discount_frac_approx"] = discount_frac
            entry["sell_discount_method"] = "constant-product аппроксимация: daily_reward_usd/(AERO_pool_reserve_usd+daily_reward_usd), НЕ котировка агрегатора"
            apy_reward_after_discount = apy_reward_onchain * (1 - discount_frac)
        else:
            apy_reward_after_discount = None
        entry["apy_reward_after_discount_pct"] = apy_reward_after_discount

        lvr_frac = c.get("lvr_at_real_30d_sigma_frac")
        apy_base_corrected = c.get("fee_apr_gt_30d_corrected")
        entry["lvr_annualized_frac"] = lvr_frac
        entry["apy_base_corrected_pct"] = (apy_base_corrected * 100) if apy_base_corrected is not None else None

        if lvr_frac is not None:
            lvr_pct = lvr_frac * 100
            if apy_base_corrected is not None:
                entry["total_unstaked_pct"] = apy_base_corrected * 100 - lvr_pct
            if apy_reward_after_discount is not None:
                entry["total_staked_pct"] = apy_reward_after_discount - lvr_pct
                stressed_reward = apy_reward_onchain * REWARD_PRICE_STRESS * (1 - (entry.get("sell_discount_frac_approx") or 0))
                entry["total_staked_stressed_50pct_pct"] = stressed_reward - lvr_pct

        entry["passes_40pct_unstaked"] = entry.get("total_unstaked_pct") is not None and entry["total_unstaked_pct"] >= THRESHOLD_PCT
        entry["passes_40pct_staked"] = entry.get("total_staked_pct") is not None and entry["total_staked_pct"] >= THRESHOLD_PCT
        entry["passes_40pct_staked_stressed"] = entry.get("total_staked_stressed_50pct_pct") is not None and entry["total_staked_stressed_50pct_pct"] >= THRESHOLD_PCT
        entry["passes_with_stress_margin"] = bool(entry["passes_40pct_staked"] and entry["passes_40pct_staked_stressed"])

        entry["lighter_hedge"] = check_lighter_hedge(c["symbol"], lighter_symbols)
        entry["capacity_pool_tvl_usd"] = c["tvl_usd"]

        results.append(entry)
        print(f"    total_unstaked={entry.get('total_unstaked_pct')}, total_staked={entry.get('total_staked_pct')}, "
              f"staked_stressed={entry.get('total_staked_stressed_50pct_pct')}, passes_with_margin={entry['passes_with_stress_margin']}")

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "aero_price_usd": aero_price, "aero_reference_pool": aero_pool,
        "threshold_pct": THRESHOLD_PCT, "reward_price_stress": REWARD_PRICE_STRESS,
        "n_candidates_with_emission": len(results),
        "n_pass_with_stress_margin": sum(1 for r in results if r.get("passes_with_stress_margin")),
        "candidates": results,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n=== ИТОГО: {len(results)} кандидатов с эмиссией, {out['n_pass_with_stress_margin']} проходят ≥40% с запасом на стресс ===")
    print(f"[emission_apy] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
