-- r1_rwa_full_universe.sql
-- Sprint R1, Шаг 2: полный список токенов, отслеживаемых курируемой
-- Dune-таблицей rwa_robinhood.balances -- это, вероятно, ПОЛНЫЙ и
-- ПРАВИЛЬНЫЙ реестр настоящих RWA сток-токенов Robinhood (не
-- факторный реестр rwa_stock_factory_robinhood, который не покрывает
-- флагманские тикеры -- см. run #18/19, и не прямой поиск по symbol в
-- dex.trades, контаминированный копиями -- run #20). last_updated
-- max/count -- сколько держателей и как давно обновлялось, для
-- понимания какие токены реально активны.
select
    token_symbol,
    token_address,
    token_standard,
    count(distinct address) as n_holders,
    max(last_updated) as last_updated_max,
    max(day) as last_day
from rwa_robinhood.balances
group by token_symbol, token_address, token_standard
order by n_holders desc
