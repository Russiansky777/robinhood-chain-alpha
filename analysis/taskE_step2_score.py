#!/usr/bin/env python3
"""Задача E владельца (2026-09-10), Шаг 2 -- ОТДЕЛЬНЫЙ, ПОЗДНИЙ запуск,
только после того как коммит Шага 1 (с уже заполненными
llm_probability_estimate) виден в истории git. Реальные исходы
запрашиваются ЗДЕСЬ и ТОЛЬКО здесь, сравниваются с УЖЕ
ЗАКОММИЧЕННЫМИ (неизменяемыми на момент этого запуска) оценками LLM.

Владелец, дословно: "Code получает только готовое число Brier score
на выходе, не сырые исходы" -- этот скрипт печатает и записывает В
ИТОГОВЫЙ ФАЙЛ только агрегаты (Brier LLM, Brier рынка, N, лучше ли
LLM рынка) -- НИ ОДНОГО поля per-market с фактическим исходом или
даже булевым "угадал/не угадал" (это тоже раскрывало бы исход) не
попадает в результат, который будет прочитан в основной сессии."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskE-step2/1.0"}
GAMMA_BASE = "https://gamma-api.polymarket.com"

IN_PATH = Path("data/p3_guard_cache/taskE_step1_questions_context.json")
OUT_PATH = Path("data/p3_guard_cache/taskE_step2_brier_result.json")


def fetch_outcome_prices(slug: str) -> list[float] | None:
    # 2026-09-10, диагностика (не патч): n_fetch_failed=28/28 в первом
    # реальном запуске -- логируем ТОЛЬКО статус-код и форму ответа
    # (тип/длина списка, есть ли ключ outcomePrices), НИКОГДА не сами
    # значения outcomePrices -- это раскрыло бы исход прямо в логах.
    r = requests.get(f"{GAMMA_BASE}/markets", params={"slug": slug}, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        print(f"[taskE_step2][diag] slug={slug} status={r.status_code} (не 200)")
        return None
    body = r.json()
    if not isinstance(body, list) or not body:
        print(f"[taskE_step2][diag] slug={slug} status=200 body_type={type(body).__name__} "
              f"body_len={len(body) if isinstance(body, list) else 'n/a'} -- пустой список")
        return None
    has_key = "outcomePrices" in body[0]
    raw = body[0].get("outcomePrices")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        result = [float(p) for p in prices] if prices else None
        if result is None:
            print(f"[taskE_step2][diag] slug={slug} status=200 has_outcomePrices_key={has_key} "
                  f"raw_type={type(raw).__name__} -- распарсилось в None/пусто")
        return result
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"[taskE_step2][diag] slug={slug} status=200 has_outcomePrices_key={has_key} "
              f"raw_type={type(raw).__name__} parse_error={exc.__class__.__name__}")
        return None


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not IN_PATH.exists():
        result["blocker"] = f"{IN_PATH} не найден -- Шаг 1 не выполнен."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"[taskE_step2] {result['blocker']}")
        return 1

    data = json.loads(IN_PATH.read_text())
    markets = data.get("markets", [])
    n_missing_estimate = sum(1 for m in markets if m.get("llm_probability_estimate") is None)
    if n_missing_estimate:
        result["blocker"] = (f"{n_missing_estimate}/{len(markets)} рынков без llm_probability_estimate -- "
                              "Шаг 1 (LLM-оценки) ещё не завершён для всех, останавливаюсь.")
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"[taskE_step2] {result['blocker']}")
        return 1

    n_scored = 0
    sum_sq_err_llm = 0.0
    sum_sq_err_market = 0.0
    n_llm_missing_price = 0
    n_fetch_failed = 0
    n_llm_better = 0

    for m in markets:
        outcome_prices = fetch_outcome_prices(m["slug"])
        time.sleep(0.15)
        if not outcome_prices:
            n_fetch_failed += 1
            continue
        actual_first_outcome = outcome_prices[0]  # 1.0 или 0.0 -- НЕ печатается, НЕ сохраняется per-market
        llm_p = m["llm_probability_estimate"]
        market_p = m.get("market_price_first_outcome_n_days_before")

        sq_err_llm = (llm_p - actual_first_outcome) ** 2
        sum_sq_err_llm += sq_err_llm
        n_scored += 1

        if market_p is not None:
            sq_err_market = (market_p - actual_first_outcome) ** 2
            sum_sq_err_market += sq_err_market
            if sq_err_llm < sq_err_market:
                n_llm_better += 1
        else:
            n_llm_missing_price += 1

    n_market_comparable = n_scored - n_llm_missing_price
    result["n_markets_total"] = len(markets)
    result["n_fetch_failed"] = n_fetch_failed
    result["n_scored"] = n_scored
    result["n_market_price_available_for_comparison"] = n_market_comparable
    result["llm_brier_score"] = (sum_sq_err_llm / n_scored) if n_scored else None
    result["market_brier_score"] = (sum_sq_err_market / n_market_comparable) if n_market_comparable else None
    result["frac_markets_llm_beat_market"] = (n_llm_better / n_market_comparable) if n_market_comparable else None
    result["llm_beats_market_overall"] = (
        result["llm_brier_score"] is not None and result["market_brier_score"] is not None
        and result["llm_brier_score"] < result["market_brier_score"]
    )
    # Намеренно: НИ ОДНОГО per-market поля с фактическим исходом или
    # даже булевым "угадал" не пишется в result -- только агрегаты.

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"[taskE_step2] n_scored={n_scored}, LLM Brier={result['llm_brier_score']}, "
          f"market Brier={result['market_brier_score']}, LLM лучше рынка в целом: {result['llm_beats_market_overall']}")
    print(f"[taskE_step2] записано {OUT_PATH} -- только агрегаты, без сырых исходов.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
