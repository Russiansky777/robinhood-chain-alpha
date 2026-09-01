-- r1_smoke_open1h.sql
-- Sprint R1, Шаг 2 (смоук): выход "открытие+1ч" -- фиксированное окно
-- (открытие+30м; открытие+90м] единственной следующей сессии в окне
-- смоука (понедельник {{next_open}}), одинаковое для ВСЕХ чекпоинтов
-- (не зависит от t) -- отдельная маленькая агрегация (26 строк, по
-- одной на токен), не дублируется в основном чекпоинт-запросе.
select
    case when token_bought_address in ({{token_address_list}}) then token_bought_address
         else token_sold_address end as token_address,
    sum(amount_usd) as vol,
    sum(case when token_bought_address in ({{token_address_list}}) then token_bought_amount
             else token_sold_amount end) as qty,
    count(*) as n
from dex.trades
where blockchain = 'robinhood'
  and (token_bought_address in ({{token_address_list}})
       or token_sold_address in ({{token_address_list}}))
  and block_time > timestamp '{{open1h_start}}'
  and block_time <= timestamp '{{open1h_end}}'
  and amount_usd is not null
group by 1
