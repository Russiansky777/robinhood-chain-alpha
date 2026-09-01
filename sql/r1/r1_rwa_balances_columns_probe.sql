-- r1_rwa_balances_columns_probe.sql
-- Sprint R1, Шаг 2: r1_flagship_trades_by_symbol (run #20) вернул 1780
-- строк для 31 тикера -- значит МНОГО разных token_address делят один
-- и тот же symbol (копии/пародии, обычная практика мем-токенов --
-- называть токен в честь популярной акции). Наивное сопоставление по
-- symbol в dex.trades ненадёжно -- нужен АВТОРИТЕТНЫЙ список адресов.
-- rwa_robinhood.balances/core_balances_enriched/extended_balances_
-- enriched (run #19) -- вероятно, Dune Spellbook отслеживает балансы
-- ТОЛЬКО для настоящих RWA-контрактов Robinhood (курируемый список),
-- не для любых токенов с похожим именем. Колонки перед платным запросом.
select table_schema, table_name, column_name, data_type, ordinal_position
from information_schema.columns
where table_schema = 'rwa_robinhood'
order by table_name, ordinal_position
