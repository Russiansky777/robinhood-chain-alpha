-- sc1_uniswap_v3_tables.sql
-- Sprint SC1: имена таблиц схемы uniswap_v3_robinhood (декодированный
-- Dune ABI Uniswap V3 на этом чейне) -- ищем таблицу с fee-тиром пула
-- по адресу (обычно PoolCreated-событие фабрики или сам пул-контракт).
select table_schema, table_name
from information_schema.tables
where table_schema = 'uniswap_v3_robinhood'
order by table_name
