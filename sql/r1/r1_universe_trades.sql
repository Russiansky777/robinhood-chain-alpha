-- r1_universe_trades.sql
-- Sprint R1, Шаг 1: гейт разведки -- по каждому сток-токену (адреса
-- подставляются из ончейн-реестра деплоя, r1_stock_token_deployments)
-- число сделок и объём в dex.trades за весь период разведки, плюс
-- разбивка на закрытые/открытые часы рынка (§2.1: вне 13:30-20:00 UTC
-- пн-пт, плюс 03.07.2026 -- НЕ фильтруется отдельно здесь, весь день
-- закрыт по календарю NYSE, США -- см. docs/R1_DESIGN.md "Механика").
-- НЕ фильтруем project='uniswap' -- сток-токены могут торговаться
-- через RFQ-агрегаторы (0x/1inch/LiFi, см. docs.robinhood.com/chain/
-- building-with-stock-tokens), которые dex.trades может относить к
-- другому project -- смотрим, что реально придёт.
with trades as (
    select
        case
            when token_bought_address in ({{token_address_list}}) then token_bought_address
            else token_sold_address
        end as token_address,
        block_time,
        amount_usd,
        project
    from dex.trades
    where blockchain = 'robinhood'
      and (token_bought_address in ({{token_address_list}})
           or token_sold_address in ({{token_address_list}}))
      and block_time >= timestamp '2026-07-01 00:00:00'
      and block_time <  timestamp '2026-09-01 00:00:00'
      and amount_usd is not null
)
select
    token_address,
    count(*) as n_trades,
    sum(amount_usd) as vol_usd,
    sum(case
        when day_of_week(block_time) between 1 and 5
             and cast(block_time as time) >= time '13:30:00'
             and cast(block_time as time) <  time '20:00:00'
        then 0 else 1
    end) as n_trades_closed_hours,
    array_agg(distinct project) as projects
from trades
group by token_address
order by n_trades desc
