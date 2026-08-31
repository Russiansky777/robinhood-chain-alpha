#!/usr/bin/env python3
"""Одноразовое восстановление уже ОПЛАЧЕННОГО результата
03b_cohort_selection в постоянный кэш -- см. docs/COST_POSTMORTEM.md,
ревизия 4: run #3 успешно исполнил и прочитал 03b (22.895 + 0.257
кредита), но результат осел только в эфемерном analysis/output/cache/,
которое умерло вместе с проваленным (из-за нехватки бюджета на
следующем шаге) джобом -- actions/cache пропускает свой post-save шаг
при ненулевом коде выхода. Без этого скрипта следующая попытка
пересчитала бы 03b ТРЕТИЙ раз (~23 кредита впустую).

Метод: одно ДОПОЛНИТЕЛЬНОЕ чтение уже завершённого execution_id (через
DuneClient.fetch_existing -- бесплатный status + гейтованное чтение
результата, ~0.26 кредита на 527 строк) -- на порядок дешевле, чем
пересчёт execute(). Пишет результат в постоянный
data/sprint15_cache/03b_cohort_selection_<hash>.csv с ТЕМ ЖЕ ключом
(sha256 отрендеренного SQL), что использует run_sql_cached -- следующий
прогон найдёт его как обычный постоянный кэш-хит.

Использование:
    python analysis/recover_03b_result.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql, substitute_query_refs, q_ts, q_list

# execution_id из лога run #3 (run_sprint_1_5.yml run id 33450621184) --
# 03b успешно исполнился и был прочитан (527 строк), но результат не
# пережил провал джоба на следующем шаге (бюджет).
EXEC_ID_03B = "01M1D29ST7R4EW7B3GRH2HVPRD"


def main() -> int:
    client = DuneClient()

    query_ids: dict[str, int] = {
        "02_swaps_raw_july": client.create_query(
            "02_swaps_raw_july",
            render_sql(read_sql("02_swaps_raw_july"), {"start_date": q_ts(CONFIG.train_start), "end_date": q_ts(CONFIG.train_end)}),
            require_cached=True,
        ),
    }
    query_ids["01_pool_creation_blocks"] = client.create_query(
        "01_pool_creation_blocks", substitute_query_refs(read_sql("01_pool_creation_blocks"), query_ids), require_cached=True
    )
    base_tokens_sql = q_list(list(CONFIG.base_token_symbols))
    query_ids["03_wallet_agg_july"] = client.create_query(
        "03_wallet_agg_july",
        render_sql(substitute_query_refs(read_sql("03_wallet_agg_july"), query_ids), {"base_token_symbols": base_tokens_sql}),
        require_cached=True,
    )

    combo_params = {
        "sniper_window_primary_minutes": CONFIG.sniper_time_window_minutes,
        "sniper_window_sensitivity_minutes": CONFIG.sniper_time_window_minutes_sensitivity,
        "cap_primary": CONFIG.copyability_max_trades,
        "cap_sensitivity": CONFIG.copyability_max_trades_sensitivity,
        "min_trades": CONFIG.min_trades,
        "min_unique_tokens": CONFIG.min_unique_tokens,
    }
    sql_03b = render_sql(
        substitute_query_refs(read_sql("03b_cohort_selection"), query_ids),
        {**combo_params, "cohort_size": CONFIG.cohort_size, "cohort_seed": "sprint15-seed42"},
    )
    cache_key = hashlib.sha256(sql_03b.encode()).hexdigest()[:16]
    permanent_cache_file = Path("data/sprint15_cache") / f"03b_cohort_selection_{cache_key}.csv"

    if permanent_cache_file.exists():
        print(f"[recover_03b] {permanent_cache_file} уже существует -- нечего восстанавливать.")
        return 0

    print(f"== Восстанавливаю 03b_cohort_selection из execution_id={EXEC_ID_03B} (ключ кэша {cache_key}) ==")
    df, status, result_stats = client.fetch_existing(
        EXEC_ID_03B, name="03b_cohort_selection_recovery", expected_max_rows=2000
    )
    print(f"  Восстановлено {len(df)} строк.")

    permanent_cache_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(permanent_cache_file, index=False)
    client._commit_permanent(
        permanent_cache_file, f"sprint15_cache: восстановлен 03b_cohort_selection ({len(df)} строк) [automated]"
    )
    print(f"[recover_03b] Записано и закоммичено: {permanent_cache_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
