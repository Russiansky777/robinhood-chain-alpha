# Модуль Meteora DBC для Code-1: как встроить

> **Проверено на 8 живых свопах, 8 из 8 до единицы, но покупка (quote → base) -- только 1 из 8, базовая комиссия только
> режима 0, динамическая комиссия -- 0 свопов.** Пока нет сверки покупок: строитель на DBC отказывает или берёт широкий
> пол min_out. Признаки непроверенного случая в ответе/состоянии: `cfg["base_fee"]["base_fee_mode"] != 0`,
> `cfg["dynamic_fee"]["initialized"]`, `pool["has_swap"] == 0`.

**Сверка:** docs/podbivka_2026-09-30_dbc.md (сбор 29–30.09; выход, `next_sqrt_price` и комиссии -- до единицы).

**Файл:** `analysis/podbivka_dbc_quote.py` -- перенос на Python кода программы Meteora Dynamic Bonding Curve
`dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` из github.com/MeteoraAg/dynamic-bonding-curve (коммит f552f20; программа в
сети развёрнута 09.09.2026 03:02Z): `state/virtual_pool.rs` (get_swap_result_from_exact_input), `curve.rs`,
`state/config.rs` (комиссии), `base_fee/fee_scheduler.rs`, `base_fee/fee_rate_limiter.rs`, `state/fee.rs`.
Раскладки: VirtualPool 424 байта (8 дискриминатор + 416), PoolConfig 1 048 (8 + 1 040). Арифметика целочисленная.
Сетевых вызовов нет.

## Инструкция (по исходникам и 55 живым сделкам источников)

Своп exact_in -- `swap2` с `swap_mode = 0`: данные `sha256("global:swap2")[:8] | amount_0 u64 (вход) | amount_1 u64
(min_out) | swap_mode u8`. Если у пула transfer hook на base (Token-2022) -- обычный swap2 программа отвергнет
(`PoolTypeMismatch`), нужен `swap2_with_transfer_hook` + счета хука в остатке (у источников 3–5 счетов).

Счета (15): pool_authority (`FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM`, одна на программу), config (из пула), pool,
наш счёт входа, наш счёт выхода, base_vault, quote_vault (оба из пула), base_mint, quote_mint, payer, token_base_program,
token_quote_program (Token или Token-2022 -- по владельцу минта), referral_token_account (нет реферала -- адрес самой
программы), event_authority (`8Ks12pbrD6PXxfty1hVQiE9sc289zgU1zHkvXhrSdriF`), program.
**От сделки к сделке по одному пулу не меняется ничего, кроме наших счетов** (по 55 живым -- 0 исключений).

## Шаги в торговом пути

1. **Пул** -- счёт 2 инструкции DBC в транзакции источника (или `pool` в событии EvtSwap2).
2. **Чтение:** `getMultipleAccounts([пул, config])` -- одним запросом, один слот. `config` меняется только
   администратором -- можно держать в памяти по `pool["config"]`, тогда читается один пул.
   `pool = разобрать_пул(...)`, `cfg = разобрать_конфиг(...)`.
3. **Направление:** покупка токена = quote → base (`quote_to_base=True`). Котировочный токен у источников не всегда SOL
   (WSOL, USDC и токены-котировки встречаются) -- `cfg["quote_mint"]`.
4. **Котировка:** `q = котировка_точный_вход(pool, cfg, вход, True, current_point)`.
   - `current_point` -- слот (`cfg["activation_type"] == 0`) или unix-время (1) ожидаемой сделки: от него зависит
     базовая комиссия со спадом.
   - `q["output_amount"]` -- выход в сырых единицах; `q["fee_numerator"]` -- итоговая доля комиссии (из 1e9).
   - `ОшибкаDBC("PoolIsCompleted")` -- кривая дошла до порога миграции, торговать здесь нельзя.
   - `ОшибкаDBC("InsufficientLiquidity …")` -- вход упирается в `migration_sqrt_price`; ExactIn в сети в этом случае
     тоже откажет (частичное исполнение -- только PartialFill, не переносился).
5. **Налог Token-2022** и transfer hook -- снаружи модуля (как у DLMM и CLMM).
6. **min_out** = `q["output_amount"] × (1 − проскальзывание)`.

## Что не входит

- PartialFill (swap_mode 1 -- 11 из 55 инструкций источников) и ExactOut (2).
- «Первый своп с минимальной комиссией» -- только флагом `первый_своп_мин` (создание пула в той же транзакции модуль
  не видит).
- Сделки между чтением и нашей покупкой меняют состояние -- то же место в блоке, что и в остальной модели.
