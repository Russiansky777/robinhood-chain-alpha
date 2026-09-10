#!/usr/bin/env python3
"""Задача E владельца (2026-09-10), ПЕРЕСОБРАНА с нуля со СТРУКТУРНЫМ
(не дисциплинарным) разделением по времени после реального инцидента
утечки (см. docstring заброшенного taskE_llm_forecast_fetch.py):
может ли LLM (Claude) дать более точную оценку вероятности, чем цена
рынка, на settled-рынках Polymarket?

Это Шаг 1. КОД ЭТОГО ФАЙЛА ФИЗИЧЕСКИ НЕ ССЫЛАЕТСЯ на поле
`outcomePrices` (или любое другое поле исхода) НИ РАЗУ -- не
запрашивает его, не парсит, не пишет никуда. Это не файловое
разделение (которое уже подводило), а структурная невозможность
утечки на уровне этого конкретного запуска: даже если харнесс потом
покажет diff этого файла кому-то, в нём физически нет исходов.

Реальные фильтры выборки (владелец, 2026-09-10, уточнено после
разбора первой попытки):
  - cutoff >= 2026-01-01 (владелец, 2026-09-10, уточнено: реальный порог
    знаний LLM -- январь 2026, не произвольный буфер поверх него;
    утечка из обучения зависит от даты СОБЫТИЯ относительно даты
    обучения, а не от файлового разделения кода само по себе)
  - ИСКЛЮЧИТЬ весь спорт И киберспорт (расширенный список: CS2,
    Valorant, League of Legends, Dota и подобные)
  - ИСКЛЮЧИТЬ крипто up/down с горизонтом <= 6 часов (шум цены, не
    рассуждение) -- горизонт парсится из реального паттерна слага
    Polymarket ("...-updown-5m-...", "...-updown-15m-...") или из
    реальной разницы startDate/eventStartTime -> endDate, когда есть.
  - ОСТАВИТЬ: макро (ставки ФРС, инфляция, занятость), регуляторика,
    политические назначения/решения, корпоративные события (сделки,
    IPO, отставки), крупные погодные/природные события -- положительный
    список ключевых слов, приоритет при выборе (не жёсткий фильтр,
    чтобы не остаться с пустой выборкой, если тема реже встречается).
  - 30-50 рынков (владелец) -- берём 40 (середина).

N_DAYS_BEFORE_RESOLUTION = 7 -- владелец не задал точное число,
явный документированный выбор (как и в прошлой попытке)."""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskE-step1/1.0"}
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

CUTOFF_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)  # владелец, 2026-09-10: сдвинуто с 1 июня --
# реальный порог знаний LLM (январь 2026, подтверждён системным контекстом сессии), не произвольная
# дата разрешения рынка -- утечка из обучения зависит от даты СОБЫТИЯ относительно cutoff тренировки,
# не от произвольного буфера поверх него.
N_DAYS_BEFORE_RESOLUTION = 7
MIN_LIFESPAN_DAYS = 5  # владелец, 2026-09-10: понижено с 8 после n_selected=0
TARGET_N_MARKETS = 40
RANDOM_SEED = 20260910
SHORT_CRYPTO_HORIZON_MAX_MINUTES = 360  # 6 часов, владелец

SPORTS_ESPORTS_KEYWORDS = (
    "nfl", "nba", "mlb", "nhl", "ncaa", "soccer", "football", "basketball",
    "baseball", "hockey", "ufc", "mma", "tennis", "golf", "boxing", "epl",
    "premier league", "champions league", "olympics", "world cup", "cricket",
    "rugby", "formula 1", "nascar", "wins the game",
    # 2026-09-10, расширено владельцем -- киберспорт пропущен в первой версии:
    "cs2", "csgo", "counter-strike", "valorant", "league of legends", "dota",
    "dota 2", "overwatch", "rocket league", "starcraft", "fortnite", "esports",
    "esport", "call of duty", "rainbow six", "apex legends", "warzone",
    "hltv", "map winner", "maps won", "best-of-", " bo3", " bo5",
)
SPORTS_ESPORTS_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in SPORTS_ESPORTS_KEYWORDS) + r")\b", re.IGNORECASE)

CRYPTO_UPDOWN_SLUG_RE = re.compile(r"updown-(\d+)(m|h)-", re.IGNORECASE)

POSITIVE_TOPIC_KEYWORDS = (
    "fed", "federal reserve", "interest rate", "rate cut", "rate hike", "inflation",
    "cpi", "unemployment", "jobs report", "nonfarm payroll", "fomc",
    "congress", "senate", "supreme court", "election", "president", "governor",
    "nominee", "confirm", "impeach", "resign", "ceo", "merger", "acquisition",
    "ipo", "bankruptcy", "sec ", "fda", "regulation", "tariff", "sanctions",
    "hurricane", "earthquake", "wildfire", "flood", "storm", "tornado",
)
POSITIVE_TOPIC_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in POSITIVE_TOPIC_KEYWORDS) + r")\b", re.IGNORECASE)

OUT_PATH = Path("data/p3_guard_cache/taskE_step1_questions_context.json")
OUT_DIAG = Path("data/p3_guard_cache/taskE_step1_diagnostics.json")


def fetch_closed_markets(max_pages: int = 40, page_size: int = 100) -> list[dict]:
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


def is_sports_or_esports(market: dict) -> bool:
    hay = " ".join(str(market.get(k, "")) for k in ("question", "slug", "description")).lower()
    return bool(SPORTS_ESPORTS_RE.search(hay))


def is_short_horizon_crypto(market: dict) -> bool:
    # 2026-09-10, реальная находка: паттерн слага "-updown-Nm-" ловил
    # только ОДНУ семью рынков (5/15-минутные) -- реальная выборка
    # оказалась сплошь заполнена ДРУГОЙ семьёй ("Will the price of
    # Bitcoin be above/between $X on <today's date>") без такого слага,
    # но по сути тот же "шум цены за короткий срок", который владелец
    # просил исключить. Расширяем: любой вопрос про крипто-цену
    # (bitcoin/ethereum/solana/bnb/xrp + above/below/between/up or down)
    # -- проверяем РЕАЛЬНЫЙ горизонт (startDate/eventStartTime -> endDate),
    # исключаем, если <=6ч ИЛИ горизонт неизвестен, но паттерн явно
    # "на сегодня" (та же календарная дата в вопросе, что publish-день).
    slug = str(market.get("slug", ""))
    question = str(market.get("question", "")).lower()
    m = CRYPTO_UPDOWN_SLUG_RE.search(slug)
    if m:
        value, unit = int(m.group(1)), m.group(2).lower()
        minutes = value if unit == "m" else value * 60
        if minutes <= SHORT_CRYPTO_HORIZON_MAX_MINUTES:
            return True

    crypto_asset = any(kw in question for kw in ("bitcoin", "ethereum", "solana", " btc", " eth ", "bnb", " xrp", "dogecoin"))
    price_pattern = any(kw in question for kw in ("above $", "below $", "between $", "up or down", "up or down?"))
    if not (crypto_asset and price_pattern):
        return False

    start_str = market.get("eventStartTime") or market.get("startDate")
    end_str = market.get("endDate")
    if start_str and end_str:
        try:
            start_dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
            horizon_minutes = (end_dt - start_dt).total_seconds() / 60
            return horizon_minutes <= SHORT_CRYPTO_HORIZON_MAX_MINUTES
        except (ValueError, TypeError):
            pass
    # Реальный горизонт неизвестен -- честно исключаем ЛЮБОЙ crypto-price
    # вопрос без надёжного горизонта, а не гадаем: смысл фильтра --
    # оставить только НЕ-крипто-ценовые вопросы, крипто-цена систематически
    # оказывается шумом независимо от точного часа.
    return True


def matches_positive_topic(market: dict) -> bool:
    hay = " ".join(str(market.get(k, "")) for k in ("question", "slug", "description")).lower()
    return bool(POSITIVE_TOPIC_RE.search(hay))


_price_snapshot_failure_samples: list[dict] = []  # реальная диагностика первых неудач, не молчим


def fetch_price_snapshot(clob_token_id: str, end_date: datetime) -> float | None:
    """Владелец, 2026-09-10 (точечная правка после n_selected=0): вместо
    жёсткой точки ровно на N_DAYS_BEFORE_RESOLUTION -- ближайшая
    реально доступная точка prices-history в окне [3; 14] дней до
    разрешения, предпочтение -- ближе к 7 дням. Один реальный запрос
    на весь window, не гадаем узкий 24-часовой срез заранее."""
    window_start_ts = int((end_date - timedelta(days=14)).timestamp())
    window_end_ts = int((end_date - timedelta(days=3)).timestamp())
    preferred_ts = int((end_date - timedelta(days=N_DAYS_BEFORE_RESOLUTION)).timestamp())
    try:
        r = requests.get(f"{CLOB_BASE}/prices-history", params={
            "market": clob_token_id, "startTs": window_start_ts, "endTs": window_end_ts, "fidelity": 60,
        }, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            if len(_price_snapshot_failure_samples) < 5:
                _price_snapshot_failure_samples.append({"status": r.status_code, "body": r.text[:300]})
            return None
        body = r.json()
        history = body.get("history", [])
        if not history:
            if len(_price_snapshot_failure_samples) < 5:
                _price_snapshot_failure_samples.append({"status": 200, "body_keys": list(body.keys()), "empty_history": True,
                                                          "window_start_ts": window_start_ts, "window_end_ts": window_end_ts})
            return None
        # Реальная ближайшая к предпочтительному моменту точка (по |t - preferred_ts|).
        best = min(history, key=lambda pt: abs(pt.get("t", preferred_ts) - preferred_ts))
        return float(best["p"])
    except Exception as exc:  # noqa: BLE001
        if len(_price_snapshot_failure_samples) < 5:
            _price_snapshot_failure_samples.append({"exception": str(exc)[:300]})
        return None


def run() -> int:
    diag: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "cutoff_date": CUTOFF_DATE.isoformat(), "n_days_before_resolution": N_DAYS_BEFORE_RESOLUTION}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"=== Реальный fetch closed Polymarket рынков, окно >= {CUTOFF_DATE.date()} ===")
    markets = fetch_closed_markets()
    diag["n_closed_markets_in_window"] = len(markets)
    print(f"[taskE_step1] реальных closed-рынков: {len(markets)}")

    no_sports = [m for m in markets if not is_sports_or_esports(m)]
    diag["n_after_sports_esports_exclusion"] = len(no_sports)
    print(f"[taskE_step1] после исключения спорт+киберспорт: {len(no_sports)}/{len(markets)}")

    no_short_crypto = [m for m in no_sports if not is_short_horizon_crypto(m)]
    diag["n_after_short_crypto_exclusion"] = len(no_short_crypto)
    print(f"[taskE_step1] после исключения крипто up/down <=6ч: {len(no_short_crypto)}/{len(no_sports)}")

    quality = []
    for m in no_short_crypto:
        vol = m.get("volumeNum") or 0
        tokens_raw = m.get("clobTokenIds")
        try:
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except (json.JSONDecodeError, TypeError):
            tokens = None
        outcomes_raw = m.get("outcomes")  # названия исходов ("Yes"/"No") -- НЕ значения, безопасно
        try:
            outcomes_names = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (json.JSONDecodeError, TypeError):
            outcomes_names = None
        if vol < 1000 or not tokens or not outcomes_names:
            continue
        # 2026-09-10, владелец: точечная правка -- порог понижен с
        # N_DAYS_BEFORE_RESOLUTION+1 (8) до MIN_LIFESPAN_DAYS (5), и
        # снимок цены теперь ищет ближайшую точку в окне [3;14] дней
        # (см. fetch_price_snapshot), не жёстко на 7-й день.
        start_str = m.get("eventStartTime") or m.get("startDate")
        end_str = m.get("endDate")
        if start_str and end_str:
            try:
                start_dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
                lifespan_days = (end_dt - start_dt).total_seconds() / 86400
                if lifespan_days < MIN_LIFESPAN_DAYS:
                    continue
            except (ValueError, TypeError):
                continue  # даты не распознались -- честно не считаем "достаточно долгоживущим"
        else:
            continue  # нет дат старта -- не можем проверить срок жизни, не гадаем
        quality.append(m)
    diag["n_after_quality_filter"] = len(quality)
    print(f"[taskE_step1] после фильтра качества (объём>=$1000, есть clobTokenIds): {len(quality)}")

    positive = [m for m in quality if matches_positive_topic(m)]
    other = [m for m in quality if not matches_positive_topic(m)]
    diag["n_positive_topic_matches"] = len(positive)
    print(f"[taskE_step1] реально совпавших с целевыми темами (макро/регуляторика/политика/корпоративное/погода): "
          f"{len(positive)}/{len(quality)}")

    random.seed(RANDOM_SEED)
    random.shuffle(positive)
    random.shuffle(other)
    selected = (positive + other)[:TARGET_N_MARKETS]
    diag["n_selected"] = len(selected)
    diag["n_selected_from_positive_topics"] = min(len(positive), TARGET_N_MARKETS)
    print(f"[taskE_step1] реально выбрано: {len(selected)} (из них по целевым темам: "
          f"{diag['n_selected_from_positive_topics']})")

    context_items = []
    n_price_ok = 0
    for m in selected:
        slug = m["slug"]
        end_date_str = m.get("endDate")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            continue
        tokens_raw = m.get("clobTokenIds")
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        outcomes_names_raw = m.get("outcomes")
        outcomes_names = json.loads(outcomes_names_raw) if isinstance(outcomes_names_raw, str) else outcomes_names_raw

        price_snapshot = fetch_price_snapshot(tokens[0], end_date) if tokens else None
        if price_snapshot is not None:
            n_price_ok += 1
        time.sleep(0.15)

        # ТОЛЬКО контекст -- ни одного поля исхода здесь и не может
        # быть, потому что этот код его никогда не запрашивал.
        context_items.append({
            "slug": slug, "question": m.get("question"), "description": m.get("description"),
            "end_date": end_date_str, "outcome_names": outcomes_names,  # названия ("Yes"/"No"), не значения
            "price_snapshot_date": (end_date - timedelta(days=N_DAYS_BEFORE_RESOLUTION)).isoformat(),
            "market_price_first_outcome_n_days_before": price_snapshot,
            "volume": m.get("volumeNum"),
        })

    diag["n_price_snapshot_ok"] = n_price_ok
    diag["price_snapshot_failure_samples"] = _price_snapshot_failure_samples
    print(f"[taskE_step1] реальных снимков цены получено: {n_price_ok}/{len(selected)}")

    OUT_PATH.write_text(json.dumps({
        "meta": {"cutoff_date": CUTOFF_DATE.isoformat(), "n_days_before_resolution": N_DAYS_BEFORE_RESOLUTION,
                  "random_seed": RANDOM_SEED, "n_markets": len(context_items),
                  "llm_estimates": "ЗАПОЛНЯЕТСЯ ОТДЕЛЬНО, ВРУЧНУЮ, ПОСЛЕ этого коммита -- см. поле 'llm_probability_estimate' "
                                    "в каждой записи markets[], изначально null"},
        "markets": [{**c, "llm_probability_estimate": None, "llm_reasoning_brief": None} for c in context_items],
    }, indent=2, ensure_ascii=False, default=str))
    OUT_DIAG.write_text(json.dumps(diag, indent=2, ensure_ascii=False, default=str))
    print(f"\n[taskE_step1] записано {OUT_PATH} -- поля llm_probability_estimate/llm_reasoning_brief ПУСТЫЕ, "
          "заполняются отдельным шагом (LLM читает этот файл, формирует оценки, коммитит).")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
