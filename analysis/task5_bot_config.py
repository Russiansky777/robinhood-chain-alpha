#!/usr/bin/env python3
"""Задача 5, живой бот -- владелец 2026-09-11/12, спецификация с
поправками: "Целевой блок -- +1, не +3: RTT 14 мс при блоке 120 мс это
позволяет." Все реальные адреса/константы ниже -- из уже полученных в
этой сессии данных (Dune, WebSearch), НЕ придуманы. Секреты (приватный
ключ) -- ТОЛЬКО из переменной окружения, тот же протокол, что
sc1_launcher.py (`--confirm-mainnet`-подобный флаг, dry-run по
умолчанию)."""
from __future__ import annotations

import os

# --- Реальные адреса и параметры цепи (Robinhood Chain, Arbitrum Orbit) ---
CHAIN_ID_MAINNET = 4663
CHAIN_ID_TESTNET = 46630
RPC_URL_MAINNET = "https://rpc.mainnet.chain.robinhood.com"
RPC_URL_TESTNET = "https://rpc.testnet.chain.robinhood.com"
SEQUENCER_FEED_URL_MAINNET = "wss://feed.mainnet.chain.robinhood.com"
SEQUENCER_FEED_URL_TESTNET = "wss://feed.testnet.chain.robinhood.com"

# WETH/USDG -- те же адреса, что во всех измерениях Задачи 5.
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
ZERO_ADDR = "0x0000000000000000000000000000000000000000"  # нативный ETH в v4
WETH_DECIMALS = 18
USDG_DECIMALS = 6

# Canonical v3 Factory -- та же, что во всех запросах этой сессии.
CANONICAL_V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
# Известный референсный пул для цены ETH/USDG (используется во всех
# измерениях этой сессии для конвертации в USD).
WETH_USDG_POOL = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
# v4 PoolManager singleton (см. docs/PROJECT_STATE.md, форензика fomo,
# 2026-09-06: "0x8366a39c... -- реально singleton PoolManager").
V4_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

# --- ЧЕСТНАЯ ОГОВОРКА про список пулов ---
# Владелец просил "список пулов -- из данных Задачи 5 (те, где были
# захваты в блоках >=1 по $10+)". У нас НЕТ сохранённого дискретного
# списка адресов пулов на этом уровне детализации -- все запросы этой
# сессии (измерение 1, кросс-таб, none/wash overlap) агрегировали
# результат на стороне Dune (group by distance/size/category), сам
# pool_key никогда не выбирался в финальный SELECT ради экономии (та же
# причина, что "сырые данные не покидают Dune" по принципу проекта).
# Владелец, 2026-09-12: кредиты нашлись (490 на Mozila) -- реальный
# запрос запущен (`analysis/task5_pool_map.py`, калибровка на коротком
# окне перед полным днём, потолок 300). См. TASK5_KNOWN_PROFITABLE_POOLS
# ниже -- заполняется реальным результатом ПОСЛЕ завершения запроса
# (см. data/p3_guard_cache/task5_pool_map_result.json).
#
# RPC-скан (bootstrap_registry_from_rpc) остаётся ДОПОЛНЕНИЕМ, не
# заменой -- бот сам строит полную вселенную канонических v3-пулов
# через PoolCreated (0 кредитов Dune), список ниже даёт ПРИОРИТЕТНЫЕ,
# уже доказанно прибыльные пулы для более быстрого/уверенного старта.
TASK5_KNOWN_PROFITABLE_POOLS: list[dict] = [
    # Заполняется после task5_pool_map.py -- ПОКА ПУСТО, запрос в процессе.
]
POOL_DISCOVERY_LOOKBACK_BLOCKS = 2_000_000  # ~2.8 дня при блоке 120мс -- разумный старт, настраивается

# --- Пороги входа (владелец, 2026-09-11/12) ---
ENTRY_THRESHOLD_USD = 10.0          # ожидаемый захват >= $10 ПОСЛЕ газа и допущения по откатам
ASSUMED_REVERT_RATE = 0.55          # владелец: "закладывай 55%" (реально измерено 54.5% на топ-20, 09-10)
TARGET_BLOCK_DISTANCE = 1           # владелец: "целевой блок -- +1, не +3"
MIN_DIVERGENCE_AGE_BLOCKS = 1       # бот НЕ участвует в блоке 0 (тот сегмент -- коллоцированные боты)

# --- Инвентарь и размер позиции (владелец, 2026-09-11/12) ---
INVENTORY_TOKENS = (WETH, USDG)     # закрытый цикл начинается/заканчивается в одном из двух
INVENTORY_TARGET_USD_MIN = 2_000.0
INVENTORY_TARGET_USD_MAX = 3_000.0
WEEK1_SIZE_FRACTION = 0.5           # первая неделя теста -- половинный лимит размера (отладка)

# --- Тест (владелец, 2026-09-11/12) ---
TEST_START_AFTER_SUBSIDY_END = True  # тест -- ПОСЛЕ 29.09, на платном газе, не в спешке до
TEST_DURATION_WEEKS = 3
SUCCESS_CRITERION_USD_PER_DAY = 200.0

# --- Секреты/ключи -- протокол проекта (sc1_launcher.py) ---
PRIVATE_KEY_ENV_VAR = "PRIVATE_KEY_TASK5_BOT"  # ТОЛЬКО из окружения, никогда в коде/логах
EXPECTED_WALLET_ENV_VAR = "TASK5_BOT_WALLET_ADDRESS"  # сверяется с адресом, выведенным из приватного ключа

# --- Телеметрия (владелец: "обязательная часть, не довесок") ---
TELEMETRY_JSONL_PATH = "data/task5_bot_live/attempts.jsonl"  # вне git по умолчанию на живом хосте,
# коммитится в git ТОЛЬКО через отдельный маркер-триггер по аналогии с P5/funding/turnover_watch


def is_dry_run(cli_confirm_mainnet: bool) -> bool:
    """Тот же протокол, что sc1_launcher.py: БЕЗ явного флага -- всегда
    dry-run, независимо от наличия PRIVATE_KEY в окружении."""
    return not cli_confirm_mainnet


def get_private_key() -> str:
    key = os.environ.get(PRIVATE_KEY_ENV_VAR, "")
    if not key:
        raise RuntimeError(
            f"{PRIVATE_KEY_ENV_VAR} не задан в окружении -- реальная отправка невозможна."
        )
    return key
