-- sc1_transactions_columns.sql
-- Колонки robinhood.transactions перед платным запросом (funding-
-- parent join и gas_used/value для августовских V1-запусков).
select column_name, data_type, ordinal_position
from information_schema.columns
where table_schema = 'robinhood' and table_name = 'transactions'
order by ordinal_position
