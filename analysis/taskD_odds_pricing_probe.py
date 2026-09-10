#!/usr/bin/env python3
"""Задача D владельца (2026-09-10): реальная точная цена The Odds API
за историческую выдачу (месячная подписка ИЛИ разовый доступ), не
оценка. Не трогает Dune/GT, не трогает Betfair (владелец: отдельный
вопрос времени, не денег -- не в этом прогоне).

Прежний прогон (taskD_odds_source_probe.py) вытащил только сырой
фрагмент HTML вокруг слова "historical" -- недостаточно для точной
цифры. Этот скрипт разбирает ВСЕ карточки тарифов на реальной
странице (title/price/period/features), явно ищет план(ы), где
"Historical Odds" НЕ зачёркнут (значит включён), и отдельно проверяет
наличие разового ("one-time"/"pay as you go"/"single purchase")
варианта доступа к историческим данным."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/taskD_odds_pricing_probe_result.json")
HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-pricing-probe/1.0"}


def fetch_pricing_html() -> str:
    r = requests.get("https://the-odds-api.com/", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s).strip()


def parse_plan_cards(html: str) -> list[dict]:
    """Каждая карточка тарифа -- <div class="plan" ...>...</div> (реальная
    структура, подтверждена прошлым прогоном). Разбираем грубо, но по
    реальным маркерам классов, не гадаем структуру заранее не проверив."""
    cards = []
    for m in re.finditer(r'<div class="plan"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r'oa-plan-title[^>]*>(.*?)</div>', block, re.DOTALL)
        price_m = re.search(r'oa-price[^>]*>(.*?)</div>', block, re.DOTALL)
        features = re.findall(r'<div class="oa-feature"[^>]*>(.*?)</div>', block, re.DOTALL)
        hist_feature = next((f for f in features if "historical" in f.lower()), None)
        cards.append({
            "title": strip_tags(title_m.group(1)) if title_m else None,
            "price_raw": strip_tags(price_m.group(1)) if price_m else None,
            "historical_feature_raw": strip_tags(hist_feature) if hist_feature else None,
            "historical_included": bool(hist_feature and "<s>" not in hist_feature and "<s " not in hist_feature),
        })
    return cards


def find_one_time_mentions(html: str) -> list[str]:
    text = strip_tags(html)
    hits = []
    for kw in ("one-time", "one time", "pay as you go", "pay-as-you-go", "single purchase", "à la carte", "credits"):
        idx = text.lower().find(kw)
        if idx >= 0:
            hits.append({"keyword": kw, "context": text[max(0, idx - 150):idx + 150]})
    return hits


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("=== Реальная страница тарифов The Odds API ===")
    try:
        html = fetch_pricing_html()
    except Exception as exc:  # noqa: BLE001
        result["blocker"] = f"Не удалось получить страницу тарифов: {exc}"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[taskD_pricing] {result['blocker']}")
        return 1

    result["html_length"] = len(html)
    cards = parse_plan_cards(html)
    result["plan_cards_parsed"] = cards
    print(f"[taskD_pricing] реальных карточек тарифов распознано: {len(cards)}")
    for c in cards:
        print(f"  {c['title']}: {c['price_raw']} -- historical: {c['historical_feature_raw']} "
              f"(included={c['historical_included']})")

    plans_with_historical = [c for c in cards if c["historical_included"]]
    result["plans_with_historical_included"] = plans_with_historical
    if plans_with_historical:
        cheapest = min(plans_with_historical, key=lambda c: c["title"] or "")
        result["cheapest_plan_with_historical_raw"] = cheapest
        print(f"\n[taskD_pricing] реальный САМЫЙ ДЕШЁВЫЙ план с включённым Historical Odds: "
              f"{cheapest['title']} -- {cheapest['price_raw']}")
    else:
        result["note"] = "Ни в одной распознанной карточке Historical Odds не отмечен как включённый (не зачёркнутый)."
        print(f"\n[taskD_pricing] {result['note']}")

    print("\n=== Реальный поиск разового ('one-time'/'pay as you go') доступа ===")
    one_time = find_one_time_mentions(html)
    result["one_time_access_mentions"] = one_time
    if one_time:
        for h in one_time:
            print(f"  найдено '{h['keyword']}': ...{h['context']}...")
    else:
        print("  ни одного упоминания разового/pay-as-you-go доступа на странице не найдено -- похоже, только подписка.")

    # Честный запасной путь -- если regex-разбор карточек не сработал
    # (структура страницы могла отличаться от предположенной), сохраняем
    # сырой текст вокруг каждого упоминания цены рядом с "Historical"
    # для ручной проверки владельцем.
    raw_hist_positions = [m.start() for m in re.finditer(r'historical', html, re.IGNORECASE)]
    result["raw_historical_mentions_count"] = len(raw_hist_positions)
    result["raw_context_around_each_mention"] = [
        strip_tags(html[max(0, p - 400):p + 400]) for p in raw_hist_positions
    ]

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[taskD_pricing] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
