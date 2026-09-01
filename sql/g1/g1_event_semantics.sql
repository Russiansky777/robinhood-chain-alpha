-- g1_event_semantics.sql
-- Sprint G1, проверка семантики события (владелец, 2026-09-01, после
-- аномалии масштаба: 266 221 "градуаций" за 6 недель выглядит
-- экстраординарно много для завершений бондинг-кривой). Проверяет:
-- 1. topic0 TokenLaunched и TokenDeployed оба присутствуют, и
--    TokenDeployed >= TokenLaunched по числу токенов (деплой должен
--    предшествовать/включать запуск, не наоборот -- иначе перепутаны
--    сигнатуры).
-- 2. Распределение задержки launched_at - deployed_at на токен: медиана
--    в секундах = мгновенные запуски/спам; в часах-днях = настоящие
--    бондинг-кривые.
--
-- ВСЯ агрегация -- на стороне Dune (approx_percentile), наружу уходит
-- ОДНА строка -- не построчная выгрузка (см. dune_client.py ревизия 3
-- гарда, требование владельца после перерасхода на построчных чтениях
-- в run #9).
with events as (
    select topic1 as token, topic0, block_time
    from robinhood.logs
    where contract_address in (
        0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB,
        0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e
    )
        and topic0 in (
            0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a,  -- TokenLaunched
            0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a   -- TokenDeployed
        )
        and block_time >= timestamp '2026-07-01 00:00:00'
        and block_time <  timestamp '2026-08-30 00:00:00'
),
per_token as (
    select
        token,
        min(case when topic0 = 0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a then block_time end) as launched_at,
        min(case when topic0 = 0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a then block_time end) as deployed_at
    from events
    group by token
),
delays as (
    select date_diff('second', deployed_at, launched_at) as delay_s
    from per_token
    where deployed_at is not null and launched_at is not null
)
select
    (select count(*) from per_token where launched_at is not null) as n_launched_tokens,
    (select count(*) from per_token where deployed_at is not null) as n_deployed_tokens,
    (select count(*) from per_token where launched_at is not null and deployed_at is not null) as n_both,
    (select count(*) from per_token where launched_at is not null and deployed_at is null) as n_launched_no_deploy_seen,
    (select count(*) from delays) as n_delay_samples,
    (select min(delay_s) from delays) as min_delay_s,
    (select approx_percentile(delay_s, 0.1) from delays) as p10_delay_s,
    (select approx_percentile(delay_s, 0.5) from delays) as median_delay_s,
    (select approx_percentile(delay_s, 0.9) from delays) as p90_delay_s,
    (select max(delay_s) from delays) as max_delay_s
