-- 05_final_cohort_pool_july.sql
-- Гейт 2 + сборка финального пула кошельков-кандидатов на когорты А/Б.
-- Применяет: исключение снайперов/инсайдеров (Гейт 1) + порог по числу
-- сделок и уникальных токенов (Гейт 2).
--
-- Params: {{min_trades}} (default 10), {{min_unique_tokens}} (default 5)
--
-- Результат этого запроса — вход для analysis/cohort_builder.py, который
-- уже на стороне Python берёт top-N по PnL (когорта А) и случайные N из
-- остальных (когорта Б), см. docs/README.md "Гейт 3".

with agg as (
    select * from query_03_wallet_agg_july
),

excluded as (
    select wallet_address from query_04_sniper_insider_exclusions
)

select
    a.wallet_address,
    a.trade_count,
    a.unique_tokens_traded,
    a.realized_pnl_usd,
    a.avg_hold_period_hours,
    rank() over (order by a.realized_pnl_usd desc) as pnl_rank
from agg a
where a.wallet_address not in (select wallet_address from excluded)
    and a.trade_count >= {{min_trades}}
    and a.unique_tokens_traded >= {{min_unique_tokens}}
order by a.realized_pnl_usd desc
