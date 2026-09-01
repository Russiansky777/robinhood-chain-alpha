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


CONFIG = Config()
