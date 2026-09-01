-- r1_flagship_trades_by_symbol.sql
-- Sprint R1, Шаг 2: КРИТИЧНОЕ расхождение (run #18/19) -- 23 токена,
-- прошедших §2.2 через ончейн-реестр rwa_stock_factory_robinhood, НЕ
-- пересекаются НИ ПО ОДНОМУ тикеру с 31 активным Chainlink-фидом
-- (AAPL, TSLA, NVDA...). Гипотеза: "флагманские" тикеры с фидами были
-- задеплоены ДО 01.07.2026 (генезис чейна) или через ДРУГОЙ контракт,
-- не decoded factory_deployer_evt_deployed -- поэтому их адреса не
-- попали в наш r1_stock_token_deployments список и, как следствие, в
-- r1_universe_trades. Проверяем напрямую по symbol в dex.trades --
-- Dune курирует token_bought_symbol/token_sold_symbol независимо от
-- того, как токен был задеплоен.
select
    case when token_bought_symbol in ('AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','COIN',
        'PLTR','MSTR','GME','AMD','INTC','MU','ORCL','RGTI','RKLB','IONQ','CRCL','SLV','SGOV',
        'EWY','QQQ','USAR','DELL','CLSK','NBIS','CRWV','USO','SNDK','SPY')
        then token_bought_symbol else token_sold_symbol end as symbol,
    case when token_bought_symbol in ('AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','COIN',
        'PLTR','MSTR','GME','AMD','INTC','MU','ORCL','RGTI','RKLB','IONQ','CRCL','SLV','SGOV',
        'EWY','QQQ','USAR','DELL','CLSK','NBIS','CRWV','USO','SNDK','SPY')
        then token_bought_address else token_sold_address end as token_address,
    count(*) as n_trades,
    sum(amount_usd) as vol_usd,
    sum(case
        when day_of_week(block_time) between 1 and 5
             and cast(block_time as time) >= time '13:30:00'
             and cast(block_time as time) <  time '20:00:00'
        then 0 else 1
    end) as n_trades_closed_hours
from dex.trades
where blockchain = 'robinhood'
  and (token_bought_symbol in ('AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','COIN',
        'PLTR','MSTR','GME','AMD','INTC','MU','ORCL','RGTI','RKLB','IONQ','CRCL','SLV','SGOV',
        'EWY','QQQ','USAR','DELL','CLSK','NBIS','CRWV','USO','SNDK','SPY')
    or token_sold_symbol in ('AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','COIN',
        'PLTR','MSTR','GME','AMD','INTC','MU','ORCL','RGTI','RKLB','IONQ','CRCL','SLV','SGOV',
        'EWY','QQQ','USAR','DELL','CLSK','NBIS','CRWV','USO','SNDK','SPY'))
  and block_time >= timestamp '2026-07-01 00:00:00'
  and block_time <  timestamp '2026-09-01 00:00:00'
  and amount_usd is not null
group by 1, 2
order by vol_usd desc
