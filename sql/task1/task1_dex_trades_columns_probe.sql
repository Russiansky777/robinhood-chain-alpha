-- task1_dex_trades_columns_probe.sql
-- Задача 1: реальная ошибка первого прогона (run 33964786227) --
-- "Cannot find common type between varbinary and varchar(42)" на
-- `token_bought_address in ({{token_address_list}})` -- ТОТ ЖЕ паттерн,
-- что уже реально работал в sql/r1/r1_universe_trades_v2.sql (тоже
-- dex.trades). Не гадаем о причине (возможный дрейф схемы Dune между
-- прогоном R1 и сейчас) -- проверяем РЕАЛЬНЫЙ текущий тип колонок,
-- метаданные, 0 кредитов.
select table_schema, table_name, column_name, data_type, ordinal_position
from information_schema.columns
where table_schema = 'dex' and table_name = 'trades'
  and column_name in ('token_bought_address', 'token_sold_address', 'blockchain', 'amount_usd', 'block_time',
                       'token_bought_amount', 'token_sold_amount')
order by ordinal_position
