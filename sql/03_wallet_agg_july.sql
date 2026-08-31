-- 03_wallet_agg_july.sql
-- Агрегация по кошельку за июль 2026: реализованный PnL (weighted-average
-- cost basis), число сделок, число уникальных токенов, средний
-- холд-период (для токенов, которые были и куплены, и проданы в периоде).
--
-- Params: {{start_date}} = '2026-07-01', {{end_date}} = '2026-08-01',
--         {{base_token_symbols}} = 'WETH','ETH','USDC','USDC.e','USDT'
--         (см. sql/00_notes.md п.2 — заменить на матчинг по адресу перед
--         боевым прогоном)
--
-- Метод realized PnL: для каждой (wallet, non-base токен) пары считаем
-- бегущую среднюю себестоимость покупок (weighted-average cost, не
-- строгий FIFO) в USD, и на каждой продаже фиксируем
--   realized_pnl_leg = usd_получено_от_продажи - qty_продано * avg_cost_на_тот_момент
-- Сумма realized_pnl_leg по всем продажам токена за период = realized PnL
-- по токену. Открытые остатки (то, что куплено, но не продано в периоде)
-- в PnL не входят — это и есть "реализованный", а не бумажный PnL.

with base_tokens as (
    select symbol from unnest(array[{{base_token_symbols}}]) as t(symbol)
),

swaps as (
    select * from query_02_swaps_raw_july  -- ссылка на сохранённый запрос 02
),

-- Классифицируем каждый своп как BUY non-base токена (потратил base,
-- получил non-base) или SELL non-base токена (потратил non-base,
-- получил base). Свопы non-base <-> non-base (напр. акция <-> акция)
-- и base <-> base игнорируются для PnL-леджера (редки, но не 0 на DEX).
legs as (
    select
        wallet_address,
        tx_hash,
        block_time,
        token_bought_symbol,
        token_sold_symbol,
        case
            when token_sold_symbol in (select symbol from base_tokens)
                 and token_bought_symbol not in (select symbol from base_tokens)
                then 'BUY'
            when token_bought_symbol in (select symbol from base_tokens)
                 and token_sold_symbol not in (select symbol from base_tokens)
                then 'SELL'
            else null
        end as side,
        case
            when token_sold_symbol in (select symbol from base_tokens)
                then token_bought_address else token_sold_address
        end as traded_token_address,
        case
            when token_sold_symbol in (select symbol from base_tokens)
                then token_bought_symbol else token_sold_symbol
        end as traded_token_symbol,
        case
            when token_sold_symbol in (select symbol from base_tokens)
                then token_bought_amount else token_sold_amount
        end as traded_qty,
        amount_usd
    from swaps
),

priced_legs as (
    select *,
        -- бегущая сумма купленного объёма и потраченного USD ДО текущей
        -- сделки (используется для средней себестоимости на момент SELL)
        sum(case when side = 'BUY' then traded_qty else 0 end)
            over (partition by wallet_address, traded_token_address
                  order by block_time
                  rows between unbounded preceding and 1 preceding) as bought_qty_before,
        sum(case when side = 'BUY' then amount_usd else 0 end)
            over (partition by wallet_address, traded_token_address
                  order by block_time
                  rows between unbounded preceding and 1 preceding) as bought_usd_before
    from legs
    where side is not null
),

realized as (
    select
        wallet_address,
        traded_token_address,
        traded_token_symbol,
        block_time,
        side,
        traded_qty,
        amount_usd,
        case
            when side = 'SELL' and bought_qty_before > 0 then
                amount_usd - traded_qty * (bought_usd_before / bought_qty_before)
            else 0.0
        end as realized_pnl_usd
    from priced_legs
),

-- Средний холд-период: для токенов, у которых в периоде был и BUY, и
-- SELL — время между первой покупкой и последней продажей (proxy,
-- не honest per-lot holding time -- honest lot-matching holding time
-- потребовал бы того же FIFO/weighted-avg трекинга на уровне лотов,
-- избыточно для kill-теста Sprint 1).
hold_periods as (
    select
        wallet_address,
        traded_token_address,
        min(case when side = 'BUY' then block_time end) as first_buy,
        max(case when side = 'SELL' then block_time end) as last_sell
    from legs
    where side is not null
    group by 1, 2
)

select
    s.wallet_address,
    count(*) as trade_count,                              -- все свопы (не только BUY/SELL non-base ноги)
    count(distinct t.traded_token_address) as unique_tokens_traded,
    coalesce(sum(r.realized_pnl_usd), 0.0) as realized_pnl_usd,
    avg(date_diff('hour', h.first_buy, h.last_sell))
        filter (where h.first_buy is not null and h.last_sell is not null
                       and h.last_sell > h.first_buy) as avg_hold_period_hours
from swaps s
left join legs t
    on t.wallet_address = s.wallet_address and t.tx_hash = s.tx_hash
left join realized r
    on r.wallet_address = s.wallet_address and r.traded_token_address = t.traded_token_address
       and r.block_time = t.block_time
left join hold_periods h
    on h.wallet_address = s.wallet_address and h.traded_token_address = t.traded_token_address
group by s.wallet_address
