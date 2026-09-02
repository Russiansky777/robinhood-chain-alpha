"""P4, калибровка семантики `direction` по РЕАЛЬНЫМ funding-начислениям
счёта (владелец, дозапрос 2026-09-01, п.5): "подготовить скрипт
p4_funding_calibration.py, который по заданному аккаунту читает
фактические funding-зачисления по позиции и выводит знак/размер за
час -- запускать только по команде владельца после открытия позиции."

СТАТУС: ОТМЕНЕНО 2026-09-01 (владелец, "Порядок и дисциплина", п.4).
Причина: единица `rate` разрешена независимо, без открытия реальной
позиции (три источника доказательств, см. `docs/P4_RECON.md`,
"Дозапрос владельца: проверка единицы `rate`...") -- фандинг
сток-перпов Lighter оказался скромной базовой ставкой
(`base_interest_rate/8 ≈ 3.5%/год`), а не premium-аномалией,
требовавшей срочной проверки знаком реальной позиции. Критерий №2
`docs/P4_KILL.md` уже применён (KILL) на скорректированных агрегатах.
Скрипт остаётся в репозитории как рабочая заготовка -- семантика
`direction` (payer/receiver) по-прежнему НЕ подтверждена документацией
и может понадобиться позже (например, если линия "дельта-нейтральный
фарм LIT" из `docs/P4_KILL.md` дойдёт до реальной позиции) -- тогда
актуален тот же принцип: запуск только по прямой команде владельца
после открытия позиции, см. ниже.

СТАТУС (исходный, до отмены выше): ЗАГОТОВКА. НЕ ЗАПУСКАТЬ АВТОНОМНО. Требует:
  1. Реально открытую позицию на Lighter (владелец решает, когда) --
     без неё `/api/v1/positionFunding` вернёт пустой список, калибровка
     невозможна в принципе (нечего калибровать).
  2. account_index владельца (публичный, не секрет сам по себе, но
     привязан к реальному счёту -- не хардкодить, только через CLI/env).
  3. Возможно, авторизацию Lighter -- см. "Об авторизации" ниже. Этот
     скрипт делает ПЕРВУЮ попытку без авторизации (документация
     говорит: "Authentication is required when fetching an account_index
     linked to a main/sub-account, but can be left empty for public
     pools" -- WebSearch, apidocs.lighter.xyz/reference/positionfunding,
     2026-09-01); если API ответит 401/403, скрипт останавливается и
     печатает чёткую инструкцию, НЕ пытается угадать/подделать схему
     подписи запроса (Lighter использует EIP-712-подобную подпись
     аккаунта через официальный SDK -- `pip install lighter-sdk`,
     `lighter.SignerClient`, НЕ обычный API-ключ-заголовок; если это
     понадобится, нужен официальный SDK, не эта заготовка).

Цель: узнать РЕАЛЬНЫЙ знак funding по позиции -- то, что диагностика
на публичных данных (analysis/p4_lighter_markets.py) выяснить не
смогла (см. docs/P4_RECON.md, "Дозапрос: единицы фандинга") -- прямое
эмпирическое наблюдение "открыл шорт -> получаю/плачу X$ за час"
устраняет всю неопределённость про direction/rate разом, без гадания
про документацию.

Эндпоинт (WebSearch, apidocs.lighter.xyz/reference/positionfunding,
дословно не проверено WebFetch -- домен заблокирован для прямого
фетча в интерактивной сессии, см. docs/P4_RECON.md):
  GET https://mainnet.zklighter.elliot.ai/api/v1/positionFunding
  Параметры (из модели PositionFundings/PositionFunding в
  elliottech/lighter-python): account_index (int, вероятно
  обязательный), market_id (int, опционально -- фильтр по рынку),
  cursor (str, опционально -- пагинация, поле next_cursor в ответе).
  Точный список ОБЯЗАТЕЛЬНЫХ vs опциональных параметров НЕ подтверждён
  дословно -- скрипт передаёт то, что дано в CLI, ничего не додумывает
  сверху.

Модель PositionFunding (elliottech/lighter-python, дословно):
  timestamp (int), market_id (int), funding_id (int), change (str),
  discount (str), rate (str), position_size (str), position_side (str).
`change`, судя по названию поля,-- это ФАКТИЧЕСКИ начисленная/списанная
сумма (не расчётная ставка) -- именно это нужно для калибровки: знак
`change` в паре с `position_side` даёт эмпирический payer/receiver
факт, не требующий понимания семантики `direction` из публичного
эндпоинта `/api/v1/fundings` вообще.

Использование (ТОЛЬКО по команде владельца):
  python analysis/p4_funding_calibration.py --account-index <N> \\
      --confirm-position-is-open
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import requests

BASE_URL = "https://mainnet.zklighter.elliot.ai"
HEADERS = {"User-Agent": "robinhood-chain-alpha-p4-calibration/1.0"}
CACHE_DIR = Path("data/p4_lighter_cache")


class NeedsAuth(RuntimeError):
    """Эндпоинт потребовал авторизацию -- см. модульный докстринг,
    "Об авторизации". Не подделывается, не обходится."""


def fetch_position_fundings(account_index: int, market_id: int | None, max_pages: int = 50) -> list[dict]:
    records: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"account_index": account_index}
        if market_id is not None:
            params["market_id"] = market_id
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(f"{BASE_URL}/api/v1/positionFunding", params=params, headers=HEADERS, timeout=30)
        if resp.status_code in (401, 403):
            raise NeedsAuth(
                f"positionFunding вернул {resp.status_code} для account_index={account_index}. "
                "Публичный доступ (без подписи) не подходит для этого счёта. Нужен официальный "
                "Lighter SDK (lighter.SignerClient, EIP-712-подобная подпись аккаунта) -- эта "
                "заготовка НЕ подделывает и не реализует схему подписи. "
                f"Тело ответа: {resp.text[:500]!r}"
            )
        resp.raise_for_status()
        body = resp.json()
        page = body.get("position_fundings", [])
        if not page:
            break
        records.extend(page)
        cursor = body.get("next_cursor")
        if not cursor:
            break
    return records


def calibrate(records: list[dict]) -> dict:
    """Знак/размер `change` за час, разбито по position_side -- прямой
    эмпирический ответ на вопрос "что значит direction=long/short"."""
    if not records:
        return {"n_records": 0, "note": "Нет записей -- позиция не открыта или account_index неверный."}

    by_side: dict[str, list[float]] = defaultdict(list)
    for r in records:
        try:
            change = float(r.get("change", 0))
        except (TypeError, ValueError):
            continue
        side = str(r.get("position_side", "unknown")).strip().lower()
        by_side[side].append(change)

    summary = {}
    for side, changes in by_side.items():
        n = len(changes)
        n_pos = sum(1 for c in changes if c > 0)
        n_neg = sum(1 for c in changes if c < 0)
        summary[side] = {
            "n_records": n,
            "n_change_positive": n_pos,
            "n_change_negative": n_neg,
            "mean_change": sum(changes) / n if n else None,
            "interpretation": (
                "position_side=%s: change>0 в %d/%d записей -> при этой стороне позиции счёт "
                "ПОЛУЧАЕТ funding в среднем чаще, чем платит" % (side, n_pos, n)
                if n_pos >= n_neg else
                "position_side=%s: change<0 в %d/%d записей -> при этой стороне позиции счёт "
                "ПЛАТИТ funding в среднем чаще, чем получает" % (side, n_neg, n)
            ),
        }
    return {"n_records": len(records), "by_position_side": summary}


def run(account_index: int, market_id: int | None) -> int:
    print(f"[p4_calibration] account_index={account_index} market_id={market_id}")
    try:
        records = fetch_position_fundings(account_index, market_id)
    except NeedsAuth as e:
        print(f"[p4_calibration] СТОП: {e}")
        return 1

    result = calibrate(records)
    print(json.dumps(result, indent=2, default=str))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"p4_funding_calibration_account{account_index}.json"
    out_path.write_text(json.dumps({"account_index": account_index, "market_id": market_id,
                                     "raw_records": records, "summary": result}, indent=2, default=str))
    print(f"[p4_calibration] записано {out_path} (СОДЕРЖИТ построчные данные аккаунта -- "
          f"не коммитить/публиковать без явного решения владельца, в отличие от "
          f"analysis/p4_lighter_markets.py, который пишет только агрегаты в docs/).")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-index", type=int, required=True, help="account_index на Lighter (не секрет, но привязан к реальному счёту)")
    parser.add_argument("--market-id", type=int, default=None, help="Опционально: ограничить одним рынком")
    parser.add_argument(
        "--confirm-position-is-open", action="store_true",
        help="Обязательный флаг -- подтверждение, что позиция реально открыта и запуск санкционирован владельцем.",
    )
    args = parser.parse_args()
    if not args.confirm_position_is_open:
        print(
            "[p4_calibration] СТОП: этот скрипт запускается ТОЛЬКО по команде владельца, "
            "ПОСЛЕ открытия реальной позиции (см. докстринг модуля). Передайте "
            "--confirm-position-is-open осознанно, если это тот самый случай."
        )
        sys.exit(1)
    sys.exit(run(args.account_index, args.market_id))
