-- sc1_v1_fee_schema_probe.sql
-- Sprint SC1: владелец потребовал фактический fee-стек V1 из
-- исходников (contractsV1/src/PonsLaunchFactory.sol, GitHub) --
-- нашли: `poolFee` (uint24) -- КОНФИГУРИРУЕМЫЙ per-launch тир
-- Uniswap V3 (не единая константа, как в V2 0.7%), плюс `launchFee`
-- (плоский native-сбор в пользу protocolFeeRecipient). Проверяем: (а)
-- есть ли fee-тир прямо в dex.trades; (б) таблицы схемы
-- uniswap_v3_robinhood (могут содержать fee пула по адресу).
select table_schema, table_name, column_name, data_type, ordinal_position
from information_schema.columns
where table_schema = 'dex' and table_name = 'trades'
order by ordinal_position
