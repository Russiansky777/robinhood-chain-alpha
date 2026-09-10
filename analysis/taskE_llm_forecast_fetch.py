#!/usr/bin/env python3
"""Задача E владельца (2026-09-10, дана отдельно от Задач C/D, начата
заново после потери при передаче): может ли LLM (Claude) дать более
точную оценку вероятности по settled-рынкам Polymarket, чем цена
рынка? Метод (владелец, дословно):

  30-50 settled-рынков Polymarket, события ПОСЛЕ 2026-06-01 (жёстко --
  иначе утечка из обучения), исход решала публичная информация (НЕ
  спорт), оценка по контексту доступному ДО разрешения (срез цены за
  N дней до через prices-history), сравнение (LLM-оценка) и
  (рыночная цена) с фактическим исходом по Brier score.

КРИТИЧНО для честности теста: этот скрипт делит результат на ДВА
раздельных файла -- (1) вопросы+контекст+рыночная цена ДО разрешения,
БЕЗ исхода; (2) реальные исходы отдельно. LLM (следующий шаг, ручной,
не в этом скрипте) читает ТОЛЬКО файл (1) и формирует оценки ДО того,
как увидит файл (2) -- так же, как во всём остальном проекте
разделяются predict/verify для честного OOS.

N_DAYS_BEFORE_RESOLUTION = 7 -- владелец не задал точное число,
явный, задокументированный выбор (не "N дней" абстрактно)."""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskE-llm-forecast/1.0"}
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

CUTOFF_DATE = datetime(2026, 6, 1, tzinfo=timezone.utc)  # владелец: жёстко, иначе утечка из обучения
N_DAYS_BEFORE_RESOLUTION = 7
TARGET_N_MARKETS = 40  # владелец: "30-50", берём середину
RANDOM_SEED = 20260910  # владелец, 2026-09-10 -- воспроизводимый выбор, не перебираем заново при желании "получше"

SPORTS_KEYWORDS = ("nfl", "nba", "mlb", "nhl", "ncaa", "soccer", "football", "basketball",
                    "baseball", "hockey", "ufc", "mma", "tennis", "golf", "boxing", "epl",
                    "premier league", "champions league", "olympics", "world cup", "cricket",
                    "rugby", "formula 1", "nascar", "wins the game")
# 2026-09-10: "f1" и "vs." убраны -- короткие/пунктуационные ключи дают
# реальные ложные срабатывания (см. is_sports docstring) и плохо
# работают с границей слова \b (точка -- не словообразующий символ).

OUT_QUESTIONS = Path("data/p3_guard_cache/taskE_questions_sealed.json")
OUT_OUTCOMES = Path("data/p3_guard_cache/taskE_outcomes_sealed.json")
OUT_DIAG = Path("data/p3_guard_cache/taskE_fetch_diagnostics.json")


def fetch_closed_markets(max_pages: int = 40, page_size: int = 100) -> list[dict]:
    """Реальный bulk-fetch closed-рынков в окне [CUTOFF_DATE; now] --
    тот же проверенный паттерн (end_date_min/max, snake_case), что
    taskC_sports_matcher.fetch_polymarket_bulk."""
    out = {}
    now = datetime.now(timezone.utc)
    window_min = CUTOFF_DATE.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_max = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for offset in range(0, max_pages * page_size, page_size):
        r = requests.get(f"{GAMMA_BASE}/markets", params={
            "limit": page_size, "offset": offset, "closed": "true",
            "order": "endDate", "ascending": "false",
            "end_date_min": window_min, "end_date_max": window_max,
        }, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            break
        body = r.json()
        if not isinstance(body, list) or not body:
            break
        for m in body:
            if m.get("slug"):
                out[m["slug"]] = m
        if len(body) < page_size:
            break
        time.sleep(0.2)
    return list(out.values())


SPORTS_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in SPORTS_KEYWORDS) + r")\b", re.IGNORECASE)


def is_sports(market: dict) -> bool:
    """2026-09-10, реальный найденный баг: прежняя версия дампила ВЕСЬ
    вложенный market['events'] через json.dumps() в общий "хаистек" и
    искала ключевые слова НАИВНОЙ подстрокой -- короткий ключ "f1"
    совпадал с случайными hex-подстроками внутри длинных
    conditionId/clobTokenIds/questionID и т.п. полей ("...af1..."),
    что дало n_non_sports=0 из 2100 (100% ложных срабатываний).
    Реальная схема Polymarket НЕ содержит category/tags вообще (см.
    sample_raw_market_fields) -- используем ТОЛЬКО читаемые текстовые
    поля самого рынка (question/slug/description) и НЕ дампим
    произвольные вложенные структуры; ищем по границе слова (\\b),
    не по голой подстроке."""
    hay = " ".join(str(market.get(k, "")) for k in ("question", "slug", "description")).lower()
    return bool(SPORTS_KEYWORD_RE.search(hay))


def fetch_price_snapshot(clob_token_id: str, target_ts: int) -> float | None:
    """Реальная цена (implied probability) из CLOB prices-history на
    заданный unix-timestamp (или ближайшая ДО него точка). Эндпоинт
    /prices-history?market=<clobTokenId>&startTs=&endTs=&fidelity= --
    реально проверяем живьём, не гадаем формат ответа заранее."""
    try:
        r = requests.get(f"{CLOB_BASE}/prices-history", params={
            "market": clob_token_id, "startTs": target_ts - 86400, "endTs": target_ts, "fidelity": 60,
        }, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        history = r.json().get("history", [])
        if not history:
            return None
        return float(history[-1]["p"])  # последняя точка до целевого момента
    except Exception:  # noqa: BLE001
        return None


def run() -> int:
    result_diag: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "cutoff_date": CUTOFF_DATE.isoformat(), "n_days_before_resolution": N_DAYS_BEFORE_RESOLUTION}
    OUT_QUESTIONS.parent.mkdir(parents=True, exist_ok=True)

    print(f"=== Реальный fetch closed Polymarket рынков, окно >= {CUTOFF_DATE.date()} ===")
    markets = fetch_closed_markets()
    print(f"[taskE_fetch] реальных closed-рынков в окне: {len(markets)}")
    result_diag["n_closed_markets_in_window"] = len(markets)
    if markets:
        result_diag["sample_raw_market_fields"] = sorted(markets[0].keys())
        print(f"[taskE_fetch] реальные поля первой записи: {result_diag['sample_raw_market_fields']}")

    non_sports = [m for m in markets if not is_sports(m)]
    print(f"[taskE_fetch] реально не-спортивных (по ключевым словам, честная эвристика): {len(non_sports)}/{len(markets)}")
    result_diag["n_non_sports"] = len(non_sports)

    # Реальный фильтр качества: нужен реальный объём (не микро-рынок,
    # где цена не отражает согласованное мнение) и реальный clobTokenIds
    # для дальнейшего prices-history.
    candidates = []
    for m in non_sports:
        vol = m.get("volumeNum") or 0
        tokens_raw = m.get("clobTokenIds")
        try:
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except (json.JSONDecodeError, TypeError):
            tokens = None
        outcomes_raw = m.get("outcomes")
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (json.JSONDecodeError, TypeError):
            outcomes = None
        if vol < 1000 or not tokens or not outcomes:
            continue
        candidates.append(m)
    print(f"[taskE_fetch] реально прошедших фильтр качества (объём>=$1000, есть clobTokenIds+outcomes): {len(candidates)}")
    result_diag["n_candidates_after_quality_filter"] = len(candidates)

    if len(candidates) < TARGET_N_MARKETS:
        result_diag["blocker"] = (f"Реальных кандидатов ({len(candidates)}) меньше целевых {TARGET_N_MARKETS} -- "
                                   "берём все, что есть, честно помечаем меньшую выборку.")
        print(f"[taskE_fetch] {result_diag['blocker']}")

    random.seed(RANDOM_SEED)
    selected = random.sample(candidates, min(TARGET_N_MARKETS, len(candidates)))
    print(f"[taskE_fetch] реально выбрано (seed={RANDOM_SEED}): {len(selected)}")

    questions_sealed = []
    outcomes_sealed = {}
    n_price_snapshot_ok = 0
    for m in selected:
        slug = m["slug"]
        end_date_str = m.get("endDate")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            continue
        target_ts = int((end_date - timedelta(days=N_DAYS_BEFORE_RESOLUTION)).timestamp())

        tokens_raw = m.get("clobTokenIds")
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        outcomes_list = m.get("outcomes")
        outcomes_list = json.loads(outcomes_list) if isinstance(outcomes_list, str) else outcomes_list

        # Цена ПЕРВОГО исхода (обычно "Yes" для бинарных рынков) за
        # N дней до разрешения -- реальный market-implied prior на тот момент.
        price_snapshot = fetch_price_snapshot(tokens[0], target_ts) if tokens else None
        if price_snapshot is not None:
            n_price_snapshot_ok += 1
        time.sleep(0.15)

        questions_sealed.append({
            "slug": slug, "question": m.get("question"), "description": m.get("description"),
            "category": m.get("category"), "end_date": end_date_str, "outcomes": outcomes_list,
            "price_snapshot_date": (end_date - timedelta(days=N_DAYS_BEFORE_RESOLUTION)).isoformat(),
            "market_price_first_outcome_n_days_before": price_snapshot,
            "volume": m.get("volumeNum"),
        })

        # ИСХОД -- outcomePrices на closed-рынке реально отражает финальный
        # результат (напр. ["1","0"] = первый исход выиграл). Уходит ТОЛЬКО
        # в отдельный запечатанный файл, не в questions_sealed.
        outcome_prices_raw = m.get("outcomePrices")
        try:
            outcome_prices = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
        except (json.JSONDecodeError, TypeError):
            outcome_prices = None
        outcomes_sealed[slug] = {"outcome_prices_final": outcome_prices, "outcomes": outcomes_list}

    print(f"[taskE_fetch] реальных снимков цены за {N_DAYS_BEFORE_RESOLUTION}д до разрешения получено: "
          f"{n_price_snapshot_ok}/{len(selected)}")
    result_diag["n_selected"] = len(selected)
    result_diag["n_price_snapshot_ok"] = n_price_snapshot_ok

    OUT_QUESTIONS.write_text(json.dumps({"meta": {"cutoff_date": CUTOFF_DATE.isoformat(),
                                                    "n_days_before_resolution": N_DAYS_BEFORE_RESOLUTION,
                                                    "random_seed": RANDOM_SEED, "n_markets": len(questions_sealed)},
                                          "markets": questions_sealed}, indent=2, ensure_ascii=False, default=str))
    OUT_OUTCOMES.write_text(json.dumps(outcomes_sealed, indent=2, ensure_ascii=False, default=str))
    OUT_DIAG.write_text(json.dumps(result_diag, indent=2, ensure_ascii=False, default=str))
    print(f"\n[taskE_fetch] ЗАПЕЧАТАНО: {OUT_QUESTIONS} (вопросы+контекст, БЕЗ исхода) и {OUT_OUTCOMES} (исходы, "
          f"НЕ читать до формирования оценок LLM).")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
