-- r1_feed_activity.sql
-- Sprint R1, Шаг 1(в): по каждому Chainlink-фиду (contract_address) --
-- число обновлений (AnswerUpdated), первое/последнее обновление,
-- признак активности в закрытые часы рынка (вне 13:30-20:00 UTC пн-пт,
-- §2.1) за период. Граница по времени обязательна (наследуется правило
-- G1). Период широкий на разведке (весь июль-август) -- уточняется
-- после фиксации фактического конца покрытия.
select
    contract_address as feed_address,
    count(*) as n_updates,
    min(evt_block_time) as first_update,
    max(evt_block_time) as last_update,
    sum(case
        when day_of_week(evt_block_time) between 1 and 5   -- Пн=1..Вс=7 (Trino)
             and cast(evt_block_time as time) >= time '13:30:00'
             and cast(evt_block_time as time) <  time '20:00:00'
        then 0 else 1
    end) as n_updates_outside_market_hours
from chainlink_robinhood.dualaggregator_evt_answerupdated
where evt_block_time >= timestamp '2026-07-01 00:00:00'
  and evt_block_time <  timestamp '2026-09-01 00:00:00'
group by contract_address
order by n_updates desc
