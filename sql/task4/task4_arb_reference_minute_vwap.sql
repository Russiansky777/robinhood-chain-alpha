-- task4_arb_reference_minute_vwap.sql
-- Задача 4 владельца (2026-09-10), фикс реальной находки Шага 1b: пул
-- с максимальным TVL для пары (кандидат в "референс") оказался
-- ботовым/wash-trading -- 924 923 реальных строки за ОДИН день на
-- ОДНОМ пуле (~10 свопов/сек), credit_guard отказался платить за
-- чтение (expected_max_rows=50000 жёстко превышен). Для референс-цены
-- (нужна только чтобы понять, где "истинная" цена пары, не для
-- расчёта времени жизни расхождения -- то считается на ХВОСТОВОМ
-- пуле, где свопов на порядки меньше) построчная детализация не
-- нужна и экономически бессмысленна при такой частоте -- минутный
-- VWAP более чем достаточен для задачи "медленный арбитраж" (порог
-- значимости -- разрыв живёт >=10 секунд), и схлопывает ~925k строк
-- в максимум 1440 минутных бакетов.
--
-- {{pool_address_list}} -- `from_hex('...')`, обычно ОДИН референс-пул.
-- {{token_address_list}} -- адрес БАЗОВОГО токена пары (та же роль,
-- что в task2_basis_hourly_vwap.sql -- определяет сторону qty).
select
    project_contract_address as pool_address,
    date_trunc('minute', block_time) as minute_utc,
    sum(amount_usd) as vol_usd,
    sum(case when token_bought_address in ({{token_address_list}}) then token_bought_amount
             else token_sold_amount end) as token_qty
from dex.trades
where blockchain = '{{chain}}'
  and project_contract_address in ({{pool_address_list}})
  and block_time >= timestamp '{{day_start}}'
  and block_time <  timestamp '{{day_end}}'
  and amount_usd is not null
  and amount_usd > 0
group by 1, 2
order by 1, 2
