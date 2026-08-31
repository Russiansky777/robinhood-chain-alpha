-- 03c_cap_summary.sql
-- Sprint 1.5, ревизия 2: агрегатная сводка для секции "Фильтр
-- копируемости" в RESULTS.md -- сколько кошельков срезано капом и какая
-- доля July PnL сети это, для каждой из 4 комбинаций sniper-окно×кап.
-- Выход -- 4 строки (по одной на комбинацию), не построчный список.
--
-- Params: те же, что в 03b_cohort_selection.sql (без cohort_seed/size).

with wallet_agg as (
    select * from query_03_wallet_agg_july
),

pools as (
    select * from query_01_pool_creation_blocks
),

swaps as (
    select * from query_02_swaps_raw_july
),

first_swap_per_wallet_pool as (
    select
        wallet_address, pool_address, min(block_time) as first_swap_time
    from swaps
    group by 1, 2
),

sniper_flags as (
    select
        f.wallet_address,
        max(case when f.first_swap_time <= p.pool_birth_time + interval '{{sniper_window_primary_minutes}}' minute then 1 else 0 end) as is_sniper_primary,
        max(case when f.first_swap_time <= p.pool_birth_time + interval '{{sniper_window_sensitivity_minutes}}' minute then 1 else 0 end) as is_sniper_sensitivity
    from first_swap_per_wallet_pool f
    join pools p on p.pool_address = f.pool_address
    group by 1
),

gated as (
    select
        w.wallet_address, w.trade_count, w.realized_pnl_usd,
        coalesce(s.is_sniper_primary, 0) as is_sniper_primary,
        coalesce(s.is_sniper_sensitivity, 0) as is_sniper_sensitivity
    from wallet_agg w
    left join sniper_flags s on s.wallet_address = w.wallet_address
    where w.trade_count >= {{min_trades}}
        and w.unique_tokens_traded >= {{min_unique_tokens}}
),

network as (
    select sum(realized_pnl_usd) as total_network_pnl_usd from wallet_agg
)

select 'sniper=5min,cap=1500' as combo,
    count(*) filter (where is_sniper_primary = 0) as n_gated,
    count(*) filter (where is_sniper_primary = 0 and trade_count > {{cap_primary}}) as n_cut,
    coalesce(sum(realized_pnl_usd) filter (where is_sniper_primary = 0 and trade_count > {{cap_primary}}), 0.0) as cut_pnl_usd,
    (select total_network_pnl_usd from network) as total_network_pnl_usd
from gated
union all
select 'sniper=5min,cap=3000',
    count(*) filter (where is_sniper_primary = 0),
    count(*) filter (where is_sniper_primary = 0 and trade_count > {{cap_sensitivity}}),
    coalesce(sum(realized_pnl_usd) filter (where is_sniper_primary = 0 and trade_count > {{cap_sensitivity}}), 0.0),
    (select total_network_pnl_usd from network)
from gated
union all
select 'sniper=1min,cap=1500',
    count(*) filter (where is_sniper_sensitivity = 0),
    count(*) filter (where is_sniper_sensitivity = 0 and trade_count > {{cap_primary}}),
    coalesce(sum(realized_pnl_usd) filter (where is_sniper_sensitivity = 0 and trade_count > {{cap_primary}}), 0.0),
    (select total_network_pnl_usd from network)
from gated
union all
select 'sniper=1min,cap=3000',
    count(*) filter (where is_sniper_sensitivity = 0),
    count(*) filter (where is_sniper_sensitivity = 0 and trade_count > {{cap_sensitivity}}),
    coalesce(sum(realized_pnl_usd) filter (where is_sniper_sensitivity = 0 and trade_count > {{cap_sensitivity}}), 0.0),
    (select total_network_pnl_usd from network)
from gated
