#!/usr/bin/env python3
"""Задача E владельца (2026-09-10), ДОКАЗАТЕЛЬНЫЙ повторный прогон.

Первый прогон (cutoff >= 2026-01-01, окно снимка [0.20;0.80]) дал
LLM Brier 0.098 против рыночных 0.221 на подмножестве -- но владелец
пометил это ПРЕДВАРИТЕЛЬНЫМ по трём причинам: (1) все рынки разрешались
1 января 2026 -- вплотную к порогу знаний модели, риск утечки через
память; (2) 18/33 -- один кластер одной недели Spotify-чарта, не
независимые наблюдения; (3) подмножество "есть преимущество" было
выделено ПОСЛЕ чтения вопросов -- пост-хок, потенциально смещённая
классификация.

Этот прогон устраняет все три причины СТРУКТУРНО:
  (1) окно разрешения 2026-03-01 -- 2026-08-31 -- на 2-8 месяцев дальше
      от порога знаний (январь 2026), не рядом с границей;
  (2) антикластер -- не более MAX_PER_FAMILY_PER_WEEK рынков на одно
      событие-семью (общий event/шаблон вопроса) в одну календарную
      неделю;
  (3) llm_edge_plausible вычисляется ДЕТЕРМИНИРОВАННЫМ КОДОМ здесь, в
      Шаге 1, ДО того как кто-либо (включая меня, LLM, на следующем
      шаге) читает текст вопроса с целью оценки. Правило: рынок
      исключается из "преимущество возможно", если исход зависит от
      цены/капитализации/курса актива, ИЛИ от будущего решения
      конкретной компании/персоны, ИЛИ является случайным процессом.
      Код применяет правило, не человек -- см. classify_edge_plausible().

КОД ЭТОГО ФАЙЛА ФИЗИЧЕСКИ НЕ ССЫЛАЕТСЯ на поле `outcomePrices` (или
любое другое поле исхода) НИ РАЗУ.

Фильтры выборки (без изменений по существу, кроме окна и антикластера):
  - разрешение (endDate) в [RESOLUTION_MIN; RESOLUTION_MAX]
  - ИСКЛЮЧИТЬ спорт и киберспорт (расширенный keyword-список + при
    необходимости ручная проверка команд на этапе LLM-оценки, как в
    прошлом раунде -- keyword-фильтр НЕ ловит команды без спортивных
    слов в тексте, это известное реальное ограничение)
  - ИСКЛЮЧИТЬ крипто up/down с горизонтом <= 6 часов
  - срок жизни >= 5 дней, объём >= $1000
  - снимок цены за 3-14 дней до разрешения (ближайшая реальная точка
    к 7 дням)
  - цена снимка в [0.20; 0.80]
  - антикластер: не более 3 рынков на событие-семью в неделю
  - цель: N >= 40 кандидатов из >= 10 разных недель"""
from __future__ import annotations

import json
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskE-step1/2.0"}
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# 2026-09-10, владелец: доказательный прогон -- окно ДАЛЕКО от порога
# знаний модели (январь 2026) в обе стороны, не рядом с границей.
RESOLUTION_MIN = datetime(2026, 3, 1, tzinfo=timezone.utc)
RESOLUTION_MAX = datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc)

N_DAYS_BEFORE_RESOLUTION = 7
MIN_LIFESPAN_DAYS = 5
MIN_VOLUME_USD = 1000
TARGET_N_MARKETS = 45  # владелец просил >=40 -- берём небольшой запас
TARGET_MIN_WEEKS = 10
RANDOM_SEED = 20260910
SHORT_CRYPTO_HORIZON_MAX_MINUTES = 360
PRICE_RANGE_MIN = 0.20
PRICE_RANGE_MAX = 0.80
MAX_PER_FAMILY_PER_WEEK = 3  # владелец: антикластер
# 2026-09-10: защитный предел -- окно в 26 недель может дать намного
# больше кандидатов, чем один день; ограничиваем реальное число
# запросов цены снимка, чтобы не упереться в timeout воркфлоу (28 мин).
# Если предел исчерпан раньше цели -- это честно видно в диагностике
# (stop_reason_selection), а не скрытый обрыв.
MAX_PRICE_FETCH_ATTEMPTS = 2200

SPORTS_ESPORTS_KEYWORDS = (
    "nfl", "nba", "mlb", "nhl", "ncaa", "soccer", "football", "basketball",
    "baseball", "hockey", "ufc", "mma", "tennis", "golf", "boxing", "epl",
    "premier league", "champions league", "olympics", "world cup", "cricket",
    "rugby", "formula 1", "nascar", "wins the game",
    "cs2", "csgo", "counter-strike", "valorant", "league of legends", "dota",
    "dota 2", "overwatch", "rocket league", "starcraft", "fortnite", "esports",
    "esport", "call of duty", "rainbow six", "apex legends", "warzone",
    "hltv", "map winner", "maps won", "best-of-", " bo3", " bo5",
    "cbb", "cwbb", "wbb", "a-league", "btts", "both teams to score",
    "college basketball", "college football", "la liga", "bundesliga",
    "serie a", "ligue 1", "mls", "wnba", "ncaaf", "ncaab", "cfb", "cfl", "afl",
    # 2026-09-10, реальное известное ограничение (не исправлено этим
    # прогоном): команды без спортивных слов в тексте ("In the upcoming
    # game... If Macarthur FC wins...") этот список НЕ ловит -- нужна
    # ручная проверка реальных названий команд/лиг при чтении вопросов
    # на этапе LLM-оценки, как в предыдущем раунде.
)
SPORTS_ESPORTS_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in SPORTS_ESPORTS_KEYWORDS) + r")\b", re.IGNORECASE)

CRYPTO_UPDOWN_SLUG_RE = re.compile(r"updown-(\d+)(m|h)-", re.IGNORECASE)

# --- 2026-09-10, владелец: критерий преимущества -- ДЕТЕРМИНИРОВАННЫЙ
# КОД, применяется здесь, в Шаге 1, ДО чтения вопросов кем-либо для
# оценки. Три категории ИСКЛЮЧАЮТ рынок из "преимущество возможно":
PRICE_ASSET_RE = re.compile(
    r"(\$\s?\d)|\bmarket cap\b|\bfdv\b|\bfully diluted valuation\b|\bexchange rate\b|"
    r"\btrading (at|price)\b|\bclosing price\b|\bhit \$|\breach(es)? \$|\bdip(s|ped)? to \$|"
    r"\babove \$|\bbelow \$|\bbetween \$|\bhigh(er)? price\b|\blow(er)? price\b",
    re.IGNORECASE)
THIRD_PARTY_DECISION_RE = re.compile(
    # 2026-09-10: "announce" сужен до личных решений (кандидатура/
    # отставка/уход) -- голый "announce" ловил бы и легитимные
    # макро/регуляторные вопросы ("Fed announces rate decision"),
    # которые владелец явно хочет ОСТАВИТЬ как разбор известных фактов.
    r"\bresign\b|\bstep(s)? down\b|\bannounce.{0,20}\b(run|candidacy|resignation|retirement|departure)\b|"
    r"\bhold(s)?\b.{0,20}\b(btc|bitcoin|eth|token|coins?)\b|"
    r"\bacquir(e|es|ed|ing)\b|\bacquisition\b|\bmerger\b|\bipo\b|\btrading competition\b|"
    r"\bwin(s)? the .*(competition|leaderboard)\b|\bpublic sale commitments\b|\braise \$|\blayoffs\b|"
    r"\bstep aside\b|\bfired\b|\bhires?\b|\bnominee\b.{0,20}\bconfirm|"
    r"\bleave(s)? the\b|\bjoin(s)? the\b|\brun(s)? for\b|\bswitch(es)? parties\b|"
    r"\bregister(s)? as (an )?independent\b|\bwon'?t run\b|\bwill not run\b|\bdeclines? to run\b|"
    r"\bpasses? on\b|\bendorse(s)?\b",
    re.IGNORECASE)
RANDOM_PROCESS_RE = re.compile(
    r"\bcoin flip\b|\blottery\b|\braffle\b|\brandom draw\b|\bdice\b|\b(happens|occurs|reaches?) first\b",
    re.IGNORECASE)


def classify_edge_plausible(market: dict) -> bool:
    """ДЕТЕРМИНИРОВАННОЕ код-правило (владелец, 2026-09-10): рынок
    попадает в "преимущество возможно", если НЕ является ни одним из
    трёх исключаемых типов. Вычисляется здесь, в Шаге 1, до того как
    кто-либо читает вопрос с целью дать оценку вероятности -- это
    единственный способ избежать пост-хок смещения классификации."""
    hay = " ".join(str(market.get(k, "")) for k in ("question", "description"))
    if PRICE_ASSET_RE.search(hay):
        return False
    if THIRD_PARTY_DECISION_RE.search(hay):
        return False
    if RANDOM_PROCESS_RE.search(hay):
        return False
    return True


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

# --- 2026-09-10, владелец: антикластер -- нормализованная сигнатура
# описания как ключ "события-семьи". Реальное ограничение (честно, как
# и раньше): капитализированные последовательности (имена команд/
# исполнителей/песен) схлопываются в один плейсхолдер "X", что реально
# объединяет варианты одного события (разные команды одной игры,
# разные песни одной недельной таблицы) под одним ключом -- но это
# эвристика, не гарантированно полное объединение всех вариантов
# (например, разная структура предложения для "ничьей" против "победы"
# в спортивном рынке может НЕ объединиться с исходами "победа команды
# X/Y" под тем же ключом -- недо-объединение безопаснее, чем пере-).
CAP_RUN_RE = re.compile(r"[A-Z][a-zA-Z0-9&'.]*(?:\s+[A-Z][a-zA-Z0-9&'.]*)*")


def family_key(market: dict, week_key: str) -> str:
    # 2026-09-10, реальная проверка: US- и Global-варианты Spotify-чарта
    # различаются только ПОСЛЕ 150-го символа описания ("in the U.S. on
    # Spotify" vs "globally on Spotify") -- при короткой отсечке они
    # ошибочно схлопывались в одну семью. 320 символов реально разносит
    # их, продолжая объединять разные песни ОДНОГО чарта (проверено).
    desc = str(market.get("description", ""))[:400]
    normalized = CAP_RUN_RE.sub("X", desc)
    normalized = re.sub(r"\d+", "#", normalized)
    return f"{week_key}::{normalized[:320]}"


def week_key_for(end_date: datetime) -> str:
    # Понедельник той недели, к которой относится дата разрешения --
    # реальный календарный якорь для группировки и подсчёта "N недель".
    monday = end_date - timedelta(days=end_date.weekday())
    return monday.strftime("%Y-%m-%d")


OUT_PATH = Path("data/p3_guard_cache/taskE_step1_questions_context.json")
OUT_DIAG = Path("data/p3_guard_cache/taskE_step1_diagnostics.json")


def fetch_closed_markets_for_window(window_start: datetime, window_end: datetime,
                                     max_pages: int = 25, page_size: int = 100) -> tuple[list[dict], dict]:
    """Владелец, 2026-09-10: понедельная пагинация вместо сплошного
    списка -- реальная находка прошлого раунда: сплошной список
    упирался в HTTP 422 на offset~2100 внутри ОДНОГО дня из-за высокой
    плотности закрытий. Понедельное окно должно давать на порядки
    меньше строк на окно, не подходя к потолку пагинации."""
    out = {}
    window_min = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_max = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    n_pages = 0
    stop_reason = None
    for offset in range(0, max_pages * page_size, page_size):
        r = requests.get(f"{GAMMA_BASE}/markets", params={
            "limit": page_size, "offset": offset, "closed": "true",
            "order": "endDate", "ascending": "true",
            "end_date_min": window_min, "end_date_max": window_max,
        }, headers=HEADERS, timeout=30)
        n_pages += 1
        if r.status_code != 200:
            stop_reason = f"http_status_{r.status_code}_at_page_{n_pages}"
            break
        body = r.json()
        if not isinstance(body, list) or not body:
            stop_reason = f"empty_body_at_page_{n_pages}"
            break
        for m in body:
            if m.get("slug"):
                out[m["slug"]] = m
        if len(body) < page_size:
            stop_reason = f"short_page_({len(body)}<{page_size})_at_page_{n_pages}"
            break
        time.sleep(0.15)
    else:
        stop_reason = f"max_pages_reached_({max_pages})"
    return list(out.values()), {"n_pages": n_pages, "stop_reason": stop_reason, "n_items": len(out)}


def is_sports_or_esports(market: dict) -> bool:
    hay = " ".join(str(market.get(k, "")) for k in ("question", "slug", "description")).lower()
    return bool(SPORTS_ESPORTS_RE.search(hay))


def is_short_horizon_crypto(market: dict) -> bool:
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
    return True


def matches_positive_topic(market: dict) -> bool:
    hay = " ".join(str(market.get(k, "")) for k in ("question", "slug", "description")).lower()
    return bool(POSITIVE_TOPIC_RE.search(hay))


_price_snapshot_failure_samples: list[dict] = []


def fetch_price_snapshot(clob_token_id: str, end_date: datetime) -> float | None:
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
                _price_snapshot_failure_samples.append({"status": 200, "body_keys": list(body.keys()), "empty_history": True})
            return None
        best = min(history, key=lambda pt: abs(pt.get("t", preferred_ts) - preferred_ts))
        return float(best["p"])
    except Exception as exc:  # noqa: BLE001
        if len(_price_snapshot_failure_samples) < 5:
            _price_snapshot_failure_samples.append({"exception": str(exc)[:300]})
        return None


def run() -> int:
    diag: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "resolution_min": RESOLUTION_MIN.isoformat(), "resolution_max": RESOLUTION_MAX.isoformat(),
                  "n_days_before_resolution": N_DAYS_BEFORE_RESOLUTION}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"=== Понедельный fetch closed Polymarket рынков, {RESOLUTION_MIN.date()} -- {RESOLUTION_MAX.date()} ===")
    all_markets: dict[str, dict] = {}
    weekly_diag = []
    week_start = RESOLUTION_MIN
    while week_start < RESOLUTION_MAX:
        week_end = min(week_start + timedelta(days=7), RESOLUTION_MAX)
        markets_w, wd = fetch_closed_markets_for_window(week_start, week_end)
        wd["window_start"] = week_start.isoformat()
        wd["window_end"] = week_end.isoformat()
        weekly_diag.append(wd)
        for m in markets_w:
            if m.get("slug"):
                all_markets[m["slug"]] = m
        print(f"[taskE_step1] неделя {week_start.date()}--{week_end.date()}: {wd['n_items']} рынков "
              f"({wd['n_pages']} стр., {wd['stop_reason']})")
        week_start = week_end
        time.sleep(0.1)

    markets = list(all_markets.values())
    diag["n_weeks_queried"] = len(weekly_diag)
    diag["weekly_pagination_diagnostics"] = weekly_diag
    diag["n_closed_markets_in_window"] = len(markets)
    print(f"[taskE_step1] реальных closed-рынков во всём окне: {len(markets)}")

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
        outcomes_raw = m.get("outcomes")
        try:
            outcomes_names = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (json.JSONDecodeError, TypeError):
            outcomes_names = None
        if vol < MIN_VOLUME_USD or not tokens or not outcomes_names:
            continue
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
                continue
        else:
            continue
        quality.append(m)
    diag["n_after_quality_filter"] = len(quality)
    print(f"[taskE_step1] после фильтра качества (объём>=${MIN_VOLUME_USD}, есть clobTokenIds, срок жизни>={MIN_LIFESPAN_DAYS}д): {len(quality)}")

    positive = [m for m in quality if matches_positive_topic(m)]
    other = [m for m in quality if not matches_positive_topic(m)]
    diag["n_positive_topic_matches"] = len(positive)

    random.seed(RANDOM_SEED)
    random.shuffle(positive)
    random.shuffle(other)
    positive_slugs = {m["slug"] for m in positive}
    candidates_ordered = positive + other

    # --- Проход 1: для каждого кандидата по очереди запрашиваем цену
    # снимка, фильтруем по диапазону [0.20;0.80], попутно считаем
    # неделю и семью-событие, применяем антикластер (не более
    # MAX_PER_FAMILY_PER_WEEK на семью в неделю). Останавливаемся, набрав
    # TARGET_N_MARKETS с покрытием >= TARGET_MIN_WEEKS недель, или
    # исчерпав пул.
    context_items = []
    family_week_count: dict[str, int] = defaultdict(int)
    weeks_covered: set[str] = set()
    n_examined = 0
    n_price_snapshot_ok = 0
    n_price_in_range = 0
    n_dropped_anticluster = 0

    for m in candidates_ordered:
        if len(context_items) >= TARGET_N_MARKETS:
            break
        if n_examined >= MAX_PRICE_FETCH_ATTEMPTS:
            break
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

        n_examined += 1
        price_snapshot = fetch_price_snapshot(tokens[0], end_date) if tokens else None
        time.sleep(0.12)
        if price_snapshot is None:
            continue
        n_price_snapshot_ok += 1
        if not (PRICE_RANGE_MIN <= price_snapshot <= PRICE_RANGE_MAX):
            continue
        n_price_in_range += 1

        wk = week_key_for(end_date)
        fam = family_key(m, wk)
        if family_week_count[fam] >= MAX_PER_FAMILY_PER_WEEK:
            n_dropped_anticluster += 1
            continue
        family_week_count[fam] += 1
        weeks_covered.add(wk)

        context_items.append({
            "slug": slug, "question": m.get("question"), "description": m.get("description"),
            "end_date": end_date_str, "outcome_names": outcomes_names,
            "week_key": wk,
            "price_snapshot_date": (end_date - timedelta(days=N_DAYS_BEFORE_RESOLUTION)).isoformat(),
            "market_price_first_outcome_n_days_before": price_snapshot,
            "volume": m.get("volumeNum"),
            "llm_edge_plausible": classify_edge_plausible(m),  # код-правило, ДО чтения кем-либо
        })

    diag["n_candidates_examined_for_price"] = n_examined
    diag["n_price_snapshot_ok"] = n_price_snapshot_ok
    diag["n_price_in_target_range"] = n_price_in_range
    diag["n_dropped_anticluster"] = n_dropped_anticluster
    diag["price_range_filter"] = [PRICE_RANGE_MIN, PRICE_RANGE_MAX]
    diag["n_selected"] = len(context_items)
    diag["n_weeks_covered"] = len(weeks_covered)
    diag["n_selected_from_positive_topics"] = sum(1 for c in context_items if c["slug"] in positive_slugs)
    diag["n_edge_plausible_selected"] = sum(1 for c in context_items if c["llm_edge_plausible"])
    if len(context_items) >= TARGET_N_MARKETS:
        diag["stop_reason_selection"] = "reached_target_n"
    elif n_examined >= MAX_PRICE_FETCH_ATTEMPTS:
        diag["stop_reason_selection"] = f"hit_max_price_fetch_attempts_({MAX_PRICE_FETCH_ATTEMPTS})"
    else:
        diag["stop_reason_selection"] = "exhausted_candidate_pool"
    diag["meets_prereg_threshold"] = (len(context_items) >= 40 and len(weeks_covered) >= TARGET_MIN_WEEKS)
    diag["price_snapshot_failure_samples"] = _price_snapshot_failure_samples
    print(f"[taskE_step1] рассмотрено: {n_examined}, снимков цены: {n_price_snapshot_ok}, "
          f"в диапазоне [{PRICE_RANGE_MIN};{PRICE_RANGE_MAX}]: {n_price_in_range}, "
          f"отброшено антикластером: {n_dropped_anticluster}, итого отобрано: {len(context_items)} "
          f"из {len(weeks_covered)} недель (порог N>=40 и недель>=10: {diag['meets_prereg_threshold']})")

    OUT_PATH.write_text(json.dumps({
        "meta": {"resolution_min": RESOLUTION_MIN.isoformat(), "resolution_max": RESOLUTION_MAX.isoformat(),
                  "n_days_before_resolution": N_DAYS_BEFORE_RESOLUTION,
                  "random_seed": RANDOM_SEED, "n_markets": len(context_items),
                  "n_weeks_covered": len(weeks_covered),
                  "llm_estimates": "ЗАПОЛНЯЕТСЯ ОТДЕЛЬНО, ВРУЧНУЮ, ПОСЛЕ этого коммита -- см. поле 'llm_probability_estimate' "
                                    "в каждой записи markets[], изначально null. Поле 'llm_edge_plausible' УЖЕ "
                                    "вычислено детерминированным кодом (classify_edge_plausible), НЕ меняется вручную."},
        "markets": [{**c, "llm_probability_estimate": None, "llm_reasoning_brief": None} for c in context_items],
    }, indent=2, ensure_ascii=False, default=str))
    OUT_DIAG.write_text(json.dumps(diag, indent=2, ensure_ascii=False, default=str))
    print(f"\n[taskE_step1] записано {OUT_PATH} -- поля llm_probability_estimate/llm_reasoning_brief ПУСТЫЕ, "
          "llm_edge_plausible УЖЕ вычислено кодом.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
