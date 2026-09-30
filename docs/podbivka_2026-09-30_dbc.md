# Meteora Dynamic Bonding Curve: сделки источников и сверка порта (сбор 29.09 22:16Z – 30.09 01:11Z)

Программа `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`, в сети развёрнута 2026-09-09 03:02Z (слот 445 503 633); порт
`analysis/podbivka_dbc_quote.py` -- с коммита f552f20 репозитория MeteoraAg/dynamic-bonding-curve от того же дня.
Данные: `data/podbivka/dbc_sbor.json` (сбор `analysis/podbivka_dbc_sbor.py`, Helius 12 524 запроса, ошибок 0),
сверка: `data/podbivka/dbc_proverka.json` (`analysis/podbivka_dbc_proverka.py`).

## 1. Сделки источников в DBC (28.09 06:00Z → 30.09)

34 кошелька источников (группы Code-1, кандидаты, снайперы). Сделок с инструкцией DBC -- **55** у 10 кошельков
(7JVQMwRj 25, 4KFjw2xf 16, остальные 1–3); 28.09 -- 15, 29.09 -- 40. Покупок (quote → base) 23, продаж 32. Пулов 11.

| что | число |
|---|---|
| вид: swap2 / swap2_with_transfer_hook / swap | 27 / 22 / 6 |
| swap_mode: 0 (ExactIn) / 1 (PartialFill) | 44 / 11 |
| внешняя инструкция / внутренняя (через агрегатор) | 25 / 30 |
| транзакции с таблицей адресов (ALT) | 37 из 55 |
| счетов в инструкции: 15 / 18 / 19 / 20 | 33 / 2 / 7 / 13 |
| событие EvtSwap2 (emit_cpi), длина 179 байт | 55 из 55 |
| с рефералом | 0 |

Котировочный токен пула не всегда SOL: 3kmygWKZ…pump 18, WSOL 16, USDC 13, C1mBfBoD… 7, CTgiaZUK… 1 инструкций.
Программа токена base: Token-2022 в 41, SPL Token в 14; quote: SPL Token 37, Token-2022 18.

### Раскладка инструкции (по исходникам: `instructions/swap/ix_swap.rs`, `ix_swap2_with_transfer_hook.rs`, `lib.rs`)

Аргументы: `swap` -- `amount_in u64, minimum_amount_out u64` (всегда ExactIn); `swap2` -- `amount_0 u64, amount_1 u64,
swap_mode u8` (0 ExactIn: amount_0 -- вход, amount_1 -- мин. выход; 1 PartialFill; 2 ExactOut);
`swap2_with_transfer_hook` -- то же + `TransferHookAccountsInfo` (borsh `Vec<{accounts_type u8, length u8}>`: 0 --
счета хука base, 1 -- хука base для реферала). Дискриминаторы -- Anchor `sha256("global:<имя>")[:8]`.

| # | счёт | запись | меняется от сделки к сделке (по 55 живым) |
|---|---|---|---|
| 0 | pool_authority | -- | нет: одно значение `FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM` у всех |
| 1 | config | -- | нет в пределах пула (поле `config` пула) |
| 2 | pool | запись | пул |
| 3 | input_token_account | запись | наш счёт входа |
| 4 | output_token_account | запись | наш счёт выхода |
| 5 | base_vault | запись | нет в пределах пула |
| 6 | quote_vault | запись | нет в пределах пула |
| 7 | base_mint | -- | нет |
| 8 | quote_mint | -- | нет |
| 9 | payer | подпись | наш кошелёк |
| 10 | token_base_program | -- | нет в пределах пула (Token или Token-2022) |
| 11 | token_quote_program | -- | нет в пределах пула |
| 12 | referral_token_account | запись | у всех 55 -- адрес самой программы (Anchor: «нет реферала») |
| 13 | event_authority | -- | нет: `8Ks12pbrD6PXxfty1hVQiE9sc289zgU1zHkvXhrSdriF` |
| 14 | program | -- | нет: сама программа (emit_cpi) |
| 15… | только у swap2_with_transfer_hook: счета transfer hook | -- | 3–5 счетов (в 22 из 22 таких) |

Ни один счёт, кроме наших и пула, от сделки к сделке по тому же пулу не менялся (0 пулов с разными значениями по любой
роли). Tick arrays и прочих «плавающих» счетов у DBC нет.

## 2. Сверка порта exact_in на живых свопах

Снимок: пул и его config одним `getMultipleAccounts` (слот S, confirmed), сделка -- первая успешная транзакция пула со
слотом > S; вход = `included_fee_input_amount` события, факт = `output_amount`. Отсев: не своп по пулу, PartialFill,
цена сдвинулась против направления (между чтением и сделкой было другое), несколько успешных транзакций пула в слоте.

**Сверено 8, до единицы 8 из 8** -- выход, `next_sqrt_price` и все три комиссии (trading / protocol / referral).
Отсев: PartialFill 2, не своп по пулу 2. Снимков всего 12: у 104 попыток за 60 с не было ни одной сделки пула (пулы
источников торгуются редко).

| пул | направление | вход | факт = модель | base_fee_mode | динамич. | activation_type |
|---|---|---|---|---|---|---|
| 6ErPdeUA… | base → quote | 5 857 286 861 533 725 | 206 597 195 | 0 | нет | 0 |
| 3z6ELHE1… | **quote → base** | 4 456 309 070 | 351 758 540 876 | 0 | нет | 1 |
| BkYnXoVk… | base → quote | 275 337 008 692 | 128 671 995 | 0 | нет | 1 |
| 3z6ELHE1… | base → quote | 8 128 923 772 712 | 96 727 318 978 | 0 | нет | 1 |
| BkYnXoVk… | base → quote | 532 538 076 472 | 238 727 144 | 0 | нет | 1 |
| BkYnXoVk… | base → quote | 3 318 042 793 354 | 1 480 265 091 | 0 | нет | 1 |
| BkYnXoVk… | base → quote | 276 505 773 799 | 120 059 796 | 0 | нет | 1 |
| 3z6ELHE1… | base → quote | 29 195 443 231 | 333 257 130 | 0 | нет | 1 |

**Чего сверка не покрыла:** покупка (quote → base) -- только 1 своп; базовая комиссия только режима 0 (линейный спад),
режимы 1 (экспоненциальный) и 2 (ограничитель частоты) и динамическая комиссия -- 0 свопов; collect_fee_mode только 0;
пулов 3; первый своп пула, реферал, дорастание до порога миграции -- 0.
