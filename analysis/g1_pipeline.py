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
            f"p.block_time > e.t0 + interval '{h}' second "
            f"and p.block_time <= e.t0 + interval '{h + delta}' second"
        )
        locf_pred = f"p.block_time <= e.t0 + interval '{h + delta}' second"
        horizon_selects.append(
            f"    count(*) filter (where {win_pred}) as exit_n_{h},\n"
            f"    coalesce(\n"
            f"        sum(p.amount_usd) filter (where {win_pred}) "
            f"/ nullif(sum(p.qty) filter (where {win_pred}), 0),\n"
            f"        max_by(p.price, p.block_time) filter (where {locf_pred})\n"
            f"    ) as exit_vwap_{h}"
        )
        horizon_cols.append(f"exit_n_{h}")
        horizon_cols.append(f"exit_vwap_{h}")
    horizon_sql = ",\n".join(horizon_selects)

    return f"""-- Сгенерировано analysis/g1_pipeline.py -- {len(events)} v2-градуаций,
-- §2.2 (n_buys_pre/vol_usd_pre) + §2.3 (entry_vwap, exit_vwap_h x{len(CONFIG.g1_horizons_s)}
-- горизонтов, LOCF-фолбэк через max_by при пустом exit-окне). Один
-- проход по dex.trades на событие, наружу -- только агрегат.
--
-- ВАЖНО (найдено и исправлено 2026-09-01, run #18): финальный SELECT
-- идёт FROM events LEFT JOIN priced (не FROM priced) -- событие БЕЗ
-- вообще ни одной сделки в окне должно остаться строкой с нулевыми
-- агрегатами (и корректно провалить §2.2 ниже, в Python), а не
-- молча исчезнуть из результата (что раньше занижало "N сырых" --
-- 99 вместо 108 на смоук-дне -- хотя на аналитическое N это не влияло,
-- т.к. такие события всё равно не проходят §2.2).
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
    left join dex.trades dt
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
    -- qty<=0 (дегенеративный своп ИЛИ отсутствие сделок вовсе -- dt.*
    -- все NULL после left join) исключается ИЗ ВСЕХ окон, включая
    -- пре-окно §2.2 -- редкий/пустой случай, не искажает фильтр
    -- торгуемости (такое событие не несёт информации о цене и корректно
    -- проваливает §2.2 при агрегации ниже).
    select token, t0, block_time, amount_usd, qty, is_buy, amount_usd / qty as price
    from trades
    where qty > 0
)
select
    e.token, e.t0,
    count(*) filter (where p.is_buy = 1 and p.block_time <= e.t0 + interval '{entry_start}' second) as n_buys_pre,
    coalesce(sum(p.amount_usd) filter (where p.is_buy = 1 and p.block_time <= e.t0 + interval '{entry_start}' second), 0) as vol_usd_pre,
    count(*) filter (where p.block_time > e.t0 + interval '{entry_start}' second and p.block_time <= e.t0 + interval '{entry_end}' second) as entry_n,
    sum(p.amount_usd) filter (where p.block_time > e.t0 + interval '{entry_start}' second and p.block_time <= e.t0 + interval '{entry_end}' second)
        / nullif(sum(p.qty) filter (where p.block_time > e.t0 + interval '{entry_start}' second and p.block_time <= e.t0 + interval '{entry_end}' second), 0) as entry_vwap,
{horizon_sql}
from events e
left join priced p on p.token = e.token and p.t0 = e.t0
group by e.token, e.t0
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


QUOTE_DISTRIBUTION_WINDOW_S = 172800  # 48ч после t0 -- квота пула не меняется со временем,
# ранняя торговля достаточна и репрезентативна для вопроса "какой актив на другой стороне".


def build_quote_distribution_query(events: pd.DataFrame) -> str:
    """§2.6/владелец, деливерабл смоука: распределение quote-токенов по
    ВСЕМ событиям (какой актив на другой стороне свопа) -- один
    агрегат по quote_symbol, не по токену (не нужно 896 строк для
    вопроса о распределении).

    БАГ, НАЙДЕННЫЙ НА РЕАЛЬНОМ ПРОГОНЕ (run #16, 2026-09-01): первая
    версия этого запроса не имела НИКАКОЙ границы по block_time --
    единственная во всём пайплайне (build_extract_query и все SQL до
    неё всегда явно ограничивали block_time). Джойн по 896 активно
    торгуемым токенам без временной границы просканировал ВСЮ историю
    dex.trades для каждого из них (некоторые торгуются до сих пор,
    объёмы до $10-18M -- см. g1_v2_recon.py) -- факт 56.49 против
    заявленных 15.0 (>2x), 2x-гард сработал и остановил пайплайн
    корректно (реальная execute-стоимость уже была записана в леджер
    ДО отказа, ничего не потеряно из виду). Исправлено: окно (t0;
    t0+48ч] -- квота пула фиксирована с момента создания, ранняя
    торговля репрезентативна, новых вопросов не решает."""
    t0_min = fmt_ts(events["t0"].min())
    t0_max_bound = fmt_ts(pd.Timestamp(events["t0"].max()) + pd.Timedelta(seconds=QUOTE_DISTRIBUTION_WINDOW_S + 60))
    rows = []
    for _, r in events.iterrows():
        rows.append(f"(0x{str(r['token']).removeprefix('0x')}, timestamp '{fmt_ts(r['t0'])}')")
    values_rows = ",\n        ".join(rows)
    return f"""-- Сгенерировано analysis/g1_pipeline.py -- распределение quote-токенов
-- по {len(events)} v2-градуациям (владелец, деливерабл смоука п.3).
-- Окно (t0; t0+{QUOTE_DISTRIBUTION_WINDOW_S}с] -- см. docstring build_quote_distribution_query
-- (баг run #16: без границы по block_time просканировало всю историю).
with events(token, t0) as (
    values
        {values_rows}
),
sides as (
    select
        case when dt.token_bought_address = e.token then dt.token_sold_symbol
             else dt.token_bought_symbol end as quote_symbol,
        e.token, dt.amount_usd
    from events e
    join dex.trades dt
        on dt.blockchain = 'robinhood'
        and dt.version = '4'
        and (dt.token_bought_address = e.token or dt.token_sold_address = e.token)
        and dt.amount_usd is not null
        and dt.block_time > e.t0
        and dt.block_time <= e.t0 + interval '{QUOTE_DISTRIBUTION_WINDOW_S}' second
        and dt.block_time >= timestamp '{t0_min}'
        and dt.block_time <= timestamp '{t0_max_bound}'
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


# ---------- Калибровка вместо угадывания (владелец, 2026-09-01, после run #16) ----------
#
# Оценка 15.0 для g1_v2_quote_distribution была догадкой, прошла санитарный
# порог credit_guard.SANITY_MAX_ESTIMATE=40 не будучи откалиброванной, факт
# оказался 56.49 (>2x). Правило на остаток спринта: любой запрос НОВОЙ формы
# сначала исполняется на узком срезе (смоук-день сам по себе им и служит --
# "один день" по букве правила владельца), оценка полной версии = факт среза
# / n_среза * n_полный * CALIBRATION_SCALE_FACTOR (запас); если оценка > 40 --
# партиционировать до соответствия. Партиционирование НЕ меняет суммарную
# ожидаемую стоимость (та же per-unit ставка на весь объём), только дробит
# её на куски, каждый из которых укладывается в санитарный порог
# credit_guard.py по отдельности.
#
# Множитель поднят 1.3 -> 2.5 (владелец, 2026-09-01, после run #18):
# линейная экстраполяция с 1.3x недооценила g1_v2_quote_distribution_full
# на ~2.45x (факт 20.09 против калиброванных 8.2) -- смоук-день (108
# самых свежих градуаций конца периода) оказался заметно менее активен
# по объёму торгов на токен, чем средняя активность по всем 896 (среди
# которых есть ранние, 04-12.08, токены с намного более высокими
# объёмами в первые 48ч) -- подтверждённая гетерогенность популяции, не
# баг формулы. Это уточнение ОЦЕНКИ (влияет только на то, когда
# партиционировать и какое число declared), не стоп-условие само по себе.
CALIBRATION_SCALE_FACTOR = 2.5
CALIBRATION_MAX_ESTIMATE = 40.0  # см. credit_guard.SANITY_MAX_ESTIMATE -- держим партицию строго под ним


def project_full_estimate(slice_actual_credits: float, slice_n: int, target_n: int) -> float:
    """Оценка полной версии запроса по факту узкого среза: (факт/n_среза) *
    n_целевой * CALIBRATION_SCALE_FACTOR."""
    if slice_n <= 0:
        raise ValueError("slice_n должен быть > 0 для калибровочной проекции")
    return (slice_actual_credits / slice_n) * target_n * CALIBRATION_SCALE_FACTOR


def calibrated_batch_size(slice_actual_credits: float, slice_n: int, max_estimate: float = CALIBRATION_MAX_ESTIMATE) -> int:
    """Максимальный размер партиции (в единицах n, событий/токенов), при
    котором её проекция (project_full_estimate) не превышает max_estimate.
    Целочисленное деление -- намеренно округляет ВНИЗ (никогда не превысит
    порог из-за округления)."""
    if slice_actual_credits <= 0 or slice_n <= 0:
        return slice_n if slice_n > 0 else 1
    per_unit = slice_actual_credits / slice_n
    size = int(max_estimate / (per_unit * CALIBRATION_SCALE_FACTOR))
    return max(1, size)


def batch_rows(df: pd.DataFrame, batch_size: int) -> list[pd.DataFrame]:
    """Режет df на последовательные непересекающиеся куски по batch_size
    строк (последний может быть короче) -- используется и для событий
    (extract), и для токенов (quote distribution)."""
    return [df.iloc[i:i + batch_size].reset_index(drop=True) for i in range(0, len(df), batch_size)]


def run_extract_calibrated(
    client: DuneClient, base_name: str, events: pd.DataFrame,
    calib_actual_credits: float, calib_n: int,
) -> tuple[pd.DataFrame, float]:
    """Полная версия build_extract_query, откалиброванная по факту смоук-
    среза (calib_actual_credits, calib_n событий). Партиционирует, если
    проекция > CALIBRATION_MAX_ESTIMATE. Возвращает (df_все_партиции,
    итоговая_проекция_до_прогона -- та же величина, что была бы без
    партиционирования, для сверки с бюджетной проекцией п.2)."""
    projection = project_full_estimate(calib_actual_credits, calib_n, len(events))
    if projection <= CALIBRATION_MAX_ESTIMATE:
        df = run_extract(client, base_name, events, projection)
        return (df if df is not None else pd.DataFrame()), projection
    batch_size = calibrated_batch_size(calib_actual_credits, calib_n)
    batches = batch_rows(events, batch_size)
    print(
        f"[{base_name}] Проекция {projection:.1f} > {CALIBRATION_MAX_ESTIMATE:.0f} -- "
        f"партиционирую на {len(batches)} батчей по <= {batch_size} событий."
    )
    parts = []
    for i, batch in enumerate(batches):
        est = project_full_estimate(calib_actual_credits, calib_n, len(batch))
        df = run_extract(client, f"{base_name}_part{i}", batch, est)
        if df is not None and len(df):
            parts.append(df)
    combined = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return combined, projection


def run_quote_distribution_calibrated(
    client: DuneClient, base_name: str, events: pd.DataFrame,
    calib_actual_credits: float, calib_n: int,
) -> tuple[pd.DataFrame, float]:
    """Полная версия build_quote_distribution_query, откалиброванная по
    факту смоук-среза. Партиционирует по токенам, если проекция > порога;
    партиции агрегируются суммой в Python (безопасно -- токен встречается
    ровно в одной партиции, дублей нет)."""
    projection = project_full_estimate(calib_actual_credits, calib_n, len(events))
    if projection <= CALIBRATION_MAX_ESTIMATE:
        sql = build_quote_distribution_query(events)
        qid = client.create_query(base_name, sql)
        df = client.run_sql_cached(
            base_name, sql, query_id=qid, estimated_credits=projection,
            expected_max_rows=50, expected_columns=4,
        )
        return (df if df is not None else pd.DataFrame()), projection
    batch_size = calibrated_batch_size(calib_actual_credits, calib_n)
    batches = batch_rows(events, batch_size)
    print(
        f"[{base_name}] Проекция {projection:.1f} > {CALIBRATION_MAX_ESTIMATE:.0f} -- "
        f"партиционирую на {len(batches)} батчей по <= {batch_size} токенов."
    )
    parts = []
    for i, batch in enumerate(batches):
        est = project_full_estimate(calib_actual_credits, calib_n, len(batch))
        sql = build_quote_distribution_query(batch)
        qid = client.create_query(f"{base_name}_part{i}", sql)
        df = client.run_sql_cached(
            f"{base_name}_part{i}", sql, query_id=qid, estimated_credits=est,
            expected_max_rows=50, expected_columns=4,
        )
        if df is not None and len(df):
            parts.append(df)
    if not parts:
        return pd.DataFrame(), projection
    combined = pd.concat(parts, ignore_index=True)
    for col in ("n_trades", "n_tokens", "vol_usd"):
        combined[col] = combined[col].astype(float)
    result = (
        combined.groupby("quote_symbol", as_index=False)
        .agg(n_trades=("n_trades", "sum"), n_tokens=("n_tokens", "sum"), vol_usd=("vol_usd", "sum"))
        .sort_values("n_tokens", ascending=False)
        .reset_index(drop=True)
    )
    return result, projection
