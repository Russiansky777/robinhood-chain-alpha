-- r1_rwa_robinhood_schema_probe.sql
-- Sprint R1, Шаг 2: КРИТИЧНОЕ расхождение обнаружено -- 23 реально
-- торгуемых токена, прошедших фильтр §2.2 (GLD, DJT, HIMS, MRNA,
-- SKHY...), НЕ пересекаются НИ ПО ОДНОМУ symbol с 31 фидом, найденным
-- через chainlink_robinhood.dualaggregator_evt_answerupdated (AAPL,
-- TSLA, NVDA, MSFT...). Прежде чем считать это провалом гейта --
-- проверяем, нет ли на самом токен-контракте (схема rwa_robinhood,
-- упомянута в run #9 рядом с rwa_stock_factory_robinhood) прямого
-- decoded вызова/события с адресом price feed -- это было бы
-- авторитетнее сопоставления по текстовому тикеру.
select table_schema, table_name
from information_schema.tables
where table_schema = 'rwa_robinhood'
order by table_name
