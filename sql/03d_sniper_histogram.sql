-- 03d_sniper_histogram.sql
-- Гистограмма общего числа июльских сделок среди кошельков, исключённых
-- как снайперы первичным окном ({{sniper_window_primary_minutes}} мин).
-- Бакеты 1-2 / 3-10 / 11-100 / 100+ -- выход 4 строки (или меньше, если
-- бакет пуст), не построчный список кошельков.
--
-- Params: {{sniper_window_primary_minutes}}=5

with wallet_agg as (
    select * from query_03_wallet_agg_july
),

pools as (
    select * from query_01_pool_creation_blocks
),

swaps as (
    select * from query_02_swaps_raw_july
),

first_swap_per_wallet_pool as (
    select wallet_address, pool_address, min(block_time) as first_swap_time
    from swaps
    group by 1, 2
),

sniper_wallets as (
    select distinct f.wallet_address
    from first_swap_per_wallet_pool f
    join pools p on p.pool_address = f.pool_address
    where f.first_swap_time <= p.pool_birth_time + interval '{{sniper_window_primary_minutes}}' minute
),

bucketed as (
    select
        case
            when w.trade_count between 1 and 2 then '1-2'
            when w.trade_count between 3 and 10 then '3-10'
            when w.trade_count between 11 and 100 then '11-100'
            else '100+'
        end as bucket
    from wallet_agg w
    join sniper_wallets s on s.wallet_address = w.wallet_address
)

select bucket, count(*) as n_wallets
from bucketed
group by 1
