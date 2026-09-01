-- sc1_v1_launchfee_exact.sql
-- Sprint SC1: исходники подтвердили (GitHub, PonsLaunchFactory.sol,
-- _payLaunchFee) -- launchFee ФИКСИРОВАННАЯ константа (uint256 public
-- launchFee, меняется только владельцем через setLaunchFee), уходит
-- НЕВОЗВРАТНО в protocolFeeRecipient (казна протокола), ОТДЕЛЬНО от
-- initialBuyAmount = msg.value - launchFee (опциональный seed buy,
-- инвестиция создателя, НЕ издержка). Ищем точное значение --
-- модальное (самое частое) value среди ненулевых транзакций: если
-- многие создатели НЕ делали seed buy, launchFee будет доминирующим
-- значением.
select
    approx_most_frequent(5, cast(value as bigint), 1000) as top5_values,
    min(cast(value as double)) filter (where cast(value as double) > 0) as value_min_nonzero,
    count(*) filter (where cast(value as double) = 0) as n_zero_value
from robinhood.transactions
where "to" = 0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB
  and success = true
  and block_time >= timestamp '2026-08-01 00:00:00'
  and block_time <  timestamp '2026-08-13 00:00:00'
