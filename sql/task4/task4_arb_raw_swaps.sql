-- task4_arb_raw_swaps.sql
-- Задача 4 владельца (2026-09-10), Шаг 2 (и оценка Шага 2 на малой
-- выборке): сырые свопы (для расчёта цены после каждого свопа и
-- определения, кто/когда закрывает расхождение >1%) по КОНКРЕТНОМУ
-- списку пулов (хвостовой + референс-пул той же пары) на одной цепи.
--
-- taker -- реальный столбец адреса трейдера (подтверждено живым
-- запросом information_schema.columns, run task4_arb_stage1 -- есть
-- также maker/tx_from/tx_to, taker выбран как стандартная роль
-- инициатора свопа в Spellbook dex.trades; сверяется на реальных
-- данных на этапе анализа -- честная, не 100% гарантированная догадка,
-- но не взятая с потолка).
--
-- {{chain}} -- голая строка ('robinhood'|'base'|'arbitrum').
-- {{pool_address_list}} -- `from_hex('...')` (VARBINARY project_contract_address), через запятую.
-- {{day_start}}/{{day_end}} -- 'YYYY-MM-DD HH:MM:SS' UTC границы.
select
    project_contract_address as pool_address,
    block_time,
    taker,
    amount_usd,
    token_bought_address,
    token_bought_amount,
    token_sold_address,
    token_sold_amount
from dex.trades
where blockchain = '{{chain}}'
  and project_contract_address in ({{pool_address_list}})
  and block_time >= timestamp '{{day_start}}'
  and block_time <  timestamp '{{day_end}}'
  and amount_usd is not null
order by project_contract_address, block_time
