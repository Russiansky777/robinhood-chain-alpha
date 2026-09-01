-- r1_schema_drilldown.sql
-- Sprint R1, Шаг 1: имена таблиц внутри схем chainlink_robinhood и
-- rwa_stock_factory_robinhood (найдены в уже закэшированном
-- g1_schemas_like_robinhood_distinct -- дёшево из Dune, метаданные
-- information_schema, не сырые данные).
select table_schema, table_name
from information_schema.tables
where table_schema in ('chainlink_robinhood', 'rwa_stock_factory_robinhood', 'rwa_robinhood')
order by table_schema, table_name
