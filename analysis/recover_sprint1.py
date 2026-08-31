"""Переиспользование дорогих результатов Sprint 1 в Sprint 1.5, вместо
их пересчёта.

Два уровня переиспользования, по возрастанию стоимости:

1. **Локальные файлы `/data/sprint1_reused/*.csv.gz`, закоммиченные в
   git.** Если уже сохранены (этим же модулем, при первом успешном
   восстановлении) — читаем их напрямую, БЕЗ единого обращения к Dune.
   Переживают что угодно: смерть контейнера, протухание actions/cache,
   даже отзыв прав на исходные execution_id -- в отличие от кэша,
   зависящего от инфраструктуры CI, это просто файлы в репозитории.

2. **Execution ID из лога прошлого прогона.** Если локальных файлов ещё
   нет (первый запуск после Sprint 1), читаем результаты по execution_id
   трёх самых дорогих запросов последнего успешного прогона Sprint 1
   (workflow run #13, 2026-08-31, commit `390fb7a`) — видны в логе CI,
   т.к. analysis/dune_client.py теперь ВСЕГДА печатает query_id/
   execution_id именно чтобы это больше не терялось. Это два дешёвых GET
   (status + results) на запрос, БЕЗ create_query/execute -- то есть без
   повторного счёта за исполнение. Результат сразу сохраняется в
   /data/sprint1_reused/, чтобы следующий прогон (даже с нуля) попал в
   уровень 1 и не тронул Dune вовсе.

Не восстанавливаем 02_swaps_raw_july и 01_pool_creation_blocks: их
execution_id никогда не печатался (раньше run_sql_cached логировал id
только для шагов с fetch_results=True) -- пробел исправлен в
dune_client.py заодно с этим спринтом. 02/01 в Sprint 1.5 пересчитываются
заново (~103.4 кредита, неизбежно на этот раз).
"""
from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from dune_client import DuneClient

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data" / "sprint1_reused"

# Execution ID из лога run #13 (workflow run_pipeline.yml, run id 33436780374).
SPRINT1_EXECUTIONS = {
    "03_wallet_agg_july": "01M1CSQ10H865136PGXEWBS6KP",
    "04_sniper_insider_exclusions_5m": "01M1CSRT5K746CDMXPJ4B7YS6V",
    "05_final_cohort_pool_july_mt10_5m": "01M1CSS7B0RQ7X778HXHNCKD2E",
}

# Реальная стоимость этих трёх запросов, как она была залогирована в
# run #13 -- используется только для расчёта "сэкономлено против
# пересчёта" в отчёте, не для каких-либо решений в коде.
SPRINT1_ORIGINAL_COST = {
    "03_wallet_agg_july": 25.470192308,
    "04_sniper_insider_exclusions_5m": 1.897236843,
    "05_final_cohort_pool_july_mt10_5m": 22.073269231,
}


@dataclass
class RecoveredBaseline:
    df_agg_july: pd.DataFrame
    df_excluded_5m: pd.DataFrame
    df_gated_mt10_5m: pd.DataFrame
    query_id_03: int | None
    recovered: bool
    from_local_files: bool
    savings_credits: float
    note: str


def _local_path(step: str) -> Path:
    return DATA_DIR / f"{step}.csv.gz"


def _load_local() -> dict[str, pd.DataFrame] | None:
    if not all(_local_path(step).exists() for step in SPRINT1_EXECUTIONS):
        return None
    dfs = {}
    for step in SPRINT1_EXECUTIONS:
        with gzip.open(_local_path(step), "rt") as f:
            dfs[step] = pd.read_csv(f)
    return dfs


def _save_local(dfs: dict[str, pd.DataFrame]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for step, df in dfs.items():
        with gzip.open(_local_path(step), "wt") as f:
            df.to_csv(f, index=False)
    readme = DATA_DIR / "README.md"
    readme.write_text(
        "# Переиспользованные результаты Sprint 1\n\n"
        "Сохранены automatически `analysis/recover_sprint1.py` при первом "
        "успешном восстановлении в Sprint 1.5 (2026-08-31), из execution_id "
        "прогона Sprint 1 (workflow run #13, commit `390fb7a`). Позволяют "
        "повторным запускам Sprint 1.5 не трогать Dune вовсе для этих трёх "
        "запросов -- ни execute, ни даже чтение существующего execution.\n\n"
        "| Файл | Строк | Исходный execution_id | Исходная стоимость |\n"
        "|---|---|---|---|\n"
        + "\n".join(
            f"| `{step}.csv.gz` | {len(dfs[step])} | `{SPRINT1_EXECUTIONS[step]}` | "
            f"{SPRINT1_ORIGINAL_COST[step]:.2f} кредитов |"
            for step in SPRINT1_EXECUTIONS
        )
        + "\n"
    )


def recover_baseline(client: DuneClient) -> RecoveredBaseline:
    print("== Попытка переиспользовать результаты Sprint 1 (без пересчёта) ==")

    local = _load_local()
    if local is not None:
        savings = sum(SPRINT1_ORIGINAL_COST.values())
        print(
            f"  [recover] Найдены локальные файлы в {DATA_DIR} (закоммичены ранее) -- "
            f"читаю их напрямую, Dune не трогаю вообще. Сэкономлено: ~{savings:.2f} кредитов "
            f"(и даже без дешёвых GET-запросов на этот раз)."
        )
        return RecoveredBaseline(
            df_agg_july=local["03_wallet_agg_july"],
            df_excluded_5m=local["04_sniper_insider_exclusions_5m"],
            df_gated_mt10_5m=local["05_final_cohort_pool_july_mt10_5m"],
            query_id_03=None,  # локальные файлы не несут query_id -- см. note
            recovered=True,
            from_local_files=True,
            savings_credits=savings,
            note=(
                "Восстановлено из закоммиченных /data/sprint1_reused/*.csv.gz. "
                "query_id_03 недоступен этим путём -- если понадобится НОВЫЙ запрос, "
                "ссылающийся на query_03_wallet_agg_july (например, другой sniper-гейт), "
                "03 будет пересчитан заново с нуля в этом прогоне (см. main())."
            ),
        )

    dfs: dict[str, pd.DataFrame] = {}
    query_ids_recovered: dict[str, int] = {}
    try:
        for step, execution_id in SPRINT1_EXECUTIONS.items():
            df, status, _stats = client.fetch_existing(execution_id)
            dfs[step] = df
            query_ids_recovered[step] = status.get("query_id")
            print(
                f"  [recover] {step}: {len(df)} строк восстановлено из execution "
                f"{execution_id} (query_id={status.get('query_id')}, "
                f"исходная стоимость исполнения была {SPRINT1_ORIGINAL_COST[step]:.2f} "
                f"кредитов -- сейчас НЕ потрачено повторно)"
            )
    except Exception as e:  # noqa: BLE001 -- любая причина -> честный fallback на пересчёт
        print(f"  [recover] НЕ УДАЛОСЬ восстановить результаты Sprint 1: {e}")
        print("  [recover] Продолжаю с пересчётом 03/04/05 с нуля (обычный путь).")
        return RecoveredBaseline(
            df_agg_july=pd.DataFrame(),
            df_excluded_5m=pd.DataFrame(),
            df_gated_mt10_5m=pd.DataFrame(),
            query_id_03=None,
            recovered=False,
            from_local_files=False,
            savings_credits=0.0,
            note=f"Восстановление не удалось: {e}",
        )

    savings = sum(SPRINT1_ORIGINAL_COST.values())
    print(f"  [recover] Восстановлено полностью через Dune API. Сэкономлено против пересчёта: ~{savings:.2f} кредитов.")
    _save_local(dfs)
    print(f"  [recover] Сохранено в {DATA_DIR} для будущих прогонов (уровень 1, вообще без Dune).")

    return RecoveredBaseline(
        df_agg_july=dfs["03_wallet_agg_july"],
        df_excluded_5m=dfs["04_sniper_insider_exclusions_5m"],
        df_gated_mt10_5m=dfs["05_final_cohort_pool_july_mt10_5m"],
        query_id_03=query_ids_recovered["03_wallet_agg_july"],
        recovered=True,
        from_local_files=False,
        savings_credits=savings,
        note="Все три результата (03, 04@5мин, 05@mt10/5мин) переиспользованы из Sprint 1 через Dune API (status+results).",
    )
