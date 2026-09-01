-- r1_columns_probe.sql
-- Sprint R1, Шаг 1: колонки декодированных таблиц-кандидатов --
-- chainlink_robinhood.dualaggregator_evt_answerupdated (реестр
-- Chainlink-фидов через contract_address, найдено run #9) и
-- rwa_stock_factory_robinhood.factory_deployer_evt_deployed (фабрика
-- деплоя сток-токенов). Метаданные, дёшево.
select table_schema, table_name, column_name, data_type, ordinal_position
from information_schema.columns
where (table_schema = 'chainlink_robinhood' and table_name = 'dualaggregator_evt_answerupdated')
   or (table_schema = 'rwa_stock_factory_robinhood' and table_name = 'factory_deployer_evt_deployed')
order by table_name, ordinal_position
