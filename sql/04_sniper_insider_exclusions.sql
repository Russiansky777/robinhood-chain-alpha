-- 04_sniper_insider_exclusions.sql
-- Гейт 1 (УПРОЩЁННЫЙ ВРЕМЕННОЙ СУРРОГАТ, см. docs/README.md "Гейт 1—
-- статус на Sprint 1"): исключаем кошельки, чей первый своп в пуле
-- случился в первые {{sniper_time_window_minutes}} минут "жизни" этого
-- пула (см. query_01_pool_creation_blocks).
--
-- ЧТО УБРАНО относительно исходного дизайна Sprint 1 (и почему):
--   - Критерий "связь с деплойером" (транзакция/трансфер от адреса,
--     задеплоившего пул, на кошелёк ДО его первого свопа) убран
--     полностью — он требовал join с robinhood.traces и
--     erc20_robinhood.evt_transfer, сырыми общечейновыми таблицами.
--     Именно несфильтрованный join с сырой таблицей чейна
--     (robinhood.transactions) стал причиной 402 Payment Required на
--     первом реальном прогоне (см. docs/DATA_ACCESS.md). Решение от
--     2026-08-31: полностью убрать сырые таблицы чейна из Sprint 1,
--     единственный источник данных — dex.trades. Настоящий
--     deployer-linked фильтр отложен до Sprint 2.
--   - "Момент создания пула" — больше не событие PoolCreated/Initialize
--     из декодированных Uniswap-контрактов, а суррогат: min(block_time)
--     по пулу в dex.trades (см. query_01_pool_creation_blocks.sql).
--   - Порог сменился с "N блоков после создания" на "N минут после
--     рождения пула" ({{sniper_time_window_minutes}}, default 5) — блоки
--     были естественной единицей для событий из сырых логов, минуты
--     естественны для timestamp-агрегата из dex.trades.

with pools as (
    select * from query_01_pool_creation_blocks
),

swaps as (
    select * from query_02_swaps_raw_july
),

first_swap_per_wallet_pool as (
    select
        wallet_address,
        pool_address,
        min(block_time) as first_swap_time
    from swaps
    group by 1, 2
)

select distinct f.wallet_address, 'early_pool_sniper' as reason
from first_swap_per_wallet_pool f
join pools p on p.pool_address = f.pool_address
where f.first_swap_time <= p.pool_birth_time + interval '{{sniper_time_window_minutes}}' minute
