-- sc1_schema_drilldown.sql
-- Sprint SC1, Шаг 1: имена таблиц внутри схемы robinhood (уже
-- используем robinhood.logs -- нужно найти таблицу нативных
-- ETH-трансферов/транзакций для funding-parent склейки, Шаг 2).
-- Метаданные information_schema, дёшево.
select table_schema, table_name
from information_schema.tables
where table_schema = 'robinhood'
order by table_name
