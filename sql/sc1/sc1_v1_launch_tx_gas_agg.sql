-- sc1_v1_launch_tx_gas_agg.sql
-- Sprint SC1: run #6/#7 показали, что построчное чтение gas_used/value
-- по ~39680 транзакциям стоило бы ~11.2 кредита ЧТЕНИЯ (при остатке
-- бюджета 8.31) -- гард корректно отказал ДО оплаты. Владелец сам
-- предусмотрел этот случай (§1 Шаг1: "если трейсы дороги -- выборка
-- 100 транзакций для медианного gas_used"). Вместо построчного чтения
-- -- АГРЕГАТ на стороне Dune (approx_percentile для медианы) -- один
-- маленький результат, копеечное чтение.
select
    count(*) as n_tx,
    sum(case when success then 1 else 0 end) as n_success,
    approx_percentile(cast(gas_used as double), 0.5) filter (where success) as gas_used_median,
    avg(cast(gas_used as double)) filter (where success) as gas_used_mean,
    min(gas_used) filter (where success) as gas_used_min,
    max(gas_used) filter (where success) as gas_used_max,
    approx_percentile(cast(gas_price as double), 0.5) filter (where success) as gas_price_median,
    sum(case when cast(value as double) > 0 then 1 else 0 end) filter (where success) as n_nonzero_value,
    approx_percentile(cast(value as double), 0.5) filter (where cast(value as double) > 0 and success) as value_median_when_nonzero,
    max(cast(value as double)) filter (where success) as value_max
from robinhood.transactions
where "to" = 0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB
  and block_time >= timestamp '2026-08-01 00:00:00'
  and block_time <  timestamp '2026-08-13 00:00:00'
