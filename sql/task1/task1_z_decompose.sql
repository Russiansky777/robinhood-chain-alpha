-- task1_z_decompose.sql
-- Задача 1, проверка 2 (владелец, 2026-09-05, дословно): "Тайминг. Z
-- разбить: Z1 = вс 20:00 -> 21:00 ET, Z2 = 21:00 -> пн 9:30. corr(X,
-- Z1), corr(X, Z2), доля обратных знаков для каждого. Где живёт
-- возврат." Не пересчитывает X (уже есть в task1_weekend_gap_result.json
-- -- джойнится в Python), только НОВЫЕ брекеты внутри Z-окна.
--
-- Z1 = [вс 20:00 ET, вс 21:00 ET) = [пн 00:00 UTC, пн 01:00 UTC) --
-- делим на 2 получасовых брекета по тому же принципу VWAP-у-границы,
-- что x_start/x_end/z_start/z_end в task1_weekend_windows.sql:
--   z1_start = [friday+3d 00:00, friday+3d 00:30)
--   z1_end   = [friday+3d 00:30, friday+3d 01:00)   -- он же начало Z2
-- Z2 = [вс 21:00 ET, пн 9:30 ET) = [пн 01:00 UTC, пн 13:30 UTC):
--   z2_start = z1_end (та же граница, переиспользуем -- ниже отдельным
--              именем для ясности, значение идентично z1_end)
--   z2_end   = [friday+3d 11:30, friday+3d 13:30) -- ТОТ ЖЕ бренд, что
--              z_end в task1_weekend_windows.sql (уже реально посчитан
--              и закэширован там, но здесь пересчитываем в одном
--              прогоне для простоты джойна, дёшево -- то же окно
--              сканирования, тот же fee).
--
-- {{weekend_friday_list}}, {{token_address_list}} -- тот же формат
-- (varbinary from_hex литералы), что в task1_weekend_windows.sql.
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
    sum(case when t.block_time >= w.friday_utc + interval '3' day
              and t.block_time <  w.friday_utc + interval '3' day + interval '30' minute
             then t.amount_usd end) as z1_start_vol,
    sum(case when t.block_time >= w.friday_utc + interval '3' day
              and t.block_time <  w.friday_utc + interval '3' day + interval '30' minute
             then t.token_qty end) as z1_start_qty,
    count(case when t.block_time >= w.friday_utc + interval '3' day
                and t.block_time <  w.friday_utc + interval '3' day + interval '30' minute
               then 1 end) as z1_start_n,
    sum(case when t.block_time >= w.friday_utc + interval '3' day + interval '30' minute
              and t.block_time <  w.friday_utc + interval '3' day + interval '1' hour
             then t.amount_usd end) as z1_end_vol,
    sum(case when t.block_time >= w.friday_utc + interval '3' day + interval '30' minute
              and t.block_time <  w.friday_utc + interval '3' day + interval '1' hour
             then t.token_qty end) as z1_end_qty,
    count(case when t.block_time >= w.friday_utc + interval '3' day + interval '30' minute
                and t.block_time <  w.friday_utc + interval '3' day + interval '1' hour
               then 1 end) as z1_end_n,
    sum(case when t.block_time >= w.friday_utc + interval '3' day + interval '11' hour + interval '30' minute
              and t.block_time <  w.friday_utc + interval '3' day + interval '13' hour + interval '30' minute
             then t.amount_usd end) as z2_end_vol,
    sum(case when t.block_time >= w.friday_utc + interval '3' day + interval '11' hour + interval '30' minute
              and t.block_time <  w.friday_utc + interval '3' day + interval '13' hour + interval '30' minute
             then t.token_qty end) as z2_end_qty,
    count(case when t.block_time >= w.friday_utc + interval '3' day + interval '11' hour + interval '30' minute
                and t.block_time <  w.friday_utc + interval '3' day + interval '13' hour + interval '30' minute
               then 1 end) as z2_end_n
from weekends w
left join trades t
  on t.token_address = w.token_address
 and t.block_time >= w.friday_utc + interval '3' day
 and t.block_time <  w.friday_utc + interval '3' day + interval '13' hour + interval '30' minute
group by w.token_address, w.friday_utc
order by w.token_address, w.friday_utc
