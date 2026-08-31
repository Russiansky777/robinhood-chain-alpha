-- 03c_cap_summary.sql (ревизия 3 -- см. docs/COST_POSTMORTEM.md)
-- ПЕРЕПИСАНО: версия с UNION ALL четырёх SELECT стоила 144.00 кредита
-- вместо оценённых 8 -- судя по всему, движок Dune пересчитывал всю
-- цепочку CTE (включая полное сканирование сырых свопов) ЗАНОВО в
-- каждой из 4 веток UNION, вместо однократного вычисления. Теперь --
-- ОДИН SELECT, ОДИН проход по `gated`, вся сводка по 4 комбинациям --
-- через condition aggregation (count/sum FILTER) в отдельных колонках
-- одной строки. Питон разворачивает эту одну широкую строку в 4-строчную
-- таблицу для отчёта -- см. analysis/sprint_1_5.py:build_cap_section.
--
-- Params: {{sniper_window_primary_minutes}}, {{sniper_window_sensitivity_minutes}},
--         {{cap_primary}}, {{cap_sensitivity}}, {{min_trades}}, {{min_unique_tokens}}

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
)

select
    (select sum(realized_pnl_usd) from wallet_agg) as total_network_pnl_usd,
    count(*) filter (where is_sniper_primary = 0) as n_gated_5,
    count(*) filter (where is_sniper_primary = 0 and trade_count > {{cap_primary}}) as n_cut_5_1500,
    coalesce(sum(realized_pnl_usd) filter (where is_sniper_primary = 0 and trade_count > {{cap_primary}}), 0.0) as cut_pnl_5_1500,
    count(*) filter (where is_sniper_primary = 0 and trade_count > {{cap_sensitivity}}) as n_cut_5_3000,
    coalesce(sum(realized_pnl_usd) filter (where is_sniper_primary = 0 and trade_count > {{cap_sensitivity}}), 0.0) as cut_pnl_5_3000,
    count(*) filter (where is_sniper_sensitivity = 0) as n_gated_1,
    count(*) filter (where is_sniper_sensitivity = 0 and trade_count > {{cap_primary}}) as n_cut_1_1500,
    coalesce(sum(realized_pnl_usd) filter (where is_sniper_sensitivity = 0 and trade_count > {{cap_primary}}), 0.0) as cut_pnl_1_1500,
    count(*) filter (where is_sniper_sensitivity = 0 and trade_count > {{cap_sensitivity}}) as n_cut_1_3000,
    coalesce(sum(realized_pnl_usd) filter (where is_sniper_sensitivity = 0 and trade_count > {{cap_sensitivity}}), 0.0) as cut_pnl_1_3000
from gated
