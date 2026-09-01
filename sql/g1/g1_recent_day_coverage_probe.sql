-- g1_recent_day_coverage_probe.sql
-- Sprint G1, Шаг 1 (разведка): один день (2026-08-30) напрямую против
-- dex.trades -- проверка покрытия данных по метаданным (max block_time),
-- не по исходам цен. Результат: coverage_probe_max_block_time =
-- 2026-08-30 23:59:59 UTC (сутки покрыты полностью) -> g1_period_end =
-- coverage_end - 24ч, см. analysis/config.py и docs/G1_DESIGN.md.
with swaps as (
    select project_contract_address as pool_address, block_time
    from dex.trades
    where blockchain = 'robinhood'
        and project = 'uniswap' and version in ('3', '4')
        and block_time >= timestamp '2026-08-30 00:00:00'
        and block_time <  timestamp '2026-08-31 00:00:00'
),
births as (
    select pool_address, min(block_time) as pool_birth_time
    from swaps
    group by 1
)
select count(*) as n_new_pools_this_day,
    (select max(block_time) from swaps) as coverage_probe_max_block_time
from births
