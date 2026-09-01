-- g1_v2_graduation_sample.sql
-- Sprint G1 v2: сырые строки PoolGraduated (topic1=token, data=
-- positionId/tokenAmount/pairTokenAmount, 3 слова по 32 байта) для
-- декодирования в Python и последующей проверки "круг замкнут" --
-- есть ли реальные свопы по token в dex.trades (v4 hook-пул, адрес
-- пула не гранулярен в dex.trades для v4 -- сверка идёт по адресу
-- ТОКЕНА, не пула, см. docs/G1_DESIGN.md).
select tx_hash, block_number, block_time, topic1, data
from robinhood.logs
where contract_address = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e
    and topic0 = 0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259
order by block_time
limit 30
