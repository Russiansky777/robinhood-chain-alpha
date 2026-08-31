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

    # --- Гейт 1: снайперы/инсайдеры ---
    sniper_block_window: int = field(default_factory=lambda: _int("SNIPER_BLOCK_WINDOW", 3))

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

    # --- Пути ---
    output_dir: str = "analysis/output"
    cache_dir: str = "analysis/output/cache"
    sql_dir: str = "sql"
    results_doc: str = "docs/RESULTS.md"
    report_template: str = "analysis/report_template.md"


CONFIG = Config()
