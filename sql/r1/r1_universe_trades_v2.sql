-- r1_universe_trades_v2.sql
-- Sprint R1, Шаг 2 (переигровка Шага 1 recon): АВТОРИТЕТНЫЕ RWA-адреса
-- из rwa_robinhood.balances (run #21/22, курируемая Dune-таблица,
-- признак -- ui_multiplier/balance_usd/price_source) вместо
-- факторного реестра rwa_stock_factory_robinhood (не покрывает
-- флагманы, run #18/19) и вместо прямого поиска по symbol (копии
-- засоряют результат, run #20). {{token_address_list}} подставляется
-- из уже локально загруженного r1_rwa_full_universe.csv (194 токена,
-- 0 доп. кредитов на этот шаг).
with trades as (
    select
        case
            when token_bought_address in ({{token_address_list}}) then token_bought_address
            else token_sold_address
        end as token_address,
        block_time,
        amount_usd
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
    end) as n_trades_closed_hours
from trades
group by token_address
order by n_trades desc
