#!/usr/bin/env python3
"""Одноразовое восстановление уже ОПЛАЧЕННОГО результата
g1_v2_quote_distribution_full в постоянный кэш -- владелец, 2026-09-01,
решение 3: "quote_distribution_full уже оплачен -- используй результат,
не перезапускай". Run #18: execute() успешно исполнился и был оплачен
(20.089 кредита), но чтение результата так и не случилось --
check_overrun_after_execute (2x-гард) сработал и остановил пайплайн
СРАЗУ после записи execute-стоимости, до вызова get_results_df.
Результат существует на Dune (execution COMPLETED), просто не был
скачан. Тот же паттерн, что analysis/recover_03b_result.py.

Метод: ОДНО дополнительное чтение уже завершённого execution_id через
DuneClient.fetch_existing (бесплатный status + гейтованное платное
чтение результата -- результат крошечный, несколько строк
quote_symbol/n_trades/n_tokens/vol_usd, читать копейки). Пишет в
постоянный data/sprintG1_cache/g1_v2_quote_distribution_full_<hash>.csv
с ТЕМ ЖЕ ключом (sha256 SQL-текста), что использует run_sql_cached /
run_quote_distribution_calibrated -- следующий прогон найдёт его как
обычный постоянный кэш-хит и не будет платить за execute повторно.

Использование: python analysis/recover_g1_v2_quote_distribution_full.py
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient
from g1_pipeline import load_full_v2_events, build_quote_distribution_query

# execution_id из лога run #18 (run_sprint_g1.yml run id 33511681014) --
# g1_v2_quote_distribution_full успешно исполнился (20.089 кредита), но
# 2x-гард остановил пайплайн ДО чтения результата.
EXEC_ID_QUOTE_FULL = "01M1EHFN7VBTRN4P0XXRNNCP2K"


def main() -> int:
    client = DuneClient()

    events = load_full_v2_events(client)
    print(f"[recover_quote_full] Загружено {len(events)} v2-градуаций (кэш, 0 кредитов).")
    sql = build_quote_distribution_query(events)
    cache_key = hashlib.sha256(sql.encode()).hexdigest()[:16]
    permanent_cache_file = Path("data/sprintG1_cache") / f"g1_v2_quote_distribution_full_{cache_key}.csv"

    if permanent_cache_file.exists():
        print(f"[recover_quote_full] {permanent_cache_file} уже существует -- нечего восстанавливать.")
        return 0

    print(f"== Восстанавливаю g1_v2_quote_distribution_full из execution_id={EXEC_ID_QUOTE_FULL} (ключ кэша {cache_key}) ==")
    df, status, result_stats = client.fetch_existing(
        EXEC_ID_QUOTE_FULL, name="g1_v2_quote_distribution_full_recovery", expected_max_rows=50,
    )
    print(f"  Восстановлено {len(df)} строк.")
    print(df.to_string(index=False))

    permanent_cache_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(permanent_cache_file, index=False)
    client._commit_permanent(
        permanent_cache_file,
        f"sprintG1_cache: восстановлен g1_v2_quote_distribution_full ({len(df)} строк) [automated]",
    )
    print(f"[recover_quote_full] Записано и закоммичено: {permanent_cache_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
