#!/usr/bin/env python3
"""Задача C -- п.3 (владелец, 2026-09-05): история цен по совпавшим
settled-играм на обеих площадках. Только чтение.

РЕАЛЬНО ПОДТВЕРЖДЕНО (`taskC_edge_probe_result.json`):
- Kalshi `/markets/trades?ticker=X` -- status 200, реальные сделки
  (yes_price_dollars/no_price_dollars/created_time). `candlesticks`
  падал 400 не потому что не работает, а потому что диапазон
  start_ts..end_ts при period_interval=60 (минуты) превысил лимит
  5000 свечей -- используем `trades` напрямую (точнее, не агрегат).
- Polymarket `/prices-history` -- падал 400 "time component is
  mandatory" -- не баг эндпоинта, просто нужны реальные `startTs`/
  `endTs` (в СЕКУНДАХ, не мс -- подтверждаем эмпирически по ответу).

Метод: для каждой из 4 изначально совпавших (теперь settled) игр --
тянем полную историю сделок Kalshi и полную историю цен Polymarket за
окно [game_start-2ч; game_end+1ч], строим синтетический ряд суммы
Yes_K+No_P и Yes_P+No_K по времени (последняя известная цена на каждый
момент с обеих сторон), считаем: долю времени с суммой<$1 после
комиссий, медианную длительность непрерывного окна, и совпадение
разрешения (кто выиграл по факту на каждой площадке)."""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from taskC_sports_matcher import fetch_polymarket_bulk, normalize  # noqa: E402

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskC-edge-history/1.0"}
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CLOB_BASE = "https://clob.polymarket.com"
OUT_PATH = Path("data/p3_guard_cache/taskC_edge_history_analysis_result.json")


def kalshi_fee(price_dollars: float) -> float:
    return math.ceil(0.07 * price_dollars * (1 - price_dollars) * 100) / 100


def kalshi_market(ticker: str) -> dict:
    r = requests.get(f"{KALSHI_BASE}/markets/{ticker}", headers=HEADERS, timeout=20)
    return r.json().get("market", {}) if r.status_code == 200 else {}


def kalshi_event_markets(event_ticker: str) -> list[dict]:
    r = requests.get(f"{KALSHI_BASE}/markets", params={"event_ticker": event_ticker}, headers=HEADERS, timeout=20)
    return r.json().get("markets", []) if r.status_code == 200 else []


def kalshi_all_trades(ticker: str, max_pages: int = 20) -> list[dict]:
    trades, cursor = [], None
    for _ in range(max_pages):
        params = {"ticker": ticker, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{KALSHI_BASE}/markets/trades", params=params, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            break
        body = r.json()
        trades.extend(body.get("trades", []))
        cursor = body.get("cursor")
        if not cursor:
            break
        time.sleep(0.2)
    return trades


def clob_prices_history(token_id: str, start_ts: int, end_ts: int, fidelity: int = 5) -> list[dict]:
    r = requests.get(f"{CLOB_BASE}/prices-history",
                      params={"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity},
                      headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return []
    return r.json().get("history", [])


def analyze_game(kalshi_event: str, kalshi_ticker: str, kalshi_title: str, pm_market: dict) -> dict:
    entry = {"kalshi_event": kalshi_event, "kalshi_ticker": kalshi_ticker, "polymarket_question": pm_market.get("question")}

    market_meta = kalshi_market(kalshi_ticker)
    entry["kalshi_result"] = market_meta.get("result")  # "yes"/"no" -- реальное разрешение

    trades = kalshi_all_trades(kalshi_ticker)
    entry["n_kalshi_trades"] = len(trades)
    if not trades:
        entry["status"] = "нет сделок Kalshi -- не считаем историю"
        return entry

    try:
        token_ids = json.loads(pm_market["clobTokenIds"]) if isinstance(pm_market["clobTokenIds"], str) else pm_market["clobTokenIds"]
        outcomes = json.loads(pm_market["outcomes"]) if isinstance(pm_market["outcomes"], str) else pm_market["outcomes"]
    except (ValueError, TypeError, KeyError):
        entry["status"] = "нет clobTokenIds/outcomes у Polymarket рынка"
        return entry

    # РЕАЛЬНАЯ НАХОДКА (taskC_edge_outcome_order_verify_result.json):
    # outcomes -- либо ["Yes","No"] (Tie/Draw, позиция безопасна), либо
    # ИМЕНА бойцов (UFC) -- порядок НЕ гарантированно совпадает с Kalshi.
    # Матчим по имени явно, не по позиции 0/1.
    outcomes_norm = [normalize(o) for o in outcomes]
    if "yes" in outcomes_norm and "no" in outcomes_norm:
        yes_idx = outcomes_norm.index("yes")
    else:
        title_norm = normalize(kalshi_title.replace(" wins", ""))
        yes_idx = next((i for i, o in enumerate(outcomes_norm) if title_norm == o or title_norm in o or o in title_norm), None)
        if yes_idx is None:
            entry["status"] = f"не нашли '{kalshi_title}' в outcomes Polymarket {outcomes} -- не гадаем"
            return entry
    no_idx = 1 - yes_idx
    entry["matched_polymarket_outcome"] = outcomes[yes_idx]

    # Реальное окно -- от первой до последней сделки Kalshi, +буфер
    times = [datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")) for t in trades]
    start_ts = int(min(times).timestamp()) - 3600
    end_ts = int(max(times).timestamp()) + 3600
    entry["window"] = [datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
                        datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()]

    pm_yes_history = clob_prices_history(token_ids[yes_idx], start_ts, end_ts)
    pm_no_history = clob_prices_history(token_ids[no_idx], start_ts, end_ts) if len(token_ids) > no_idx else []
    entry["n_pm_yes_points"] = len(pm_yes_history)
    entry["n_pm_no_points"] = len(pm_no_history)
    if not pm_yes_history or not pm_no_history:
        entry["status"] = "Polymarket prices-history пуста -- не считаем совмещённый ряд"
        return entry

    # Реальный разрешённый исход Polymarket (последняя цена -- 0 или 1 на settled рынке)
    entry["polymarket_yes_final_price"] = pm_yes_history[-1].get("p") if pm_yes_history else None
    entry["polymarket_outcomes"] = outcomes

    # Синтетический ряд: на каждый момент сделки Kalshi берём последнюю
    # известную к этому моменту цену Polymarket (forward-fill), считаем
    # обе комбинации.
    pm_yes_sorted = sorted(pm_yes_history, key=lambda x: x["t"])
    pm_no_sorted = sorted(pm_no_history, key=lambda x: x["t"])

    def price_at(history: list[dict], ts: float) -> float | None:
        result = None
        for pt in history:
            if pt["t"] <= ts:
                result = pt["p"]
            else:
                break
        return result

    # РЕАЛЬНАЯ НАХОДКА: Kalshi /markets/trades отдаёт сделки НОВЫЕ->СТАРЫЕ
    # (подтверждено эмпирически, taskC_edge_probe_result.json -- убывающий
    # created_time). Без явной сортировки по возрастанию непрерывные
    # "окна ниже доллара" считались задом наперёд -- отрицательная
    # длительность окна была прямым признаком этого бага.
    trades_sorted = sorted(trades, key=lambda t: t["created_time"])

    below_dollar = 0
    windows = []
    min_costs_below = []
    in_window = False
    window_start = None
    total = 0
    last_ts = None
    for t in trades_sorted:
        ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
        yes_k = float(t.get("yes_price_dollars", 0))
        no_k = float(t.get("no_price_dollars", 0))
        yes_p = price_at(pm_yes_sorted, ts)
        no_p = price_at(pm_no_sorted, ts)
        if yes_p is None or no_p is None:
            continue
        total += 1
        last_ts = ts
        combo_a = yes_k + kalshi_fee(yes_k) + no_p
        combo_b = yes_p + no_k + kalshi_fee(no_k)
        min_cost = min(combo_a, combo_b)
        is_below = min_cost < 1.0
        if is_below:
            below_dollar += 1
            min_costs_below.append(min_cost)
        if is_below and not in_window:
            in_window, window_start = True, ts
        elif not is_below and in_window:
            windows.append(ts - window_start)
            in_window = False
    if in_window and last_ts is not None:
        windows.append(last_ts - window_start)

    entry["n_points_compared"] = total
    entry["fraction_time_below_dollar"] = round(below_dollar / total, 4) if total else None
    entry["n_windows_below_dollar"] = len(windows)
    entry["median_window_seconds"] = sorted(windows)[len(windows) // 2] if windows else None
    # РЕАЛЬНАЯ величина спреда (не только флаг "<$1") -- нужна для
    # предрегистрированного порога владельца "чистый спред >=1.5%"
    if min_costs_below:
        net_spreads = sorted(round((1 - c) * 100, 4) for c in min_costs_below)  # в % от $1
        entry["net_spread_pct_median"] = net_spreads[len(net_spreads) // 2]
        entry["net_spread_pct_max"] = net_spreads[-1]
        entry["net_spread_pct_min"] = net_spreads[0]
    entry["status"] = "ok"
    return entry


# РЕАЛЬНАЯ ФИКСАЦИЯ (не читаем taskC_sports_matcher_result.json "matched"
# -- этот файл живой, п.1/п.2 уже дважды перезаписали его для своих целей,
# список "matched" сейчас другой набор игр, не оригинальные 4). Ниже --
# ИМЕННО те 4 игры, что были реально совпадены и опубликованы владельцу
# изначально (2026-09-05, run 33978555122): 3 EPL + 1 UFC. MCICOV
# сознательно ПРОПУЩЕН -- реально подтверждено (taskC_edge_probe_result.json),
# что для неё на Polymarket вообще нет ни draw, ни moneyline рынка
# (только corners O/U, несопоставимо по семантике) -- не гадаем замену.
ORIGINAL_MATCHED_GAMES = [
    {"kalshi_event": "KXEPLGAME-26SEP05FULCRY", "has_tie": True,
     "pm_question": "Will Fulham FC vs. Crystal Palace FC end in a draw?"},
    {"kalshi_event": "KXEPLGAME-26SEP05BRESUN", "has_tie": True,
     "pm_question": "Will Brentford FC vs. Sunderland AFC end in a draw?"},
    {"kalshi_event": "KXUFCFIGHT-26SEP05DUCDIA", "has_tie": False,
     "pm_question": "UFC Fight Night: Luis Felipe Dias vs. Matthieu Letho Duclos (Middleweight, Prelims)"},
]


def run() -> int:
    pm_markets = fetch_polymarket_bulk()
    print(f"[history] реальных Polymarket-рынков для поиска: {len(pm_markets)}")

    results = []
    for g in ORIGINAL_MATCHED_GAMES:
        pm_market = next((m for m in pm_markets if normalize(m.get("question", "")) == normalize(g["pm_question"])), None)
        if not pm_market:
            results.append({"kalshi_event": g["kalshi_event"], "status": "Polymarket рынок больше не найден в bulk (вероятно, ротировался из окна)"})
            continue
        # Реальный тикер конкретного Kalshi-рынка -- НЕ строковая склейка
        # (событие != рынок): для EPL берём Tie-исход (сопоставлен с Draw),
        # для UFC -- первый исход "X wins" (симметрично матчу-победителю,
        # any_side_verify.py уже подтвердил корректность name-based match).
        event_markets = kalshi_event_markets(g["kalshi_event"])
        if g["has_tie"]:
            chosen = next((m for m in event_markets if m.get("title") == "Tie is the result"), None)
        else:
            chosen = next((m for m in event_markets if (m.get("title") or "").endswith(" wins")), None)
        if not chosen:
            results.append({"kalshi_event": g["kalshi_event"], "status": "не нашли реальный тикер рынка по событию -- пропуск"})
            continue
        entry = analyze_game(g["kalshi_event"], chosen["ticker"], chosen["title"], pm_market)
        results.append(entry)
        print(json.dumps(entry, indent=2, ensure_ascii=False, default=str))
        time.sleep(0.5)

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[history] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
