#!/usr/bin/env python3
"""Задача F, Шаг 1b -- реальный (бесплатный, публичный API, БЕЗ
капитала) сбор near-ATM опционных сделок Derive/Lyra за 90 дней, для
последующего backing-out implied volatility (Black-Scholes) по каждой
сделке. НЕ дорогой шаг (в деньгах) -- Derive public API без авторизации
и без лимита кредитов, но потенциально ДОЛГИЙ (много страниц пагинации
назад по времени) -- чекпоинтится тем же паттерном git push-retry, что
уже использовался для дорогих шагов (taskD, ретро-сторожок), т.к.
процесс может быть прерван по таймауту джоба и резюмируется по
сохранённому курсору.

Near-ATM определение -- ТО ЖЕ, что в Шаге 0c (taskF_derive_liquidity_
probe.py, уже подтверждено реальными данными): экспирация 7-30 дней от
момента сделки, |strike-index_price|/index_price <= 15%. Инструмент
кодирует все нужные поля в имени: CCY-YYYYMMDD-STRIKE-C/P (подтверждено
Шагом 0b)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskF-trade-fetch/1.0", "Accept": "application/json"}
BASE = "https://api.lyra.finance"

WINDOW_DAYS = int(os.environ.get("TASKF_WINDOW_DAYS", "90"))
CURRENCIES = os.environ.get("TASKF_CURRENCIES", "BTC,ETH").split(",")
MAX_PAGES_PER_RUN = int(os.environ.get("TASKF_MAX_PAGES_PER_RUN", "800"))  # предохранитель на один прогон джоба
CHECKPOINT_EVERY_N_PAGES = 25

OUT_PATH = Path("data/p3_guard_cache/taskF_derive_near_atm_trades.json")
DIAG_PATH = Path("data/p3_guard_cache/taskF_derive_trade_fetch_diagnostics.json")


def git_checkpoint(message: str) -> None:
    try:
        subprocess.run(["git", "add", str(OUT_PATH), str(DIAG_PATH)], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if diff.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message], check=False)
        for attempt in range(5):
            push = subprocess.run(["git", "push"], check=False)
            if push.returncode == 0:
                return
            print(f"[taskF_fetch] push чекпоинта отклонён, попытка {attempt + 1}/5 -- pull --rebase и повтор")
            subprocess.run(["git", "pull", "--rebase"], check=False)
            time.sleep(3)
        print("[taskF_fetch] чекпоинт НЕ запушился после 5 попыток")
    except Exception as exc:  # noqa: BLE001
        print(f"[taskF_fetch] чекпоинт-коммит не удался (не критично, продолжаем): {exc}")


def is_near_atm(trade: dict) -> tuple[bool, dict | None]:
    try:
        parts = trade["instrument_name"].split("-")
        if len(parts) != 4:
            return False, None
        _, expiry_str, strike_str, opt_type = parts
        expiry_dt = datetime.strptime(expiry_str, "%Y%m%d").replace(tzinfo=timezone.utc)
        trade_dt = datetime.fromtimestamp(trade["timestamp"] / 1000, tz=timezone.utc)
        days_to_expiry = (expiry_dt - trade_dt).total_seconds() / 86400
        strike = float(strike_str)
        index_price = float(trade["index_price"])
        if index_price <= 0:
            return False, None
        pct_from_atm = abs(strike - index_price) / index_price
        if not (7 <= days_to_expiry <= 30 and pct_from_atm <= 0.15):
            return False, None
        return True, {
            "instrument_name": trade["instrument_name"], "timestamp": trade["timestamp"],
            "trade_price": trade["trade_price"], "trade_amount": trade["trade_amount"],
            "index_price": trade["index_price"], "direction": trade["direction"],
            "expiry_str": expiry_str, "strike": strike, "option_type": opt_type,
            "days_to_expiry_at_trade": days_to_expiry,
        }
    except (KeyError, ValueError, IndexError, TypeError):
        return False, None


def run() -> int:
    now_ms = int(time.time() * 1000)
    from_ts_ms = now_ms - WINDOW_DAYS * 86400 * 1000

    state = {"currencies": {}}
    if DIAG_PATH.exists():
        try:
            state = json.loads(DIAG_PATH.read_text())
            print(f"[taskF_fetch] найден диагностический файл -- резюмируем прогресс")
        except json.JSONDecodeError:
            pass
    state.setdefault("currencies", {})

    near_atm_by_currency: dict[str, list[dict]] = {}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text())
            near_atm_by_currency = existing.get("near_atm_trades", {})
            print(f"[taskF_fetch] найдены уже сохранённые near-ATM сделки: "
                  f"{ {k: len(v) for k, v in near_atm_by_currency.items()} }")
        except json.JSONDecodeError:
            pass

    total_pages_this_run = 0
    for currency in CURRENCIES:
        cstate = state["currencies"].setdefault(currency, {
            "cur_to_ms": now_ms, "n_pages_total": 0, "n_trades_total": 0,
            "n_near_atm_total": len(near_atm_by_currency.get(currency, [])),
            "reached_from_ts": False, "stop_reason": None,
        })
        if cstate.get("reached_from_ts"):
            print(f"[taskF_fetch] {currency}: уже дошли до начала окна ранее -- пропускаем")
            continue
        near_atm_by_currency.setdefault(currency, [])
        print(f"[taskF_fetch] {currency}: старт/резюме с cur_to={cstate['cur_to_ms']}, "
              f"уже страниц={cstate['n_pages_total']}, near-ATM накоплено={cstate['n_near_atm_total']}")

        while total_pages_this_run < MAX_PAGES_PER_RUN:
            try:
                r = requests.get(f"{BASE}/public/get_trade_history", params={
                    "currency": currency, "instrument_type": "option",
                    "from_timestamp": from_ts_ms, "to_timestamp": cstate["cur_to_ms"], "page_size": 100,
                }, headers=HEADERS, timeout=20)
            except requests.exceptions.RequestException as exc:
                cstate["stop_reason"] = f"exception_{str(exc)[:100]}"
                print(f"[taskF_fetch] {currency}: исключение, останов страницы -- {exc}")
                break
            total_pages_this_run += 1
            cstate["n_pages_total"] += 1
            if r.status_code != 200:
                cstate["stop_reason"] = f"http_{r.status_code}"
                print(f"[taskF_fetch] {currency}: HTTP {r.status_code} -- {r.text[:200]}")
                break
            trades = r.json().get("result", {}).get("trades", [])
            if not trades:
                cstate["stop_reason"] = "empty_page"
                cstate["reached_from_ts"] = True  # пустая страница раньше окна -- реально дошли до конца доступной истории
                break
            cstate["n_trades_total"] += len(trades)
            for t in trades:
                ok, rec = is_near_atm(t)
                if ok:
                    near_atm_by_currency[currency].append(rec)
                    cstate["n_near_atm_total"] += 1
            oldest_ts = min(t["timestamp"] for t in trades)
            if oldest_ts <= from_ts_ms:
                cstate["stop_reason"] = "reached_from_ts"
                cstate["reached_from_ts"] = True
                break
            cstate["cur_to_ms"] = oldest_ts - 1
            if len(trades) < 100:
                cstate["stop_reason"] = "short_page"
                cstate["reached_from_ts"] = True  # короткая страница -- реально меньше 100 сделок раньше этой точки, конец истории
                break

            if cstate["n_pages_total"] % CHECKPOINT_EVERY_N_PAGES == 0:
                OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
                OUT_PATH.write_text(json.dumps({"near_atm_trades": near_atm_by_currency}, indent=2, ensure_ascii=False, default=str))
                DIAG_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str))
                git_checkpoint(f"Задача F: чекпоинт сбора near-ATM сделок, {currency} стр.{cstate['n_pages_total']} [automated]")
                print(f"[taskF_fetch] {currency}: чекпоинт стр.{cstate['n_pages_total']}, "
                      f"near-ATM={cstate['n_near_atm_total']}, oldest_reached={datetime.fromtimestamp(cstate['cur_to_ms']/1000, tz=timezone.utc)}")
            time.sleep(0.15)
        else:
            cstate["stop_reason"] = cstate.get("stop_reason") or "max_pages_this_run_reached"

        print(f"[taskF_fetch] {currency}: итог прогона -- страниц всего={cstate['n_pages_total']}, "
              f"near-ATM всего={cstate['n_near_atm_total']}, reached_from_ts={cstate['reached_from_ts']}, "
              f"stop_reason={cstate['stop_reason']}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({"near_atm_trades": near_atm_by_currency}, indent=2, ensure_ascii=False, default=str))
    state["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["window_days"] = WINDOW_DAYS
    DIAG_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str))
    git_checkpoint(f"Задача F: финальный чекпоинт сбора near-ATM сделок [automated]")

    all_done = all(state["currencies"].get(c, {}).get("reached_from_ts") for c in CURRENCIES)
    print(f"\n[taskF_fetch] ИТОГО: {'ВСЕ валюты дошли до начала {}д-окна'.format(WINDOW_DAYS) if all_done else 'НЕ все валюты дошли до конца окна за этот прогон -- нужен повторный запуск для резюме'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
