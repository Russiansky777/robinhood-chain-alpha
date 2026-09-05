-- task1_chainlink_weekend_prices.sql
-- Проверка 2 к переключению Y на Chainlink (владелец, 2026-09-05):
-- реальные сырые обновления `current` (int256, decimals=8, см.
-- data/sprintR1_cache/r1_feed_token_map.csv) для 5 фидов в широком
-- окне вокруг каждого выходного (пт 12:00 UTC .. пн 16:00 UTC) --
-- "ближайший до закрытия"/"ближайший после открытия" выбираются в
-- Python (проще и надёжнее, чем корреляционные подзапросы, объём
-- данных крошечный). contract_address -- VARBINARY, from_hex()
-- (реально подтверждено, information_schema.columns).
select
    contract_address as feed_address,
    evt_block_time,
    current as price_raw
from chainlink_robinhood.dualaggregator_evt_answerupdated
where contract_address in ({{feed_address_list}})
  and evt_block_time >= timestamp '{{window_start}}'
  and evt_block_time <  timestamp '{{window_end}}'
order by contract_address, evt_block_time
