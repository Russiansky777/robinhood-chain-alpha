-- r1_rwa_authoritative_tokens.sql
-- Sprint R1, Шаг 2: rwa_robinhood.balances (run #21) -- курируемая
-- Dune-таблица с ui_multiplier/balance_usd/price_source -- явно
-- построена именно для НАСТОЯЩИХ RWA-контрактов Robinhood (эти поля
-- не имеют смысла для случайного мем-токена). Достаём АВТОРИТЕТНЫЙ
-- token_address на symbol -- без контаминации копиями/пародиями,
-- которые засорили прямой поиск по symbol в dex.trades (run #20,
-- 1780 адресов на 31 тикер).
select distinct
    token_symbol,
    token_address,
    token_standard,
    price_source
from rwa_robinhood.balances
where token_symbol in ('AAPL','TSLA','NVDA','MSFT','AMZN','META','GOOGL','COIN',
    'PLTR','MSTR','GME','AMD','INTC','MU','ORCL','RGTI','RKLB','IONQ','CRCL','SLV','SGOV',
    'EWY','QQQ','USAR','DELL','CLSK','NBIS','CRWV','USO','SNDK','SPY')
order by token_symbol
