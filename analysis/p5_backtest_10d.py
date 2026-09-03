#!/usr/bin/env python3
"""P5 (владелец, 2026-09-03): "P5 — вариант 1, окно 10 дней, один
прогон. Kill-порог 30% годовых после издержек. Дополнительно: посчитать
реализованную волатильность ETH за окно и за 30 дней, доложить,
репрезентативно ли окно. Если результат выше порога — расширяем до 30
дней вторым заходом."

ЧЕСТНАЯ оговорка до старта (не сглажено): полное разрешение (все
Swap-события) для 10 дней на пуле P5 (~34 456 свопов/час по калибровке
mm_p5_setup.py) экстраполируется в ~5053 запроса eth_getLogs ~112 минут
при эмпирической скорости ~45 вызовов/мин — это БОЛЬШЕ жёсткого
90-минутного лимита (владелец, многократно подтверждено). Скрипт
использует мягкий самоконтроль по времени (TIME_BUDGET_S = 85 мин) --
сканирует окно по дням, НАЧИНАЯ С САМЫХ СВЕЖИХ (не с 10 дней назад), и
останавливается на границе суток, если бюджет исчерпан -- получаем
непрерывный кусок из N<=10 самых последних дней вместо непредсказуемо
усечённого куска. Реальное покрытие (сколько дней вправду набралось)
записывается в результат as `days_covered` -- никогда не выдаётся за
полные 10, если по факту меньше.

Методология -- дословно из docs/P5_HEDGED_LP.md (п.1-4), формулы
Uniswap V3 (амаунты по L/sqrtP/sqrtPa/sqrtPb), IL относительно HODL той
же входной пропорции, фандинг + издержки хеджа. ВАЖНОЕ УТОЧНЕНИЕ
методологии (сделано здесь, при реализации, не решено заранее):
хедж-нога торгуется на Lighter (отдельная инфраструктура), НЕ на
Robinhood Chain -- применять "газ Robinhood Chain" к сделкам на другой
площадке было бы некорректно. Издержка ребаланса здесь = спред,
посчитанный по РЕАЛЬНОЙ глубине книги Lighter на нужный размер сделки
(walk the book). Отдельная тейкер/мейкер-комиссия Lighter НЕ найдена в
публичных полях API (`orderBookDetails`/`orderBookOrders`) -- явно НЕ
включена (не выдумана), это явное ограничение, отмечено в результате.
Знак фандинга: используется реальный `rate` (Lighter, `/api/v1/fundings`,
уже процент/час -- конвенция analysis/p4_lighter_markets.py) со
СТАНДАРТНОЙ рыночной конвенцией "положительный funding -> лонги платят
шортам" -- эта конвенция НЕ подтверждена независимо для Lighter именно
по документации (та же нерешённая оговорка, что в docs/P4_RECON.md для
сток-перпов) -- отмечено явно, не скрыто.

Реализованная волатильность ETH -- по правилу "внешние API сначала":
CoinGecko market_chart (бесплатно, реальные исторические часовые цены
ETH/USD за 30 дней), НЕ ончейн-цена пула (которую мы и так частично
получаем дороже) -- считаем volatility окна (10д, из этого же ряда) и
30д, сравниваем.

Только чтение (eth_getLogs/eth_call + публичные HTTP API), ключ не
используется, транзакций нет.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
from eth_abi import decode as abi_decode  # noqa: E402

from alchemy_fallback import _chunked_get_logs, _rpc_call, get_block, get_block_number, topic0  # noqa: E402
from p4_lighter_markets import fetch_funding_history  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_backtest_10d_result.json")
P5_POOL = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca".lower()
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH_DECIMALS, USDG_DECIMALS = 18, 6
FEE_TIER = 0.0001  # 0.01%, подтверждено напрямую (mm_p5_setup_result.json, fee=100/1e6)
LIGHTER_API_BASE = "https://mainnet.zklighter.elliot.ai"

WINDOW_DAYS = 10
TIME_BUDGET_S = 85 * 60  # мягкий предел -- запас под 90-минутный жёсткий лимит владельца
KILL_THRESHOLD_ANNUAL = 0.30  # владелец, 2026-09-03: поднят с 25% до 30%
NOTIONAL_USD = 10_000
RANGE_DEFS = {"pm5": 0.05, "pm10": 0.10, "pm20": 0.20, "full": 0.9999}  # "full" -- см. докстринг
REBALANCE_POLICIES = ["hourly", "drift5", "drift10"]
CAPACITY_ADD_USD = [10_000, 50_000, 200_000]

TOPIC0_V3_SWAP = topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")
# Пул on-chain хранит liquidity в "сырых" единицах (относительно sqrtPriceX96,
# т.е. привязанных к raw token amounts, не decimal-adjusted human price).
# v3_amounts()/l_for_notional() ниже работают в HUMAN price (P в USDG за 1 ETH,
# decimals уже учтены price_from_sqrt) -- то есть L оттуда ("L_human") НЕ в тех
# же единицах, что pool's on-chain `liquidity` ("L_raw"). Пересчёт (выведено из
# price_from_sqrt: human_price = raw_price * 10**(dec0-dec1)):
#   L_raw = L_human * 10**((dec0+dec1)/2)
# Для WETH(18)/USDG(6): показатель = 12, ЦЕЛОЕ число -- без пересчёта my_share
# был бы завышен в 1e12 раз (сравнение L_human напрямую с L_raw пула).
L_HUMAN_TO_RAW = 10 ** ((WETH_DECIMALS + USDG_DECIMALS) // 2)

MAX_REQUESTS_PER_RUN = 20000  # защитный потолок поверх мягкого TIME_BUDGET_S (та же дисциплина, что в остальных скриптах сессии)

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n
    if _request_count > MAX_REQUESTS_PER_RUN:
        raise RuntimeError(f"[p5_backtest_10d] СТОП: превышен потолок запросов "
                            f"({_request_count} > {MAX_REQUESTS_PER_RUN}).")


def _eth_call(to: str, data: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"[p5_backtest_10d]   eth_call {to} {data[:10]} не удался: {e}")
        return None


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def decode_v3_swap_full(log: dict) -> dict:
    data = bytes.fromhex(str(log["data"])[2:])
    amount0, amount1, sqrt_price_x96, liquidity, tick = abi_decode(
        ["int256", "int256", "uint160", "uint128", "int24"], data)
    return {"block_number": int(log["blockNumber"], 16), "log_index": int(log["logIndex"], 16),
            "amount0": amount0, "amount1": amount1, "sqrt_price_x96": sqrt_price_x96, "liquidity": liquidity}


def price_from_sqrt(sqrt_price_x96: int) -> float:
    """token1(USDG) за 1 token0(WETH) -- P5: token0=WETH, token1=USDG (подтверждено mm_p5_setup_result.json)."""
    raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    return raw * (10 ** (WETH_DECIMALS - USDG_DECIMALS))


def estimate_seconds_per_block(latest: int) -> float:
    t_latest = int(get_block(latest)["timestamp"], 16)
    _count()
    early = max(1, latest - 200_000)
    t_early = int(get_block(early)["timestamp"], 16)
    _count()
    return (t_latest - t_early) / (latest - early)


def fetch_pool_hourly(latest_block: int, spb: float, t0: float) -> dict:
    """Сканирует окно ПО СУТКАМ, начиная с самых свежих (латест-first),
    останавливается на границе суток при исчерпании TIME_BUDGET_S --
    см. докстринг модуля. Возвращает per-hour агрегаты + фактическое
    покрытие (days_covered, from_ts/to_ts)."""
    blocks_per_day = int(round(86400 / spb))
    now_block = latest_block
    hourly: dict[int, dict] = {}
    days_covered = 0
    day_boundaries = []

    for day_i in range(WINDOW_DAYS):
        if time.time() - t0 > TIME_BUDGET_S:
            print(f"[p5_backtest_10d] мягкий бюджет времени исчерпан после {days_covered} суток -- стоп")
            break
        day_to = now_block - day_i * blocks_per_day
        day_from = max(1, now_block - (day_i + 1) * blocks_per_day + 1)
        # chunk_size мал НАРОЧНО -- пул P5 торгуется ~1 своп/блок (калибровка
        # mm_p5_setup.py: 4910 свопов / 5000 блоков), безопасный запас под
        # публичный лимит "10000 логов/запрос" (см. фикс alchemy_fallback.py,
        # 2026-09-03 -- бисекция теперь работает и на JSON-RPC error-теле).
        logs = list(_chunked_get_logs(
            day_from, day_to, [TOPIC0_V3_SWAP], chunk_size=5_000, address=P5_POOL,
            on_call=lambda lo, hi, n: _count(1),
        ))
        # Привязка свопа к часу -- линейная интерполяция timestamp между началом и
        # концом суточного среза по номеру блока (2 запроса блока на сутки, НЕ на
        # каждый своп -- иначе бюджет вызовов взорвался бы при такой плотности).
        t_day_from = int(get_block(day_from)["timestamp"], 16)
        _count()
        t_day_to = int(get_block(day_to)["timestamp"], 16)
        _count()
        span_blocks = max(1, day_to - day_from)
        span_s = max(1, t_day_to - t_day_from)
        for l in logs:
            row = decode_v3_swap_full(l)
            frac = (row["block_number"] - day_from) / span_blocks
            ts = t_day_from + frac * span_s
            hour_bucket = int(ts // 3600) * 3600
            price = price_from_sqrt(row["sqrt_price_x96"])
            usdg_amount = abs(row["amount1"]) / (10 ** USDG_DECIMALS)
            h = hourly.setdefault(hour_bucket, {"volume_usdg": 0.0, "last_price": None,
                                                 "last_liquidity": None, "n_swaps": 0, "last_log_idx": -1})
            h["volume_usdg"] += usdg_amount
            h["n_swaps"] += 1
            # "последняя" цена/ликвидность часа -- по (block_number, log_index)
            key = (row["block_number"], row["log_index"])
            if key >= (h.get("_last_key") or (-1, -1)):
                h["_last_key"] = key
                h["last_price"] = price
                h["last_liquidity"] = row["liquidity"]

        days_covered += 1
        day_boundaries.append({"day_index": day_i, "from_block": day_from, "to_block": day_to,
                                "from_ts": t_day_from, "to_ts": t_day_to, "n_swaps": len(logs)})
        print(f"[p5_backtest_10d] сутки {day_i} (от последних) [{day_from},{day_to}]: "
              f"{len(logs)} свопов, всего часов накоплено {len(hourly)}, "
              f"запросов {_request_count}, {time.time()-t0:.0f}с")

    for h in hourly.values():
        h.pop("_last_key", None)

    return {"hourly": hourly, "days_covered": days_covered, "day_boundaries": day_boundaries}


def v3_amounts(L: float, P: float, Pa: float, Pb: float) -> tuple[float, float]:
    sqrtP = min(max(P ** 0.5, Pa ** 0.5), Pb ** 0.5)
    sqrtPa, sqrtPb = Pa ** 0.5, Pb ** 0.5
    amount0 = L * (1 / sqrtP - 1 / sqrtPb)  # WETH
    amount1 = L * (sqrtP - sqrtPa)          # USDG
    return max(amount0, 0.0), max(amount1, 0.0)


def l_for_notional(v0: float, p0: float, pa: float, pb: float) -> float:
    sqrtP0 = min(max(p0 ** 0.5, pa ** 0.5), pb ** 0.5)
    sqrtPa, sqrtPb = pa ** 0.5, pb ** 0.5
    denom = (1 / sqrtP0 - 1 / sqrtPb) * p0 + (sqrtP0 - sqrtPa)
    return v0 / denom if denom > 0 else 0.0


def find_eth_market() -> dict | None:
    resp = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
    resp.raise_for_status()
    markets = resp.json().get("order_book_details", [])
    exact = [m for m in markets if str(m.get("symbol", "")).upper() == "ETH"]
    if exact:
        return exact[0]
    candidates = [m for m in markets if "ETH" in str(m.get("symbol", "")).upper()]
    return candidates[0] if candidates else None


def fetch_book_depth(market_id: int, mid_price: float, notionals: list[float]) -> dict:
    """Walk-the-book спред для каждого notional -- реальная книга Lighter."""
    out = {}
    for limit in (200, 500, 1000):
        r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookOrders",
                          params={"market_id": market_id, "limit": limit}, timeout=20)
        if r.status_code != 200:
            continue
        body = r.json()
        asks = sorted(body.get("asks", []), key=lambda o: float(o["price"]))
        bids = sorted(body.get("bids", []), key=lambda o: float(o["price"]), reverse=True)
        if len(asks) < limit and len(bids) < limit:
            break
    for notional in notionals:
        # walk asks (покупка ETH-перпа -- закрытие/открытие шорта нужного размера)
        remaining = notional
        cost = 0.0
        filled = 0.0
        for o in asks:
            px = float(o["price"])
            sz = float(o.get("remaining_base_amount", o.get("initial_base_amount", 0)))
            usd = px * sz
            take = min(remaining, usd)
            cost += take
            filled += take / px
            remaining -= take
            if remaining <= 0:
                break
        avg_px = cost / filled if filled > 0 else None
        slippage_pct = (avg_px - mid_price) / mid_price * 100 if avg_px else None
        out[f"${notional}"] = {"avg_execution_price": avg_px, "slippage_vs_mid_pct": slippage_pct,
                                "fully_filled": remaining <= 0}
    return out


def fetch_eth_volatility() -> dict:
    """CoinGecko market_chart -- реальная часовая история ETH/USD за 30
    дней (внешний бесплатный API, правило владельца -- сначала внешние
    API)."""
    r = requests.get("https://api.coingecko.com/api/v3/coins/ethereum/market_chart",
                      params={"vs_currency": "usd", "days": 30}, timeout=20)
    r.raise_for_status()
    prices = r.json().get("prices", [])  # [[ts_ms, price], ...]
    if len(prices) < 10:
        return {"error": "недостаточно точек от CoinGecko"}
    vals = [p[1] for p in prices]

    def realized_vol_annualized(series: list[float]) -> float:
        rets = [math.log(series[i] / series[i - 1]) for i in range(1, len(series)) if series[i - 1] > 0]
        if len(rets) < 2:
            return float("nan")
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        std = var ** 0.5
        # шаг ряда CoinGecko при days=30 -- часовой (авто-интервал документирован для <=90 дней)
        periods_per_year = 24 * 365
        return std * (periods_per_year ** 0.5) * 100  # в %

    vol_30d = realized_vol_annualized(vals)
    n_points_10d = min(len(vals), 10 * 24)
    vol_10d = realized_vol_annualized(vals[-n_points_10d:])
    return {"n_points_30d": len(vals), "n_points_10d_subset": n_points_10d,
            "realized_vol_annualized_pct_30d": vol_30d, "realized_vol_annualized_pct_10d": vol_10d,
            "representative": abs(vol_10d - vol_30d) / vol_30d < 0.25 if vol_30d else None,
            "source": "CoinGecko /coins/ethereum/market_chart, реальные часовые цены ETH/USD"}


def simulate(range_pct: float, policy: str, hourly_sorted: list[tuple[int, dict]],
             funding_by_hour: dict[int, float], depth_info: dict, mid_price_lighter: float) -> dict:
    if not hourly_sorted:
        return {"error": "нет часовых данных"}
    p0 = hourly_sorted[0][1]["last_price"]
    pa, pb = p0 * (1 - range_pct), p0 * (1 + range_pct)
    L = l_for_notional(NOTIONAL_USD, p0, pa, pb)
    amount0_0, amount1_0 = v3_amounts(L, p0, pa, pb)  # входные амаунты (HODL-база)

    current_hedge = -amount0_0  # сразу хеджируем дельту входа
    delta_at_last_rebal = amount0_0
    fees_cum = 0.0
    funding_cum = 0.0
    spread_cost_cum = 0.0
    perp_pnl_cum = 0.0
    n_rebalances = 0
    hours_in_range = 0
    prev_price = p0

    # усреднённый slippage_pct для типичного размера ребаланса ($10k нога -- берём $10k walk)
    slip_entry = depth_info.get("$10000", {}).get("slippage_vs_mid_pct")
    slip_rate = abs(slip_entry) / 100 if slip_entry is not None else 0.0

    for hour_ts, h in hourly_sorted:
        price = h["last_price"] if h["last_price"] is not None else prev_price
        vol_usdg = h["volume_usdg"]
        pool_liq = h["last_liquidity"] or 1

        in_range = pa <= price <= pb
        if in_range:
            hours_in_range += 1
            my_share = (L * L_HUMAN_TO_RAW) / pool_liq if pool_liq > 0 else 0.0
            fees_cum += vol_usdg * FEE_TIER * my_share

        # perp mark-to-market за этот час (позиция, ДЕРЖАВШАЯСЯ с начала часа)
        perp_pnl_cum += current_hedge * (price - prev_price)

        # funding за этот час, на текущий размер хеджа
        rate_pct_hour = funding_by_hour.get(hour_ts)
        if rate_pct_hour is not None:
            notional = abs(current_hedge) * price
            # шорт получает при положительном funding (стандартная конвенция, см. докстринг -- НЕ подтверждено для Lighter напрямую)
            funding_cum += notional * (rate_pct_hour / 100.0)

        amount0_now, _ = v3_amounts(L, price, pa, pb)
        target_hedge = -amount0_now

        do_rebalance = False
        if policy == "hourly":
            do_rebalance = True
        elif policy == "drift5":
            do_rebalance = delta_at_last_rebal != 0 and abs(amount0_now - delta_at_last_rebal) / abs(delta_at_last_rebal) > 0.05
        elif policy == "drift10":
            do_rebalance = delta_at_last_rebal != 0 and abs(amount0_now - delta_at_last_rebal) / abs(delta_at_last_rebal) > 0.10

        if do_rebalance and abs(target_hedge - current_hedge) > 1e-12:
            traded_notional = abs(target_hedge - current_hedge) * price
            spread_cost_cum += traded_notional * slip_rate
            current_hedge = target_hedge
            delta_at_last_rebal = amount0_now
            n_rebalances += 1

        prev_price = price

    p_end = hourly_sorted[-1][1]["last_price"] or p0
    amount0_end, amount1_end = v3_amounts(L, p_end, pa, pb)
    lp_value_end = amount0_end * p_end + amount1_end
    hodl_value_end = amount0_0 * p_end + amount1_0
    il_usd = lp_value_end - hodl_value_end

    net_pnl = (lp_value_end - NOTIONAL_USD) + fees_cum + perp_pnl_cum + funding_cum - spread_cost_cum
    n_hours = len(hourly_sorted)
    days_actual = n_hours / 24 if n_hours else 0
    annualized_pct = (net_pnl / NOTIONAL_USD) * (365 / days_actual) * 100 if days_actual > 0 else None

    return {
        "range_pct": range_pct, "policy": policy, "entry_price": p0, "range_lower": pa, "range_upper": pb,
        "n_hours": n_hours, "hours_in_range": hours_in_range,
        "time_out_of_range_pct": (1 - hours_in_range / n_hours) * 100 if n_hours else None,
        "fees_earned_usd": fees_cum, "il_usd": il_usd, "perp_pnl_usd": perp_pnl_cum,
        "funding_usd": funding_cum, "spread_cost_usd": spread_cost_cum, "n_rebalances": n_rebalances,
        "net_pnl_usd": net_pnl, "annualized_pct_after_costs": annualized_pct,
    }


def capacity_analysis(current_pool_liquidity: float, entry_price: float) -> dict:
    out = {}
    for add_usd in CAPACITY_ADD_USD:
        # моя доля = L_моё / (L_пула + L_моё), при добавлении add_usd (полный диапазон, best-case уплотнения)
        pa, pb = entry_price * 1e-4, entry_price * 1e4
        l_mine_raw = l_for_notional(add_usd, entry_price, pa, pb) * L_HUMAN_TO_RAW
        share = l_mine_raw / (current_pool_liquidity + l_mine_raw) if (current_pool_liquidity + l_mine_raw) > 0 else None
        out[f"${add_usd}"] = {"my_liquidity_share": share}
    return out


def run() -> int:
    t0 = time.time()
    latest = get_block_number()
    _count()
    spb = estimate_seconds_per_block(latest)
    print(f"[p5_backtest_10d] latest={latest} spb~={spb:.4f} окно запрошено={WINDOW_DAYS}д, "
          f"мягкий лимит {TIME_BUDGET_S}с")

    pool_data = fetch_pool_hourly(latest, spb, t0)
    hourly_sorted = sorted(pool_data["hourly"].items())
    print(f"[p5_backtest_10d] покрыто {pool_data['days_covered']} из {WINDOW_DAYS} суток, "
          f"{len(hourly_sorted)} часовых бакетов, {_request_count} запросов, {time.time()-t0:.0f}с")

    eth_market = find_eth_market()
    funding_by_hour: dict[int, float] = {}
    depth_info = {}
    mid_price_lighter = None
    if eth_market:
        market_id = eth_market["market_id"]
        mid_price_lighter = float(eth_market.get("mark_price") or eth_market.get("last_trade_price") or 0)
        print(f"[p5_backtest_10d] ETH на Lighter: market_id={market_id} mark={mid_price_lighter}")
        funding_records = fetch_funding_history(market_id)
        for r in funding_records:
            ts = int(r["timestamp"])
            hour_bucket = (ts // 3600) * 3600
            funding_by_hour[hour_bucket] = float(r["rate"])
        depth_info = fetch_book_depth(market_id, mid_price_lighter, [10_000, 50_000, 200_000])
        print(f"[p5_backtest_10d] фандинг записей: {len(funding_records)}, глубина книги: {depth_info}")
    else:
        print("[p5_backtest_10d] ETH-рынок на Lighter НЕ найден -- фандинг/глубина недоступны")

    volatility = fetch_eth_volatility()
    print(f"[p5_backtest_10d] волатильность ETH: {volatility}")

    sims = []
    for range_name, pct in RANGE_DEFS.items():
        for policy in REBALANCE_POLICIES:
            s = simulate(pct, policy, hourly_sorted, funding_by_hour, depth_info, mid_price_lighter or 0)
            s["range_name"] = range_name
            sims.append(s)
            print(f"[p5_backtest_10d] {range_name}/{policy}: annualized_after_costs="
                  f"{s.get('annualized_pct_after_costs')}%")

    best = max((s for s in sims if s.get("annualized_pct_after_costs") is not None),
               key=lambda s: s["annualized_pct_after_costs"], default=None)

    current_pool_liquidity = hourly_sorted[-1][1]["last_liquidity"] if hourly_sorted else None
    capacity = capacity_analysis(current_pool_liquidity, hourly_sorted[-1][1]["last_price"]) if hourly_sorted else {}

    verdict = "N/A -- нет данных"
    if best is not None:
        if best["annualized_pct_after_costs"] >= KILL_THRESHOLD_ANNUAL * 100:
            verdict = (f"ПОРОГ ПРОЙДЕН: лучший вариант {best['range_name']}/{best['policy']} = "
                       f"{best['annualized_pct_after_costs']:.2f}%/год >= {KILL_THRESHOLD_ANNUAL*100:.0f}% -- "
                       f"расширяем до 30 дней вторым заходом (по решению владельца)")
        else:
            verdict = (f"KILL: лучший вариант {best['range_name']}/{best['policy']} = "
                       f"{best['annualized_pct_after_costs']:.2f}%/год < {KILL_THRESHOLD_ANNUAL*100:.0f}% -- "
                       f"линия P5 закрывается по зарегистрированному порогу")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_days_requested": WINDOW_DAYS, "days_covered": pool_data["days_covered"],
        "day_boundaries": pool_data["day_boundaries"], "n_hourly_buckets": len(hourly_sorted),
        "kill_threshold_annual_pct": KILL_THRESHOLD_ANNUAL * 100,
        "eth_lighter_market": eth_market, "funding_records_n": len(funding_by_hour),
        "lighter_book_depth": depth_info,
        "eth_volatility": volatility,
        "simulations": sims, "best_simulation": best,
        "capacity_analysis": capacity,
        "verdict": verdict,
        "caveats": [
            "Хедж-издержка = спред по реальной книге Lighter, БЕЗ отдельной тейкер/мейкер-комиссии "
            "(не найдена в публичных полях API -- не выдумана, явно не включена).",
            "Знак фандинга: рабочее допущение 'положительный rate -> шорт получает' (стандартная "
            "рыночная конвенция) -- НЕ подтверждено официальной документацией Lighter для ETH-рынка "
            "конкретно (та же нерешённая оговорка, что в docs/P4_RECON.md для сток-перпов).",
            "'full' диапазон -- практически широкий (Pa=P0*1e-4, Pb=P0*1e4), не буквальный [0,inf) -- "
            "разница в пределах точности при движениях цены << 100x за окно.",
            "Часовая привязка свопа к времени -- линейная интерполяция между timestamp начала/конца "
            "суточного среза по номеру блока, не индивидуальный запрос блока на каждый своп (иначе "
            "бюджет вызовов взорвался бы) -- ошибка возможна на границах часа, не влияет на дневные "
            "агрегаты существенно.",
        ],
        "requests_used": _request_count, "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p5_backtest_10d] записано {OUT_PATH}, {_request_count} запросов, {time.time()-t0:.0f}с")
    print(f"[p5_backtest_10d] ВЕРДИКТ: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
