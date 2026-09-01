-- r1_smoke_trades.sql
-- Sprint R1, Шаг 2 (смоук): сделки по 26 ликвидным токенам с известным
-- активным Chainlink-фидом (пересечение §2.2-прохождения и наличия
-- фида, см. docs/R1_DESIGN.md "Шаг 2") за один срединный уикенд
-- (25-26.07.2026) + буфер до/после для checkpoint-анкоринга и
-- горизонтов выхода (open+1ч в понедельник). {{window_start}}/
-- {{window_end}} подставляются, {{token_address_list}} -- 26 адресов
-- из r1_token_feed_map.csv (self-contained, тот же паттерн, что
-- r1_universe_trades.sql).
select
    case when token_bought_address in ({{token_address_list}}) then token_bought_address
         else token_sold_address end as token_address,
    block_time,
    amount_usd,
    case when token_bought_address in ({{token_address_list}}) then token_bought_amount
         else token_sold_amount end as token_qty
from dex.trades
where blockchain = 'robinhood'
  and (token_bought_address in ({{token_address_list}})
       or token_sold_address in ({{token_address_list}}))
  and block_time >= timestamp '{{window_start}}'
  and block_time <  timestamp '{{window_end}}'
  and amount_usd is not null
