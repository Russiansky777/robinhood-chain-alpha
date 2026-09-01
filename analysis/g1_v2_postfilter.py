#!/usr/bin/env python3
"""Sprint G1 v2 -- пост-фильтровый N (§2.2) по ПОЛНОМУ набору
V2-градуаций (896 событий за весь период -- не нужно сэмплировать, см.
владелец: "может использовать полный набор напрямую, если дёшево").

Тянет все сырые строки PoolGraduated (агрегатно небольшие -- 896 строк,
внутри expected_max_rows), декодирует token/t0 в Python, строит ОДИН
VALUES-джойн против dex.trades (v4, по адресу токена -- не пула, см.
g1_v2_recon.py) в окне (t0; t0+30с], считает n_buys/vol_usd на событие
на стороне Dune, наружу -- только (n_total, n_passing), не сырые строки.

Гейт N>=200 (§2.1/2.7) -- по ПОСТ-ФИЛЬТРОВОМУ n_passing, не по сырому
счёту 896 (трактовка владельца: N в §2.7 -- размер выборки после
фильтра торгуемости).

Использование: python analysis/g1_v2_postfilter.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG
from dune_client import DuneClient
from run_pipeline import read_sql
from g1_common import decode_address_word, decode_uint_word


def decode_pool_graduated(row: dict) -> dict:
    d = str(row["data"]).strip()
    if d.startswith("0x"):
        d = d[2:]
    words = [d[i:i + 64] for i in range(0, len(d), 64)]
    return {
        "tx_hash": row["tx_hash"],
        "block_time": row["block_time"],
        "token": decode_address_word(row["topic1"]),
        "position_id": decode_uint_word(words[0]),
    }


def build_postfilter_query(events: pd.DataFrame) -> str:
    rows = []
    for _, r in events.iterrows():
        t0_str = pd.Timestamp(r["block_time"]).tz_localize(None).strftime("%Y-%m-%d %H:%M:%S") \
            if pd.Timestamp(r["block_time"]).tzinfo else pd.Timestamp(r["block_time"]).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(f"(0x{r['token'].removeprefix('0x')}, timestamp '{t0_str}')")
    values_sql = ",\n        ".join(rows)
    return f"""-- Сгенерировано analysis/g1_v2_postfilter.py -- ВСЕ {len(events)} v2-градуаций
-- (не выборка). Фильтр торгуемости §2.2: buy vol >= $250 И >= 3 сделки
-- в (t0; t0+30с]. Наружу -- только (n_total, n_passing).
with events(token, t0) as (
    values
        {values_sql}
),
buys as (
    select e.token, e.t0,
        count(dt.block_time) as n_buys,
        coalesce(sum(dt.amount_usd), 0) as vol_usd
    from events e
    left join dex.trades dt
        on dt.blockchain = 'robinhood'
        and dt.version = '4'
        and dt.token_bought_address = e.token
        and dt.block_time > e.t0
        and dt.block_time <= e.t0 + interval '30' second
    group by e.token, e.t0
)
select
    count(*) as n_total,
    count(*) filter (where n_buys >= 3 and vol_usd >= 250) as n_passing
from buys
"""


def main() -> int:
    client = DuneClient()
    sql = read_sql("g1/g1_v2_graduation_full")
    print("\n===== g1_v2_graduation_full (оценка 15.0) =====")
    qid = client.create_query("g1_v2_graduation_full", sql)
    df = client.run_sql_cached(
        "g1_v2_graduation_full", sql, query_id=qid, estimated_credits=15.0,
        expected_max_rows=1000, expected_columns=5,
    )
    if df is None or len(df) == 0:
        print("[g1_v2_postfilter] СТОП: пусто -- расходится с агрегатом (896 ожидалось).")
        return 1
    print(f"[g1_v2_postfilter] Получено {len(df)} сырых событий PoolGraduated.")

    events = pd.DataFrame([decode_pool_graduated(r) for r in df.to_dict("records")])
    events = events.drop_duplicates(subset=["token"], keep="first")
    print(f"[g1_v2_postfilter] После дедупа по token: {len(events)}.")

    postfilter_sql = build_postfilter_query(events)
    Path("sql/g1/generated").mkdir(parents=True, exist_ok=True)
    Path("sql/g1/generated/g1_v2_postfilter_full.sql").write_text(postfilter_sql)

    print(f"\n===== g1_v2_postfilter_full (n={len(events)}, оценка 20.0) =====")
    qid2 = client.create_query("g1_v2_postfilter_full", postfilter_sql)
    df2 = client.run_sql_cached(
        "g1_v2_postfilter_full", postfilter_sql, query_id=qid2, estimated_credits=20.0,
        expected_max_rows=2, expected_columns=2,
    )
    if df2 is None or len(df2) == 0:
        print("[g1_v2_postfilter] СТОП: пустой результат агрегата -- неожиданно.")
        return 1
    n_total = int(df2.iloc[0]["n_total"])
    n_passing = int(df2.iloc[0]["n_passing"])
    pass_rate = n_passing / n_total if n_total else 0.0
    print(f"[g1_v2_postfilter] n_total={n_total}, n_passing={n_passing} ({100*pass_rate:.1f}%)")

    write_postfilter_note(n_total, n_passing, pass_rate)

    if n_passing < CONFIG.g1_min_n_events:
        print(f"\n[g1_v2_postfilter] N (пост-фильтр) = {n_passing} < {CONFIG.g1_min_n_events} -- "
              "ГЕЙТ НЕ ПРОЙДЕН. UNDERPOWERED.")
        return 1
    print(f"\n[g1_v2_postfilter] N (пост-фильтр) = {n_passing} >= {CONFIG.g1_min_n_events} -- ГЕЙТ ПРОЙДЕН.")
    return 0


def write_postfilter_note(n_total: int, n_passing: int, pass_rate: float) -> None:
    design_path = Path(CONFIG.g1_design_doc)
    text = design_path.read_text()
    marker = "## Пост-фильтровый N v2 (§2.2, полный набор) -- владелец, 2026-09-01"
    if marker in text:
        print(f"[g1_v2_postfilter] {design_path} уже содержит секцию -- не дублирую.")
        return
    gate = "ПРОЙДЕН" if n_passing >= CONFIG.g1_min_n_events else "НЕ ПРОЙДЕН -- UNDERPOWERED"
    note = f"""

{marker}

Полный набор v2-градуаций (не выборка, все {n_total} уникальных по
token), фильтр торгуемости §2.2 (buy vol >= $250 И >= 3 сделки в
(t0; t0+30с], v4-свопы по адресу токена, dex.trades) применён на
стороне Dune, наружу -- только агрегат (n_total, n_passing).

- N (сырых, дедуп по token) = {n_total}
- N (пост-фильтр, §2.2) = **{n_passing}** ({100*pass_rate:.1f}% прошли фильтр)
- Гейт N>=200 (§2.1/2.7) по пост-фильтровому N: **{gate}**"""
    design_path.write_text(text + note)
    print(f"[g1_v2_postfilter] {design_path} обновлён.")


if __name__ == "__main__":
    raise SystemExit(main())
