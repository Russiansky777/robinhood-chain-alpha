-- g1_v2_graduation_full.sql
-- Sprint G1 v2: ВСЕ строки PoolGraduated за период (не выборка -- их
-- всего 896 по агрегату g1_v2_daily_graduations, не нужно сэмплировать,
-- см. владелец: "может использовать полный набор напрямую, если
-- дёшево"). Та же схема, что g1_v2_graduation_sample.sql, без LIMIT 30.
select tx_hash, block_number, block_time, topic1, data
from robinhood.logs
where contract_address = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e
    and topic0 = 0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259
order by block_time
limit 1000
