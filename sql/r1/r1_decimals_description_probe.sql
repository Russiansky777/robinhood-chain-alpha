-- r1_decimals_description_probe.sql
-- Sprint R1, Шаг 2: перед тем как городить RPC-путь (ALCHEMY_API_KEY не
-- настроен в секретах репозитория, run #14) -- проверяем, не декодировал
-- ли Dune уже вызовы (не только события) decimals()/description() на
-- контрактах chainlink_robinhood (Spellbook иногда генерирует
-- `<contract>_call_<method>` таблицы из трейсов для верифицированных
-- ABI). Если да -- закрывает decimals-вопрос и, возможно,
-- token<->feed сопоставление одним и тем же дешёвым запросом, без RPC.
select table_schema, table_name
from information_schema.tables
where table_schema = 'chainlink_robinhood'
  and (table_name like '%call%' or table_name like '%decimal%' or table_name like '%description%')
order by table_name
