-- g1_schemas_like_robinhood_distinct.sql
-- Sprint G1, Шаг 1 (разведка): какие decoded-схемы вообще существуют
-- для chain id 4663 на Dune. Distinct/агрегированная форма (не
-- table_schema, table_name с LIMIT) -- иначе результат может быть
-- целиком поглощён одним контрактом с большой ABI (см. run #3
-- recon: accountable_v1_1_robinhood, 300+ таблиц, съел LIMIT 300 до
-- того как дошло до uniswap_v3_robinhood). Используется для поиска
-- pons.family-специфичной схемы (не нашлась -- см. docs/G1_DESIGN.md,
-- "Механика детекции").
select table_schema, count(*) as n_tables
from information_schema.tables
where table_schema like '%robinhood%'
group by 1
order by 1
