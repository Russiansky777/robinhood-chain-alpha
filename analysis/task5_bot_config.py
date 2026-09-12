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
# Владелец, 2026-09-12: официальный Nitro feed relay (Offchain Labs,
# тот же образ offchainlabs/nitro-node, --entrypoint relay) на Ohio --
# ОДНО постоянное подключение к SEQUENCER_FEED_URL_MAINNET, локальная
# раздача бота/замеров через это (не отдельным подключением каждого
# потребителя к публичному хосту). Не константа по умолчанию -- явный
# --feed-url оверрайд в CLI-скриптах (task5_bot_run.py и т.п.), пока
# relay не поднят и не провалидирован (см. scripts/deploy_feed_relay.sh).
SEQUENCER_FEED_URL_LOCAL_RELAY = "ws://127.0.0.1:9642"
# Write-only приёмник секвенсера (eth_sendRawTransaction), НЕ за Cloudflare
# -- см. docs/PROJECT_STATE.md ("sequencer-эндпоинт -- точная формулировка")
# и docs/TASK5_WRITEPATH_CLEAN_SPEC.md. Имя mainnet-хоста -- по аналогии с
# документированным testnet-эндпоинтом (blockrazor.io), поведение
# подтверждено живыми запросами (scripts/writepath_test.sh,
# scripts/writepath_test_clean.sh) в прошлых раундах этой сессии.
SEQUENCER_SUBMIT_URL_MAINNET = "https://sequencer.mainnet.chain.robinhood.com"
SEQUENCER_SUBMIT_URL_TESTNET = "https://sequencer.testnet.chain.robinhood.com"

# Владелец, 2026-09-12: "публичный RPC не для bootstrap 1257 пулов -- Robinhood
# сам пишет 'not for production' и отправляет к провайдерам. Alchemy и
# Chainstack официально поддерживают Robinhood Chain на бесплатном плане."
# Реально подтверждено (WebSearch, 2026-09-12): Alchemy -- нативная поддержка,
# URL-формат `https://robinhood-mainnet.g.alchemy.com/v2/<API_KEY>` (testnet --
# `https://robinhood-testnet.g.alchemy.com/v2/<API_KEY>`, https://www.alchemy.com/rpc/robinhood).
# Chainstack -- поддержка подтверждена (https://chainstack.com/build-better-with-robinhood-chain/),
# но у Chainstack URL выдаётся ЦЕЛИКОМ (с ключом внутри пути) из дашборда после
# создания Global Node (типичный паттерн для их остальных цепей --
# `https://nd-<id>.p2pify.com/<key>`, конкретно для Robinhood Chain -- см.
# "Access and credentials" в докс Chainstack) -- нет фиксированного шаблона,
# который можно было бы собрать самим из одного API-ключа, в отличие от Alchemy.
# Поэтому здесь -- ОДНА переменная окружения на ГОТОВЫЙ, полный URL (владелец
# либо сам подставит свой Alchemy-ключ в шаблон выше, либо вставит то, что дал
# Chainstack дашборд целиком) -- код одинаково работает с любым HTTP JSON-RPC
# эндпоинтом, не разбирает провайдера отдельно.
#
# /etc/bot/env (тот же протокол, что PRIVATE_KEY_NOX/EXPECTED_WALLET -- ТОЛЬКО
# из окружения, никогда в репозитории): RPC_URL_PROVIDER=<полный URL>. Пусто/не
# задано -- используется публичный RPC_URL_MAINNET напрямую, как раньше.
# Публичный RPC остаётся резервом (fallback при ошибке провайдера) и
# единственным путём для write-side (отправка/чтение рецептов -- вне этой
# сессии, sender/executor).
RPC_URL_PROVIDER_ENV_VAR = "RPC_URL_PROVIDER"

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

# --- Путь А, минимальный (владелец, 2026-09-12, "добавка"): декодер calldata
# роутеров вместо/в дополнение к пост-блочному Swap-событию -- триггер по
# РАЗМЕРУ входящего свопа (calldata из фида, ДО исполнения), без предсказания
# итоговой цены -- "точность не нужна, контракт отсеивает сам". ---
PATH_A_MIN_NOTIONAL_USD = 2_000.0   # владелец: "стартово эквивалент $2 000" -- параметр, не константа навсегда
# Владелец, 2026-09-12 ("усиление"): "триггер -- по ожидаемому сдвигу цены, не по
# размеру в долларах... сдвиг = amountIn / liquidity, порог >= 0.3%." Грубая
# оценка (та же честная оговорка, что и весь остальной детектор в этой сессии --
# "точность не нужна, контракт отсеивает сам"), НЕ полная формула сдвига цены V3
# (той нужен tick-by-tick симулятор, не сделан в этой сессии). ТЕПЕРЬ основной
# триггер живого бота -- этот, не PATH_A_MIN_NOTIONAL_USD (тот остаётся для
# офлайн-сравнения версий, см. task5_bot_router_backtest.py).
PATH_A_MIN_PRICE_IMPACT_FRACTION = 0.003  # 0.3%

# Владелец, 2026-09-12: "429 -- тот же урок, что с фидом: bootstrap один раз,
# кешировать реестр на диск, не перезапрашивать RPC каждый прогон." Кеш --
# ТОЛЬКО для повторных офлайн-прогонов анализа/бэктеста (task5_bot_router_
# backtest.py), НЕ для живого бота (task5_bot_run.py всегда бутстрапит заново
# -- цены должны быть свежими для реальной торговли, кеш их устарит)."
POOL_REGISTRY_CACHE_PATH = "data/task5_bot_pool_registry_cache.json"
# Реальные адреса, найденные `task5_bot_router_histogram.py` (захват через собственный
# relay, 2026-09-12, ~5 минут/41695 decoded tx) -- ТОЛЬКО для телеметрии/логов (какой
# роутер сработал), детектор НЕ фильтрует по этому списку -- триггер идёт по СЕЛЕКТОРУ
# calldata (см. task5_bot_router_decode.py::KNOWN_SWAP_SELECTORS), не по адресу, потому
# что один и тот же стандартный интерфейс (SwapRouter02/UniversalRouter) реально
# встретился на НЕСКОЛЬКИХ разных адресах в захвате (минимум 3), список ниже -- для
# читаемости паспорта/логов, не источник истины для решения "декодировать или нет".
PATH_A_KNOWN_ROUTER_ADDRESSES_FROM_HISTOGRAM = {
    "0xcaf681a66d020601342297493863e78c959e5cb2": "SwapRouter02-style (1749 tx/5мин, 99.5% decoded, ~$55.2k)",
    "0x8876789976decbfcbbbe364623c63652db8c0904": "UniversalRouter (962 tx/5мин, 100% decoded, ~$71.1k)",
    "0xaf9d6fa019328d8cf8598d0d5be22cc6622b09f1": "UniversalRouter, второй адрес (197 tx/5мин, ~$30.6k)",
}

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

# --- Адрес задеплоенного ClosedCycleExecutorV3 -- владелец, 2026-09-13:
# заполняется здесь ПОСЛЕ реального деплоя (contracts/build/deploy_params.json --
# байткод/ABI/constructor args готовы, сам деплой -- скрипт владельца, не эта сессия).
# None -- контракт ещё не задеплоен; --contract-address из CLI (task5_bot_run.py)
# по-прежнему имеет приоритет, если передан явно, это только запасное значение по
# умолчанию, чтобы не забыть/не потерять адрес между запусками.
#
# Реальный деплой (владелец, 2026-09-12, scripts/deploy_executor.sh через
# run_task5_deploy_executor.yml, Ohio VPS): tx_hash
# 0x8e4cab21411a9838a7c7bd3746c4491a8a7e72d5ad2e10b6e3ee25eb982407b4,
# block 60775091, gas_used 796881 -- см. contracts/build/deploy_result.json.
EXECUTOR_CONTRACT_ADDRESS_MAINNET: str | None = "0xeFBf06bCB9B0c6d0956c5bF149c0D9d9F34D5010"
EXECUTOR_CONTRACT_ADDRESS_TESTNET: str | None = None

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
