#!/usr/bin/env python3
"""Задача 5, живой бот -- владелец 2026-09-11/12, спецификация с
поправками: "Целевой блок -- +1, не +3: RTT 14 мс при блоке 120 мс это
позволяет." Все реальные адреса/константы ниже -- из уже полученных в
этой сессии данных (Dune, WebSearch), НЕ придуманы. Секреты (приватный
ключ) -- ТОЛЬКО из переменной окружения, тот же протокол, что
sc1_launcher.py (`--confirm-mainnet`-подобный флаг, dry-run по
умолчанию).

**Владелец, 2026-09-13: третий VPS в AWS us-east-2 (Огайо) -- та же
область, что и sequencer-хост -- "целиться в блок 0, не в +1".**
`TARGET_BLOCK_DISTANCE` ниже обновлён с прежних 1 на 0 -- пересмотр
прежней спецификации, не опечатка (см. история этого файла/паспорт).
Пересчёт по реальным данным (`analysis/task5_writepath_block_recompute.py`,
NL/Dallas) уже показал, что путь через `sequencer.mainnet...` даёт
отрицательный `blocks_to_inclusion` (попадание РАНЬШЕ наивной оценки
"текущего" блока) в обеих локациях -- колокация в Огайо должна усилить
этот эффект дальше, отсюда и пересмотр цели с +1 на 0."""
from __future__ import annotations

import os

# --- Реальные адреса и параметры цепи (Robinhood Chain, Arbitrum Orbit) ---
CHAIN_ID_MAINNET = 4663
CHAIN_ID_TESTNET = 46630
RPC_URL_MAINNET = "https://rpc.mainnet.chain.robinhood.com"
RPC_URL_TESTNET = "https://rpc.testnet.chain.robinhood.com"
SEQUENCER_FEED_URL_MAINNET = "wss://feed.mainnet.chain.robinhood.com"
SEQUENCER_FEED_URL_TESTNET = "wss://feed.testnet.chain.robinhood.com"
# Write-only приёмник секвенсера (eth_sendRawTransaction), НЕ за Cloudflare
# -- см. docs/PROJECT_STATE.md ("sequencer-эндпоинт -- точная формулировка")
# и docs/TASK5_WRITEPATH_CLEAN_SPEC.md. Имя mainnet-хоста -- по аналогии с
# документированным testnet-эндпоинтом (blockrazor.io), поведение
# подтверждено живыми запросами (scripts/writepath_test.sh,
# scripts/writepath_test_clean.sh) в прошлых раундах этой сессии.
SEQUENCER_SUBMIT_URL_MAINNET = "https://sequencer.mainnet.chain.robinhood.com"
SEQUENCER_SUBMIT_URL_TESTNET = "https://sequencer.testnet.chain.robinhood.com"

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
# РЕАЛЬНЫЙ результат task5_pool_map.py, 2026-09-12: калибровка на ПЕРВЫХ
# 3 ЧАСАХ 09-10 (не полный день -- честно: полный день экстраполировался
# в ~1325 кредитов, далеко за потолком 300 и большей частью остатка
# цикла 490, НЕ запущен). 45 пулов найдено всего, из них ТОЛЬКО 15 --
# v3 (ниже) -- v4-пулы (30 из 45, часто с БОЛЬШЕЙ прибылью, топ-2 пула
# по $ вообще НЕ WETH/USDG) реально существуют в данных, но
# PoolRegistry/V3PoolState в этой версии НЕ поддерживает v4 (singleton
# PoolManager, другая архитектура цены/событий) -- честный, известный
# gap, не добавлены сюда специально, а не по недосмотру.
TASK5_KNOWN_PROFITABLE_POOLS: list[dict] = [
    {"pool_key": "0x52E65B17FB6E5BA00ED806F37AFCD2DAA50271CA", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168", "total_profit_usd_3h_window": 373.98},
    {"pool_key": "0x995C1AD5EB998B1BDD89F515C4BB64760C411B62", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0xB90A19FF0AF67F7779AFF50A882A9CFF42446400", "total_profit_usd_3h_window": 204.01},
    {"pool_key": "0xC4A21F9D6485FC5893DD4A491B320A83DAF4DA1D", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0x2E8C31162B855A2FFA90F6F8634643AD6F111E18", "total_profit_usd_3h_window": 138.28},
    {"pool_key": "0xA4BDB396A69617EB7F70E2CC1EF526F7340B1B0D", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0xC0D6457C16CC70D6790DD43521C899C87CE02F35", "total_profit_usd_3h_window": 130.00},
    {"pool_key": "0xD4EB21209C4D6093F80B5B84F5C45CC093EA14A3", "token0": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168", "token1": "0xD0601CE157DB5BDC3162BBAC2A2C8AF5320D9EEC", "total_profit_usd_3h_window": 102.14},
    {"pool_key": "0x10CC6BD38112CAC182DB90B6A71D8BB5939526BA", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0x39DBED3A2BD333467115DE45665CC57F813C4571", "total_profit_usd_3h_window": 70.55},
    {"pool_key": "0x34D0DC122CF9A8EB296FC5E0D3A233625D7D19B7", "token0": "0x2E0847E8910A9732EB3FB1BB4B70A580ADAD4FE3", "token1": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168", "total_profit_usd_3h_window": 63.85},
    {"pool_key": "0xE2C12A7379706A291CADAAEC1D22458BE2F7239D", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0x385F4F8AE47651CE5F58F5265395A669F8281E18", "total_profit_usd_3h_window": 62.44},
    {"pool_key": "0x69BFAF19C9F377BB306A89AED9F6B07E2C1A8D9A", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168", "total_profit_usd_3h_window": 23.69},
    {"pool_key": "0xBD5CD6515CA6285941FBC177381DC8ED4844E6B8", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0x98096D17E191B3DA1D5F99A6D7B3584351B11E18", "total_profit_usd_3h_window": 23.69},
    {"pool_key": "0xD78480CAFEF722D75519E13B9F516E5704D0D659", "token0": "0x0BD7D308F8E1639FAB988DF18A8011F41EACAD73", "token1": "0x2E8C31162B855A2FFA90F6F8634643AD6F111E18", "total_profit_usd_3h_window": 19.90},
    {"pool_key": "0xE547C18F46DB55AB788343BCC503F9CF0BD7D564", "token0": "0x2E8C31162B855A2FFA90F6F8634643AD6F111E18", "token1": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168", "total_profit_usd_3h_window": 15.07},
    {"pool_key": "0x0EBD4650C9E641E9745B5A508A2D46935DFE753E", "token0": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168", "token1": "0xF6589F11BC40B669E584073F428B05562F568733", "total_profit_usd_3h_window": 12.71},
    {"pool_key": "0xC8C90D3A1C1A24967E773AC2AD0D456BA3E31F64", "token0": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168", "token1": "0xCCEE82FE024C36FA15E1005EDE3E9E4787E23D09", "total_profit_usd_3h_window": 10.64},
    {"pool_key": "0x8AC92DA74AB5F3B1D024DC1943AD7E15DC4179EF", "token0": "0x12F190A9F9D7D37A250758B26824B97CE941BF54", "token1": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168", "total_profit_usd_3h_window": 10.15},
]
POOL_DISCOVERY_LOOKBACK_BLOCKS = 2_000_000  # ~2.8 дня при блоке 120мс -- разумный старт, настраивается

# --- Пороги входа (владелец, 2026-09-11/12) ---
ENTRY_THRESHOLD_USD = 10.0          # ожидаемый захват >= $10 ПОСЛЕ газа и допущения по откатам
ASSUMED_REVERT_RATE = 0.55          # владелец: "закладывай 55%" (реально измерено 54.5% на топ-20, 09-10)
TARGET_BLOCK_DISTANCE = 0           # ПЕРЕСМОТРЕНО владельцем 2026-09-13 (было 1): "целиться в блок 0, не в
# +1" -- обоснование, VPS в Огайо (us-east-2, та же область, что sequencer-хост) даёт минимальный сетевой
# путь до секвенсера, см. docstring модуля выше и docs/TASK5_WRITEPATH_CLEAN_SPEC.md. Это цель ВКЛЮЧЕНИЯ
# СОБСТВЕННОЙ транзакции бота относительно момента детекции -- НЕ то же самое, что MIN_DIVERGENCE_AGE_BLOCKS
# ниже (тот порог -- про возраст ЧУЖОГО расхождения перед входом, разные величины, не путать).
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
