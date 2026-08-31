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
    sprint15_credit_budget: float = field(
        default_factory=lambda: _float("SPRINT15_CREDIT_BUDGET", 250.0)
    )

    # --- Пути ---
    output_dir: str = "analysis/output"
    cache_dir: str = "analysis/output/cache"
    sql_dir: str = "sql"
    results_doc: str = "docs/RESULTS.md"
    report_template: str = "analysis/report_template.md"


CONFIG = Config()
