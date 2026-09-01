-- sc1_funding_parent.sql
-- Sprint SC1, Шаг 2 (уровень 2): funding parent = отправитель первой
-- входящей native ETH-транзакции кошелька-деплоера (§ владелец).
-- Деплоеры выводятся ВНУТРИ этого же запроса (distinct "from" к
-- PonsLaunchFactory V1 в окне августовских запусков) -- без внешнего
-- IN-листа на 14545+ адресов. Только 2 колонки в выдаче (минимизация
-- стоимости чтения -- см. docs/SC1_NOTE.md, run #6/#7 урок).
with deployers as (
    select distinct "from" as deployer
    from robinhood.transactions
    where "to" = 0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB
      and success = true
      and block_time >= timestamp '2026-08-01 00:00:00'
      and block_time <  timestamp '2026-08-13 00:00:00'
)
select
    t."to" as deployer,
    min_by(t."from", t.block_time) as funding_parent
from robinhood.transactions t
join deployers d on d.deployer = t."to"
where cast(t.value as double) > 0
  and t.block_time >= timestamp '2026-07-01 00:00:00'
  and t.block_time <  timestamp '2026-08-13 00:00:00'
group by t."to"
