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
WHERE LOWER(table_name) LIKE '%robinhood%' OR LOWER(table_schema) LIKE '%robinhood%'
LIMIT 100
"""
# ПЕРВАЯ попытка использовала ILIKE -- реальная ошибка Dune (2026-09-04,
# execution 01M1PBWQ7EFDZDHKBQW2Z6HCTZ): "mismatched input 'ILIKE'.
# Expecting: ... <predicate>" -- парсер Dune для information_schema-запросов
# не принимает ILIKE (в отличие от полного DuneSQL/Trino для основных
# запросов, не проверено отдельно, не обобщаем). LOWER(...) LIKE --
# портируемая замена, не требует ILIKE вообще.
#
# Реальный результат первого прогона: ВСЕ 100 строк (лимит съеден целиком)
# попали в ОДНУ схему 'accountable_v1_1_robinhood' (алфавитно первая) --
# не значит, что других схем по 'robinhood' нет, просто LIMIT 100 без
# ORDER BY отрезал раньше, чем дошёл до остальных. Следующие два запроса --
# сначала список РАЗЛИЧНЫХ схем (дёшево, чтобы не упереться в тот же
# лимит), потом точечный поиск по названиям, похожим на Uniswap v3/пул.
DISTINCT_SCHEMAS_SQL = """
SELECT DISTINCT table_schema
FROM information_schema.tables
WHERE LOWER(table_schema) LIKE '%robinhood%'
LIMIT 100
"""

SWAP_TABLE_SEARCH_SQL = """
SELECT table_catalog, table_schema, table_name
FROM information_schema.tables
WHERE (LOWER(table_schema) LIKE '%robinhood%' OR LOWER(table_name) LIKE '%robinhood%')
  AND (LOWER(table_name) LIKE '%swap%' OR LOWER(table_name) LIKE '%uniswap%'
       OR LOWER(table_name) LIKE '%pool%' OR LOWER(table_name) LIKE '%v3%'
       OR LOWER(table_name) LIKE '%usdg%' OR LOWER(table_schema) LIKE '%uniswap%')
LIMIT 100
"""
# Реальный результат: этот широкий фильтр зашумлён мостом Across
# (across_v3_robinhood -- 'v3' в названии схемы, но это SpokePool моста,
# не DEX) -- алфавитно раньше настоящих кандидатов, LIMIT 100 съеден им
# целиком. distinct_schemas (см. выше) реально нашёл кандидатов в DEX:
# robinswap_v3_robinhood, sushiswap_v3_robinhood, pancakeswap_v3_robinhood,
# ramsesxyz_cl_robinhood, gigadex_v3_robinhood, poolsfun_robinhood -- этот
# третий запрос узко ищет ТОЛЬКО evt_swap (Swap-событие), не 'pool'/'v3'
# вообще, чтобы не собирать мостовые/vault-таблицы снова.
EVT_SWAP_SEARCH_SQL = """
SELECT table_schema, table_name
FROM information_schema.tables
WHERE LOWER(table_schema) LIKE '%robinhood%' AND LOWER(table_name) LIKE '%evt_swap%'
LIMIT 100
"""

# Реальный результат: НЕСКОЛЬКО форков используют идентичную ABI/имя
# таблицы 'uniswapv3pool_evt_swap' / 'clpool_evt_swap' (сам контракт
# UniswapV3Pool переиспользован разными проектами) -- нельзя угадать,
# какая схема держит НАШ конкретный адрес пула
# (0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca, ETH/USDG, см.
# PROJECT_STATE.md §1), не проверив по факту. Один UNION ALL с COUNT(*)
# по contract_address -- ветки независимы (не пересчитывают общий CTE
# друг друга, не тот паттерн, что дал 144 кредита в 03c, см.
# credit_guard.check_sql_sanity) -- определит, в какой схеме реально
# есть строки для этого адреса.
POOL_ADDRESS = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
IDENTIFY_POOL_SCHEMA_SQL = f"""
SELECT 'uniswap_v3_robinhood' AS project, count(*) AS n FROM uniswap_v3_robinhood.uniswapv3pool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'robinswap_robinhood', count(*) FROM robinswap_robinhood.uniswapv3pool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'sushiswap_v3_robinhood', count(*) FROM sushiswap_v3_robinhood.uniswapv3pool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'ramsesxyz_robinhood_v3pool', count(*) FROM ramsesxyz_robinhood.ramsesv3pool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'sheriff_robinhood_pool', count(*) FROM sheriff_robinhood.sheriffpool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'sheriff_robinhood_algebra', count(*) FROM sheriff_robinhood.algebrapool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'gigadex_robinhood_cl', count(*) FROM gigadex_robinhood.clpool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'uponrh_robinhood', count(*) FROM uponrh_robinhood.clpool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'pancakeswap_v3_robinhood', count(*) FROM pancakeswap_v3_robinhood.pancakev3pool_evt_swap WHERE contract_address = {POOL_ADDRESS}
UNION ALL SELECT 'swaphood_robinhood', count(*) FROM swaphood_robinhood.pancakev3pool_evt_swap WHERE contract_address = {POOL_ADDRESS}
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

    def run_query_step(step_name: str, sql: str, estimated_credits: float) -> None:
        """Общая обёртка: LIMIT 100 запрос -> результат/причина падения в
        result['steps'][step_name]. Реальное падение Dune (не гвард) не
        должно ронять весь скрипт без следа -- стоимость уже фиксируется
        гвардом (record_execution) независимо от того, что случится тут."""
        try:
            df = client.run_sql_cached(
                name=step_name, sql=sql, estimated_credits=estimated_credits,
                expected_max_rows=100, expected_columns=5,
            )
            result["steps"][step_name] = {
                "rows": df.to_dict(orient="records") if df is not None else None,
                "n_rows": len(df) if df is not None else 0,
            }
            print(f"[recon] {step_name}: найдено строк {len(df) if df is not None else 0}")
            if df is not None and len(df):
                print(df.to_string())
        except SystemExit as exc:
            result["steps"][step_name] = {"stopped": True, "reason": str(exc)}
            print(f"[recon] {step_name} остановлен гвардом: {exc}")
        except Exception as exc:  # noqa: BLE001
            result["steps"][step_name] = {"failed": True, "reason": str(exc)[:2000]}
            print(f"[recon] {step_name} УПАЛ (не гвард, реальная ошибка Dune): {exc}")

    print("\n=== 2. Поиск схем/таблиц по 'robinhood' (LIMIT 100, information_schema) ===")
    run_query_step("mozila_schema_search_robinhood", SCHEMA_SEARCH_SQL, 2.0)

    print("\n=== 3. Список РАЗЛИЧНЫХ схем по 'robinhood' (предыдущий LIMIT 100 съело одной схемой) ===")
    run_query_step("mozila_distinct_schemas_robinhood", DISTINCT_SCHEMAS_SQL, 2.0)

    print("\n=== 4. Точечный поиск таблиц swap/uniswap/pool/v3/usdg среди robinhood-схем ===")
    run_query_step("mozila_swap_table_search", SWAP_TABLE_SEARCH_SQL, 2.0)

    print("\n=== 5. Узкий поиск ТОЛЬКО evt_swap среди всех robinhood-схем (без шума моста/vault) ===")
    run_query_step("mozila_evt_swap_search", EVT_SWAP_SEARCH_SQL, 2.0)

    print(f"\n=== 6. Идентификация схемы для НАШЕГО адреса пула {POOL_ADDRESS} (UNION ALL COUNT по кандидатам) ===")
    run_query_step("mozila_identify_pool_schema", IDENTIFY_POOL_SCHEMA_SQL, 5.0)

    print(f"\n=== Остаток бюджета разведки (funding_mozila): "
          f"{RECON_BUDGET - load_state()['funding_mozila']['spent']:.2f} из {RECON_BUDGET} ===")
    print(f"=== Остаток цикла (заявленный, не API-подтверждённый): {remaining_cycle_budget():.2f} ===")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[recon] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
