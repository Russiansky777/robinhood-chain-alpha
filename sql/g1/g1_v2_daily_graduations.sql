-- g1_v2_daily_graduations.sql
-- Sprint G1, перенацеливание на Pons V2 (владелец, 2026-09-01):
-- настоящее событие градуации v2 -- `PoolGraduated(address indexed
-- token, uint256 positionId, uint256 tokenAmount, uint256
-- pairTokenAmount)`, объявлено в contractsV2/src/v2/PonsV2LaunchFactory.sol
-- (см. data/pons_family/SOURCE.md за источником и командой
-- воспроизведения хэша). НЕ "TokenLaunched" -- это имя в v2 переиспользовано
-- для события деплоя на бондинг-кривую (аналог TokenDeployed в v1),
-- что и объясняет аномалию нулевой задержки в v1-выборке.
--
-- Посуточный агрегат за ВЕСЬ период §2.1 (не только "с начала августа" --
-- это оценка владельца, не факт; сканируем весь диапазон, чтобы найти
-- реальную первую градуацию v2).
select date_trunc('day', block_time) as day, count(*) as n_graduations
from robinhood.logs
where contract_address = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e
    and topic0 = 0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259
    and block_time >= timestamp '2026-07-01 00:00:00'
    and block_time <  timestamp '2026-08-30 00:00:00'
group by 1
order by 1
