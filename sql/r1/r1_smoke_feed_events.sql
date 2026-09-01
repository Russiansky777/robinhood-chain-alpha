-- r1_smoke_feed_events.sql
-- Sprint R1, Шаг 2 (смоук): AnswerUpdated по 26 фидам за уикенд-окно.
-- chainlink_robinhood.dualaggregator_evt_answerupdated -- УЖЕ декодированная
-- Dune-таблица (current -- готовый int256, не сырой hex) -- не нужен
-- собственный декодер raw-логов (r1_common.decode_answer_updated
-- остаётся неиспользуемым для этого пути, был рассчитан на случай,
-- если бы пришлось читать сырые robinhood.logs).
select
    contract_address as feed_address,
    evt_block_time as block_time,
    current
from chainlink_robinhood.dualaggregator_evt_answerupdated
where contract_address in ({{feed_address_list}})
  and evt_block_time >= timestamp '{{window_start}}'
  and evt_block_time <  timestamp '{{window_end}}'
order by feed_address, evt_block_time
