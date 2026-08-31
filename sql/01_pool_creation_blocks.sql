-- 01_pool_creation_blocks.sql
-- Момент создания (и деплойер) каждого Uniswap v3/v4 пула на Robinhood Chain.
-- Нужно для Гейта 1 (снайперы): "первый своп кошелька в пуле произошёл
-- в первые N блоков после создания пула".
--
-- Params: {{sniper_block_window}} (default 3) -- см. analysis/config.py:
--   SNIPER_BLOCK_WINDOW
-- Params: {{start_date}}, {{end_date}} -- то же окно, что и в
--   02_swaps_raw_july.sql/06_wallet_agg_august.sql. ОБЯЗАТЕЛЬНО задавать:
--   без этого фильтра join со сплошной робинхуд-таблицей transactions
--   сканирует её целиком без партиционного прунинга -- именно на этом
--   запросе (без фильтра по дате) реальный прогон 2026-08-31 упёрся в
--   402 Payment Required на первом же шаге пайплайна (см.
--   docs/DATA_ACCESS.md, "Инцидент: 402 на шаге 1").
--
-- Схема/имена таблиц подтверждены запросом к information_schema.tables
-- на реальном Dune-аккаунте 2026-08-31 (см. analysis/_probe_schema.py и
-- docs/DATA_ACCESS.md, раздел "Реально обнаруженная схема Dune") —
-- НЕ "uniswap_v3_robinhood_chain", а "uniswap_v3_robinhood"/"uniswap_v4_robinhood",
-- таблицы <contract>_evt_<event> в нижнем регистре без разделителей.

with v3_pools as (
    select
        pool as pool_address,
        3 as version,
        evt_block_number as creation_block,
        evt_block_time as creation_time,
        evt_tx_hash as creation_tx_hash
    from uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated
    where evt_block_time >= timestamp {{start_date}}
      and evt_block_time <  timestamp {{end_date}}
),

v4_pools as (
    select
        id as pool_address,
        4 as version,
        evt_block_number as creation_block,
        evt_block_time as creation_time,
        evt_tx_hash as creation_tx_hash
    from uniswap_v4_robinhood.poolmanager_evt_initialize
    where evt_block_time >= timestamp {{start_date}}
      and evt_block_time <  timestamp {{end_date}}
),

all_pools as (
    select * from v3_pools
    union all
    select * from v4_pools
),

-- "Деплойер" = tx_from транзакции создания пула. Это прокси для адреса,
-- который потенциально мог зафандить снайпер-кошельки до/во время
-- запуска пула (Гейт 1.2). Фильтр по t.block_time логически избыточен
-- (t.hash однозначно определяет строку), но критичен для partition
-- pruning -- без него Dune читает всю таблицу transactions.
pool_creators as (
    select
        p.pool_address,
        p.version,
        p.creation_block,
        p.creation_time,
        p.creation_tx_hash,
        t."from" as deployer_address
    from all_pools p
    join robinhood.transactions t
        on t.hash = p.creation_tx_hash
       and t.block_time >= timestamp {{start_date}}
       and t.block_time <  timestamp {{end_date}}
)

select
    pool_address,
    version,
    creation_block,
    creation_time,
    creation_tx_hash,
    deployer_address,
    creation_block + {{sniper_block_window}} as sniper_window_end_block
from pool_creators
