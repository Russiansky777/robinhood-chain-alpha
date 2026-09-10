-- task2_basis_hourly_vwap.sql
-- Задача 2 владельца (2026-09-10): часовой VWAP по реальным ПУЛАМ
-- (project_contract_address -- предикат по адресам пулов, как явно
-- попросил владелец, не только по адресу токена) на Robinhood chain,
-- для токен/перп базиса. Та же реально проверенная схема dex.trades,
-- что task1_weekend_windows.sql (VARBINARY-адреса, amount_usd, case
-- who-bought определяет сторону количества токена).
--
-- {{pool_address_list}} -- `from_hex('...')` (VARBINARY, project_contract_
-- address реальных пулов), через запятую.
-- {{token_address_list}} -- `from_hex('...')` (VARBINARY, адрес именно
-- СТОК-ТОКЕНА в каждом пуле -- не котируемого актива) -- та же роль,
-- что в task1_weekend_windows.sql: определяет, какая сторона сделки
-- (bought/sold) даёт qty стокового токена, а не котируемого (USDG/WETH).
-- {{trades_start}}/{{trades_end}} -- 'YYYY-MM-DD HH:MM:SS' UTC границы.
select
    project_contract_address as pool_address,
    date_trunc('hour', block_time) as hour_utc,
    sum(amount_usd) as vol_usd,
    sum(case when token_bought_address in ({{token_address_list}}) then token_bought_amount
             else token_sold_amount end) as token_qty
from dex.trades
where blockchain = 'robinhood'
  and project_contract_address in ({{pool_address_list}})
  and (token_bought_address in ({{token_address_list}}) or token_sold_address in ({{token_address_list}}))
  and block_time >= timestamp '{{trades_start}}'
  and block_time <  timestamp '{{trades_end}}'
  and amount_usd is not null
  and amount_usd > 0
group by 1, 2
order by 1, 2
