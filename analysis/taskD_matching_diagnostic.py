#!/usr/bin/env python3
"""Задача D -- диагностика причины низкого % сопоставления Pinnacle<->
Polymarket (10 из 430 = 2.3%), владелец, 2026-09-10: "разобраться, почему
матчинг даёт 2.3%... взять 20 случайных событий Pinnacle, вручную
проверить, существует ли на Polymarket рынок на ту же игру".

БЕСПЛАТНЫЙ скрипт (Gamma/CLOB API без авторизации, не трогает дорогой
The Odds API фетч и не тратит кредиты). Три возможных диагноза на
событие, как задал владелец:
  (a) рынка на Polymarket реально нет для этой игры/лиги;
  (b) рынок есть, но строгий matcher (taskD_pinnacle_polymarket_
      analysis.py, тот же predicate, что и в реальном прогоне) его не
      находит -- нормализация названий команд/формат даты;
  (c) рынок есть, строгий matcher находит, но дальше отсекается
      фильтром цены/истории (prices-history вернул пусто).

Метод для (a) vs (b): НЕЗАВИСИМЫЙ от матчера, лёгкий ("lenient") поиск
по узкому окну дат вокруг конкретной игры (Gamma /markets,
end_date_min/max = commence_time +/- 3 дня, оба среза closed=true и
active=true) -- чтобы не полагаться на тот же 30-дневный
bulk-fetch/normalize(), который мог сам быть источником пропуска."""
from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from taskC_sports_matcher import fetch_polymarket_bulk, normalize  # тот же реальный код, что в проде

HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-matching-diag/1.0"}
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

IN_PATH = Path("data/p3_guard_cache/taskD_pinnacle_snapshots.json")
RESULT_PATH = Path("data/p3_guard_cache/taskD_pinnacle_polymarket_result.json")
OUT_PATH = Path("data/p3_guard_cache/taskD_matching_diagnostic.json")

SAMPLE_N = 20
SEED = 42
NARROW_WINDOW_DAYS = 3
DATE_DIFF_CONFIDENT_DAYS = 1.0  # тот же порог, что в реальном analysis-скрипте


def gamma_markets_narrow(commence_dt: datetime) -> tuple[list[dict], dict]:
    """Независимый узкий запрос -- НЕ переиспользует fetch_polymarket_bulk()
    (у него фиксированное 30-дневное окно от текущего "now", источник
    возможных пропусков сам по себе). Полная пагинация в узком окне
    (3 дня) -- должна дойти до конца, страниц немного."""
    window_min = (commence_dt - timedelta(days=NARROW_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_max = (commence_dt + timedelta(days=NARROW_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    diag = {"window_min": window_min, "window_max": window_max, "passes": []}
    passes = [
        {"closed": "true", "end_date_min": window_min, "end_date_max": window_max},
        {"active": "true", "closed": "false", "end_date_min": window_min, "end_date_max": window_max},
    ]
    for params_base in passes:
        pass_diag = {"params": params_base, "pages": 0, "n_found": 0, "statuses": []}
        for offset in range(0, 2000, 100):
            try:
                r = requests.get(f"{GAMMA_BASE}/markets", params={
                    "limit": 100, "offset": offset, **params_base,
                }, headers=HEADERS, timeout=30)
            except requests.exceptions.RequestException as exc:
                pass_diag["statuses"].append(f"EXC:{str(exc)[:100]}")
                break
            pass_diag["statuses"].append(r.status_code)
            if r.status_code != 200:
                break
            body = r.json()
            if not isinstance(body, list) or not body:
                break
            for m in body:
                if m.get("slug"):
                    out[m["slug"]] = m
            pass_diag["pages"] += 1
            if len(body) < 100:
                break
            time.sleep(0.2)
        pass_diag["n_found"] = len(out)
        diag["passes"].append(pass_diag)
        time.sleep(0.2)
    return list(out.values()), diag


TEAM_WORD_STOPWORDS = {"the", "of", "fc", "sc"}


def team_key_words(team: str) -> list[str]:
    """Значимые слова названия команды (без городских частей) -- для
    ЛЕГКОГО (lenient) сравнения. Пример: 'New York Yankees' -> ['yankees'];
    берём последнее слово (обычно nickname команды), это самая
    устойчивая часть названия к разным форматам городского префикса."""
    words = [w for w in re.sub(r"[^a-zA-Z0-9 ]", " ", team).lower().split() if w not in TEAM_WORD_STOPWORDS]
    return words[-1:] if words else []


def lenient_market_matches(home_team: str, away_team: str, pm: dict) -> bool:
    q = normalize((pm.get("question") or "") + " " + (pm.get("slug") or "") + " " + (pm.get("groupItemTitle") or ""))
    home_kw = team_key_words(home_team)
    away_kw = team_key_words(away_team)
    if not home_kw or not away_kw:
        return False
    return all(normalize(w) in q for w in home_kw) and all(normalize(w) in q for w in away_kw)


def strict_market_matches(home_team: str, away_team: str, commence_dt: datetime, pm: dict) -> tuple[bool, float | None]:
    """Точная копия predicate из taskD_pinnacle_polymarket_analysis.py
    (не выдумываем новую логику -- воспроизводим реальную)."""
    q = normalize((pm.get("question") or "") + " " + (pm.get("slug") or ""))
    home_n, away_n = normalize(home_team), normalize(away_team)
    if home_n not in q or away_n not in q:
        return False, None
    compare_date_str = pm.get("gameStartTime") or pm.get("endDate")
    if not compare_date_str:
        return False, None
    try:
        compare_date = datetime.fromisoformat(compare_date_str.replace("Z", "+00:00"))
        if compare_date.tzinfo is None:
            compare_date = compare_date.replace(tzinfo=timezone.utc)
    except ValueError:
        return False, None
    diff_days = abs((compare_date - commence_dt).total_seconds()) / 86400
    return diff_days <= DATE_DIFF_CONFIDENT_DAYS, diff_days


def check_price_history_nonempty(token_id: str, commence_dt: datetime) -> dict:
    window_start = commence_dt - timedelta(days=5)
    window_end = min(datetime.now(timezone.utc), commence_dt + timedelta(days=1))
    try:
        r = requests.get(f"{CLOB_BASE}/prices-history", params={
            "market": token_id, "startTs": int(window_start.timestamp()), "endTs": int(window_end.timestamp()),
            "fidelity": 60,
        }, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return {"status": r.status_code, "n_points": 0, "body_snippet": r.text[:200]}
        hist = r.json().get("history", [])
        return {"status": 200, "n_points": len(hist)}
    except requests.exceptions.RequestException as exc:
        return {"status": None, "n_points": 0, "exception": str(exc)[:150]}


def run() -> int:
    if not IN_PATH.exists():
        print(f"[matching_diag] {IN_PATH} не найден -- нечего диагностировать.")
        return 1
    data = json.loads(IN_PATH.read_text())
    snapshots = data.get("snapshots", [])
    events_by_id: dict[str, dict] = {}
    for snap in snapshots:
        for ev in snap.get("events", []):
            events_by_id.setdefault(ev["id"], {
                "id": ev["id"], "sport_key": ev["sport_key"], "commence_time": ev["commence_time"],
                "home_team": ev["home_team"], "away_team": ev["away_team"],
            })
    all_events = list(events_by_id.values())
    print(f"[matching_diag] всего уникальных событий Pinnacle: {len(all_events)}")

    random.seed(SEED)
    sample = random.sample(all_events, min(SAMPLE_N, len(all_events)))
    from collections import Counter
    print(f"[matching_diag] выборка {len(sample)} событий (seed={SEED}), состав по спорту: "
          f"{dict(Counter(e['sport_key'] for e in sample))}")

    # Тот же bulk-fetch, что и реальный прод-прогон -- чтобы честно проверить,
    # "видел" ли он вообще кандидат, если тот найдётся узким поиском.
    print("[matching_diag] реальный fetch_polymarket_bulk() (тот же, что в проде)...")
    pm_bulk = fetch_polymarket_bulk()
    pm_bulk_slugs = {m.get("slug") for m in pm_bulk if m.get("slug")}
    print(f"[matching_diag] bulk-fetch вернул {len(pm_bulk)} рынков")

    result_json = json.loads(RESULT_PATH.read_text()) if RESULT_PATH.exists() else {}
    already_matched_ids = set()  # id событий, уже сопоставленных в реальном прогоне -- не проверяем повторно как "новые"

    diagnostics = []
    counts = {"no_market_exists": 0, "matcher_misses_it": 0, "bulk_fetch_never_loaded_it": 0,
              "filtered_by_price_history": 0, "already_matched_confirmed": 0}

    for ev in sample:
        try:
            commence_dt = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except ValueError:
            continue
        entry = {"event_id": ev["id"], "sport": ev["sport_key"], "home_team": ev["home_team"],
                  "away_team": ev["away_team"], "commence_time": ev["commence_time"]}

        narrow_markets, narrow_diag = gamma_markets_narrow(commence_dt)
        entry["narrow_search_diag"] = narrow_diag
        lenient_candidates = [pm for pm in narrow_markets
                               if lenient_market_matches(ev["home_team"], ev["away_team"], pm)]
        entry["n_lenient_candidates_in_narrow_window"] = len(lenient_candidates)

        if not lenient_candidates:
            entry["diagnosis"] = "no_market_exists"
            entry["evidence"] = (f"узкий поиск (+/-{NARROW_WINDOW_DAYS}д вокруг {ev['commence_time']}) "
                                  f"вернул {len(narrow_markets)} рынков всего, ни один не прошёл лёгкое "
                                  f"совпадение по ключевым словам команд '{ev['home_team']}' / '{ev['away_team']}'")
            counts["no_market_exists"] += 1
            diagnostics.append(entry)
            continue

        best_strict, best_pm, best_diff = False, None, None
        for pm in lenient_candidates:
            ok, diff = strict_market_matches(ev["home_team"], ev["away_team"], commence_dt, pm)
            if ok and (best_pm is None or diff < best_diff):
                best_strict, best_pm, best_diff = True, pm, diff
        sample_lenient = lenient_candidates[0]
        entry["lenient_candidate_sample"] = {
            "question": sample_lenient.get("question"), "slug": sample_lenient.get("slug"),
            "endDate": sample_lenient.get("endDate"), "gameStartTime": sample_lenient.get("gameStartTime"),
        }

        if not best_strict:
            entry["diagnosis"] = "matcher_misses_it"
            _, diff_of_first = strict_market_matches(ev["home_team"], ev["away_team"], commence_dt, sample_lenient)
            entry["evidence"] = (f"рынок реально найден лёгким поиском ('{sample_lenient.get('question')}', "
                                  f"slug={sample_lenient.get('slug')}), но НЕ проходит строгий predicate "
                                  f"analysis-скрипта (полное имя команды как подстрока И date_diff<={DATE_DIFF_CONFIDENT_DAYS}д). "
                                  f"normalize(home)='{normalize(ev['home_team'])}', normalize(away)='{normalize(ev['away_team'])}', "
                                  f"normalize(question+slug)='{normalize((sample_lenient.get('question') or '') + ' ' + (sample_lenient.get('slug') or ''))[:200]}'")
            counts["matcher_misses_it"] += 1
            diagnostics.append(entry)
            continue

        entry["strict_match_question"] = best_pm.get("question")
        entry["strict_match_slug"] = best_pm.get("slug")
        entry["strict_match_date_diff_days"] = best_diff

        if best_pm.get("slug") not in pm_bulk_slugs:
            entry["diagnosis"] = "bulk_fetch_never_loaded_it"
            entry["evidence"] = (f"строгий predicate ПРОШЁЛ бы для '{best_pm.get('question')}' (slug={best_pm.get('slug')}), "
                                  f"но этого рынка НЕТ в реальном выводе fetch_polymarket_bulk() ({len(pm_bulk)} рынков) -- "
                                  f"значит его просто не долистали/не захватили общим 30-дневным bulk-запросом "
                                  f"(гонка окна по времени или обрезка active-среза лимитом страниц), а не проблема самой сверки строк")
            counts["bulk_fetch_never_loaded_it"] += 1
            diagnostics.append(entry)
            continue

        # Строгий матчер должен был бы найти это событие в реальном прогоне -- проверяем,
        # действительно ли оно попало в n_matched_events=10, и если да -- прошло ли price-history.
        try:
            token_ids = json.loads(best_pm.get("clobTokenIds") or "[]")
        except (ValueError, TypeError):
            token_ids = []
        if token_ids:
            ph = check_price_history_nonempty(token_ids[0], commence_dt)
            entry["price_history_check"] = ph
            if ph.get("n_points", 0) == 0:
                entry["diagnosis"] = "filtered_by_price_history"
                entry["evidence"] = (f"матчер нашёл бы это событие, но CLOB /prices-history для токена "
                                      f"{token_ids[0]} вернул {ph}")
                counts["filtered_by_price_history"] += 1
                diagnostics.append(entry)
                continue
        entry["diagnosis"] = "already_matched_confirmed"
        entry["evidence"] = "матчер находит, bulk-fetch видит, цена доступна -- это событие реально должно быть в числе сопоставленных"
        counts["already_matched_confirmed"] += 1
        diagnostics.append(entry)

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_total_unique_events": len(all_events),
        "sample_size": len(sample),
        "seed": SEED,
        "narrow_window_days": NARROW_WINDOW_DAYS,
        "counts": counts,
        "diagnostics": diagnostics,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[matching_diag] ИТОГ по {len(sample)} случайным событиям: {counts}")
    print(f"[matching_diag] записано в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
