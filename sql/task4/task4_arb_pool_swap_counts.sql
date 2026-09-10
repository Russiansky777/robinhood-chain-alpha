-- task4_arb_pool_swap_counts.sql
-- Задача 4 владельца (2026-09-10), Шаг 1 (дешёвый, один день, одна
-- цепь -- "оценка стоимости на одной цепи за один день до полного"):
-- находим РЕАЛЬНЫХ кандидатов в "хвостовые" пулы -- считаем число
-- свопов и объём по КАЖДОМУ пулу (project_contract_address) на данной
-- цепи за один день. TVL-фильтр ($50k-$500k) применяется ОТДЕЛЬНО,
-- вне Dune, через GT (текущий снимок reserve_in_usd, бесплатно) --
-- здесь только реальная активность (число свопов), это единственное,
-- что реально нужно Dune для этого шага.
--
-- {{chain}} -- 'robinhood' | 'base' | 'arbitrum' (голая строка, ЭТО
-- поле blockchain -- VARCHAR по всем прежним запросам этого репозитория,
-- НЕ VARBINARY, в отличие от адресов).
-- {{day_start}}/{{day_end}} -- 'YYYY-MM-DD HH:MM:SS' UTC границы (один день).
--
-- having count(*) >= 10 -- честный низкий фильтр "не пусто и не шум
-- в 1-2 сделки", НЕ финальный порог >=50/сутки владельца (тот
-- применяется в Python после реального результата, чтобы не терять
-- пограничные пулы из-за жёсткого SQL-фильтра на später перепроверке).
select
    project_contract_address as pool_address,
    count(*) as n_swaps,
    sum(amount_usd) as volume_usd,
    min(block_time) as first_trade,
    max(block_time) as last_trade
from dex.trades
where blockchain = '{{chain}}'
  and block_time >= timestamp '{{day_start}}'
  and block_time <  timestamp '{{day_end}}'
  and amount_usd is not null
  and amount_usd > 0
  and project_contract_address is not null
group by 1
having count(*) >= 10
order by n_swaps desc
