-- task1_chainlink_weekend_updates.sql
-- Проверка 1 к переключению Y на Chainlink (владелец, 2026-09-05):
-- "Обновляется ли фид по выходным: таймстемпы AnswerUpdated для 5
-- фидов за три последних выходных, пт 20:00 -> вс 20:00 ET. Число
-- обновлений в окне." Таблица/поля -- ТЕ ЖЕ, что уже реально
-- использованы в Sprint R1 (r1_feed_activity.sql):
-- chainlink_robinhood.dualaggregator_evt_answerupdated(contract_address,
-- evt_block_time). {{feed_address_list}} -- 5 адресов через запятую в
-- кавычках; {{weekend_friday_list}} -- список timestamp-литералов
-- (пятница 00:00 UTC) через запятую.
with weekends as (
    select feed.feed_address, wk.friday_utc
    from unnest(array[{{weekend_friday_list}}]) as wk(friday_utc)
    cross join unnest(array[{{feed_address_list}}]) as feed(feed_address)
)
select
    w.feed_address,
    w.friday_utc,
    count(e.evt_block_time) as n_updates_in_window,
    min(e.evt_block_time) as first_update_in_window,
    max(e.evt_block_time) as last_update_in_window
from weekends w
left join chainlink_robinhood.dualaggregator_evt_answerupdated e
  on e.contract_address = w.feed_address
     -- окно: пт 20:00 ET -> вс 20:00 ET = сб 00:00 UTC -> вс 24:00(=пн 00:00) UTC при EDT=UTC-4
 and e.evt_block_time >= w.friday_utc + interval '1' day
 and e.evt_block_time <  w.friday_utc + interval '3' day
group by w.feed_address, w.friday_utc
order by w.feed_address, w.friday_utc
