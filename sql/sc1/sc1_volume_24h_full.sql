-- sc1_volume_24h_full.sql
-- Sprint SC1, Шаг 3: объём торгов в первые 24ч после запуска, ВСЕ
-- августовские V1-запуски (01-13.08.2026 -- см. sc1_volume_24h_calib.sql
-- за пояснением подхода). Партиционируется по неделям в Python, если
-- калибровка покажет проекцию >40 кредитов на весь диапазон разом
-- (см. analysis/sc1_pipeline.py).
with v1_launches as (
    select
        substr(topic1, 13, 20) as token,
        substr(substr(data, 33, 32), 13, 20) as pool,
        block_time as t0
    from robinhood.logs
    where contract_address = 0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB
      and topic0 = 0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a
      and block_time >= timestamp '{{window_start}}'
      and block_time <  timestamp '{{window_end}}'
)
select
    l.token,
    sum(t.amount_usd) as vol_usd_24h,
    count(*) as n_trades_24h
from v1_launches l
join dex.trades t
  on t.project_contract_address = l.pool
 and t.blockchain = 'robinhood'
 and t.block_time > l.t0
 and t.block_time <= l.t0 + interval '24' hour
 and t.amount_usd is not null
group by l.token
