-- r1_full_checkpoint_windows.sql
-- Sprint R1, Шаг 3 (полный прогон): чекпоинт-окна на партицию, ИСПРАВЛЕНО
-- после run #27 (wk07, 12-18.08.2026): партиция стоила 81.16 кредита
-- по факту вместо оценки 6.0 (>>2x + >=25 -- сработал автостоп
-- credit_guard.check_overrun_after_execute) -- на порядок дороже
-- соседних недель (0.24-4.1 кредита каждая).
--
-- Причина: унаследованный от смоука join `on t.token_address =
-- c.token_address` БЕЗ временной границы -- вся привязка к окну
-- (P/entry/exit4h/exit12h) делалась ПОСЛЕ джойна, в CASE WHEN самого
-- SELECT. Для токена с обычным объёмом это не страшно (движок Dune
-- переваривает джойн 168 чекпоинтов x N_сделок_за_неделю), но при
-- всплеске объёма У ОДНОГО токена В ОДНОЙ неделе (правдоподобно --
-- корпоративное действие/всплеск ажиотажа, см. docs/R1_DESIGN.md,
-- "Оракул-паузы во время корп. действий") промежуточное произведение
-- 168 x N_сделок взрывается ЗАДОЛГО до GROUP BY -- квадратичный
-- эффект, которого не было видно на смоуке (только 4 дня, спокойная
-- выборка) и на первых 6 партициях (видимо, обычный объём).
--
-- Исправление: временная граница ПЕРЕНЕСЕНА В САМ JOIN (диапазон,
-- покрывающий объединение всех четырёх окон: (t-30м; t+12ч]) -- джойн
-- ограничен ЛОКАЛЬНОЙ плотностью сделок вокруг конкретного чекпоинта,
-- а не общим недельным объёмом токена, независимо от того, насколько
-- сильный всплеск объёма был в остальное время недели. Семантика
-- CASE WHEN (какая сделка в какое под-окно попадает) не изменена --
-- изменена только граница самого JOIN.
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
left join trades t
  on t.token_address = c.token_address
 and t.block_time > c.t_checkpoint - interval '30' minute
 and t.block_time <= c.t_checkpoint + interval '12' hour
group by c.token_address, c.t_checkpoint
order by c.token_address, c.t_checkpoint
