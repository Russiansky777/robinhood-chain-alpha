-- r1_smoke_checkpoint_windows.sql
-- Sprint R1, Шаг 2 (смоук): ИСПРАВЛЕНО после run #24 -- сырое чтение
-- строк по сделкам (116069 строк за 1 день) стоило 18.9 кредита,
-- проекция на 4 дня 191.9 >> 40 санитарного лимита. Правило G1/SC1
-- ("чтение результатов биллится по объёму, агрегируй на стороне
-- Dune") было нарушено -- этот запрос агрегирует ВСЕ окна (цена
-- P(t-30m,t], вход (t,t+30m], выходы 4ч/12ч) на стороне Dune, наружу
-- идут только ~26 токенов x ~93 чекпоинта = ~2400 маленьких строк
-- вместо сотен тысяч сырых сделок.
with checkpoints as (
    select tok.token_address, seq.t_checkpoint
    from unnest(sequence(
        timestamp '{{checkpoint_start}}', timestamp '{{checkpoint_end}}', interval '1' hour
    )) as seq(t_checkpoint)
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
    c.token_address,
    c.t_checkpoint,
    sum(case when t.block_time > c.t_checkpoint - interval '30' minute
              and t.block_time <= c.t_checkpoint then t.amount_usd end) as p_vol,
    sum(case when t.block_time > c.t_checkpoint - interval '30' minute
              and t.block_time <= c.t_checkpoint then t.token_qty end) as p_qty,
    count(case when t.block_time > c.t_checkpoint - interval '30' minute
                and t.block_time <= c.t_checkpoint then 1 end) as p_n,
    sum(case when t.block_time > c.t_checkpoint
              and t.block_time <= c.t_checkpoint + interval '30' minute then t.amount_usd end) as entry_vol,
    sum(case when t.block_time > c.t_checkpoint
              and t.block_time <= c.t_checkpoint + interval '30' minute then t.token_qty end) as entry_qty,
    count(case when t.block_time > c.t_checkpoint
                and t.block_time <= c.t_checkpoint + interval '30' minute then 1 end) as entry_n,
    sum(case when t.block_time > c.t_checkpoint + interval '3' hour + interval '30' minute
              and t.block_time <= c.t_checkpoint + interval '4' hour then t.amount_usd end) as exit4h_vol,
    sum(case when t.block_time > c.t_checkpoint + interval '3' hour + interval '30' minute
              and t.block_time <= c.t_checkpoint + interval '4' hour then t.token_qty end) as exit4h_qty,
    count(case when t.block_time > c.t_checkpoint + interval '3' hour + interval '30' minute
                and t.block_time <= c.t_checkpoint + interval '4' hour then 1 end) as exit4h_n,
    sum(case when t.block_time > c.t_checkpoint + interval '11' hour + interval '30' minute
              and t.block_time <= c.t_checkpoint + interval '12' hour then t.amount_usd end) as exit12h_vol,
    sum(case when t.block_time > c.t_checkpoint + interval '11' hour + interval '30' minute
              and t.block_time <= c.t_checkpoint + interval '12' hour then t.token_qty end) as exit12h_qty,
    count(case when t.block_time > c.t_checkpoint + interval '11' hour + interval '30' minute
                and t.block_time <= c.t_checkpoint + interval '12' hour then 1 end) as exit12h_n
from checkpoints c
left join trades t on t.token_address = c.token_address
group by c.token_address, c.t_checkpoint
order by c.token_address, c.t_checkpoint
