-- g1_dex_trades_projects_july.sql
-- Sprint G1, Шаг 1 (разведка): разбивка июльских свопов по
-- (project, version) через уже материализованный query_02_swaps_raw_july
-- (скан бесплатен, платится только агрегация поверх него). Показывает
-- ВСЕ протоколы, активные на чейне в июле -- не только uniswap -- на
-- случай, если pons.family тегируется отдельно (не нашлось -- 100%
-- объёма идёт под project='uniswap', см. docs/G1_DESIGN.md).
select project, version, count(*) as n_swaps,
    count(distinct pool_address) as n_pools,
    min(block_time) as first_seen, max(block_time) as last_seen
from query_02_swaps_raw_july
group by 1, 2
order by n_swaps desc
limit 50
