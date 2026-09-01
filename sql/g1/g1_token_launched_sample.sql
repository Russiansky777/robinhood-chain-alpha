-- g1_token_launched_sample.sql
-- Sprint G1, блокирующее условие 3, задача 2: несколько сырых строк
-- TokenLaunched (topic0 подтверждён ончейн, см.
-- sql/g1/g1_factory_logs_topic0_probe.sql и data/pons_family/SOURCE.md)
-- для ручного декодирования в Python (topic1/2/3 -- индексированные
-- адреса, data -- 7 слов по 32 байта: pairToken, pool, dexId,
-- launchConfigId, positionId, restrictionsEndBlock, initialBuyAmount).
-- block_number включён для оценки среднего времени блока по двум
-- последовательным логам (не хардкодится).
select
    tx_hash,
    block_number,
    block_time,
    topic1,
    topic2,
    topic3,
    data
from robinhood.logs
where contract_address in (
    0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB,
    0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e
)
    and topic0 = 0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a
    and block_time >= timestamp '2026-07-13 00:00:00'
    and block_time <  timestamp '2026-07-15 00:00:00'
order by block_time
limit 25
