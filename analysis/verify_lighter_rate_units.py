#!/usr/bin/env python3
"""Владелец, 2026-09-04: проверить единицу измерения поля `rate` в ответе
Lighter `/api/v1/fundings` НАШИМИ ЖЕ данными -- реальным Δfunding на живой
позиции 1000756 (хедж ETH на api.rh.lighter.xyz, market_id=0, размер
hedge_size_eth_entry=0.0377, data/p5_live_position_state.json), а не
догадкой/документацией.

Метод: берём ДВЕ последовательные точки из data/p5_fee_accrual.jsonl
(реальный, уже накопленный ряд), считаем implied_rate = Δfunding_usd /
(notional_usd × Δt_часов) -- это ФАКТИЧЕСКАЯ часовая ставка в долях
(fraction). Параллельно запрашиваем у Lighter `/api/v1/fundings` для
market_id=0 (ETH) РОВНО за то же окно времени и сравниваем `rate` из
ответа API с implied_rate: если `rate` (сырое число, напр. "0.0012")
надо интерпретировать как ПРОЦЕНТ -- rate/100 должно совпасть с
implied_rate (fraction) с точностью до нормального часового шума
funding (не x100/x10000 разъезда). Если совпадает как ДОЛЯ -- rate
само по себе (без /100) должно совпасть с implied_rate.

Хост (api.rh.lighter.xyz) заблокирован из интерактивной песочницы --
выполняется только через GH Actions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-funding-logger/1.0"}
ETH_MARKET_ID = 0  # data/funding_pairs.json: symbol=ETH, lighter_market_id=0
HEDGE_SIZE_ETH = 0.0377  # data/p5_live_position_state.json, hedge_size_eth_entry -- неизменно на протяжении позиции


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def run() -> int:
    rows = [json.loads(l) for l in Path("data/p5_fee_accrual.jsonl").read_text().splitlines() if l.strip()]
    rows = rows[-4:]  # последние несколько точек достаточно

    result = {"pairs": []}
    for a, b in zip(rows[:-1], rows[1:]):
        t_a, t_b = parse_ts(a["timestamp_utc"]), parse_ts(b["timestamp_utc"])
        dt_h = (t_b - t_a).total_seconds() / 3600
        d_funding = b["hedge_funding_paid_out_usd"] - a["hedge_funding_paid_out_usd"]
        avg_price = (a["pool_price_usd"] + b["pool_price_usd"]) / 2
        notional = HEDGE_SIZE_ETH * avg_price
        implied_rate_fraction_per_hour = d_funding / (notional * dt_h) if notional and dt_h else None

        # Реальные записи Lighter за то же окно (с запасом по часу в обе
        # стороны -- funding считается "в конце часа", границы бакетов
        # могут не совпадать секунда-в-секунду с нашими snapshot'ами).
        start_ts = int(t_a.timestamp()) - 3600
        end_ts = int(t_b.timestamp()) + 3600
        try:
            resp = requests.get(f"{LIGHTER_API_BASE}/api/v1/fundings", headers=HEADERS, params={
                "market_id": ETH_MARKET_ID, "resolution": "1h",
                "start_timestamp": start_ts, "end_timestamp": end_ts, "count_back": 10,
            }, timeout=20)
            resp.raise_for_status()
            fundings = resp.json().get("fundings", [])
        except Exception as exc:  # noqa: BLE001
            fundings = []
            print(f"[verify] Lighter /fundings упал для окна {a['timestamp_utc']}..{b['timestamp_utc']}: {exc}")

        pair_result = {
            "t_a": a["timestamp_utc"], "t_b": b["timestamp_utc"], "dt_hours": dt_h,
            "d_funding_usd": d_funding, "notional_usd": notional,
            "implied_rate_fraction_per_hour": implied_rate_fraction_per_hour,
            "implied_rate_if_expressed_as_pct": implied_rate_fraction_per_hour * 100 if implied_rate_fraction_per_hour is not None else None,
            "lighter_fundings_in_window": fundings,
        }
        if fundings and implied_rate_fraction_per_hour is not None:
            avg_raw_rate = sum(float(r["rate"]) for r in fundings) / len(fundings)
            pair_result["lighter_avg_raw_rate_in_window"] = avg_raw_rate
            # Гипотеза "raw rate уже %": rate/100 (доля) должен совпасть с implied_rate_fraction_per_hour
            pair_result["ratio_if_rate_is_pct"] = (avg_raw_rate / 100) / implied_rate_fraction_per_hour if implied_rate_fraction_per_hour else None
            # Гипотеза "raw rate -- доля": rate (доля) должен совпасть напрямую
            pair_result["ratio_if_rate_is_fraction"] = avg_raw_rate / implied_rate_fraction_per_hour if implied_rate_fraction_per_hour else None
        result["pairs"].append(pair_result)
        print(json.dumps(pair_result, indent=2, ensure_ascii=False, default=str))

    Path("data/p3_guard_cache/verify_lighter_rate_units_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
