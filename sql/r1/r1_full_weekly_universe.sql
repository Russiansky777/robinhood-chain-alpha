-- r1_full_weekly_universe.sql
-- Sprint R1, Шаг 3: §2.2 требует еженедельный пересчёт вселенной
-- ("Токен входит во вселенную на неделе W, если за предыдущие 7 дней:
-- >=100 сделок И >=$10 000 объёма"). Разведка/Шаг 2 проверили порог на
-- ВЕСЬ период разом (с огромным запасом, см. docs/R1_DESIGN.md) --
-- этот запрос честно проверяет то же самое понедельно, по календарным
-- неделям партиционирования Шага 3 (упрощение относительно буквально
-- скользящего 7-дневного окна, зафиксировано и объяснено в отчёте:
-- при объёмах в разы-десятки выше порога разница между календарной
-- неделей и скользящим окном не меняет исход ни для одного токена).
with weeks as (
    select tok.token_address, wk.week_start
    from unnest(array[{{week_start_list}}]) as wk(week_start)
    cross join unnest(array[{{token_address_list}}]) as tok(token_address)
),
trades as (
    select
        case when token_bought_address in ({{token_address_list}}) then token_bought_address
             else token_sold_address end as token_address,
        block_time,
        amount_usd
    from dex.trades
    where blockchain = 'robinhood'
      and (token_bought_address in ({{token_address_list}})
           or token_sold_address in ({{token_address_list}}))
      and block_time >= timestamp '{{trades_start}}'
      and block_time <  timestamp '{{trades_end}}'
      and amount_usd is not null
)
select
    w.token_address,
    w.week_start,
    sum(case when t.block_time >= w.week_start
              and t.block_time <  w.week_start + interval '7' day then t.amount_usd end) as vol_usd,
    count(case when t.block_time >= w.week_start
                and t.block_time <  w.week_start + interval '7' day then 1 end) as n_trades
from weeks w
left join trades t on t.token_address = w.token_address
group by w.token_address, w.week_start
order by w.token_address, w.week_start
