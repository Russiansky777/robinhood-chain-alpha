-- sc1_volume_24h_calib.sql
-- Sprint SC1, Шаг 3: калибровка узким срезом (владелец, наследуется из
-- G1: "калибровка узким срезом x2.5") ПЕРЕД полным 12-дневным прогоном.
-- Один день (01.08.2026) V1-запусков -- self-contained: адреса
-- пулов/токенов выводятся ВНУТРИ запроса из robinhood.logs (topic0
-- TokenLaunched, contract_address = PonsLaunchFactory V1), без
-- внешнего IN-листа. JOIN на dex.trades по project_contract_address
-- (адрес пула) с окном (t0; t0+24ч].
with v1_launches as (
    select
        substr(topic1, 13, 20) as token,
        substr(substr(data, 33, 32), 13, 20) as pool,
        block_time as t0
    from robinhood.logs
    where contract_address = 0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB
      and topic0 = 0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a
      and block_time >= timestamp '2026-08-01 00:00:00'
      and block_time <  timestamp '2026-08-02 00:00:00'
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
