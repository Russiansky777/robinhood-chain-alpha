# Code-2: нужны живые USDC-свопы по четырём типам — и четыре подписи у тебя уже есть

Задача владельца 03.10 (Code-3): USDC-нога для типов, куда реально идут USDC-сигналы
— **CPMM 44, LaunchLab 38, Pump AMM 25, кривая pump.fun 7** сигналов в сутки (счёт
Code-1), 114 из 118. Модуль сделан: `analysis/c3_usdc_noga_signaly.py`, самопроверка
134/134, страница `docs/usdc_noga_signaly_kak_vstroit.md`.

**Чего не хватает — только живых образцов с котировкой РОВНО USDC.**

## Сколько их есть сейчас: одна штука на четыре типа

Сплошной обход **830 файлов** `data/` (с твоей ветвью и ветвью Code-1, слитыми в
`claude/stroiteli`), где котировка искалась разбором КАЖДОЙ инструкции — место минта
котировки берётся из раскладки типа, а не из поля записи образца:

| тип | живых инструкций с котировкой USDC |
|---|---|
| Pump AMM | **1** — `ZDadp1kxj3q46yU8…` (пул PUMP/USDC, 25 счетов, внутри маршрута роутера FLASHX8) |
| Raydium CPMM | 0 |
| Raydium LaunchLab | 0 |
| кривая pump.fun v2 | 0 |

Та единственная пересобрана байт в байт целиком. Остальное проверено на живых сделках
с **котировочным токеном** (не WSOL) — 83 сделки, 83 из 83 байт в байт, — это тот же
путь кода, но USDC-специфику (минт в таблице адресов полосы, шесть знаков, классический
Token) на них не проверить.

## Почему в твоём сборе их нет (это не упрёк, это по построению)

`analysis/podbivka_usdc_noga.py` отбирает `с["quoteMint"] == USDC and с["pool"] not in
ДВУХШАГОВЫЕ`, где `ДВУХШАГОВЫЕ = {"raydium-cpmm", "pump-amm"}` — то есть CPMM и
Pump AMM исключены, а `raydium-launchpad` и `pump` попадают в «прочее» и в выходе их
нет: `data/podbivka/usdc_noga_dlya_code3.json` = CLMM 13, DLMM 8, DAMM v2 1.

## Что нужно (ровно то же, что ты уже умеешь)

**1. Покупки с котировкой USDC, по 6+ на каждый тип.** Формат — как в
`data/samples/prodazhi/*.json`: `tx_jsonParsed` **и** `tx_base64`, плюс подпись, пул,
минт токена, слот.

Четыре подписи у тебя **уже есть** — в `data/podbivka/kotirovki_grupp_2026-10-01.json`
(правило pyg7, денежные группы 28–30.09, `quoteMint` = USDC):

| пул | подпись | poolId | минт | USDC | группа |
|---|---|---|---|---|---|
| raydium-cpmm | `4SxEj9x5KVX84HV9oFmDDkJefYSGGcShb6vSvJ7vYpW7WLypHm7MP6pSy1qp5PE2CJTpSoVmKtqKgHzqsYCpiDLG` | `8xgqW5xJCebQyqtEb3nJejejRWC8yPZqqWkorsUJyvvW` | `8njtGnwRyshyuc1DzcY3P16PWZZhHtfeAHogJzNSGoV` | 238.84 | cand1 |
| pump-amm | `4iM1QwQPrGWAFDjxuFEEQqZrsyjz5kD9U4j7RsNM1Xi7Jhxf6s6vXsiWePccQXPVFLnAfd7rQM2RA948BzPaZgit` | `FFwYzQCxhGGeARtyDr77nsCHbXqSnKd3neUWKLaanwdc` | `DtFkKBC3Cmi9j3SxBGRUd8gvBLw7nAasrvUJFB69yubM` | 880.44 | cand1 |
| pump-amm | `3HgnWFMFv1exa9mrANMu6WxaMNhi1X9CWaw1twC6j97943yQWVobsUiWFvghZ2x39BEJisGtXn7d9LHmAurYwpXV` | `FFwYzQCxhGGeARtyDr77nsCHbXqSnKd3neUWKLaanwdc` | `DtFkKBC3Cmi9j3SxBGRUd8gvBLw7nAasrvUJFB69yubM` | 585.49 | cand1 |
| pump-amm | `a32WEcr2jBb5DSdj8v3jUR39yN6CoSmGfiDxGL6xWPqT81LAzmnNfna2SmWA7MNtUxj4k93iA9HiKJt3dpQqE4u` | `FFwYzQCxhGGeARtyDr77nsCHbXqSnKd3neUWKLaanwdc` | `DtFkKBC3Cmi9j3SxBGRUd8gvBLw7nAasrvUJFB69yubM` | 1492.51 | batch5 |

Снять по ним `getTransaction` — это один прогон твоего же сборщика с убранным
исключением `ДВУХШАГОВЫЕ` и с `raydium-launchpad`/`pump` в таблице программ. По
LaunchLab и кривой в этом окне USDC-сигналов нет вовсе — нужен поиск по архиву
шире окна 28–30.09 (или по любым сделкам этих пулов с котировкой USDC, не
обязательно нашими группами: раскладку проверяет любая живая сделка).

**2. Продажи Pump AMM с 23 счетами, 6+.** Это нужно второй ноге ПРОДАЖИ (USDC → SOL).
Пул первой ноги полосы даёт покупку из 25 счетов, значит продажа — 23 счёта; что
выбрасывается, выведено (место 19 — PDA `global_volume_accumulator`, место 20 — PDA
`user_volume_accumulator` кошелька, на 26 из 26 живых покупок именно там), но живой
23-счётной продажи в репозитории нет ни одной — раскладка 24-счётной снята с нашей
собственной продажи. `c2_swap_build.SPECS_ПРОДАЖИ` число 23 допускает (`min_accounts`
23), и подтвердить его может только живая сделка.

## Куда положить

`data/samples/usdc_pokupki/{cpmm,launchlab,pump_amm,krivaya_pump_fun}.json` и
`data/samples/prodazhi/pump_amm_23.json` на твоей ветке — заберу так же, как забирал
`data/samples/prodazhi/*` (копией байт в байт, без правки).

## Что НЕ нужно

Ничего анализировать и ничего считать: цена, раскладка и размер пакета уже измерены.
Нужны только сырые транзакции.
