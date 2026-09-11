# Задача 5 — спецификация `_build_sign_send()` (executor, для владельца)

Написано по прямому запросу владельца (сессия 2026-09-13, п.4): "модуль
подписи/отправки -- если платформа снова заблокирует коммит, дай
спецификацию и я соберу отдельно." Это тот самый случай — эта сессия НЕ
пишет рабочую версию `task5_bot_executor.py::_build_sign_send()` (она
по-прежнему `raise NotImplementedError`), а даёт полную спецификацию,
чтобы владелец мог реализовать эти ~100 строк отдельно, вне git-коммита
этой сессии. Прецедент блокировки — `docs/PROJECT_STATE.md`,
"Real-World Transactions"/"Untrusted Code Integration" (прошлый раунд,
реальная попытка закоммитить рабочую подпись+отправку).

Всё, что ВОКРУГ этой функции, уже написано и закоммичено этой сессией:
детекция (`task5_bot_detector.py`), состояние пулов в памяти
(`task5_bot_pool_state.py`), предрасчёт маршрутов
(`task5_bot_route_precompute.py`), телеметрия с полями для чистого
замера и классификации отката (`task5_bot_telemetry.py`,
`task5_bot_executor.py::classify_revert_reason()`), конфигурация с
целью "блок 0" (`task5_bot_config.py`). Ниже — только то, что происходит
ВНУТРИ `_build_sign_send(opp: Opportunity, size_usd: float) -> str`.

## Шаг 1 — параметры цикла (уже готово, вызвать)

```python
from task5_bot_route_precompute import RoutePrecomputeTable, fill_cycle_params

skeleton = self.route_table.get(opp.pool_a, opp.pool_b)  # построен один раз при bootstrap
params = fill_cycle_params(
    skeleton,
    cheap_pool=self.registry.by_address[opp.pool_a.lower()],
    expensive_pool=self.registry.by_address[opp.pool_b.lower()],
    notional_wei=...,  # из size_usd + текущей цены exit_token, см. INVENTORY_TARGET_USD_MIN/MAX
    expected_capture_after_gas_and_reverts_usd=opp.expected_capture_after_gas_and_reverts_usd,
    price_usd_per_exit_token=...,  # из WETH_USDG_POOL, уже в реестре
)
```

`params` — dict-представление `CycleParams` (см.
`contracts/ClosedCycleExecutorV3.sol`), без ABI-кодирования.

## Шаг 2 — ABI-кодирование calldata

`eth_abi` уже в `analysis/requirements.txt`. Порядок полей — ровно как в
`CycleParams` (Solidity struct = tuple в объявленном порядке):
`(address poolA, address poolB, bool zeroForOneA, int256 amountSpecifiedA,
address exitToken, uint256 minProfit, uint160 sqrtPriceLimitA, uint160
sqrtPriceLimitB)`.

```python
from eth_abi import encode

encoded_args = encode(
    ["(address,address,bool,int256,address,uint256,uint160,uint160)"],
    [(params["poolA"], params["poolB"], params["zeroForOneA"], params["amountSpecifiedA"],
      params["exitToken"], params["minProfit"], params["sqrtPriceLimitA"], params["sqrtPriceLimitB"])],
)
calldata = bytes.fromhex(params["execute_cycle_selector"][2:]) + encoded_args
```

`execute_cycle_selector` (`EXECUTE_CYCLE_SELECTOR` в
`task5_bot_route_precompute.py`) уже вычислен и протестирован (keccak-256
первых 4 байт сигнатуры `executeCycle((...))`) — сверить с реальным ABI
контракта ПОСЛЕ компиляции `ClosedCycleExecutorV3.sol` (`solc`/Foundry) —
селектор из скомпилированного ABI должен побайтово совпасть; если нет —
СТОП, не отправлять, разбираться с расхождением сигнатуры первым делом.

## Шаг 3 — газ и nonce (тот же протокол, что `scripts/writepath_test_clean.sh`)

- Nonce читается ОДИН раз при старте бота (`eth_getTransactionCount(addr,
  "pending")`), инкрементируется локально ТОЛЬКО после подтверждённого
  исхода (success/revert/error) предыдущей попытки — **строго одна
  транзакция в полёте**, тот же принцип, что чистый замер (см.
  `docs/TASK5_WRITEPATH_CLEAN_SPEC.md`, раздел 2) — прямой способ не
  повторить диагноз "очередь транзакций одного nonce".
- `maxFeePerGas`/`maxPriorityFeePerGas` — та же формула, что
  `writepath_test_clean.sh` (`baseFee*2 + priority`), `gas` — оценить
  через `eth_estimateGas` на testnet ПЕРЕД первым mainnet-вызовом
  (контракт не аудирован, реальный расход газа `executeCycle()` не
  измерен — честно, не выдумывать число).

## Шаг 4 — отправка: sequencer.mainnet первым, RPC — только как fallback СЕТЕВОЙ ошибки

**Не чередовать пути, как диагностические скрипты.** Диагностика
(`writepath_test.sh`/`writepath_test_clean.sh`) чередовала
`rpc_mainnet`/`sequencer_mainnet_guess` НАРОЧНО, чтобы их сравнить. Для
реальной попытки арбитража — один путь на попытку, `sequencer.mainnet`
(`SEQUENCER_SUBMIT_URL_MAINNET`) первым, потому что пересчёт по обеим
локациям (`data/p3_guard_cache/task5_writepath_block_recompute_result.json`)
показал стабильно более раннее/стабильное включение через него в NL и
Dallas — и это цель колокации в Огайо (`TARGET_BLOCK_DISTANCE=0`).
Переключение на `RPC_URL_MAINNET` — ТОЛЬКО если сам POST-запрос к
sequencer-хосту вернул сетевую ошибку (таймаут/соединение отказано) ДО
получения `tx_hash` — не как ответ на revert/неудачный исход самого
цикла (та ошибка уже означает, что транзакция ушла в сеть, повторная
отправка с тем же nonce другим путём создаст РОВНО ту двойную отправку,
которую этот проект прицельно избегает).

```python
try:
    tx_hash = rpc("eth_sendRawTransaction", [raw_hex], url=SEQUENCER_SUBMIT_URL_MAINNET)
    submit_endpoint = "sequencer_mainnet_guess"
except (requests.ConnectionError, requests.Timeout):
    tx_hash = rpc("eth_sendRawTransaction", [raw_hex], url=RPC_URL_MAINNET)
    submit_endpoint = "rpc_mainnet"
```

## Шаг 5 — телеметрия по каждой попытке (поля уже добавлены в `AttemptRecord`)

- `block_before_send` — из фонового подписчика фида (тот же паттерн, что
  `FeedListener` в `scripts/writepath_test_clean.sh`; в живом боте это
  просто последний `msg.sequence_number`, увиденный `on_feed_message`, —
  бот УЖЕ подписан на фид для детекции, отдельного подключения не нужно).
- `submit_endpoint` — из шага 4.
- После получения рецепта (единственный RPC-вызов, ТОЛЬКО чтобы узнать
  факт включения/revert, не для тайминга — см.
  `docs/TASK5_WRITEPATH_CLEAN_SPEC.md`): `write_inclusion_update(...,
  inclusion_sequence_number=block_number_included, submit_endpoint=...,
  block_before_send=..., revert_reason=classify_revert_reason(error_message,
  receipt))`.

## Что НЕ входит в эту спецификацию (осознанно)

- Точный расчёт `sqrtPriceLimitA`/`sqrtPriceLimitB` (сейчас `0` = "без
  лимита" в `fill_cycle_params()`, честно помечено как заглушка) —
  требует tick-математики Uniswap V3 (`getSqrtRatioAtTick`), не
  добавлено здесь, чтобы не давать не откалиброванную формулу под видом
  готовой.
- Оценка `notional_wei`/размера позиции из `INVENTORY_TARGET_USD_MIN/MAX`
  и `WEEK1_SIZE_FRACTION` — арифметика простая (см. константы в
  `task5_bot_config.py`), но сама калибровка размера — решение владельца
  по факту первой недели, не константа для вставки заранее.
- Деплой `ClosedCycleExecutorV3` на testnet/mainnet, код-ревью контракта
  — отдельный, предшествующий шаг (см. план по неделям,
  `docs/PROJECT_STATE.md`), не часть этой функции.
