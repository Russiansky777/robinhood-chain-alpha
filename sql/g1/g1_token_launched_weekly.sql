-- g1_token_launched_weekly.sql
-- Sprint G1, полнопериодный счёт градуаций (блокирующее требование 2,
-- пересчитано по подтверждённому событию, не по старому прокси "новый
-- v3-пул"). Один недельный партиций -- держит запрос под потолком
-- санитарной проверки (40 кредитов/запрос) и избегает UNION ALL по
-- тяжёлым источникам (см. analysis/credit_guard.py, Dune Rule 2).
--
-- Возвращает СЫРЫЕ строки (не агрегирует на стороне Dune) -- дедуп "по
-- первому событию на токен" и полное декодирование делаются в Python
-- (analysis/g1_graduation_events.py), т.к. это тот же ABI-layout, что
-- уже проверен в sql/g1/g1_token_launched_sample.sql.
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
    and block_time >= timestamp '{{week_start}}'
    and block_time <  timestamp '{{week_end}}'
order by block_time
