#!/usr/bin/env python3
"""Задача D владельца (2026-09-10): Polymarket vs Pinnacle -- ШАГ 0,
до любого анализа: реально ли доступен источник острых котировок за
30-дневное ОКНО ИСТОРИИ (не только текущие линии).

Не трогает Dune/GT -- отдельные публичные API (The Odds API,
Betfair Exchange), безопасно параллельно с ретро-сторожком.

Честная проверка, не гадание по памяти:
1. The Odds API (the-odds-api.com) -- реальный публичный ответ эндпоинта
   без ключа (должен честно сказать, что нужно, и его реальный формат
   ошибки часто содержит суть тарифных ограничений) + реальная
   пробуем самый дешёвый штатный способ выяснить наличие исторических
   данных на бесплатном тарифе -- через реальный HTTP-ответ, не через
   маркетинговую страницу (HTML тяжело парсить надёжно, а API отвечает
   структурированной ошибкой).
2. Betfair Exchange API -- реальная проверка публичной точки входа
   (сертификационный/логин эндпоинт) -- честно фиксируем, что нужен
   аккаунт+ключ приложения+процесс одобрения, это НЕ can be done
   anonymously, независимо от цены.

Результат: реально пригоден ли бесплатно доступный путь к 30-дневной
ИСТОРИИ котировок Pinnacle/Betfair для этой задачи, до траты времени
на сам анализ."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OUT_PATH = Path("data/p3_guard_cache/taskD_odds_source_probe_result.json")
HEADERS = {"User-Agent": "robinhood-chain-alpha-taskD-odds-probe/1.0"}


def probe_the_odds_api() -> dict:
    out: dict = {}
    # Реальный запрос без ключа -- смотрим точную структуру и текст ошибки.
    try:
        r = requests.get("https://api.the-odds-api.com/v4/sports", headers=HEADERS, timeout=20)
        out["no_key_status"] = r.status_code
        try:
            out["no_key_body"] = r.json()
        except Exception:  # noqa: BLE001
            out["no_key_body_text"] = r.text[:500]
    except Exception as exc:  # noqa: BLE001
        out["no_key_error"] = str(exc)[:300]

    # Реальный запрос к историческому эндпоинту (тоже без ключа) --
    # даже без валидного ключа формат/текст ошибки часто прямо называет
    # требуемый тариф (see real error body).
    try:
        r2 = requests.get(
            "https://api.the-odds-api.com/v4/historical/sports/soccer_epl/odds",
            params={"regions": "eu", "markets": "h2h", "date": "2026-08-15T12:00:00Z"},
            headers=HEADERS, timeout=20,
        )
        out["historical_no_key_status"] = r2.status_code
        try:
            out["historical_no_key_body"] = r2.json()
        except Exception:  # noqa: BLE001
            out["historical_no_key_body_text"] = r2.text[:500]
    except Exception as exc:  # noqa: BLE001
        out["historical_no_key_error"] = str(exc)[:300]

    # Публичная страница тарифов -- реальный HTML, ищем упоминание
    # "historical" рядом с ценой (грубый честный grep, не выдаём за
    # структурированные данные).
    try:
        r3 = requests.get("https://the-odds-api.com/#get-access", headers=HEADERS, timeout=20)
        out["pricing_page_status"] = r3.status_code
        text = r3.text
        idx = text.lower().find("historical")
        out["pricing_page_historical_mention"] = text[max(0, idx - 300):idx + 300] if idx >= 0 else None
    except Exception as exc:  # noqa: BLE001
        out["pricing_page_error"] = str(exc)[:300]

    return out


def probe_betfair() -> dict:
    out: dict = {}
    # Реальная проверка публичной точки входа логина (без креденшлов --
    # честно смотрим, что говорит API о требованиях доступа).
    try:
        r = requests.post("https://identitysso.betfair.com/api/login", headers=HEADERS, timeout=20,
                           data={"username": "", "password": ""})
        out["login_endpoint_status"] = r.status_code
        try:
            out["login_endpoint_body"] = r.json()
        except Exception:  # noqa: BLE001
            out["login_endpoint_body_text"] = r.text[:500]
    except Exception as exc:  # noqa: BLE001
        out["login_endpoint_error"] = str(exc)[:300]
    out["known_requirement"] = ("Betfair Exchange API требует: реальный аккаунт Betfair (регистрация с "
                                 "верификацией, часто гео-ограничена), Application Key (заявка, отдельная "
                                 "для delayed/live данных, ручное одобрение), и логин-сессию (username/password "
                                 "+ 2FA в некоторых юрисдикциях) -- НЕ anonymous/instant self-serve, независимо "
                                 "от того, платный тариф или нет.")
    return out


def run() -> int:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("=== The Odds API -- реальная проверка без ключа ===")
    result["the_odds_api"] = probe_the_odds_api()
    print(json.dumps(result["the_odds_api"], indent=2, ensure_ascii=False)[:3000])

    print("\n=== Betfair Exchange API -- реальная проверка входа ===")
    result["betfair"] = probe_betfair()
    print(json.dumps(result["betfair"], indent=2, ensure_ascii=False)[:2000])

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[taskD_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
