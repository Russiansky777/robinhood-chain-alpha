-- r1_feed_decimals_description.sql
-- Sprint R1, Шаг 2: decoded CALL-трейсы decimals()/description() на
-- chainlink_robinhood.dualaggregator (найдены run #17, r1_calls_columns_
-- probe) -- решает и вопрос decimals (доки: "most USD feeds use 8", но
-- не факт для сток-фидов конкретно), и token<->feed сопоставление
-- (description() обычно "AAPL / USD") БЕЗ RPC (ALCHEMY_API_KEY не
-- настроен, run #14). Агрегируем по contract_address -- один и тот же
-- фид может быть вызван много раз в разных транзакциях, берём
-- distinct-значение (должно быть константным для decimals(), почти
-- всегда константным для description()).
select
    d.contract_address as feed_address,
    d.output_0 as decimals,
    de.output_0 as description,
    count(*) as n_calls
from chainlink_robinhood.dualaggregator_call_decimals d
left join chainlink_robinhood.dualaggregator_call_description de
  on de.contract_address = d.contract_address
 and de.call_success = true
where d.call_success = true
group by d.contract_address, d.output_0, de.output_0
order by feed_address
