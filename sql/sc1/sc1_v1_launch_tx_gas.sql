-- sc1_v1_launch_tx_gas.sql
-- Sprint SC1: gas_used + value (для проверки launchFee) по ВСЕМ
-- транзакциям в PonsLaunchFactory V1 (0xA5aAb3F0c6EeadF30Ef1D3Eb997108
-- E976351feB) за окно августовских V1-запусков (01-13.08.2026) --
-- каждая такая транзакция ДОЛЖНА быть launch() (проверяется по
-- count(*) ~= 39680). Также по этой же таблице -- funding-parent для
-- деплоеров (Шаг 2): первая входящая native-транзакция на адрес
-- деплоера, производится отдельным запросом (см. sc1_funding_parent.sql).
select
    hash as tx_hash,
    "from" as caller,
    "to" as factory_address,
    value,
    gas_used,
    gas_price,
    block_time
from robinhood.transactions
where "to" = 0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB
  and block_time >= timestamp '2026-08-01 00:00:00'
  and block_time <  timestamp '2026-08-13 00:00:00'
