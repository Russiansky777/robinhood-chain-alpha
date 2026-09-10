#!/usr/bin/env python3
"""Задача F, Шаг 0b -- углублённая разведка Derive (api.lyra.finance).
Реальный рабочий базовый URL найден в первой разведке (api.derive.xyz
отдаёт 404 на всё, api.lyra.finance -- реальный живой API).

Проверяем:
  1. Полную структуру одного инструмента (есть ли явное поле IV).
  2. Реальную глубину get_trade_history за явно заданный диапазон
     (90 дней назад) -- пагинацию, число реальных сделок.
  3. Наличие эндпоинта исторических свечей перпа (для RV) на этой же
     площадке."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskF-depth-probe/1.0", "Accept": "application/json"}
BASE = "https://api.lyra.finance"
OUT_PATH = Path("data/p3_guard_cache/taskF_derive_depth_probe_result.json")


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    # 1. Полная структура одного BTC ATM-подобного инструмента с ближней экспирацией.
    r = requests.get(f"{BASE}/public/get_instruments", params={
        "currency": "BTC", "instrument_type": "option", "expired": "false",
    }, headers=HEADERS, timeout=20)
    instruments = r.json().get("result", []) if r.status_code == 200 else []
    result["n_btc_option_instruments_live"] = len(instruments)
    # Ищем инструмент с ближайшей экспирацией 7-30 дней от сейчас.
    near_expiry_candidates = []
    for inst in instruments:
        od = inst.get("option_details", {})
        expiry_ts = od.get("expiry")
        if expiry_ts:
            days_to_expiry = (expiry_ts - now.timestamp()) / 86400
            if 7 <= days_to_expiry <= 30:
                near_expiry_candidates.append((days_to_expiry, inst))
    near_expiry_candidates.sort(key=lambda x: x[0])
    result["n_btc_instruments_expiry_7_30d"] = len(near_expiry_candidates)
    if near_expiry_candidates:
        sample_inst = near_expiry_candidates[0][1]
        result["sample_instrument_full"] = sample_inst
        sample_name = sample_inst.get("instrument_name")
    else:
        sample_name = None
    result["sample_instrument_name"] = sample_name

    # 2. Реальная глубина get_trade_history -- запрашиваем явно за 90 дней.
    ninety_days_ago_ms = int((now - timedelta(days=90)).timestamp() * 1000)
    now_ms = int(now.timestamp() * 1000)
    r2 = requests.get(f"{BASE}/public/get_trade_history", params={
        "currency": "BTC", "instrument_type": "option",
        "from_timestamp": ninety_days_ago_ms, "to_timestamp": now_ms,
        "page_size": 100,
    }, headers=HEADERS, timeout=20)
    result["trade_history_90d_status"] = r2.status_code
    result["trade_history_90d_body_snippet"] = r2.text[:800]
    if r2.status_code == 200:
        try:
            body2 = r2.json().get("result", {})
            trades = body2.get("trades", []) if isinstance(body2, dict) else []
            result["trade_history_90d_n_trades_page1"] = len(trades)
            if trades:
                timestamps = [t.get("timestamp") for t in trades if t.get("timestamp")]
                if timestamps:
                    oldest = datetime.fromtimestamp(min(timestamps) / 1000, tz=timezone.utc)
                    newest = datetime.fromtimestamp(max(timestamps) / 1000, tz=timezone.utc)
                    result["trade_history_90d_oldest_trade"] = oldest.isoformat()
                    result["trade_history_90d_newest_trade"] = newest.isoformat()
                result["trade_history_sample_record"] = trades[0]
            result["trade_history_pagination_keys"] = list(body2.keys()) if isinstance(body2, dict) else None
        except (json.JSONDecodeError, ValueError):
            result["trade_history_90d_parse_error"] = True

    # 3. Если есть sample_name -- history сделок конкретно по этому инструменту.
    if sample_name:
        r3 = requests.get(f"{BASE}/public/get_trade_history", params={
            "instrument_name": sample_name, "from_timestamp": ninety_days_ago_ms, "to_timestamp": now_ms,
        }, headers=HEADERS, timeout=20)
        result["single_instrument_trade_history_status"] = r3.status_code
        if r3.status_code == 200:
            try:
                trades3 = r3.json().get("result", {}).get("trades", [])
                result["single_instrument_n_trades"] = len(trades3)
            except (json.JSONDecodeError, ValueError, AttributeError):
                pass

    # 4. Свечи перпа (для RV) -- пробуем правдоподобные пути.
    perp_candidates = [
        ("GET", "/public/get_tradehistory_candles", {"instrument_name": "BTC-PERP", "resolution": "3600",
                                                        "start_timestamp": ninety_days_ago_ms, "end_timestamp": now_ms}),
        ("GET", "/public/get_candles", {"instrument_name": "BTC-PERP", "resolution": "3600"}),
        ("GET", "/public/get_perp_history", {"currency": "BTC"}),
        ("GET", "/public/get_ticker", {"instrument_name": "BTC-PERP"}),
    ]
    perp_results = []
    for method, path, params in perp_candidates:
        try:
            rp = requests.get(f"{BASE}{path}", params=params, headers=HEADERS, timeout=15)
            perp_results.append({"path": path, "status": rp.status_code, "body_snippet": rp.text[:300]})
        except requests.exceptions.RequestException as exc:
            perp_results.append({"path": path, "exception": str(exc)[:200]})
        time.sleep(0.2)
    result["perp_candles_probe"] = perp_results

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: v for k, v in result.items() if k not in ("sample_instrument_full",)}, indent=2, ensure_ascii=False, default=str)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
