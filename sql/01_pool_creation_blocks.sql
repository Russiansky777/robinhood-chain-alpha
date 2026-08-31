-- 01_pool_creation_blocks.sql
-- Момент создания (и деплойер) каждого Uniswap v3/v4 пула на Robinhood Chain.
-- Нужно для Гейта 1 (снайперы): "первый своп кошелька в пуле произошёл
-- в первые N блоков после создания пула".
--
-- Params: {{sniper_block_window}} (default 3) -- см. analysis/config.py:
--   SNIPER_BLOCK_WINDOW
--
-- Fallback, если dex.trades ещё не покрывает Robinhood Chain: замените
-- источник на прямое объединение
--   uniswap_v3_robinhood_chain.Factory_evt_PoolCreated
--   uniswap_v4_robinhood_chain.PoolManager_evt_Initialize
-- (см. sql/00_notes.md, п.1)

with v3_pools as (
    select
        pool as pool_address,
        3 as version,
        evt_block_number as creation_block,
        evt_block_time as creation_time,
        evt_tx_hash as creation_tx_hash
    from uniswap_v3_robinhood_chain.Factory_evt_PoolCreated
),

v4_pools as (
    select
        id as pool_address,
        4 as version,
        evt_block_number as creation_block,
        evt_block_time as creation_time,
        evt_tx_hash as creation_tx_hash
    from uniswap_v4_robinhood_chain.PoolManager_evt_Initialize
),

all_pools as (
    select * from v3_pools
    union all
    select * from v4_pools
),

-- "Деплойер" = tx_from транзакции создания пула. Это прокси для адреса,
-- который потенциально мог зафандить снайпер-кошельки до/во время
-- запуска пула (Гейт 1.2).
pool_creators as (
    select
        p.pool_address,
        p.version,
        p.creation_block,
        p.creation_time,
        p.creation_tx_hash,
        t."from" as deployer_address
    from all_pools p
    join robinhood_chain.transactions t
        on t.hash = p.creation_tx_hash
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
