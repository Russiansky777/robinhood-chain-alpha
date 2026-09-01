#!/usr/bin/env python3
"""Одноразовое восстановление уже ОПЛАЧЕННОГО результата
g1_v2_full_extract в постоянный кэш -- run #20: execute() успешно
исполнился и был оплачен (34.230 кредита), но >2x-гард (факт 34.23
против оценки 12.0 -- 2.85x И >= абсолютного минимума 25, см.
credit_guard.OVERRUN_MIN_ABSOLUTE) остановил пайплайн СРАЗУ после
записи execute-стоимости, до вызова get_results_df. Результат
существует на Dune (execution COMPLETED), не был скачан. Тот же
паттерн, что recover_g1_v2_quote_distribution_full.py /
recover_03b_result.py.

Метод: ОДНО дополнительное чтение уже завершённого execution_id через
DuneClient.fetch_existing (бесплатный status + гейтованное платное
чтение результата -- 896 строк x 26 колонок, по калибровке read-
стоимости в этом проекте (~2.71e-5/датапоинт x1.5 запас) это ~0.9-1
кредит, на порядок дешевле, чем пересчёт execute() (34.23)). Пишет в
постоянный data/sprintG1_cache/g1_v2_full_extract_<hash>.csv с ТЕМ ЖЕ
ключом (sha256 SQL-текста), что использует run_sql_cached /
run_extract_calibrated -- следующий прогон найдёт его как обычный
постоянный кэш-хит.

Использование: python analysis/recover_g1_v2_full_extract.py
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from dune_client import DuneClient
from g1_pipeline import load_full_v2_events, build_extract_query

# execution_id из лога run #20 (run_sprint_g1.yml run id 33513889051) --
# g1_v2_full_extract успешно исполнился (34.230 кредита), но 2x-гард
# (с абсолютным минимумом 25) остановил пайплайн ДО чтения результата.
EXEC_ID_FULL_EXTRACT = "01M1EJQ2883JXAWE99KTYBNPW2"


def main() -> int:
    client = DuneClient()

    events = load_full_v2_events(client)
    print(f"[recover_full_extract] Загружено {len(events)} v2-градуаций (кэш, 0 кредитов).")
    sql = build_extract_query(events)
    cache_key = hashlib.sha256(sql.encode()).hexdigest()[:16]
    permanent_cache_file = Path("data/sprintG1_cache") / f"g1_v2_full_extract_{cache_key}.csv"

    if permanent_cache_file.exists():
        print(f"[recover_full_extract] {permanent_cache_file} уже существует -- нечего восстанавливать.")
        return 0

    n_cols = 6 + 2 * len(CONFIG.g1_horizons_s)
    print(
        f"== Восстанавливаю g1_v2_full_extract из execution_id={EXEC_ID_FULL_EXTRACT} "
        f"(ключ кэша {cache_key}, ожидаю <= {len(events)} строк x {n_cols} колонок) =="
    )
    df, status, result_stats = client.fetch_existing(
        EXEC_ID_FULL_EXTRACT, name="g1_v2_full_extract_recovery", expected_max_rows=len(events),
        expected_columns=n_cols,
    )
    print(f"  Восстановлено {len(df)} строк x {len(df.columns)} колонок.")

    permanent_cache_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(permanent_cache_file, index=False)
    client._commit_permanent(
        permanent_cache_file,
        f"sprintG1_cache: восстановлен g1_v2_full_extract ({len(df)} строк) [automated]",
    )
    print(f"[recover_full_extract] Записано и закоммичено: {permanent_cache_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
