-- r1_calls_columns_probe.sql
-- Sprint R1, Шаг 2: run #16 нашёл decoded CALL-таблицы (не только
-- события) на chainlink_robinhood: dualaggregator_call_decimals и
-- dualaggregator_call_description -- решает и decimals-вопрос, и
-- token<->feed сопоставление БЕЗ RPC (ALCHEMY_API_KEY не настроен).
-- Колонки перед платным запросом агрегатов по ним.
select table_schema, table_name, column_name, data_type, ordinal_position
from information_schema.columns
where table_schema = 'chainlink_robinhood'
  and table_name in ('dualaggregator_call_decimals', 'dualaggregator_call_description')
order by table_name, ordinal_position
