#!/usr/bin/env python3
"""Sprint G1 -- блокирующее требование 2 (пересчитано по подтверждённому
событию TokenLaunched, не по старому прокси "новый v3-пул"): полный
посуточно-... точнее понедельный счёт градуаций за ВЕСЬ период §2.1
(01.07.2026 -> g1_period_end = коней покрытия минус 24ч), обе фабрики
(V1+V2), с дедупом "по первому событию на токен".

Партиционирование по календарным неделям (~9 партиций для периода в
~60 дней) -- один проход = один SELECT сырых логов за неделю, без
UNION ALL по тяжёлым источникам (см. analysis/credit_guard.py,
check_sql_sanity). Каждая неделя кэшируется перманентно сразу после
оплаченной операции (см. dune_client.run_sql_cached) -- переживает
краш/таймаут между неделями.

По завершении: применяет гейт N>=200 (§2.1/2.7). N<200 -> СТОП,
вердикт UNDERPOWERED, дальше не двигаемся ни в Шаг 2 (смоук), ни в
Шаг 3 (полный прогон) -- один из 4 явных случаев возврата к владельцу.

Использование: python analysis/g1_graduation_events.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql
from g1_common import decode_token_launched

DECODED_EVENTS_PATH = Path(CONFIG.g1_cache_dir) / "g1_graduation_events_decoded.csv"


def weekly_partitions(start: str, end_inclusive_ts: str) -> list[tuple[str, str]]:
    """[start 00:00:00, ... 7-дневные шаги ..., end) -- последняя партиция
    обрезается по end_inclusive_ts (последний допустимый t0, включительно),
    т.е. верхняя граница = следующие сутки после даты в end_inclusive_ts."""
    t0 = datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.strptime(end_inclusive_ts.split(" ")[0], "%Y-%m-%d")
    end_exclusive = end_date + timedelta(days=1)  # верхняя граница периода, half-open
    parts = []
    cur = t0
    while cur < end_exclusive:
        nxt = min(cur + timedelta(days=7), end_exclusive)
        parts.append((cur.strftime("%Y-%m-%d %H:%M:%S"), nxt.strftime("%Y-%m-%d %H:%M:%S")))
        cur = nxt
    return parts


def main() -> int:
    client = DuneClient()
    parts = weekly_partitions(CONFIG.g1_period_start, CONFIG.g1_period_end)
    print(f"[g1_graduation_events] {len(parts)} недельных партиций: "
          f"{parts[0][0]} .. {parts[-1][1]} (период §2.1: {CONFIG.g1_period_start} -> {CONFIG.g1_period_end})")

    sql_template = read_sql("g1/g1_token_launched_weekly")
    all_decoded: list[dict] = []
    per_week_counts: list[dict] = []

    for i, (week_start, week_end) in enumerate(parts, start=1):
        name = f"g1_token_launched_week{i:02d}_{week_start[:10]}"
        sql = render_sql(sql_template, {"week_start": week_start, "week_end": week_end})
        print(f"\n===== {name} [{week_start}, {week_end}) (оценка 10.0) =====")
        qid = client.create_query(name, sql)
        df = client.run_sql_cached(
            name, sql, query_id=qid, estimated_credits=10.0,
            expected_max_rows=5000, expected_columns=7,
        )
        n_raw = 0 if df is None else len(df)
        print(f"[g1_graduation_events] {name}: {n_raw} сырых строк TokenLaunched.")
        per_week_counts.append({"week_start": week_start, "week_end": week_end, "n_raw_logs": n_raw})
        if df is not None and n_raw > 0:
            decoded = [decode_token_launched(r) for r in df.to_dict("records")]
            all_decoded.extend(decoded)

    if not all_decoded:
        print("\n[g1_graduation_events] СТОП: ноль событий TokenLaunched за весь период. "
              "N=0 < 200 -> UNDERPOWERED.")
        write_underpowered_note(n_total=0, per_week_counts=per_week_counts)
        return 1

    events_df = pd.DataFrame(all_decoded)
    events_df["block_time"] = pd.to_datetime(events_df["block_time"])
    # Дедуп "по первому событию на токен" (в рамках ОДНОЙ фабрики токен
    # градуируется один раз; дедуп защищает от редких дублей на границах
    # партиций/повторных событий) -- берём строку с MIN(block_time) per token.
    events_df = events_df.sort_values("block_time")
    n_before_dedup = len(events_df)
    deduped = events_df.drop_duplicates(subset=["token"], keep="first").reset_index(drop=True)
    n_total = len(deduped)
    n_dupes = n_before_dedup - n_total

    DECODED_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    deduped.to_csv(DECODED_EVENTS_PATH, index=False)
    commit_decoded_cache()

    print(f"\n[g1_graduation_events] Сырых событий: {n_before_dedup}, дублей по token схлопнуто: "
          f"{n_dupes}, N (уникальных градуаций) = {n_total}.")
    print(f"[g1_graduation_events] Первая градуация в выборке: {deduped['block_time'].min()}, "
          f"последняя: {deduped['block_time'].max()}.")
    print(pd.DataFrame(per_week_counts).to_string())

    if n_total < CONFIG.g1_min_n_events:
        print(f"\n[g1_graduation_events] СТОП: N={n_total} < {CONFIG.g1_min_n_events} -> "
              "UNDERPOWERED. Это один из 4 явных случаев возврата к владельцу. "
              "Дальше (смоук/полный прогон) не двигаюсь.")
        write_underpowered_note(n_total=n_total, per_week_counts=per_week_counts, deduped=deduped)
        return 1

    print(f"\n[g1_graduation_events] N={n_total} >= {CONFIG.g1_min_n_events} -- гейт ПРОЙДЕН. "
          "Продолжаю в Шаг 2 (смоук-тест) без остановки.")
    write_n_gate_note(n_total=n_total, n_dupes=n_dupes, per_week_counts=per_week_counts, deduped=deduped)
    return 0


def commit_decoded_cache() -> None:
    import subprocess
    try:
        subprocess.run(["git", "add", str(DECODED_EVENTS_PATH)], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if diff.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", f"sprintG1_cache: g1_graduation_events_decoded.csv [automated]"], check=False)
        subprocess.run(["git", "push"], check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[g1_graduation_events] ПРЕДУПРЕЖДЕНИЕ: не удалось закоммитить decoded cache: {exc}")


def write_underpowered_note(n_total: int, per_week_counts: list[dict], deduped=None) -> None:
    design_path = Path(CONFIG.g1_design_doc)
    text = design_path.read_text()
    marker = "## Полнопериодный счёт градуаций и гейт N (2026-09-01)"
    if marker in text:
        return
    note = f"""

{marker}

**Вердикт: UNDERPOWERED.** N = {n_total} < {CONFIG.g1_min_n_events} (§2.1/2.7). Дальнейшие
оплаченные шаги (смоук-тест, полный прогон) НЕ выполняются -- решение
за владельцем (§2.7: "N < 200 -> UNDERPOWERED (решение за владельцем)").

Посуточно/понедельно (партиции, событие TokenLaunched, обе фабрики):
{pd.DataFrame(per_week_counts).to_string(index=False)}
"""
    design_path.write_text(text + note)


def write_n_gate_note(n_total: int, n_dupes: int, per_week_counts: list[dict], deduped: pd.DataFrame) -> None:
    design_path = Path(CONFIG.g1_design_doc)
    text = design_path.read_text()
    marker = "## Полнопериодный счёт градуаций и гейт N (2026-09-01)"
    if marker in text:
        return
    note = f"""

{marker}

**Гейт N>=200 (§2.1/2.7): ПРОЙДЕН.** N = {n_total} уникальных градуаций
(событие `TokenLaunched`, обе фабрики V1+V2, дедуп по первому событию
на токен -- {n_dupes} дублей схлопнуто) за период
{CONFIG.g1_period_start} -> {CONFIG.g1_period_end}.

Первая градуация в выборке: {deduped['block_time'].min()}. Последняя:
{deduped['block_time'].max()}.

Понедельные партиции (сырые строки TokenLaunched до дедупа):
{pd.DataFrame(per_week_counts).to_string(index=False)}

Полный декодированный список событий закэширован постоянно:
`{DECODED_EVENTS_PATH}` (token, deployer, dex_factory, pair_token, pool,
dex_id, launch_config_id, position_id, restrictions_end_block,
initial_buy_amount, block_number, block_time, tx_hash).
"""
    design_path.write_text(text + note)


if __name__ == "__main__":
    raise SystemExit(main())
