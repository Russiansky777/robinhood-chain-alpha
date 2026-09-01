-- g1_factory_logs_topic0_probe.sql
-- Sprint G1, "блокирующее условие 3" (2026-09-01): верификация адреса
-- фабрики pons.family И событие градуации ОДНИМ пробником -- сырые логи
-- (декодированной схемы для pons.family на Dune нет, см.
-- docs/G1_DESIGN.md) с contract_address из ДВУХ фабрик
-- (data/pons_family/SOURCE.md), окно 7 дней, группировка по topic0.
--
-- Если строк ноль -- адрес неверный / не тот чейн / не то поле
-- (contract_address vs "address"), это единственный случай, когда
-- пайплайн возвращается к владельцу без дальнейших шагов.
--
-- Ожидание (НЕ хардкодится в SQL, только для интерпретации результата):
-- topic0 = 0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a
-- должен соответствовать TokenLaunched (посчитан локально Keccak-256 от
-- точной сигнатуры типов из ABI, см. data/pons_family/SOURCE.md) --
-- сверяется ПОСЛЕ получения результата, не заранее.
select
    topic0,
    count(*) as n_logs,
    count(distinct tx_hash) as n_txs,
    min(block_time) as first_seen,
    max(block_time) as last_seen
from robinhood.logs
where contract_address in (
    0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB,
    0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e
)
    and block_time >= timestamp '2026-07-08 00:00:00'
    and block_time <  timestamp '2026-07-15 00:00:00'
group by 1
order by n_logs desc
