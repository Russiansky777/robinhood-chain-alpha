-- r1_full_session_open_windows.sql
-- Sprint R1, Шаг 3 (полный прогон): "открытие+1ч" = VWAP в
-- (открытие+30м; открытие+90м] -- но теперь не один захардкоженный
-- понедельник (как на смоуке), а КАЖДАЯ торговая сессия периода
-- (список торговых дат передаётся из Python -- он уже знает
-- календарь NYSE на 01.07-30.08.2026, включая праздник 03.07, см.
-- docs/R1_DESIGN.md, "Механика"). Один ряд на (токен, дата сессии) --
-- динамический выбор "ближайшей следующей сессии" для конкретного
-- чекпоинта делается локально в Python (next_session_date()), здесь
-- считается общий пул возможных исходов ОДИН раз на дату, а не
-- по чекпоинту -- те же чекпоинты, что резолвятся в одну и ту же
-- сессию, переиспользуют один и тот же ряд.
with sessions as (
    select tok.token_address, sess.session_date
    from unnest(array[{{session_date_list}}]) as sess(session_date)
    cross join unnest(array[{{token_address_list}}]) as tok(token_address)
),
trades as (
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
      and block_time >= timestamp '{{trades_start}}'
      and block_time <  timestamp '{{trades_end}}'
      and amount_usd is not null
)
select
    s.token_address,
    s.session_date,
    sum(case when t.block_time > s.session_date + interval '14' hour
              and t.block_time <= s.session_date + interval '15' hour then t.amount_usd end) as vol,
    sum(case when t.block_time > s.session_date + interval '14' hour
              and t.block_time <= s.session_date + interval '15' hour then t.token_qty end) as qty,
    count(case when t.block_time > s.session_date + interval '14' hour
                and t.block_time <= s.session_date + interval '15' hour then 1 end) as n
from sessions s
left join trades t on t.token_address = s.token_address
group by s.token_address, s.session_date
order by s.token_address, s.session_date
