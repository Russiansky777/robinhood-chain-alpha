-- r1_stock_token_deployments.sql
-- Sprint R1, Шаг 1: ончейн-реестр деплоя сток-токенов --
-- rwa_stock_factory_robinhood.factory_deployer_evt_deployed (найдено
-- run #9/#10) -- symbol, name, адрес токена (stock), uid. Кросс-сверка
-- с data/sprintR1_cache/r1_stock_tokens_raw.json (живой REST
-- /rhj/assets). Граница по времени -- весь период покрытия на
-- разведке.
select
    symbol,
    name,
    stock as token_address,
    uid,
    evt_block_time as deployed_at,
    contract_address as factory_address
from rwa_stock_factory_robinhood.factory_deployer_evt_deployed
where evt_block_time >= timestamp '2026-07-01 00:00:00'
  and evt_block_time <  timestamp '2026-09-01 00:00:00'
order by evt_block_time
