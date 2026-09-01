-- g1_v2_graduation_full.sql
-- Sprint G1 v2: ВСЕ строки PoolGraduated за ПЕРИОД §2.1 (не выборка --
-- их 896 по агрегату g1_v2_daily_graduations за тот же период, не
-- нужно сэмплировать, см. владелец: "может использовать полный набор
-- напрямую, если дёшево"). Та же схема, что g1_v2_graduation_sample.sql,
-- без LIMIT 30.
--
-- ВАЖНО (найдено и исправлено 2026-09-01, run #14): первая версия этого
-- файла не имела границы по block_time вообще -- при LIMIT 1000 запрос
-- зацепил градуации ПОСЛЕ g1_period_end (события продолжаются как
-- минимум до 2026-09-01, см. g1_v2_swap_crosscheck), вернув ровно 1000
-- строк (упёрлись в LIMIT) вместо 896 в периоде -- пост-фильтровый N
-- считался бы по грязной, не пре-регистрированной выборке. Граница
-- ниже -- та же, что в g1_v2_daily_graduations.sql.
select tx_hash, block_number, block_time, topic1, data
from robinhood.logs
where contract_address = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e
    and topic0 = 0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259
    and block_time >= timestamp '2026-07-01 00:00:00'
    and block_time <  timestamp '2026-08-30 00:00:00'
order by block_time
limit 1000
