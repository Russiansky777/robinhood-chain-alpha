-- sc1_eth_usd_price.sql
-- Sprint SC1: нужен ETH/USD курс для перевода launchFee+газ (в ETH) в
-- доллары для отчёта (владелец требует "$X"/"$Y"). Не гадаем по
-- памяти -- берём медианную имплицированную цену из САМИХ данных
-- dex.trades за то же окно (август 2026): сделки WETH/ETH против
-- стейблкоинов, amount_usd / кол-во ETH в сделке.
-- Правка: первый прогон упал в рантайме Dune ("value must be finite; was
-- NaN") -- деление на ноль/некорректный amount у части сделок. Фильтруем
-- знаменатель > 0 явно перед делением.
with eth_leg as (
    select
        amount_usd,
        case when token_bought_symbol in ('WETH','ETH') then token_bought_amount
             else token_sold_amount end as eth_amount
    from dex.trades
    where blockchain = 'robinhood'
      and ((token_bought_symbol in ('WETH','ETH') and token_sold_symbol in ('USDC','USDT','USDG','DAI'))
        or (token_sold_symbol in ('WETH','ETH') and token_bought_symbol in ('USDC','USDT','USDG','DAI')))
      and amount_usd is not null
      and block_time >= timestamp '2026-08-01 00:00:00'
      and block_time <  timestamp '2026-08-13 00:00:00'
)
select
    approx_percentile(amount_usd / eth_amount, 0.5) as eth_usd_price_median,
    count(*) as n_trades
from eth_leg
where eth_amount > 0
  and amount_usd > 0
