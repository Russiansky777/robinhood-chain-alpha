-- 02_swaps_raw_july.sql
-- Все своп-транзакции Uniswap v3/v4 на Robinhood Chain, июль 2026.
-- Одна строка = один своп (одна "нога" сделки со стороны трейдера).
--
-- Источник: dune.dex.trades — унифицированная кросс-чейн таблица трейдов
-- (уже нормализует v3 и v4 в одну схему и считает amount_usd). См.
-- sql/00_notes.md п.1 про фолбэк на сырые декодированные логи, если
-- Robinhood Chain ещё не покрыт этой таблицей на момент запуска.
--
-- Params: {{start_date}} = '2026-07-01', {{end_date}} = '2026-08-01'
-- (полуоткрытый интервал [start, end) — используется как для июля, так
-- и для августа, см. sql/06_wallet_agg_august.sql)

select
    taker as wallet_address,          -- адрес трейдера (не роутер/агрегатор)
    tx_hash,
    block_number,
    block_time,
    project,                          -- 'uniswap'
    version,                          -- '2' / '3' / '4'
    project_contract_address as pool_address,
    token_bought_address,
    token_bought_symbol,
    token_bought_amount,
    token_sold_address,
    token_sold_symbol,
    token_sold_amount,
    amount_usd
from dex.trades
where blockchain = 'robinhood_chain'
    and project = 'uniswap'
    and version in ('3', '4')
    and block_time >= timestamp {{start_date}}
    and block_time <  timestamp {{end_date}}
    and amount_usd is not null         -- отбрасываем свопы без ценового оракула
                                        -- (низколиквидные/новые токены без
                                        -- надёжной USD-цены) -- см. ограничения
                                        -- в docs/README.md
