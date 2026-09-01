-- g1_pool_births_daily_july.sql
-- Sprint G1, Шаг 1 (разведка): посуточные "рождения" Uniswap v3-пулов
-- за июль (min(block_time) по pool_address) -- ПЕРВОНАЧАЛЬНЫЙ прокси
-- события градуации, через query_02 (бесплатно). С 2026-09-01 этот
-- прокси заменён на подтверждённое ончейн событие TokenLaunched фабрик
-- pons.family (см. g1_graduations_full_period.sql,
-- docs/G1_DESIGN.md) -- файл оставлен для истории/сверки порядка
-- величины, НЕ используется в финальном счёте N.
with swaps as (
    select pool_address, block_time
    from query_02_swaps_raw_july
    where project = 'uniswap' and version in ('3', '4')
),
births as (
    select pool_address, min(block_time) as pool_birth_time
    from swaps
    group by 1
)
select date_trunc('day', pool_birth_time) as day, count(*) as n_new_pools
from births
group by 1
order by 1
