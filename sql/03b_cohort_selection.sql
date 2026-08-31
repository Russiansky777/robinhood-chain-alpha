-- 03b_cohort_selection.sql
-- Sprint 1.5, ревизия 2 (см. docs/COST_POSTMORTEM.md): чтение ПОЛНОГО
-- результата 03 через API стоило 163.98 кредита ЗА РАЗ (биллится по
-- объёму данных, не только execute() -- вторая, ранее невидимая дыра в
-- гарде). Решение: гейт 1 (снайперы, обе версии окна), гейт 2 (порог
-- активности), фильтр копируемости (капы) и отбор когорт (топ-200 +
-- псевдослучайные 200) — ВСЁ на стороне Dune, одним запросом. Наружу
-- через API едет только результат уже ПОСЛЕ отбора: строки кошельков,
-- попавших хоть в одну из 8 групп (когорта А/Б × 4 комбинации
-- sniper-окно×кап) -- на 2-3 порядка меньше полного результата 03.
--
-- Псевдослучайный контроль: вместо numpy random.seed(42) (потребовал бы
-- читать полный список в Python) -- детерминированный хэш
-- sha256(seed || wallet_address), ранжирование по нему. Не идентично
-- Sprint 1 побитово, но так же воспроизводимо и предрегистрируемо.
--
-- Params: {{sniper_window_primary_minutes}}=5, {{sniper_window_sensitivity_minutes}}=1,
--         {{cap_primary}}=1500, {{cap_sensitivity}}=3000,
--         {{min_trades}}=10, {{min_unique_tokens}}=5, {{cohort_size}}=200,
--         {{cohort_seed}}='sprint15-seed42' (строка, встраивается как литерал)

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
        wallet_address,
        pool_address,
        min(block_time) as first_swap_time
    from swaps
    group by 1, 2
),

sniper_flags as (
    -- Кошелёк -- снайпер в данном окне, если ХОТЯ БЫ ОДИН его первый
    -- своп в каком-либо пуле попал в первые N минут жизни этого пула.
    select
        f.wallet_address,
        max(case when f.first_swap_time <= p.pool_birth_time + interval '{{sniper_window_primary_minutes}}' minute then 1 else 0 end) as is_sniper_primary,
        max(case when f.first_swap_time <= p.pool_birth_time + interval '{{sniper_window_sensitivity_minutes}}' minute then 1 else 0 end) as is_sniper_sensitivity
    from first_swap_per_wallet_pool f
    join pools p on p.pool_address = f.pool_address
    group by 1
),

gated as (
    -- Гейт 2 (порог активности) -- baseline для ВСЕХ комбинаций.
    select
        w.wallet_address,
        w.trade_count,
        w.unique_tokens_traded,
        w.realized_pnl_usd,
        coalesce(s.is_sniper_primary, 0) as is_sniper_primary,
        coalesce(s.is_sniper_sensitivity, 0) as is_sniper_sensitivity
    from wallet_agg w
    left join sniper_flags s on s.wallet_address = w.wallet_address
    where w.trade_count >= {{min_trades}}
        and w.unique_tokens_traded >= {{min_unique_tokens}}
),

combo_eligible as (
    -- 4 комбинации sniper-окно x кап копируемости -- "прошёл гейты 1-2
    -- и не превысил кап" для каждой.
    select
        wallet_address, trade_count, unique_tokens_traded, realized_pnl_usd,
        case when is_sniper_primary = 0 and trade_count <= {{cap_primary}} then 1 else 0 end as eligible_5_1500,
        case when is_sniper_primary = 0 and trade_count <= {{cap_sensitivity}} then 1 else 0 end as eligible_5_3000,
        case when is_sniper_sensitivity = 0 and trade_count <= {{cap_primary}} then 1 else 0 end as eligible_1_1500,
        case when is_sniper_sensitivity = 0 and trade_count <= {{cap_sensitivity}} then 1 else 0 end as eligible_1_3000
    from gated
),

ranked as (
    select
        *,
        row_number() over (partition by eligible_5_1500 order by realized_pnl_usd desc) as rank_pnl_5_1500,
        row_number() over (partition by eligible_5_3000 order by realized_pnl_usd desc) as rank_pnl_5_3000,
        row_number() over (partition by eligible_1_1500 order by realized_pnl_usd desc) as rank_pnl_1_1500,
        row_number() over (partition by eligible_1_3000 order by realized_pnl_usd desc) as rank_pnl_1_3000,
        to_hex(sha256(to_utf8('{{cohort_seed}}') || wallet_address)) as rand_key
    from combo_eligible
),

cohort_a_flags as (
    select
        *,
        case when eligible_5_1500 = 1 and rank_pnl_5_1500 <= {{cohort_size}} then 1 else 0 end as cohort_a_5_1500,
        case when eligible_5_3000 = 1 and rank_pnl_5_3000 <= {{cohort_size}} then 1 else 0 end as cohort_a_5_3000,
        case when eligible_1_1500 = 1 and rank_pnl_1_1500 <= {{cohort_size}} then 1 else 0 end as cohort_a_1_1500,
        case when eligible_1_3000 = 1 and rank_pnl_1_3000 <= {{cohort_size}} then 1 else 0 end as cohort_a_1_3000
    from ranked
),

cohort_b_ranked as (
    -- "Случайный" пул -- элегибл, но НЕ в когорте А -- ранжирован по
    -- детерминированному хэшу, top-{{cohort_size}} = когорта Б.
    select
        *,
        row_number() over (
            partition by (case when eligible_5_1500 = 1 and cohort_a_5_1500 = 0 then 1 else 0 end)
            order by rand_key
        ) as rand_rank_5_1500,
        row_number() over (
            partition by (case when eligible_5_3000 = 1 and cohort_a_5_3000 = 0 then 1 else 0 end)
            order by rand_key
        ) as rand_rank_5_3000,
        row_number() over (
            partition by (case when eligible_1_1500 = 1 and cohort_a_1_1500 = 0 then 1 else 0 end)
            order by rand_key
        ) as rand_rank_1_1500,
        row_number() over (
            partition by (case when eligible_1_3000 = 1 and cohort_a_1_3000 = 0 then 1 else 0 end)
            order by rand_key
        ) as rand_rank_1_3000
    from cohort_a_flags
),

final as (
    select
        wallet_address, trade_count, unique_tokens_traded, realized_pnl_usd,
        cohort_a_5_1500,
        case when eligible_5_1500 = 1 and cohort_a_5_1500 = 0 and rand_rank_5_1500 <= {{cohort_size}} then 1 else 0 end as cohort_b_5_1500,
        cohort_a_5_3000,
        case when eligible_5_3000 = 1 and cohort_a_5_3000 = 0 and rand_rank_5_3000 <= {{cohort_size}} then 1 else 0 end as cohort_b_5_3000,
        cohort_a_1_1500,
        case when eligible_1_1500 = 1 and cohort_a_1_1500 = 0 and rand_rank_1_1500 <= {{cohort_size}} then 1 else 0 end as cohort_b_1_1500,
        cohort_a_1_3000,
        case when eligible_1_3000 = 1 and cohort_a_1_3000 = 0 and rand_rank_1_3000 <= {{cohort_size}} then 1 else 0 end as cohort_b_1_3000
    from cohort_b_ranked
)

select *
from final
where cohort_a_5_1500 = 1 or cohort_b_5_1500 = 1
   or cohort_a_5_3000 = 1 or cohort_b_5_3000 = 1
   or cohort_a_1_1500 = 1 or cohort_b_1_1500 = 1
   or cohort_a_1_3000 = 1 or cohort_b_1_3000 = 1
