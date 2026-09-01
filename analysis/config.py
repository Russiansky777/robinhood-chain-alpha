"""Единая точка конфигурации порогов и параметров пайплайна.

Всё читается из переменных окружения (.env) с разумными дефолтами —
чтобы менять пороги без правки кода / SQL. Никаких секретов здесь не
хранится, только их имена (см. .env.example).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv не обязателен, если .env уже в env
    pass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    # --- Секреты (никогда не хардкодить, только из env) ---
    dune_api_key: str = field(default_factory=lambda: os.environ.get("DUNE_API_KEY", ""))
    alchemy_api_key: str = field(default_factory=lambda: os.environ.get("ALCHEMY_API_KEY", ""))
    alchemy_rpc_url: str = field(
        default_factory=lambda: os.environ.get("ALCHEMY_ROBINHOOD_RPC_URL", "")
    )

    # --- Период анализа (Sprint 1: июль -> август 2026) ---
    train_start: str = "2026-07-01"
    train_end: str = "2026-08-01"     # полуоткрытый интервал [start, end)
    test_start: str = "2026-08-01"
    test_end: str = "2026-09-01"

    # --- Гейт 1: снайперы/инсайдеры (временной суррогат, см. docs/README.md) ---
    sniper_time_window_minutes: int = field(
        default_factory=lambda: _int("SNIPER_TIME_WINDOW_MINUTES", 5)
    )

    # --- Смоук-тест перед масштабированием (см. docs/DATA_ACCESS.md) ---
    smoke_date: str = field(default_factory=lambda: os.environ.get("SMOKE_DATE", "2026-07-15"))
    smoke_credit_budget: float = field(
        default_factory=lambda: _float("SMOKE_CREDIT_BUDGET", 120.0)
    )

    # --- Гейт 2: фильтр шума ---
    min_trades: int = field(default_factory=lambda: _int("MIN_TRADES", 10))
    min_unique_tokens: int = field(default_factory=lambda: _int("MIN_UNIQUE_TOKENS", 5))

    # --- Гейт 3: размер когорт ---
    cohort_size: int = field(default_factory=lambda: _int("COHORT_SIZE", 200))
    random_seed: int = 42

    # --- Базовые токены (quote-активы), используются в PnL-леджере ---
    base_token_symbols: tuple[str, ...] = ("WETH", "ETH", "USDC", "USDC.e", "USDT")

    # --- Вердикт (зафиксировано заранее, см. docs/README.md) ---
    significance_alpha: float = 0.05

    # --- Sprint 1.5: фильтр копируемости + двухчастный тест (см.
    # docs/README.md, "Sprint 1.5") ---
    copyability_max_trades: int = field(default_factory=lambda: _int("COPYABILITY_MAX_TRADES", 1500))
    copyability_max_trades_sensitivity: int = field(
        default_factory=lambda: _int("COPYABILITY_MAX_TRADES_SENSITIVITY", 3000)
    )
    sniper_time_window_minutes_sensitivity: int = field(
        default_factory=lambda: _int("SNIPER_TIME_WINDOW_MINUTES_SENSITIVITY", 1)
    )
    part1_alpha: float = field(default_factory=lambda: _float("PART1_ALPHA", 0.01))
    part1_min_lift: float = field(default_factory=lambda: _float("PART1_MIN_LIFT", 2.0))
    part2_alpha: float = field(default_factory=lambda: _float("PART2_ALPHA", 0.05))
    # УСТАРЕЛО (не читается analysis/sprint_1_5.py ревизии 2): реальный
    # бюджетный лимит теперь -- персистентный analysis/credit_guard.py +
    # data/credits_spent.json (жёсткая граница 150 на остаток спринта,
    # см. docs/COST_POSTMORTEM.md). Оставлено, чтобы не ломать импорт,
    # если что-то ещё на него ссылается.
    sprint15_credit_budget: float = field(
        default_factory=lambda: _float("SPRINT15_CREDIT_BUDGET", 150.0)
    )

    # --- Пути ---
    output_dir: str = "analysis/output"
    cache_dir: str = "analysis/output/cache"
    sql_dir: str = "sql"
    results_doc: str = "docs/RESULTS.md"
    report_template: str = "analysis/report_template.md"

    # --- Sprint G1: ретро-тест graduation-momentum (см. docs/G1_DESIGN.md) ---
    # Пре-регистрировано 2026-09-01, §2 заморожен -- значения ниже НЕ
    # менять после коммита Шага 0, кроме дат периода (уточняются на
    # Шаге 1 по факту метаданных покрытия Dune, см. g1_period_end_note).
    g1_launchpad: str = "pons.family"
    g1_chain_id: int = 4663
    g1_period_start: str = "2026-07-01"     # или дата первой градуации, если позже (§2.1)
    # Заполнено на Шаге 1 (recon, 2026-09-01): g1_recent_day_coverage_probe
    # дал coverage_probe_max_block_time = 2026-08-30 23:59:59 UTC (полное
    # покрытие суток 30.08, не по исходам цен -- по max(block_time) в
    # dex.trades). §2.1 требует конец выборки = "coverage-end минус 24ч"
    # (событию нужны все горизонты вплоть до +24ч), поэтому:
    #   coverage_end (факт метаданных)     = 2026-08-30 23:59:59 UTC
    #   g1_period_end (последний допустимый t0) = coverage_end - 24h
    #                                       = 2026-08-29 23:59:59 UTC
    g1_coverage_end: str = field(
        default_factory=lambda: os.environ.get("G1_COVERAGE_END", "2026-08-30 23:59:59")
    )
    g1_period_end: str = field(
        default_factory=lambda: os.environ.get("G1_PERIOD_END", "2026-08-29 23:59:59")
    )
    g1_min_n_events: int = 200               # §2.1/2.7: N < 200 -> UNDERPOWERED
    g1_pre_window_buy_usd_min: float = 250.0  # §2.2
    g1_pre_window_trades_min: int = 3         # §2.2
    g1_entry_window_start_s: int = 30         # §2.3: (t0+30s; t0+90s]
    g1_entry_window_end_s: int = 90
    g1_horizons_s: tuple[int, ...] = (30, 60, 120, 300, 900, 1800, 3600, 14400, 43200, 86400)  # §2.3
    g1_exit_delta_min_s: int = 30             # δ = max(30s, 0.1*h), §2.3
    g1_exit_delta_frac: float = 0.1
    g1_cost_scenarios: tuple[float, ...] = (0.01, 0.03, 0.05)  # §2.4, базовый = 0.03
    g1_cost_scenario_base: float = 0.03
    g1_bh_alpha: float = 0.05                 # §2.6
    g1_bootstrap_n: int = 10_000              # §2.6
    g1_trimmed_mean_pct: float = 0.05         # §2.6, 5%-усечённое среднее
    g1_go_min_median_pct: float = 0.02        # §2.7: GO требует медиану >= +2%
    g1_go_min_horizon_s: int = 60             # §2.7: h* >= 1 мин
    g1_excluded_events_max_share: float = 0.30  # §2.3: >30% без entry-сделок -> ограничение выборки

    # Бюджет спринта -- отдельное пространство в credit_guard.py
    # (namespace "sprintG1"), НЕ смешивается со Sprint 1.5. Смета по
    # этапам -- разведка ≤20, смоук ≤30, полный прогон ≤200, резерв 50.
    g1_credit_budget: float = field(default_factory=lambda: _float("G1_CREDIT_BUDGET", 300.0))
    g1_step1_budget: float = 20.0
    g1_step2_budget: float = 30.0
    g1_step3_budget: float = 200.0
    g1_cache_dir: str = "data/sprintG1_cache"
    g1_design_doc: str = "docs/G1_DESIGN.md"

    # --- Sprint R1: ретро-тест RWA-конвергенции (сток-токены), см. docs/R1_DESIGN.md ---
    # Пре-регистрировано 2026-09-01, §2 заморожен -- значения ниже НЕ
    # менять после коммита Шага 0, кроме дат покрытия/списка
    # праздников NYSE (уточняются на Шаге 1, см. r1_coverage_end).
    r1_period_start: str = "2026-07-01"       # §2.1
    # Заполнено на Шаге 1 (recon, 2026-09-01): отдельного coverage-проба
    # для R1 не гонялось -- используется значение, установленное в
    # Sprint G1 (g1_recent_day_coverage_probe, тот же чейн, тот же Dune-
    # индексер) как консервативная оценка: coverage_end = 2026-08-30
    # 23:59:59 UTC. r1_feed_activity (run #11) подтвердил реальные
    # обновления фидов минимум до 2026-08-31 23:06 UTC -- оценка не
    # завышена. period_end = coverage_end - 48ч (§2.1).
    r1_coverage_end: str = field(
        default_factory=lambda: os.environ.get("R1_COVERAGE_END", "2026-08-30 23:59:59")
    )
    r1_period_end: str = field(
        default_factory=lambda: os.environ.get("R1_PERIOD_END", "2026-08-28 23:59:59")
    )
    # Закрытые часы: вне 13:30-20:00 UTC пн-пт (§2.1), плюс праздники/
    # укороченные сессии NYSE -- список из Шага 1 (r1_nyse_holidays.json).
    r1_market_open_utc: str = "13:30"
    r1_market_close_utc: str = "20:00"
    r1_universe_lookback_days: int = 7        # §2.2
    r1_universe_min_trades: int = 100         # §2.2
    r1_universe_min_vol_usd: float = 10_000.0  # §2.2
    r1_checkpoint_price_window_min: int = 30   # §2.3: VWAP в (t-30м; t]
    r1_checkpoint_min_trades: int = 3          # §2.3
    r1_checkpoint_min_vol_usd: float = 500.0   # §2.3
    r1_thetas: tuple[float, ...] = (0.01, 0.025, 0.05)  # §2.3
    r1_entry_window_min: int = 30              # §2.4: VWAP в (t; t+30м]
    r1_horizons: tuple[str, ...] = ("4h", "12h", "open1h")  # §2.4
    r1_horizon_open1h_start_min: int = 30      # §2.4: (открытие+30м; открытие+90м]
    r1_horizon_open1h_end_min: int = 90
    r1_cost_scenarios: tuple[float, ...] = (0.005, 0.015, 0.03)  # §2.4, базовый = 1.5%
    r1_cost_scenario_base: float = 0.015
    r1_bh_alpha: float = 0.05                  # §2.7
    r1_bootstrap_n: int = 10_000               # §2.7
    r1_trimmed_mean_pct: float = 0.05          # §2.7
    r1_min_n_per_cell: int = 50                # §2.7/2.8: ячейка допущена при N>=50
    r1_go_min_median_pct: float = 0.01         # §2.8: GO требует медиану >= +1%
    r1_go_min_control_excess_pct: float = 0.01  # §2.8: превышение медианы контроля >= +1%
    r1_control_max_abs_d: float = 0.005        # §2.6: контроль |D| <= 0.5%
    r1_recon_min_tokens: int = 15              # Шаг 1 гейт разведки
    r1_recon_min_closed_hours_trades: int = 50  # Шаг 1 гейт разведки

    # Бюджет спринта -- отдельное пространство credit_guard.py
    # (namespace "sprintR1"). Смета: разведка <=15, смоук <=15, полный
    # <=50, резерв 20, кап 100 (владелец, п.3).
    r1_credit_budget: float = field(default_factory=lambda: _float("R1_CREDIT_BUDGET", 100.0))
    r1_step1_budget: float = 15.0
    r1_step2_budget: float = 15.0
    r1_step3_budget: float = 50.0
    r1_cache_dir: str = "data/sprintR1_cache"
    r1_design_doc: str = "docs/R1_DESIGN.md"

    # --- Sprint SC1: конвейерный запуск токенов (serial creators), см. docs/SC1_NOTE.md ---
    # Параллельно R1, приоритет ресурсов у R1. Пре-регистрировано
    # 2026-09-01, критерий заморожен -- см. docs/SC1_NOTE.md.
    sc1_month_start: str = "2026-08-01"        # август 2026, полуоткрытый интервал
    sc1_month_end: str = "2026-09-01"
    sc1_pipeline_min_launches: int = 50         # "конвейер" = кластер с 50+ запусков
    sc1_solo_max_launches: int = 4              # "штучный создатель" = 1-4 запуска (контраст)
    sc1_go_min_fee_to_gas_ratio: float = 2.0    # критерий: комиссии >= 2x газа по пост-вейверным ценам
    sc1_lottery_concentration_threshold: float = 0.90  # >90% комиссий у топ-5% кластеров -> лотерея
    sc1_lottery_top_share: float = 0.05         # "топ-5% кластеров"
    sc1_v2_hook_fee_bps: int = 100              # см. docs/G1_DESIGN.md, V2 fee-стек
    sc1_v2_protocol_fee_share_bps: int = 3000
    sc1_v2_creator_fee_share_of_volume: float = 0.007  # ~0.7% объёма создателю (V2), см. SC1_NOTE.md
    # V1 -- НЕ bonding curve, одностороняя Uniswap V3 позиция создателя,
    # доход = LP-комиссии на ней. Fee-тир НЕ константа V2 (0.7%) --
    # определён эмпирически: uniswap_v3_robinhood.uniswapv3factory_evt_
    # poolcreated пересечён с пулами всех 39680 августовских V1-запусков,
    # 100% совпадение, fee=10000 (Uniswap V3 units) = 1.00% у ВСЕХ.
    # Допущение: позиция создателя доминирует в пуле (см. SC1_NOTE.md
    # оговорку) -- эффективная ставка близка к полному тиру.
    sc1_v1_pool_fee_tier: int = 10_000           # Uniswap V3 fee units (сотые доли бипса)
    sc1_v1_creator_fee_share_of_volume: float = 0.01  # 1.00% объёма, см. SC1_NOTE.md
    sc1_early_window_h: int = 24                # объём торгов в первые 24ч после запуска
    sc1_gas_sample_n: int = 100                 # выборка транзакций для медианного gas_used, если трейсы дороги

    sc1_credit_budget: float = field(default_factory=lambda: _float("SC1_CREDIT_BUDGET", 20.0))
    sc1_cache_dir: str = "data/sprintSC1_cache"
    sc1_note_doc: str = "docs/SC1_NOTE.md"


CONFIG = Config()
