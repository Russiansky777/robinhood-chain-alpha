-- 06_wallet_agg_august.sql
-- То же самое, что 02+03 (сырые свопы -> агрегация по кошельку), но:
--   (a) период = август 2026 ({{start_date}}='2026-08-01', {{end_date}}='2026-09-01')
--   (b) ограничено списком адресов когорт А+Б из Гейта 3
--       ({{cohort_wallets}} -- подставляется в run_pipeline.py как готовый
--       список 'адрес1','адрес2',... из когорт А+Б, чтобы не гонять полную
--       августовскую агрегацию по всем кошелькам чейна -- экономия Dune
--       credits, см. docs/DATA_ACCESS.md)
--
-- Кошелёк из когорты, не совершивший ни одного свопа в августе,
-- НЕ появится в результате этого запроса -- analysis/cohort_builder.py
-- дозаполняет такие адреса realized_pnl_usd = 0 при сборке финальной
-- таблицы (см. docs/README.md, Гейт 4: "не выбрасывать 'умерших'
-- трейдеров").

with base_tokens as (
    select symbol from unnest(array[{{base_token_symbols}}]) as t(symbol)
),

swaps as (
    select
        taker as wallet_address,
        tx_hash,
        block_time,
        token_bought_address,
        token_bought_symbol,
        token_bought_amount,
        token_sold_address,
        token_sold_symbol,
        token_sold_amount,
        amount_usd
    from dex.trades
    where blockchain = 'robinhood'  -- см. sql/02_swaps_raw_july.sql
        and project = 'uniswap'
        and version in ('3', '4')
        and block_time >= timestamp {{start_date}}
        and block_time <  timestamp {{end_date}}
        and amount_usd is not null
        and taker in ({{cohort_wallets}})
),

legs as (
    select
        wallet_address, tx_hash, block_time,
        case
            when token_sold_symbol in (select symbol from base_tokens)
                 and token_bought_symbol not in (select symbol from base_tokens) then 'BUY'
            when token_bought_symbol in (select symbol from base_tokens)
                 and token_sold_symbol not in (select symbol from base_tokens) then 'SELL'
            else null
        end as side,
        case when token_sold_symbol in (select symbol from base_tokens)
             then token_bought_address else token_sold_address end as traded_token_address,
        case when token_sold_symbol in (select symbol from base_tokens)
             then token_bought_amount else token_sold_amount end as traded_qty,
        amount_usd
    from swaps
),

priced_legs as (
    select *,
        sum(case when side = 'BUY' then traded_qty else 0 end)
            over (partition by wallet_address, traded_token_address
                  order by block_time rows between unbounded preceding and 1 preceding) as bought_qty_before,
        sum(case when side = 'BUY' then amount_usd else 0 end)
            over (partition by wallet_address, traded_token_address
                  order by block_time rows between unbounded preceding and 1 preceding) as bought_usd_before
    from legs
    where side is not null
),

realized as (
    select
        wallet_address,
        case
            when side = 'SELL' and bought_qty_before > 0
                then amount_usd - traded_qty * (bought_usd_before / bought_qty_before)
            else 0.0
        end as realized_pnl_usd
    from priced_legs
)

select
    s.wallet_address,
    count(*) as trade_count_august,
    count(distinct l.traded_token_address) as unique_tokens_august,
    coalesce((select sum(r.realized_pnl_usd) from realized r where r.wallet_address = s.wallet_address), 0.0) as realized_pnl_usd_august
from swaps s
left join legs l on l.wallet_address = s.wallet_address and l.tx_hash = s.tx_hash
group by s.wallet_address
