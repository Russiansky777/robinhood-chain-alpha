#!/usr/bin/env python3
"""Владелец, 2026-09-04: разведка нового Dune-аккаунта (secrets.
DUNE_API_KEY_MOZILA, заявлено 2000+ кредитов) -- ОТДЕЛЬНОГО от старого
(secrets.DUNE_API_KEY, 2500/цикл, привязан к billing_cycle.external_truth
в data/credits_spent.json). Этот скрипт использует ОТДЕЛЬНЫЙ леджер
(CREDIT_GUARD_FILE=data/credits_spent_mozila.json, CREDIT_GUARD_NAMESPACE=
funding_mozila, задаются в workflow env) -- см. правку credit_guard.py.

Цель (п.1 задания владельца, "до трат"):
  - какие таблицы по Robinhood Chain существуют на Dune;
  - декодированы ли Swap-события Uniswap v3;
  - с какой даты покрытие;
  - есть ли трансферы сток-токенов (ERC20 transfer events);
  - фактический остаток кредитов и стоимость выполнения запроса.

Правило владельца (п.3): каждый запрос -- сначала LIMIT 100 для проверки
синтаксиса/структуры. Здесь ВСЕ запросы уже ограничены LIMIT 100 --
это ПЕРВЫЙ содержательный контакт с этим аккаунтом, полных прогонов нет.

Стоимость каждого шага печатается и пишется в отдельный леджер --
владелец, "фактическую стоимость каждого запуска записывать".
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "funding_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state, CREDITS_FILE
from dune_client import DuneClient

OUT_PATH = Path("data/p3_guard_cache/dune_mozila_schema_recon_result.json")

# Разведочный бюджет (владелец: "Кредиты сверх разведки -- только по
# решению владельца") -- намеренно маленький, дальше без явного решения
# владельца не расширяется этим скриптом.
RECON_BUDGET = 50.0


def ensure_ledger_seeded() -> None:
    """Если это первый запуск нового леджера -- инициализируем
    billing_cycle с ЗАЯВЛЕННЫМ владельцем (не API-подтверждённым на
    момент написания) остатком, явно помеченным как такой. Реальный
    остаток уточняется ниже (probe_account_endpoints) и, если API его
    не отдаёт (как было со старым аккаунтом), остаётся требованием к
    владельцу свериться с dune.com/settings/billing вручную -- не
    выдумываем точное число."""
    if CREDITS_FILE.exists():
        return
    state = {
        "billing_cycle": {
            "external_limit": 2000.0,
            "initialized_spent": 0.0,
            "initialized_at": "2026-09-04",
            "initialized_by": (
                "владелец, устно ('2000+ кредитов') -- НЕ подтверждено API на момент "
                "инициализации, см. account_endpoint_probe в результате этого скрипта. "
                "ОТДЕЛЬНЫЙ аккаунт от data/credits_spent.json (secrets.DUNE_API_KEY, "
                "2500/цикл) -- бюджеты НЕ смешивать."
            ),
            "reset_at": None,
            "reset_at_source": "неизвестно -- новый аккаунт, дата сброса цикла не сообщена владельцем",
            "external_truth": {
                "cycle_spent": 0.0,
                "as_of": "2026-09-04",
                "source": "инициализация с нуля (новый аккаунт), намеренно НЕТ калибровки по факту",
                "namespace_snapshot_at_anchor": {"funding_mozila": 0.0},
                "reserve_buffer": 20.0,
                "reserve_buffer_source": "тот же неснижаемый страховой остаток, что и в старом аккаунте (владелец, 2026-09-01)",
            },
        },
        "funding_mozila": {"budget_remaining_at_init": RECON_BUDGET, "spent": 0.0},
        "entries": [],
    }
    CREDITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDITS_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    print(f"[recon] новый леджер {CREDITS_FILE} создан: лимит цикла 2000 (заявлено владельцем, не API), "
          f"бюджет разведки {RECON_BUDGET}.")


def probe_account_endpoints(client: DuneClient) -> dict:
    """Те же бесплатные account/usage эндпоинты, что audit_dune_account.py
    пробовал на СТАРОМ аккаунте (там ни один не сработал на free tier) --
    новый аккаунт может быть на другом плане, стоит перепробовать вживую,
    не полагаясь на прошлый отрицательный результат."""
    results = {}
    for path in ["/user", "/account", "/account/usage", "/billing", "/user/usage", "/team",
                 "/subscription", "/user/credits", "/usage", "/credits"]:
        try:
            resp = client._get(path)
            print(f"[recon] GET {path}: OK -- {json.dumps(resp, ensure_ascii=False, default=str)[:500]}")
            results[path] = resp
        except Exception as exc:
            print(f"[recon] GET {path}: {str(exc)[:150]}")
            results[path] = {"error": str(exc)[:300]}
    return results


SCHEMA_SEARCH_SQL = """
SELECT table_catalog, table_schema, table_name
FROM information_schema.tables
WHERE table_name ILIKE '%robinhood%' OR table_schema ILIKE '%robinhood%'
LIMIT 100
"""


def run() -> int:
    ensure_ledger_seeded()
    ensure_namespace("funding_mozila", RECON_BUDGET)

    client = DuneClient()
    result: dict = {"generated_at_utc": None, "steps": {}}
    import time
    result["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print("=== 1. Account/usage эндпоинты (бесплатно, метаданные) ===")
    result["steps"]["account_endpoint_probe"] = probe_account_endpoints(client)

    print("\n=== 2. Поиск схем/таблиц по 'robinhood' (LIMIT 100, information_schema) ===")
    try:
        df = client.run_sql_cached(
            name="mozila_schema_search_robinhood",
            sql=SCHEMA_SEARCH_SQL,
            estimated_credits=2.0,
            expected_max_rows=100,
            expected_columns=3,
        )
        result["steps"]["schema_search"] = {
            "rows": df.to_dict(orient="records") if df is not None else None,
            "n_rows": len(df) if df is not None else 0,
        }
        print(f"[recon] найдено строк: {len(df) if df is not None else 0}")
        if df is not None and len(df):
            print(df.to_string())
    except SystemExit as exc:
        result["steps"]["schema_search"] = {"stopped": True, "reason": str(exc)}
        print(f"[recon] schema_search остановлен гвардом: {exc}")

    print(f"\n=== Остаток бюджета разведки (funding_mozila): "
          f"{RECON_BUDGET - load_state()['funding_mozila']['spent']:.2f} из {RECON_BUDGET} ===")
    print(f"=== Остаток цикла (заявленный, не API-подтверждённый): {remaining_cycle_budget():.2f} ===")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[recon] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
