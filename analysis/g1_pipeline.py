#!/usr/bin/env python3
"""Sprint G1 v2 -- пер-событийная выгрузка VWAP (§2.3) и статистика (§2.6)
поверх уже подтверждённого набора V2-градуаций (PoolGraduated,
data/sprintG1_cache/g1_v2_graduation_full_*.csv, 896 событий,
04.08-29.08.2026).

Один SQL-проход на набор событий: JOIN dex.trades по адресу токена (не
пула -- v4 не гранулярен по пулу в dex.trades, см. g1_v2_recon.py) в
ОДНОМ общем окне [t0; t0 + max(h)+delta(max(h))], дальше все
пре-/entry-/exit-агрегаты считаются `filter (where ...)` В ОДНОМ проходе
-- наружу уходит один агрегат на событие (~26 колонок), не сырые строки
свопов (правило "сырые данные не покидают Dune", Sprint 1.5 ревизия 2).

Exit(h) (§2.3): VWAP сделок в (t0+h; t0+h+delta] если сделки есть,
иначе LOCF -- цена последней сделки <= t0+h+delta (`max_by(price,
block_time) filter (...)`), с флагом no_exit_liquidity_h = (сделок в
окне не было).

Использование: импортируется analysis/sprint_g1.py, не запускается
напрямую.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from config import CONFIG
from dune_client import DuneClient
from run_pipeline import read_sql
from g1_common import decode_pool_graduated, fmt_ts


def load_full_v2_events(client: DuneClient) -> pd.DataFrame:
    """Переиспользует уже оплаченный и постоянно закэшированный
    g1_v2_graduation_full (896 строк, run #15 -- см. docs/G1_DESIGN.md) --
    SQL-текст не менялся с того прогона, поэтому это гарантированный
    кэш-хит (0 кредитов), не новый execute."""
    sql = read_sql("g1/g1_v2_graduation_full")
    qid = client.create_query("g1_v2_graduation_full", sql)
    df = client.run_sql_cached(
        "g1_v2_graduation_full", sql, query_id=qid, estimated_credits=15.0,
        expected_max_rows=1000, expected_columns=5,
    )
    if df is None or len(df) == 0:
        raise RuntimeError("g1_v2_graduation_full пуст -- расходится с известными 896 (run #15).")
    decoded = pd.DataFrame([decode_pool_graduated(r) for r in df.to_dict("records")])
    decoded = decoded.drop_duplicates(subset=["token"], keep="first").reset_index(drop=True)
    decoded["t0"] = pd.to_datetime(decoded["block_time"])
    return decoded[["token", "t0", "tx_hash", "block_number"]]


def horizon_delta(h: int) -> int:
    """delta(h) = max(config.g1_exit_delta_min_s, config.g1_exit_delta_frac * h) -- §2.3."""
    return max(CONFIG.g1_exit_delta_min_s, int(round(CONFIG.g1_exit_delta_frac * h)))


def max_offset_s() -> int:
    return max(h + horizon_delta(h) for h in CONFIG.g1_horizons_s)


def build_extract_query(events: pd.DataFrame) -> str:
    """events: DataFrame[token, t0] -- строит ОДИН SQL с VALUES-джойном,
    считающий n_buys_pre/vol_usd_pre (§2.2), entry_n/entry_vwap (§2.3
    entry), и exit_n_h/exit_vwap_h (§2.3 exit, VWAP окна ИЛИ LOCF-
    фолбэк) для каждого горизонта h в config.g1_horizons_s. Наружу --
    один агрегат на событие (len(events) строк, 26 колонок), не сырые
    свопы."""
    rows = []
    for _, r in events.iterrows():
        t0_str = fmt_ts(r["t0"])
        rows.append(f"(0x{str(r['token']).removeprefix('0x')}, timestamp '{t0_str}')")
    values_sql = ",\n        ".join(rows)

    t0_min = fmt_ts(events["t0"].min())
    t0_max_bound = fmt_ts(pd.Timestamp(events["t0"].max()) + pd.Timedelta(seconds=max_offset_s() + 60))

    entry_start = CONFIG.g1_entry_window_start_s
    entry_end = CONFIG.g1_entry_window_end_s

    horizon_cols = []
    horizon_selects = []
    for h in CONFIG.g1_horizons_s:
        delta = horizon_delta(h)
        win_pred = (
            f"block_time > t0 + interval '{h}' second "
            f"and block_time <= t0 + interval '{h + delta}' second"
        )
        locf_pred = f"block_time <= t0 + interval '{h + delta}' second"
        horizon_selects.append(
            f"    count(*) filter (where {win_pred}) as exit_n_{h},\n"
            f"    coalesce(\n"
            f"        sum(amount_usd) filter (where {win_pred}) "
            f"/ nullif(sum(qty) filter (where {win_pred}), 0),\n"
            f"        max_by(price, block_time) filter (where {locf_pred})\n"
            f"    ) as exit_vwap_{h}"
        )
        horizon_cols.append(f"exit_n_{h}")
        horizon_cols.append(f"exit_vwap_{h}")
    horizon_sql = ",\n".join(horizon_selects)

    return f"""-- Сгенерировано analysis/g1_pipeline.py -- {len(events)} v2-градуаций,
-- §2.2 (n_buys_pre/vol_usd_pre) + §2.3 (entry_vwap, exit_vwap_h x{len(CONFIG.g1_horizons_s)}
-- горизонтов, LOCF-фолбэк через max_by при пустом exit-окне). Один
-- проход по dex.trades на событие, наружу -- только агрегат.
with events(token, t0) as (
    values
        {values_sql}
),
trades as (
    select
        e.token, e.t0, dt.block_time, dt.amount_usd,
        case when dt.token_bought_address = e.token then dt.token_bought_amount
             else dt.token_sold_amount end as qty,
        case when dt.token_bought_address = e.token then 1 else 0 end as is_buy
    from events e
    join dex.trades dt
        on dt.blockchain = 'robinhood'
        and dt.version = '4'
        and (dt.token_bought_address = e.token or dt.token_sold_address = e.token)
        and dt.amount_usd is not null
        and dt.block_time > e.t0
        and dt.block_time <= e.t0 + interval '{max_offset_s()}' second
        and dt.block_time >= timestamp '{t0_min}'
        and dt.block_time <= timestamp '{t0_max_bound}'
),
priced as (
    -- qty<=0 (дегенеративный своп, VWAP не определён) исключается ИЗ ВСЕХ
    -- окон, включая пре-окно §2.2 -- редкий крайний случай, не искажает
    -- фильтр торгуемости (такой своп не несёт информации о цене).
    select token, t0, block_time, amount_usd, qty, is_buy, amount_usd / qty as price
    from trades
    where qty > 0
)
select
    token, t0,
    count(*) filter (where is_buy = 1 and block_time <= t0 + interval '{entry_start}' second) as n_buys_pre,
    coalesce(sum(amount_usd) filter (where is_buy = 1 and block_time <= t0 + interval '{entry_start}' second), 0) as vol_usd_pre,
    count(*) filter (where block_time > t0 + interval '{entry_start}' second and block_time <= t0 + interval '{entry_end}' second) as entry_n,
    sum(amount_usd) filter (where block_time > t0 + interval '{entry_start}' second and block_time <= t0 + interval '{entry_end}' second)
        / nullif(sum(qty) filter (where block_time > t0 + interval '{entry_start}' second and block_time <= t0 + interval '{entry_end}' second), 0) as entry_vwap,
{horizon_sql}
from priced
group by token, t0
limit {len(events)}
"""


def run_extract(client: DuneClient, name: str, events: pd.DataFrame, estimated_credits: float) -> pd.DataFrame:
    sql = build_extract_query(events)
    gen_dir = Path("sql/g1/generated")
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / f"{name}.sql").write_text(sql)
    n_cols = 6 + 2 * len(CONFIG.g1_horizons_s)  # token,t0,n_buys_pre,vol_usd_pre,entry_n,entry_vwap + 2*horizons
    qid = client.create_query(name, sql)
    df = client.run_sql_cached(
        name, sql, query_id=qid, estimated_credits=estimated_credits,
        expected_max_rows=len(events), expected_columns=n_cols,
    )
    return df


def apply_filters(raw: pd.DataFrame) -> pd.DataFrame:
    """§2.2 (торгуемость) + §2.3 (entry-окно непусто) -- на выходе df с
    флагами pass_filter/pass_entry, не мутирует остальное."""
    df = raw.copy()
    df["pass_filter"] = (df["n_buys_pre"] >= CONFIG.g1_pre_window_trades_min) & (
        df["vol_usd_pre"] >= CONFIG.g1_pre_window_buy_usd_min
    )
    df["pass_entry"] = df["entry_n"] > 0
    return df


STRESS_LOG_SENTINEL = -10.0  # см. analysis/g1_pipeline.py docstring compute_returns()


def compute_returns(df: pd.DataFrame, cost: float, horizons: tuple[int, ...], stress: bool = False) -> dict[int, np.ndarray]:
    """r_i(h) = ln(Exit_i(h)/Entry_i) - cost (§2.4). Только для событий,
    прошедших pass_filter И pass_entry (аналитическое N, см. владелец:
    "N в §2.7 -- события, реально вошедшие в расчёт доходностей").

    stress=True (§2.6 робастность (в)): для событий с no_exit_liquidity_h
    на данном горизонте r(h) заменяется на STRESS_LOG_SENTINEL (=-10,
    log-эквивалент ~99.995% потери -- "практически ноль"). Точное -100%
    арифметически недостижимо в лог-шкале (ln(0) не определён); т.к.
    первичный тест -- ЗНАКОВЫЙ (только знак имеет значение, не величина),
    выбор конкретного отрицательного sentinel не влияет на знаковый тест,
    только на величину в бутстрепе усечённого среднего (там это явно
    подписанный worst-case, не точная цифра)."""
    analytic = df[df["pass_filter"] & df["pass_entry"]].copy()
    out: dict[int, np.ndarray] = {}
    for h in horizons:
        exit_col = f"exit_vwap_{h}"
        n_col = f"exit_n_{h}"
        entry = analytic["entry_vwap"].astype(float)
        exit_vwap = analytic[exit_col].astype(float)
        no_liq = analytic[n_col].fillna(0).astype(float) == 0
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.log(exit_vwap / entry) - cost
        r = r.to_numpy()
        if stress:
            r = np.where(no_liq.to_numpy(), STRESS_LOG_SENTINEL, r)
        # Событие без exit-цены вообще (даже LOCF не нашёл сделку <= t0+h+delta,
        # напр. самые свежие события у правого края периода) -> NaN, исключаем
        # из ЭТОГО горизонта явно (не 0, не в knowledge базе -- отсутствие данных).
        r = r[~np.isnan(r)]
        out[h] = r
    return out


def build_quote_distribution_query(events: pd.DataFrame) -> str:
    """§2.6/владелец, деливерабл смоука: распределение quote-токенов по
    ВСЕМ событиям (какой актив на другой стороне свопа) -- один
    агрегат по quote_symbol, не по токену (не нужно 896 строк для
    вопроса о распределении)."""
    tokens = sorted(events["token"].unique())
    values_rows = ",\n        ".join(f"(0x{str(t).removeprefix('0x')})" for t in tokens)
    return f"""-- Сгенерировано analysis/g1_pipeline.py -- распределение quote-токенов
-- по {len(tokens)} v2-градуациям (владелец, деливерабл смоука п.3).
with tokens(token) as (
    values
        {values_rows}
),
sides as (
    select
        case when dt.token_bought_address = t.token then dt.token_sold_symbol
             else dt.token_bought_symbol end as quote_symbol,
        t.token, dt.amount_usd
    from tokens t
    join dex.trades dt
        on dt.blockchain = 'robinhood'
        and dt.version = '4'
        and (dt.token_bought_address = t.token or dt.token_sold_address = t.token)
        and dt.amount_usd is not null
)
select
    coalesce(quote_symbol, '(NULL/unknown)') as quote_symbol,
    count(*) as n_trades,
    count(distinct token) as n_tokens,
    sum(amount_usd) as vol_usd
from sides
group by 1
order by n_tokens desc
limit 50
"""
