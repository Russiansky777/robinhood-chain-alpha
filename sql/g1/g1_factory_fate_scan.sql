-- g1_factory_fate_scan.sql
-- Sprint G1, владелец 2026-09-01: судьба фабрики после 2026-08-12
-- 19:42:33 (последний известный TokenLaunched с V1/V2). Свободная
-- проверка докс/репозитория pons (github.com/ponsdotdev/ponsfamily)
-- migration/changelog не нашла ничего -- узкий ончейн-скан: тот же
-- topic0 (TokenLaunched), БЕЗ фильтра по contract_address (любой адрес
-- на чейне), 3 дня после стопа, жёсткий LIMIT как вторая линия защиты
-- (см. analysis/dune_client.py, обязывающий гейт чтения -- LIMIT в SQL
-- плюс expected_max_rows в Python).
--
-- Если результат ПУСТ -- сигнатура TokenLaunched вообще не встречается
-- нигде на чейне после стопа (согласуется с гипотезой "конкурент
-- вытеснил pons.family", не "мы смотрим не туда"). Если есть строки с
-- ДРУГИМ contract_address -- кандидат на новую фабрику/миграцию.
select contract_address, count(*) as n_logs, min(block_time) as first_seen, max(block_time) as last_seen
from robinhood.logs
where topic0 = 0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a
    and block_time >= timestamp '2026-08-12 19:42:33'
    and block_time <  timestamp '2026-08-15 19:42:33'
group by 1
order by n_logs desc
limit 50
