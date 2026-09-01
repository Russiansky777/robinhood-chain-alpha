#!/usr/bin/env python3
"""Sprint G1 -- владелец, 2026-09-01, Задача 4: пост-фильтровые счёты по
неделям. §2.7's N -- это размер ВЫБОРКИ ПОСЛЕ фильтра торгуемости
(§2.2: buy vol >= $250 И >= 3 сделки в (t0; t0+30с]), а не сырых
событий (266 221). Полный джойн 266K событий против свопов -- слишком
дорого и снова нарушил бы "агрегаты, не сырые данные наружу" на этапе
исполнения; вместо этого -- случайная выборка (владелец разрешил
заранее ≤5K per-event строк для эксплораторных целей) на неделю из уже
закэшированных декодированных событий (0 доп. кредитов, локально), и
ОДИН агрегатный запрос на неделю (VALUES-джойн против dex.trades,
жёстко ограниченный по дате этой недели для partition pruning) --
наружу только (n_sampled, n_passing) на неделю, не сырые строки.

Пост-фильтровый N экстраполируется как raw_N_week * (n_passing/n_sampled),
суммируется по неделям.

Использование: python analysis/g1_postfilter_sample.py
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG
from dune_client import DuneClient

N_PER_WEEK = 200  # см. docstring -- суммарно намного меньше разрешённых 5K
RANDOM_SEED = 42

DECODED_EVENTS_PATH = Path(CONFIG.g1_cache_dir) / "g1_graduation_events_decoded.csv"
GENERATED_SQL_DIR = Path("sql/g1/generated")


def weekly_partitions(start: str, end_inclusive_ts: str) -> list[tuple[str, str]]:
    t0 = datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.strptime(end_inclusive_ts.split(" ")[0], "%Y-%m-%d")
    end_exclusive = end_date + timedelta(days=1)
    parts = []
    cur = t0
    while cur < end_exclusive:
        nxt = min(cur + timedelta(days=7), end_exclusive)
        parts.append((cur.strftime("%Y-%m-%d %H:%M:%S"), nxt.strftime("%Y-%m-%d %H:%M:%S")))
        cur = nxt
    return parts


def build_query(week_start: str, week_end: str, sample: pd.DataFrame) -> str:
    """VALUES-джойн: sample -- (token, pool, t0) для этой недели. Джойн
    против dex.trades БЕЗ фильтра по project (см. владелец: "не хардкодь
    один DEX -- есть dexId/dexFactory, запуски могут идти в разные
    пулы") -- матчим напрямую по pool_address (высокоселективно) и per-
    event узкому окну (t0; t0+30с]. Жёсткая граница по block_time на
    диапазон НЕДЕЛИ (не полного периода) -- даёт partition pruning на
    стороне Dune, тот же принцип, что держал недельные партиции в run #9
    дешёвыми (0.26-1.44 кредита/неделя на EXECUTE)."""
    rows = []
    for _, r in sample.iterrows():
        # r['block_time'] -- tz-aware pd.Timestamp (UTC); Trino's bare
        # `timestamp '...'` литерал не принимает offset-суффикс (+00:00) --
        # тот же формат, что уже успешно работает во всех предыдущих
        # запросах проекта (см. sql/g1/*.sql). tz_localize(None) убирает
        # только СУФФИКС, значение уже в UTC, час не сдвигается.
        t0_str = r["block_time"].tz_localize(None).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(f"(0x{r['token'].removeprefix('0x')}, 0x{r['pool'].removeprefix('0x')}, timestamp '{t0_str}')")
    values_sql = ",\n        ".join(rows)
    return f"""-- Сгенерировано analysis/g1_postfilter_sample.py -- неделя [{week_start}, {week_end}).
-- Случайная выборка {len(sample)} событий из data/sprintG1_cache/g1_graduation_events_decoded.csv
-- (seed={RANDOM_SEED}), НЕ полный список -- см. владелец, разрешение на
-- выборку <=5K per-event строк для эксплораторных/оценочных целей.
with sample(token, pool, t0) as (
    values
        {values_sql}
),
buys as (
    select s.token, s.t0,
        count(dt.block_time) as n_buys,
        coalesce(sum(dt.amount_usd), 0) as vol_usd
    from sample s
    left join dex.trades dt
        on dt.blockchain = 'robinhood'
        and dt.project_contract_address = s.pool
        and dt.token_bought_address = s.token
        and dt.block_time > s.t0
        and dt.block_time <= s.t0 + interval '30' second
        and dt.block_time >= timestamp '{week_start}'
        and dt.block_time <  timestamp '{week_end}'
    group by s.token, s.t0
)
select
    count(*) as n_sampled,
    count(*) filter (where n_buys >= 3 and vol_usd >= 250) as n_passing
from buys
"""


def main() -> int:
    if not DECODED_EVENTS_PATH.exists():
        print(f"[g1_postfilter_sample] СТОП: {DECODED_EVENTS_PATH} не найден -- нужен Шаг 'count' (run #9).")
        return 1
    events = pd.read_csv(DECODED_EVENTS_PATH)
    events["block_time"] = pd.to_datetime(events["block_time"])
    rng = random.Random(RANDOM_SEED)

    parts = weekly_partitions(CONFIG.g1_period_start, CONFIG.g1_period_end)
    client = DuneClient()
    GENERATED_SQL_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for i, (week_start, week_end) in enumerate(parts, start=1):
        week_events = events[
            (events["block_time"] >= pd.Timestamp(week_start, tz="UTC"))
            & (events["block_time"] < pd.Timestamp(week_end, tz="UTC"))
        ]
        n_raw = len(week_events)
        if n_raw == 0:
            results.append({"week_start": week_start, "week_end": week_end, "n_raw": 0, "n_sampled": 0, "n_passing": 0})
            print(f"[g1_postfilter_sample] week{i:02d} {week_start}: 0 сырых событий, пропуск.")
            continue
        n_sample = min(N_PER_WEEK, n_raw)
        sample = week_events.sample(n=n_sample, random_state=rng.randint(0, 2**31))

        sql = build_query(week_start, week_end, sample)
        name = f"g1_postfilter_week{i:02d}_{week_start[:10]}"
        sql_file = GENERATED_SQL_DIR / f"{name}.sql"
        sql_file.write_text(sql)

        print(f"\n===== {name} (n_sample={n_sample}/{n_raw}, оценка 5.0) =====")
        qid = client.create_query(name, sql)
        df = client.run_sql_cached(
            name, sql, query_id=qid, estimated_credits=5.0,
            expected_max_rows=2, expected_columns=2,
        )
        if df is None or len(df) == 0:
            print(f"[g1_postfilter_sample] {name}: пустой результат -- неожиданно для агрегата, пропуск недели.")
            results.append({"week_start": week_start, "week_end": week_end, "n_raw": n_raw, "n_sampled": n_sample, "n_passing": None})
            continue
        n_sampled_actual = int(df.iloc[0]["n_sampled"])
        n_passing = int(df.iloc[0]["n_passing"])
        print(f"[g1_postfilter_sample] {name}: n_sampled={n_sampled_actual}, n_passing={n_passing} "
              f"({100*n_passing/n_sampled_actual:.1f}% прошли фильтр)")
        results.append({
            "week_start": week_start, "week_end": week_end, "n_raw": n_raw,
            "n_sampled": n_sampled_actual, "n_passing": n_passing,
        })

    results_df = pd.DataFrame(results)
    results_df["pass_rate"] = results_df.apply(
        lambda r: (r["n_passing"] / r["n_sampled"]) if r["n_sampled"] else 0.0, axis=1
    )
    results_df["n_postfilter_estimate"] = (results_df["n_raw"] * results_df["pass_rate"]).round().astype(int)

    total_raw = int(results_df["n_raw"].sum())
    total_postfilter_estimate = int(results_df["n_postfilter_estimate"].sum())
    total_sampled = int(results_df["n_sampled"].sum())
    total_passing = int(results_df["n_passing"].fillna(0).sum())

    print("\n" + results_df.to_string(index=False))
    print(f"\n[g1_postfilter_sample] ИТОГО: raw N={total_raw}, sampled={total_sampled}, "
          f"passing={total_passing} ({100*total_passing/total_sampled:.2f}% на всей выборке), "
          f"экстраполированный пост-фильтровый N ~= {total_postfilter_estimate}")

    write_design_note(results_df, total_raw, total_sampled, total_passing, total_postfilter_estimate)

    if total_postfilter_estimate < CONFIG.g1_min_n_events:
        print(f"\n[g1_postfilter_sample] N (пост-фильтр, оценка) = {total_postfilter_estimate} < "
              f"{CONFIG.g1_min_n_events} -- ГЕЙТ НЕ ПРОЙДЕН. UNDERPOWERED.")
        return 1
    print(f"\n[g1_postfilter_sample] N (пост-фильтр, оценка) = {total_postfilter_estimate} >= "
          f"{CONFIG.g1_min_n_events} -- гейт пройден на оценке. Точный пересчёт -- задача Шага 3.")
    return 0


def write_design_note(results_df: pd.DataFrame, total_raw: int, total_sampled: int, total_passing: int, total_postfilter_estimate: int) -> None:
    design_path = Path(CONFIG.g1_design_doc)
    text = design_path.read_text()
    marker = "## Пост-фильтровые счёты (§2.2) по неделям -- владелец, 2026-09-01"
    if marker in text:
        print(f"[g1_postfilter_sample] {design_path} уже содержит секцию -- не дублирую.")
        return
    note = f"""

{marker}

**Трактовка N (§2.7) как решение штаба:** N -- размер ВЫБОРКИ ПОСЛЕ
фильтра торгуемости §2.2 (buy vol >= $250 И >= 3 сделки в
(t0; t0+30с]), не сырых событий -- прямое чтение §2.2 ("событие
включается" только через фильтр), не изменение критериев §2.7.

**Метод:** случайная выборка (seed={RANDOM_SEED}, N_PER_WEEK=200) из
уже закэшированных декодированных событий
(data/sprintG1_cache/g1_graduation_events_decoded.csv), по неделе --
джойн VALUES-выборки против dex.trades (без фильтра по project, см.
`dexId` в событии) в узком окне (t0; t0+30с], жёстко ограниченном по
дате этой недели для partition pruning. Наружу -- только (n_sampled,
n_passing) на неделю, не сырые строки. Полный точный пересчёт по ВСЕЙ
выборке -- задача Шага 3 (отдельный бюджет).

{results_df.to_string(index=False)}

**Итого:** сырых событий N={total_raw}, из них засемплировано
{total_sampled}, прошло фильтр {total_passing}
({100*total_passing/total_sampled:.2f}% на выборке).
**Экстраполированный пост-фильтровый N ~= {total_postfilter_estimate}**
(raw_N_неделя * pass_rate_неделя, суммировано)."""
    design_path.write_text(text + note)
    print(f"[g1_postfilter_sample] {design_path} обновлён.")


if __name__ == "__main__":
    raise SystemExit(main())
