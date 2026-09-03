#!/usr/bin/env python3
"""P5 -- реальная история пула ETH/USDG с GeckoTerminal + реальная сигма.

Владелец, 2026-09-03, три смежные задачи одним прогоном:

1. Скачать OHLCV-историю пула (15-минутки + часовики для сверки),
   посчитать реальные log-return sigma (15м и часовую, аннуализированные),
   сравнить расхождение, и отдельно эмпирическое распределение
   max|ln(P_t/P_0)| на горизонтах 1ч/6ч/24ч/72ч -- прямой ответ на
   вопрос "сколько живёт диапазон +-10%".
2. (Комиссии по 1000756 -- ОТДЕЛЬНЫЙ скрипт, см.
   p5_live_position_snapshot.py -- не дублируется здесь.)
3. Перечитать /pools/{addr} на GeckoTerminal (ТЕКУЩИЙ TVL/объём/tx) и
   сравнить с цифрами, записанными в docs/P5_HEDGED_LP.md (TVL $21.1M,
   объём 24ч $823M, владелец/интерфейс, 2026-09-03) -- расхождение
   напрямую меняет APR-оценку.

ВАЖНО, честно: предыдущий прогон ЭТОЙ ЖЕ сессии (`mm_p5_setup.py`, run
33742262365, см. docs/P5_HEDGED_LP.md §2) проверил `GET /api/v2/networks`
и НЕ нашёл "Robinhood Chain" в списке (поиск по подстроке "robin", ноль
совпадений) -- вывод тогда был "GeckoTerminal этот пул/сеть не
поддерживает". Владелец сейчас называет слаг `robinhood` (не
`robinhood-chain`) и прямую ссылку на страницу пула. Это ПРОТИВОРЕЧИЕ
двух реальных источников -- ни один не принимается на веру, обе версии
проверяются здесь заново, напрямую по конкретному эндпоинту пула
(`/networks/{network}/pools/{address}`), а не по списку сетей (старая
проверка искала подстроку в списке сетей, что могло быть багом метода,
а не фактом отсутствия сети -- см. аналогичный баг №"0 из 24 сток-
токенов имеют v3-пул" в docs/PROJECT_STATE.md §4, тот же класс ошибки:
короткое/неверное окно поиска маскирует реальные данные).

Только чтение (HTTP GET, публичный API, без ключа). Пагинация
`before_timestamp` (unix секунды). Лимит 30 запросов/мин -- самотроттлинг
между запросами.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_gt_pool_history_result.json")
GT_BASE = "https://api.geckoterminal.com/api/v2"
NETWORK_SLUG_CANDIDATES = ["robinhood", "robinhood-chain"]  # проверяем ОБА -- владелец сказал "robinhood", предыдущий прогон сессии искал "robinhood-chain"
POOL_ADDRESS = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
MIN_REQUEST_INTERVAL_S = 2.2  # 30 запросов/мин лимит -> >=2.0с между вызовами, с запасом
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "robinhood-chain-alpha-p5/1.0"}

_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _last_call + MIN_REQUEST_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _get(url: str, params: dict | None = None) -> tuple[int, dict | str]:
    _throttle()
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:500]


def find_working_network_slug() -> tuple[str | None, dict]:
    """Проверяет ОБА кандидата напрямую по эндпоинту пула (не по списку
    сетей -- см. докстринг модуля почему список сетей мог дать ложный
    отрицательный результат)."""
    probes = {}
    for net in NETWORK_SLUG_CANDIDATES:
        status, body = _get(f"{GT_BASE}/networks/{net}/pools/{POOL_ADDRESS}")
        probes[net] = {"status": status, "ok": status == 200 and isinstance(body, dict) and "data" in body,
                       "body_snippet": body if status != 200 else "OK"}
        print(f"[gt_history] проба сети '{net}': HTTP {status}, ok={probes[net]['ok']}")
        if probes[net]["ok"]:
            return net, probes
    return None, probes


def fetch_pool_snapshot(network: str) -> dict:
    status, body = _get(f"{GT_BASE}/networks/{network}/pools/{POOL_ADDRESS}")
    if status != 200:
        return {"status": status, "error": body}
    return {"status": status, "attributes": body.get("data", {}).get("attributes", {}),
            "relationships": body.get("data", {}).get("relationships", {})}


def fetch_ohlcv_paginated(network: str, timeframe: str, aggregate: int, target_candles: int, max_pages: int) -> list[list]:
    """timeframe: 'minute'|'hour'|'day'. Возвращает список [ts, o, h, l, c, v],
    отсортированный по возрастанию ts, без дублей. Пагинация -- через
    before_timestamp = самый старый ts предыдущей страницы."""
    all_rows: dict[int, list] = {}
    before_ts: int | None = None
    for page in range(max_pages):
        params = {"aggregate": aggregate, "limit": 1000, "currency": "token", "token": "quote",
                  "include_empty_intervals": "true"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        status, body = _get(f"{GT_BASE}/networks/{network}/pools/{POOL_ADDRESS}/ohlcv/{timeframe}", params=params)
        if status != 200:
            print(f"[gt_history] ohlcv {timeframe} aggregate={aggregate} страница {page}: HTTP {status} -- {str(body)[:300]}")
            break
        rows = body.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        print(f"[gt_history] ohlcv {timeframe} aggregate={aggregate} страница {page}: {len(rows)} свечей"
              f" (before_timestamp={before_ts})")
        if not rows:
            break
        for row in rows:
            all_rows[int(row[0])] = row
        oldest_ts = min(int(row[0]) for row in rows)
        if before_ts is not None and oldest_ts >= before_ts:
            break  # защита от зацикливания, если API не уважает before_timestamp
        before_ts = oldest_ts
        if len(rows) < 1000:
            break  # последняя (неполная) страница -- дальше данных нет
        if len(all_rows) >= target_candles:
            break
    return [all_rows[ts] for ts in sorted(all_rows.keys())]


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]


def annualized_sigma(rets: list[float], periods_per_year: float) -> float | None:
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(periods_per_year)


def max_abs_log_move_distribution(closes: list[float], window_candles: int) -> dict:
    """Для каждого возможного старта окна длиной window_candles -- считает
    max|ln(P_t/P_0)| ВНУТРИ окна (P_0 = цена в начале окна). Возвращает
    перцентили этого распределения по ВСЕМ окнам ряда -- эмпирический
    ответ на "как далеко цена реально уходит за X часов", не
    теоретическая оценка по нормальному распределению."""
    n = len(closes)
    if n <= window_candles:
        return {"n_windows": 0}
    values = []
    for start in range(0, n - window_candles):
        p0 = closes[start]
        if p0 <= 0:
            continue
        window = closes[start:start + window_candles + 1]
        max_move = max(abs(math.log(p / p0)) for p in window if p > 0)
        values.append(max_move)
    if not values:
        return {"n_windows": 0}
    values.sort()

    def pct(p: float) -> float:
        idx = min(len(values) - 1, int(round(p * (len(values) - 1))))
        return values[idx]

    return {
        "n_windows": len(values),
        "median_pct": pct(0.50) * 100, "p75_pct": pct(0.75) * 100, "p90_pct": pct(0.90) * 100,
        "p95_pct": pct(0.95) * 100, "p99_pct": pct(0.99) * 100, "max_pct": values[-1] * 100,
        "share_windows_exceeding_10pct": sum(1 for v in values if v > 0.10) / len(values) * 100,
    }


def run() -> int:
    t0 = time.time()

    print("=== 0. Определение рабочего слэга сети (реальная проверка обоих кандидатов) ===")
    network, probes = find_working_network_slug()
    if network is None:
        result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "abort_reason": "Ни один слаг сети не дал 200 OK на эндпоинте пула -- см. network_probes.",
                   "network_probes": probes, "runtime_s": time.time() - t0}
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[gt_history] СТОП: {result['abort_reason']}")
        return 1
    print(f"[gt_history] рабочий слэг сети: '{network}'")

    print("\n=== 3. Снимок /pools/{addr} СЕЙЧАС (сравнение с docs/P5_HEDGED_LP.md) ===")
    snapshot = fetch_pool_snapshot(network)
    attrs = snapshot.get("attributes", {})
    print(f"[gt_history] reserve_in_usd={attrs.get('reserve_in_usd')} "
          f"volume_usd={attrs.get('volume_usd')} transactions.h24={attrs.get('transactions', {}).get('h24')} "
          f"fdv_usd={attrs.get('fdv_usd')} price_change_percentage={attrs.get('price_change_percentage')}")
    recorded_in_docs = {
        "source": "docs/P5_HEDGED_LP.md, строка 8-9, владелец/интерфейс GeckoTerminal, 2026-09-03",
        "tvl_usd": 21_100_000, "volume_24h_usd": 823_000_000, "apr_pct": 106.59,
        "cross_check_dexscreener_same_day": {"tvl_usd": 21_106_795.3, "volume_24h_usd": 828_339_768.62,
                                              "source": "docs/P5_HEDGED_LP.md, строка 98-99"},
    }
    reserve_now = float(attrs["reserve_in_usd"]) if attrs.get("reserve_in_usd") else None
    volume_24h_now = float(attrs["volume_usd"]["h24"]) if attrs.get("volume_usd", {}).get("h24") else None
    fee_tier = 0.0001  # 0.01%, подтверждено ончейн (docs/P5_HEDGED_LP.md §1)
    apr_recalc_now_pct = (volume_24h_now * fee_tier * 365 / reserve_now * 100) if (volume_24h_now and reserve_now) else None
    comparison_vs_docs = {
        "reserve_in_usd_now": reserve_now, "reserve_in_usd_recorded": recorded_in_docs["tvl_usd"],
        "reserve_ratio_now_over_recorded": (reserve_now / recorded_in_docs["tvl_usd"]) if reserve_now else None,
        "volume_24h_usd_now": volume_24h_now, "volume_24h_usd_recorded": recorded_in_docs["volume_24h_usd"],
        "volume_ratio_now_over_recorded": (volume_24h_now / recorded_in_docs["volume_24h_usd"]) if volume_24h_now else None,
        "apr_pct_recalculated_now": apr_recalc_now_pct, "apr_pct_recorded": recorded_in_docs["apr_pct"],
    }
    print(f"[gt_history] TVL сейчас/записано в доках = {comparison_vs_docs['reserve_ratio_now_over_recorded']}, "
          f"объём сейчас/записано = {comparison_vs_docs['volume_ratio_now_over_recorded']}, "
          f"APR пересчитан из текущих цифр = {apr_recalc_now_pct}%")

    # Реальный pool_created_at из снимка (не предположение "~1 месяц") --
    # определяет, сколько страниц реально нужно перебрать.
    pool_created_at = attrs.get("pool_created_at")
    print(f"[gt_history] pool_created_at (реальное поле снимка) = {pool_created_at}")

    print("\n=== 1. OHLCV -- 15-минутки (currency=token, token=quote -> цена WETH в USDG) ===")
    rows_15m = fetch_ohlcv_paginated(network, "minute", 15, target_candles=8000, max_pages=9)
    print(f"[gt_history] получено {len(rows_15m)} 15-минутных свечей "
          f"({rows_15m[0][0] if rows_15m else None} .. {rows_15m[-1][0] if rows_15m else None})")

    print("\n=== сверка: часовики (aggregate=1) за тот же период ===")
    rows_1h = fetch_ohlcv_paginated(network, "hour", 1, target_candles=2000, max_pages=4)
    print(f"[gt_history] получено {len(rows_1h)} часовых свечей "
          f"({rows_1h[0][0] if rows_1h else None} .. {rows_1h[-1][0] if rows_1h else None})")

    closes_15m = [float(r[4]) for r in rows_15m]
    closes_1h = [float(r[4]) for r in rows_1h]
    rets_15m = log_returns(closes_15m)
    rets_1h = log_returns(closes_1h)
    sigma_15m_annual = annualized_sigma(rets_15m, 4 * 24 * 365)  # 35040 15-минуток/год
    sigma_1h_annual = annualized_sigma(rets_1h, 24 * 365)  # 8760 часов/год
    divergence_pct = (abs(sigma_15m_annual - sigma_1h_annual) / sigma_1h_annual * 100) if (sigma_15m_annual and sigma_1h_annual) else None
    use_hourly_due_to_microstructure_noise = (divergence_pct is not None and divergence_pct > 20)

    print(f"[gt_history] sigma_15m (аннуализ. x sqrt(35040)) = {sigma_15m_annual}")
    print(f"[gt_history] sigma_1h  (аннуализ. x sqrt(8760))  = {sigma_1h_annual}")
    print(f"[gt_history] расхождение = {divergence_pct}% -- "
          f"{'>20%, берём часовую (микроструктурный шум)' if use_hourly_due_to_microstructure_noise else '<=20%, обе согласуются'}")

    print("\n=== эмпирическое распределение max|ln(Pt/P0)| на горизонтах 1ч/6ч/24ч/72ч (по 15-минуткам) ===")
    horizons_candles = {"1h": 4, "6h": 24, "24h": 96, "72h": 288}
    horizon_distributions = {label: max_abs_log_move_distribution(closes_15m, n) for label, n in horizons_candles.items()}
    for label, dist in horizon_distributions.items():
        print(f"[gt_history] горизонт {label}: {dist}")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network_slug_used": network, "network_probes": probes,
        "pool_snapshot_now": snapshot, "recorded_in_docs": recorded_in_docs,
        "comparison_vs_docs": comparison_vs_docs,
        "ohlcv_15m": {"n_candles": len(rows_15m), "first_ts": rows_15m[0][0] if rows_15m else None,
                      "last_ts": rows_15m[-1][0] if rows_15m else None, "raw": rows_15m},
        "ohlcv_1h": {"n_candles": len(rows_1h), "first_ts": rows_1h[0][0] if rows_1h else None,
                     "last_ts": rows_1h[-1][0] if rows_1h else None, "raw": rows_1h},
        "volatility": {
            "n_returns_15m": len(rets_15m), "n_returns_1h": len(rets_1h),
            "sigma_15m_annualized": sigma_15m_annual, "sigma_1h_annualized": sigma_1h_annual,
            "divergence_pct": divergence_pct, "use_hourly_due_to_microstructure_noise": use_hourly_due_to_microstructure_noise,
        },
        "max_abs_log_move_distribution_by_horizon": horizon_distributions,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[gt_history] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
