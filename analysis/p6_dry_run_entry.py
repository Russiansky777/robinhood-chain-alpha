#!/usr/bin/env python3
"""P6, шаг 2 (владелец, 2026-09-04): dry-run входа, НИЧЕГО НЕ ОТПРАВЛЯЯ.
Все числа -- ПРОЧИТАНЫ реально (RPC/API), не предположены. Прямой урок
`canceled-margin-not-allowed` (P5, docs/PROJECT_STATE.md §4 п.1) --
плечо/маржа ВСЕГДА читаются с аккаунта, не зашиваются константой.

Переиспользует БЕЗ переизобретения:
  - get_liquidity_for_amounts/v3_amounts -- дословно Uniswap v3-periphery
    LiquidityAmounts.sol (analysis/p5_live_precheck.py, уже провалидировано).
  - Формулу проекции цены ликвидации через (collateral + size*p0)/(size*(1+mmf))
    -- та же формула УЖЕ СВЕРЕНА с реальным полем Lighter `liquidation_price`
    для ETH-хеджа P5 (mmf_formula_check, analysis/p5_live_position_snapshot.py) --
    здесь применяется как ПРОЕКЦИЯ (позиции ещё нет), не переизобретается.
  - real_eth_leverage()-паттерн (читает initial_margin_fraction С АККАУНТА,
    не константой) -- для BTC вместо ETH.

ОБНОВЛЕНО, 2026-09-04 (владелец, ПОСЛЕ реального закрытия P5): P5
закрыт по-настоящему (ETH-шорт flatten + decreaseLiquidity/collect на
1000756, см. RESULTS.md) -- аккаунт 22012 сейчас реально БЕЗ открытых
позиций, весь collateral свободен. Новое правило размера: свободная
маржа на Lighter ПОСЛЕ открытия BTC-шорта >= 40% от collateral. Если
целевой LP-капитал ($250 по умолчанию) требует маржи больше допустимой
-- УМЕНЬШАЕТСЯ LP (TARGET_TOTAL_CAPITAL_USD), а не маржа/плечо
(владелец, дословно: "если на LP-ногу остаётся меньше $250, уменьшить
LP, не маржу"). Целевой капитал читается из env P6_TARGET_CAPITAL_USD
(default 250.0) -- позволяет пересчитать под правило без редактирования
константы вручную на каждой итерации."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

BASE_RPC = "https://mainnet.base.org"
RPC_MIN_INTERVAL_S = 1.5
RPC_RETRY_BACKOFF_S = 15.0
RPC_MAX_RETRIES = 3

POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"  # USDC-CBBTC, Base, реальный (RPC-подтверждённый)
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"  # тот же хост, что P5
LIGHTER_ACCOUNT_INDEX = 22012  # тот же аккаунт, что P5 (P5 реально закрыт -- см. докстринг)

RANGE_PCT = 0.10  # владелец: диапазон +-10%, тот же, что P5
TARGET_TOTAL_CAPITAL_USD = float(os.environ.get("P6_TARGET_CAPITAL_USD") or "250.0")
MIN_FREE_MARGIN_PCT = 40.0  # владелец, 2026-09-04: свободная маржа после открытия шорта >= 40% от collateral

SELECTORS = {
    "slot0": "0x3850c7bd", "liquidity": "0x1a686502", "tickSpacing": "0xd0c93a7c",
    "decimals": "0x313ce567", "token0": "0x0dfe1681", "token1": "0xd21220a7",
}
def _topic0(signature: str) -> str:
    """Реально ВЫЧИСЛЕННЫЙ keccak256 сигнатуры события -- та же функция,
    что analysis/alchemy_fallback.py::topic0, не переписанное магическое
    число из памяти."""
    k = keccak.new(digest_bits=256)
    k.update(signature.encode())
    return "0x" + k.hexdigest()


MINT_TOPIC0 = _topic0("Mint(address,address,int24,int24,uint128,uint256,uint256)")

_last_rpc_call = 0.0


def _throttle() -> None:
    global _last_rpc_call
    wait = _last_rpc_call + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_rpc_call = time.monotonic()


def rpc(method: str, params: list) -> dict:
    for attempt in range(RPC_MAX_RETRIES + 1):
        _throttle()
        r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        if r.status_code == 429 and attempt < RPC_MAX_RETRIES:
            print(f"    RPC 429, жду {RPC_RETRY_BACKOFF_S:.0f}с")
            time.sleep(RPC_RETRY_BACKOFF_S)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method} {params}: {body['error']}")
        return body["result"]
    raise RuntimeError(f"RPC 429 после {RPC_MAX_RETRIES + 1} попыток")


def eth_call(to: str, selector: str) -> str:
    return rpc("eth_call", [{"to": to, "data": selector}, "latest"])


def read_pool_state() -> dict:
    slot0 = eth_call(POOL_ADDRESS, SELECTORS["slot0"])
    liquidity = int(eth_call(POOL_ADDRESS, SELECTORS["liquidity"]), 16)
    tick_spacing_raw = int(eth_call(POOL_ADDRESS, SELECTORS["tickSpacing"]), 16)
    tick_spacing = tick_spacing_raw - (1 << 256) if tick_spacing_raw >= (1 << 255) else tick_spacing_raw
    hexdata = slot0[2:]
    sqrt_price_x96 = int(hexdata[0:64], 16)
    tick_word = int(hexdata[64:128], 16)
    tick = tick_word - (1 << 256) if tick_word >= (1 << 255) else tick_word
    return {"sqrtPriceX96": sqrt_price_x96, "tick": tick, "liquidity_raw": liquidity, "tick_spacing": tick_spacing}


def get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, amount0, amount1) -> float:
    """Дословно Uniswap v3-periphery LiquidityAmounts.sol (уже провалидировано
    в analysis/p5_live_precheck.py -- не переизобретается)."""
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


def find_recent_mint_tx() -> dict:
    """Реальная недавняя Mint-транзакция на ЭТОМ пуле -- для реальной
    оценки gasUsed (не общеизвестная оценка "обычно 300-400k газа").

    ИСПРАВЛЕНО (реальная ошибка первого прогона): mainnet.base.org
    реально вернул 413 "Payload Too Large" на eth_getLogs с окном
    200_000 блоков -- публичный RPC ограничивает диапазон одного
    вызова (не задокументировано явно, найдено по факту). Чанкинг
    назад от latest небольшими окнами, с ранним выходом при первой
    находке -- не гадаем лимит заранее, ловим ошибку и сокращаем."""
    latest = int(rpc("eth_blockNumber", []), 16)
    chunk_size = 10_000
    max_chunks = 20  # 20x10_000 = 200_000 блоков ~ 4.6 дня, тот же общий охват
    to_block = latest
    for _ in range(max_chunks):
        from_block = max(1, to_block - chunk_size)
        try:
            logs = rpc("eth_getLogs", [{
                "address": POOL_ADDRESS, "topics": [MINT_TOPIC0],
                "fromBlock": hex(from_block), "toBlock": hex(to_block),
            }])
        except Exception as exc:  # noqa: BLE001
            print(f"    eth_getLogs [{from_block}, {to_block}] упал: {exc}")
            to_block = from_block - 1
            continue
        if logs:
            tx_hash = logs[-1]["transactionHash"]
            receipt = rpc("eth_getTransactionReceipt", [tx_hash])
            return {"found": True, "tx_hash": tx_hash, "gas_used": int(receipt["gasUsed"], 16),
                    "n_mints_in_chunk": len(logs), "chunk_range": [from_block, to_block]}
        to_block = from_block - 1
        if from_block <= 1:
            break
    return {"found": False, "window_blocks": chunk_size * max_chunks}


def lighter_account_full() -> dict | None:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/account",
                      params={"by": "index", "value": str(LIGHTER_ACCOUNT_INDEX)}, timeout=20)
    r.raise_for_status()
    accounts = r.json().get("accounts", [])
    return accounts[0] if accounts else None


def real_leverage(account_full: dict, market_symbol: str) -> dict:
    pos = next((p for p in account_full.get("positions", [])
                if str(p.get("symbol", "")).upper() == market_symbol.upper()), None)
    if pos is None:
        return {"found": False}
    imf_pct = float(pos["initial_margin_fraction"])
    return {"found": True, "initial_margin_fraction_pct": imf_pct,
            "leverage": 100.0 / imf_pct if imf_pct else None}


def lighter_btc_market() -> dict | None:
    r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    r.raise_for_status()
    markets = r.json().get("order_book_details", [])
    exact = [m for m in markets if str(m.get("symbol", "")).upper() == "BTC"]
    return exact[0] if exact else None


def eth_price_usd_coingecko() -> float | None:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "ethereum", "vs_currencies": "usd"},
                          headers={"User-Agent": "robinhood-chain-alpha-p6/1.0"}, timeout=20)
        r.raise_for_status()
        return float(r.json()["ethereum"]["usd"])
    except Exception as exc:  # noqa: BLE001
        print(f"    ETH/USD CoinGecko упал: {exc}")
        return None


def across_quote(origin_chain_id: int, amount_wei: str) -> dict:
    result = {}
    try:
        r = requests.get("https://app.across.to/api/available-routes",
                          params={"originChainId": origin_chain_id, "destinationChainId": 8453}, timeout=20)
        routes = r.json() if r.status_code == 200 else None
        usdg_route = next((x for x in (routes or []) if x.get("originTokenSymbol") == "USDG"), None)
        if not usdg_route:
            result["error"] = "маршрут USDG->Base не найден в available-routes"
            return result
        r2 = requests.get("https://app.across.to/api/suggested-fees", params={
            "originChainId": origin_chain_id, "destinationChainId": 8453,
            "token": usdg_route["originToken"], "amount": amount_wei,
        }, timeout=20)
        result = r2.json() if r2.status_code == 200 else {"error": r2.text[:500]}
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:300]
    return result


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "note": "DRY-RUN, ничего не отправлено"}

    print("=== 1. Реальные decimals (RPC, не предположены) ===")
    dec_usdc = int(eth_call(USDC, SELECTORS["decimals"]), 16)
    dec_cbbtc = int(eth_call(CBBTC, SELECTORS["decimals"]), 16)
    print(f"[p6_dry_run] USDC decimals={dec_usdc}, cbBTC decimals={dec_cbbtc}")

    print("\n=== 2. Реальное состояние пула (fresh RPC) ===")
    pool = read_pool_state()
    price_raw = (pool["sqrtPriceX96"] / (2 ** 96)) ** 2
    # price_human_token1_per_token0 = raw * 10^(dec0-dec1) -- ТА ЖЕ формула,
    # что price_from_sqrt в analysis/p5_live_precheck.py (проверено там на
    # реальном пуле P5). Для НАШЕГО пула token0=USDC, token1=cbBTC -- это
    # даёт "cbBTC на 1 USDC" (крошечное число), НЕ цену cbBTC в USD --
    # порядок токенов здесь ОБРАТНЫЙ относительно P5 (там token1 был
    # стейблом, здесь token0). Инвертируем явно, не полагаясь на угадывание.
    price_cbbtc_per_usdc_human = price_raw * (10 ** (dec_usdc - dec_cbbtc))
    price_cbbtc_usd = 1 / price_cbbtc_per_usdc_human  # USDC~=$1
    print(f"[p6_dry_run] tick={pool['tick']} tickSpacing={pool['tick_spacing']} "
          f"liquidity_raw={pool['liquidity_raw']} price_cbbtc_per_usdc_human={price_cbbtc_per_usdc_human}")
    result["pool_state"] = {**pool, "price_cbbtc_usd_estimate": price_cbbtc_usd}
    print(f"[p6_dry_run] cbBTC price = ${price_cbbtc_usd:,.2f} (сверить на правдоподобие вручную -- реальная цена BTC на дату прогона)")

    print("\n=== 3. Диапазон +-10%, суммы токенов для LP-ноги ===")
    p0 = price_cbbtc_usd
    pa_usd, pb_usd = p0 * (1 - RANGE_PCT), p0 * (1 + RANGE_PCT)

    # РАБОТАЕМ ЦЕЛИКОМ В ЧЕЛОВЕЧЕСКИХ ЦЕНАХ -- тот же паттерн, что
    # analysis/p5_live_precheck.py::run() (sqrt_p=p0**0.5 напрямую от
    # человеческой цены, БЕЗ повторного домножения на decimals -- эта
    # декимal-поправка уже целиком учтена при получении price_cbbtc_usd
    # выше). "price" для get_liquidity_for_amounts/v3_amounts здесь --
    # ОБРАТНАЯ величина (cbBTC на 1 USDC = 1/price_cbbtc_usd), т.к. в этом
    # пуле token0=USDC (не волатильный актив, как в P5, а стейбл) --
    # диапазон ±10% в USD-цене cbBTC даёт ОБРАТНЫЙ (переставленный) диапазон
    # в этой шкале: большая USD-цена -> МЕНЬШЕЕ cbBTC-на-USDC значение.
    def usd_to_domain(p_usd: float) -> float:
        return 1.0 / p_usd  # cbBTC per USDC (человеческое), USDC~=$1

    sqrt_p = usd_to_domain(p0) ** 0.5
    # pb_usd (большая цена) -> МЕНЬШИЙ domain -> это НИЖНЯЯ граница sqrt;
    # get_liquidity_for_amounts сама сортирует sqrt_pa/sqrt_pb, но считаем
    # правильно с самого начала, не полагаясь только на авто-сортировку.
    sqrt_pa, sqrt_pb = usd_to_domain(pb_usd) ** 0.5, usd_to_domain(pa_usd) ** 0.5

    # LP-капитал -- $250 полностью в LP-ногу для оценки требуемых сумм
    # (отдельно ниже -- маржа под BTC-шорт, владелец решит реальный сплит).
    lp_capital_usd = TARGET_TOTAL_CAPITAL_USD
    amount0_target_human = (lp_capital_usd / 2) / 1.0  # USDC ~= $1
    amount1_target_human = (lp_capital_usd / 2) / p0  # cbBTC

    L = get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, amount0_target_human, amount1_target_human)
    amount0_used, amount1_used = v3_amounts(L, sqrt_p, sqrt_pa, sqrt_pb)
    print(f"[p6_dry_run] диапазон цены: ${pa_usd:,.2f} .. ${pb_usd:,.2f}")
    print(f"[p6_dry_run] реальные суммы для ~${lp_capital_usd} LP: USDC={amount0_used:.4f}, cbBTC={amount1_used:.8f}")

    # Тик-диапазон -- владелец прямо просил (не только цены). raw_price(p_usd)
    # = (1/p_usd) * 10^(dec_cbbtc-dec_usdc) (та же decimals-инверсия, что
    # price_cbbtc_usd выше, в обратную сторону) -- tick = floor(ln(raw)/ln(1.0001)),
    # выровнен на tick_spacing реально прочитанный из контракта (не 60 по
    # умолчанию для 0.3% -- этот пул fee=0.033%, tickSpacing может быть
    # нестандартным для Aerodrome Slipstream, читается реально в read_pool_state()).
    def usd_price_to_tick(p_usd: float) -> int:
        raw = (1 / p_usd) * (10 ** (dec_cbbtc - dec_usdc))
        return math.floor(math.log(raw) / math.log(1.0001))

    ts = pool["tick_spacing"]
    tick_at_pb = usd_price_to_tick(pb_usd)  # выше USD-цена -> ниже raw -> ниже tick
    tick_at_pa = usd_price_to_tick(pa_usd)
    tick_lower = math.floor(tick_at_pb / ts) * ts
    tick_upper = math.ceil(tick_at_pa / ts) * ts
    print(f"[p6_dry_run] tick_lower={tick_lower}, tick_upper={tick_upper} (tickSpacing={ts}, текущий tick={pool['tick']})")

    result["lp_leg"] = {
        "target_capital_usd": lp_capital_usd, "price_range_usd": [pa_usd, pb_usd],
        "tick_lower": tick_lower, "tick_upper": tick_upper, "current_tick": pool["tick"], "tick_spacing": ts,
        "usdc_amount": amount0_used, "cbbtc_amount": amount1_used,
        "cbbtc_notional_usd": amount1_used * p0,
    }

    print("\n=== 4. Lighter -- реальный BTC-рынок + реальное текущее плечо аккаунта ===")
    btc_market = lighter_btc_market()
    account_full = lighter_account_full()
    if account_full is None:
        result["abort_reason"] = "не удалось прочитать аккаунт Lighter -- СТОП"
        Path("data/p3_guard_cache/p6_dry_run_entry_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1
    btc_leverage = real_leverage(account_full, "BTC")
    print(f"[p6_dry_run] BTC market mark_price={btc_market.get('mark_price') if btc_market else None}")
    print(f"[p6_dry_run] РЕАЛЬНОЕ текущее плечо BTC на аккаунте {LIGHTER_ACCOUNT_INDEX}: {btc_leverage}")

    # НАЙДЕНО (2026-09-04, реальный прогон): аккаунт 22012 НИКОГДА не
    # торговал BTC -- в отличие от ETH (см. p5_live_precheck.py), positions[]
    # не содержит даже нулевой BTC-записи, значит real_leverage() честно не
    # находит плечо. Аутентифицированные /api/v1/accountLimits и
    # /api/v1/accountMetadata (единственные кандидаты на "настройку плеча
    # без истории позиции") реально проверялись 2026-09-0X
    # (data/p3_guard_cache/lighter_order_history_probe_result.json) --
    # ОБА требуют подписи ключом (400 "auth query param... empty"), что
    # выходит за рамки "дешёвого шага только на чтение" (владелец,
    # docs/P6_HEDGED_LP.md). Фолбэк -- биржевой default_initial_margin_fraction
    # рынка BTC, ЯВНО помечен как НЕподтверждённый под этот аккаунт (не
    # молчаливое допущение, как было в canceled-margin-not-allowed -- там
    # ошибка была в том, что допущение НЕ было помечено и не сверялось).
    btc_leverage_is_confirmed_account_setting = bool(btc_leverage.get("found") and btc_leverage.get("leverage"))
    # Владелец, 2026-09-04: "Плечо BTC не брать по дефолту, а выставить
    # аутентифицированным вызовом (не ордер) и перечитать." -- реальный
    # update_leverage уже отправлен отдельным шагом (analysis/p6_set_btc_leverage.py,
    # ЕДИНСТВЕННАЯ мутирующая транзакция во всей цепочке P6-dry-run, не
    # ордер, не позиция). Если аккаунт до сих пор не подтверждает плечо
    # BTC -- честный стоп, БЕЗ фолбэка на биржевой дефолт (тот путь был
    # оставлен только как история в data/p3_guard_cache/p6_dry_run_entry_result.json
    # прошлых прогонов, здесь сознательно убран).
    if not btc_leverage_is_confirmed_account_setting:
        result["abort_reason"] = ("реальное плечо BTC НЕ подтверждено настройкой аккаунта 22012 -- "
                                   "запустите analysis/p6_set_btc_leverage.py (update_leverage) первым, "
                                   "фолбэк на биржевой дефолт отключён по прямому указанию владельца.")
        result["btc_leverage_probe"] = btc_leverage
        Path("data/p3_guard_cache/p6_dry_run_entry_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[p6_dry_run] СТОП: {result['abort_reason']}")
        return 1
    print(f"[p6_dry_run] РЕАЛЬНОЕ подтверждённое плечо BTC (аккаунт, не дефолт): {btc_leverage}")

    leverage_val = btc_leverage["leverage"]
    short_notional_usd = amount1_used * p0  # хедж = cbBTC-экспозиция LP
    required_margin_usd = short_notional_usd / leverage_val
    collateral_now = float(account_full.get("collateral", 0))
    available_now = float(account_full.get("available_balance", 0))
    cross_initial_margin_requirement_now = float(account_full.get("cross_initial_margin_requirement", 0) or 0)

    print(f"[p6_dry_run] требуемая маржа под шорт BTC notional=${short_notional_usd:.2f} "
          f"при плече {leverage_val}x = ${required_margin_usd:.2f}")
    print(f"[p6_dry_run] РЕАЛЬНОЕ текущее состояние аккаунта: collateral=${collateral_now:.2f}, "
          f"available_balance=${available_now:.2f}, cross_initial_margin_requirement(текущая, от ETH P5)=${cross_initial_margin_requirement_now:.2f}")

    # П5 РЕАЛЬНО закрыт (RESULTS.md, 2026-09-04) -- аккаунт сейчас реально
    # без открытых позиций (проверяется явно ниже, не предполагается),
    # поэтому формула ликвидации для BTC-шорта здесь -- уже НЕ изолированная
    # условность "как если бы", а точная формула для факта "одна позиция
    # на аккаунте" (P_liq = (equity + size*entry)/(size*(1+mmf)), та же,
    # что уже сверена с реальным Lighter `liquidation_price` для ETH,
    # p5_live_position_snapshot.py, mmf_formula_check). entry = p0 (текущая
    # цена, т.к. позиции ещё нет, реального avg_entry_price не существует).
    other_open_positions = [p for p in account_full.get("positions", [])
                             if str(p.get("symbol", "")).upper() != "BTC" and abs(float(p.get("position", 0))) > 1e-9]
    mmf_raw = btc_market.get("maintenance_margin_fraction") if btc_market else None
    size_btc = amount1_used
    liq_price_single_position = None
    if mmf_raw is not None and size_btc and not other_open_positions:
        mmf = float(mmf_raw) / 10000
        liq_price_single_position = (collateral_now + size_btc * p0) / (size_btc * (1 + mmf))

    free_margin_usd = collateral_now - required_margin_usd
    free_margin_pct = (free_margin_usd / collateral_now * 100) if collateral_now else None
    margin_rule_satisfied = (free_margin_pct is not None and free_margin_pct >= MIN_FREE_MARGIN_PCT)

    result["hedge"] = {
        "btc_leverage_used": btc_leverage,
        "btc_leverage_confirmed_account_setting": btc_leverage_is_confirmed_account_setting,
        "short_notional_usd": short_notional_usd,
        "required_margin_usd": required_margin_usd,
        "account_collateral_now_usd": collateral_now, "account_available_balance_now_usd": available_now,
        "account_cross_initial_margin_requirement_now_usd": cross_initial_margin_requirement_now,
        "other_open_positions_besides_btc": other_open_positions,
        "free_margin_usd": free_margin_usd, "free_margin_pct": free_margin_pct,
        "min_free_margin_pct_rule": MIN_FREE_MARGIN_PCT, "margin_rule_satisfied": margin_rule_satisfied,
        "liquidation_price_single_position_usd": liq_price_single_position,
        "note": ("P5 реально закрыт -- на аккаунте нет других позиций (проверено по факту), поэтому "
                 "это точная формула для 'одна позиция на аккаунте', не проекция 'как если бы'."
                 if not other_open_positions else
                 "На аккаунте ЕСТЬ другие открытые позиции кроме BTC -- формула единственной позиции не применяется, ликвидация не считается."),
    }
    print(f"[p6_dry_run] требуемая маржа=${required_margin_usd:.2f}, collateral=${collateral_now:.2f}, "
          f"свободная маржа=${free_margin_usd:.2f} ({free_margin_pct:.1f}%), "
          f"правило >={MIN_FREE_MARGIN_PCT:.0f}% -- {'ВЫПОЛНЕНО' if margin_rule_satisfied else 'НЕ выполнено'}, "
          f"ликвидация(единств. позиция)=${liq_price_single_position}")

    print("\n=== 5. Реальный газ (последняя Mint-транзакция на этом пуле) ===")
    mint_tx = find_recent_mint_tx()
    gas_price_wei = int(rpc("eth_gasPrice", []), 16)
    eth_usd = eth_price_usd_coingecko()
    gas_cost_usd = None
    if mint_tx.get("found") and eth_usd:
        gas_cost_eth = mint_tx["gas_used"] * gas_price_wei / 1e18
        gas_cost_usd = gas_cost_eth * eth_usd
    print(f"[p6_dry_run] mint_tx={mint_tx}, gas_price={gas_price_wei/1e9:.6f} gwei, ETH/USD={eth_usd}, "
          f"оценка газа входа=${gas_cost_usd}")
    result["gas_estimate"] = {**mint_tx, "gas_price_gwei": gas_price_wei / 1e9, "eth_usd": eth_usd, "estimated_cost_usd": gas_cost_usd}

    print("\n=== 6. Across-котировка на реальную сумму (не $100 условных) ===")
    try:
        resp = requests.post("https://rpc.mainnet.chain.robinhood.com", json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=20)
        robinhood_chain_id = int(resp.json()["result"], 16)
        amount_wei = str(int(lp_capital_usd * 1e6))  # USDG 6 decimals
        across = across_quote(robinhood_chain_id, amount_wei)
        result["across_quote_real_amount"] = across
        print(f"[p6_dry_run] Across котировка на ${lp_capital_usd}: {json.dumps(across, default=str)[:500]}")
    except Exception as exc:  # noqa: BLE001
        result["across_quote_error"] = str(exc)[:300]

    Path("data/p3_guard_cache/p6_dry_run_entry_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    print("\n[p6_dry_run] ГОТОВО -- ничего не отправлено, все числа выше прочитаны реально.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
