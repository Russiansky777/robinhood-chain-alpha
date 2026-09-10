-- task4_dex_trades_full_columns_probe.sql
-- Задача 4 владельца (2026-09-10): нужен реальный столбец с адресом
-- ТРЕЙДЕРА (кто отправил своп) для расчёта концентрации "кто закрывает
-- расхождения > 1%". Прежний probe (task1_dex_trades_columns_probe.sql)
-- явно ограничивал список колонок под свои нужды (адреса токенов,
-- amount_usd, block_time) и НЕ включал столбец трейдера. Не гадаем имя
-- по памяти (tx_from/taker/trader -- в разных версиях Spellbook
-- называется по-разному) -- здесь запрашиваем ВЕСЬ реальный список
-- колонок dex.trades, 0/минимальные кредиты (information_schema).
select column_name, data_type, ordinal_position
from information_schema.columns
where table_schema = 'dex' and table_name = 'trades'
order by ordinal_position
