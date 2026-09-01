-- sc1_pool_created_columns.sql
-- Колонки uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated перед
-- платным запросом (та же осторожность, что r1_columns_probe.sql).
select column_name, data_type, ordinal_position
from information_schema.columns
where table_schema = 'uniswap_v3_robinhood' and table_name = 'uniswapv3factory_evt_poolcreated'
order by ordinal_position
