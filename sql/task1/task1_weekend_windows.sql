-- task1_weekend_windows.sql
-- Задача 1 (владелец, 2026-09-05, замена закрытому Query 3): для каждой
-- пары (токен, выходные с 01.07.2026) -- VWAP-брекеты в начале и конце
-- окна X (пт 20:00 -> вс 19:55 ET, т.е. сб 00:00 -> вс 23:55 UTC при
-- EDT=UTC-4) и окна Z (вс 20:00 -> пн 9:30 ET, т.е. пн 00:00 -> пн
-- 13:30 UTC) -- ТОТ ЖЕ стиль чекпоинт-брекетов, что уже реально
-- проверен и оплачен в Sprint R1 (r1_full_session_open_windows.sql,
-- r1_full_checkpoint_windows.sql): 2-часовой брекет у каждой границы
-- окна, амount_usd/token_qty из dex.trades (blockchain='robinhood'),
-- НЕ единичная сделка -- устойчивее к тонкому объёму на границе.
--
-- {{weekend_friday_list}} -- список ISO-дат пятниц (UTC полночь) через
-- запятую, каждая обёрнута в timestamp ''; {{token_address_list}} --
-- список `from_hex('...')`-литералов (VARBINARY), НЕ голых varchar-строк
-- в кавычках, как в sql/r1/*.sql -- РЕАЛЬНАЯ проверка 2026-09-05
-- (task1_dex_trades_columns_probe.sql, information_schema.columns)
-- показала, что token_bought_address/token_sold_address в dex.trades
-- сейчас VARBINARY -- первый прогон (run 33964786227) упал с "Cannot
-- find common type between varbinary and varchar(42)" на голых
-- varchar-литералах. Возможно, схема dex.trades реально изменилась
-- между прогоном Sprint R1 (01-04.09.2026, тот же паттерн там работал)
-- и сейчас (05.09.2026) -- sql/r1/*.sql НЕ переписаны задним числом
-- (их результат уже реально получен и закрыт), это отдельная находка о
-- дрейфе схемы Dune, зафиксированная здесь для будущих запросов к
-- dex.trades.
with weekends as (
    select tok.token_address, wk.friday_utc
    from unnest(array[{{weekend_friday_list}}]) as wk(friday_utc)
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
      and amount_usd > 0
)
select
    w.token_address,
    w.friday_utc,
    -- X-окно: [friday+1d 00:00, friday+3d 00:00) = сб 00:00 UTC .. вс 23:55(+5м) UTC
    sum(case when t.block_time >= w.friday_utc + interval '1' day
              and t.block_time <  w.friday_utc + interval '1' day + interval '2' hour
             then t.amount_usd end) as x_start_vol,
    sum(case when t.block_time >= w.friday_utc + interval '1' day
              and t.block_time <  w.friday_utc + interval '1' day + interval '2' hour
             then t.token_qty end) as x_start_qty,
    count(case when t.block_time >= w.friday_utc + interval '1' day
                and t.block_time <  w.friday_utc + interval '1' day + interval '2' hour
               then 1 end) as x_start_n,
    sum(case when t.block_time >= w.friday_utc + interval '3' day - interval '2' hour - interval '5' minute
              and t.block_time <  w.friday_utc + interval '3' day - interval '5' minute
             then t.amount_usd end) as x_end_vol,
    sum(case when t.block_time >= w.friday_utc + interval '3' day - interval '2' hour - interval '5' minute
              and t.block_time <  w.friday_utc + interval '3' day - interval '5' minute
             then t.token_qty end) as x_end_qty,
    count(case when t.block_time >= w.friday_utc + interval '3' day - interval '2' hour - interval '5' minute
                and t.block_time <  w.friday_utc + interval '3' day - interval '5' minute
               then 1 end) as x_end_n,
    sum(case when t.block_time >= w.friday_utc + interval '1' day
              and t.block_time <  w.friday_utc + interval '3' day - interval '5' minute
             then t.amount_usd end) as x_full_vol,
    count(case when t.block_time >= w.friday_utc + interval '1' day
                and t.block_time <  w.friday_utc + interval '3' day - interval '5' minute
               then 1 end) as x_full_n,
    -- Z-окно: [friday+3d 00:00, friday+3d 13:30) = пн 00:00 .. пн 13:30 UTC
    sum(case when t.block_time >= w.friday_utc + interval '3' day
              and t.block_time <  w.friday_utc + interval '3' day + interval '2' hour
             then t.amount_usd end) as z_start_vol,
    sum(case when t.block_time >= w.friday_utc + interval '3' day
              and t.block_time <  w.friday_utc + interval '3' day + interval '2' hour
             then t.token_qty end) as z_start_qty,
    count(case when t.block_time >= w.friday_utc + interval '3' day
                and t.block_time <  w.friday_utc + interval '3' day + interval '2' hour
               then 1 end) as z_start_n,
    sum(case when t.block_time >= w.friday_utc + interval '3' day + interval '11' hour + interval '30' minute
              and t.block_time <  w.friday_utc + interval '3' day + interval '13' hour + interval '30' minute
             then t.amount_usd end) as z_end_vol,
    sum(case when t.block_time >= w.friday_utc + interval '3' day + interval '11' hour + interval '30' minute
              and t.block_time <  w.friday_utc + interval '3' day + interval '13' hour + interval '30' minute
             then t.token_qty end) as z_end_qty,
    count(case when t.block_time >= w.friday_utc + interval '3' day + interval '11' hour + interval '30' minute
                and t.block_time <  w.friday_utc + interval '3' day + interval '13' hour + interval '30' minute
               then 1 end) as z_end_n
from weekends w
left join trades t
  on t.token_address = w.token_address
 and t.block_time >= w.friday_utc + interval '1' day
 and t.block_time <  w.friday_utc + interval '3' day + interval '13' hour + interval '30' minute
group by w.token_address, w.friday_utc
order by w.token_address, w.friday_utc
