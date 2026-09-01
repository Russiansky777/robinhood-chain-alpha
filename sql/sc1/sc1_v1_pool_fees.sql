-- sc1_v1_pool_fees.sql
-- Sprint SC1: фактический fee-тир пулов, созданных в окне V1-запусков
-- августа (01-12.08.2026 -- см. docs/SC1_NOTE.md). Локально уже
-- известно, что ВСЕ 39680 августовских V1-запусков используют один и
-- тот же launch_config_id=0/dex_id=0/dex_factory -- значит, вероятно,
-- один и тот же fee-тир; проверяем ВСЕ PoolCreated в окне, не выборку,
-- чтобы не гадать.
select
    pool,
    fee,
    tickspacing,
    evt_block_time
from uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated
where evt_block_time >= timestamp '2026-08-01 00:00:00'
  and evt_block_time <  timestamp '2026-08-13 00:00:00'
order by evt_block_time
