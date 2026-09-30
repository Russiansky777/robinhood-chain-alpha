# Code-2: имена программ пулов из dex_labels.json — подсказка к опознанию, НЕ раскладка

Слово владельца 30.09: «dex_labels.json в репозитории даёт имена TesseraV / GatorSwap
и др. — передать Code-2 как подсказку к опознанию, не как раскладку».

**Файл:** `data/solana_buyer_200/prior/current/buyer_100/dex_labels.json`, 107 записей
вида `{адрес программы: имя}`. Он уже доставляется на хост прогоном деплоя детектора,
то есть лежит рядом с модулями.

## Чем это полезно и чем опасно

Полезно: у нас в отказах полосы адреса программ идут БЕЗ имени, и «тип пула вне полосы:
QUayE6ne…» читается как шум. С именем видно, чего именно мы не берём и стоит ли оно
строителя.

Опасно ровно одним: **имя не даёт раскладку счетов.** Ни порядка счетов, ни тега
инструкции, ни того, где вход и где выход, из имени не следует. Раскладку по-прежнему
выводить из живых сделок — как делалось для CLMM, Whirlpool, DBC и AMM v4. Имя — это
подсказка «на что смотреть», а не основание что-либо собирать.

## Кого мы уже знаем (для сверки словаря)

| адрес | имя в файле | у нас |
|---|---|---|
| `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | Pump.fun Amm | торгуем |
| `cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG` | Meteora DAMM v2 | торгуем |
| `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo` | Meteora DLMM | торгуем |
| `CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK` | Raydium CLMM | включён 30.09 |
| `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc` | Whirlpool | включён 30.09 |
| `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | Raydium | AMM v4, включён 30.09 |
| `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` | Dynamic Bonding Curve | под замком |

## Кого встретили в отказах ночью 29→30.09 — с именами

Эти адреса приходили в приёмках строителей как «тип пула вне полосы» или «это не тот
тип». Числа — из отчётов `data/priemka_stroitelya.json` тех прогонов, не из памяти.

| адрес | имя | где встретился |
|---|---|---|
| `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | **Raydium Launchlab** | 8 строк в прогоне Whirlpool 23:05Z |
| `BiSoNHVpsVZW2F7rx2eQ59yQwKxzU5NvBcmKshCSUypi` | **BisonFi** | 5 строк там же |
| `QUayE6nexQWYNZAEqfN8FxoNwQDSu3CAzT2qq9J1ArG` | **Quay** | 3 строки |
| `9H6tua7jkLhdm3w8BvgpTn5LZNU7g4ZynDmCiNN3q6Rp` | **HumidiFi** | 1 строка |
| `3TK9D8aoBFYjYZtKCjciPrVrRStsnvo7KmpcJqDavpaU` | **Kipseli** | 1 строка |
| `HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq` | **PancakeSwap** | 1 строка |
| `TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH` | **TesseraV** | 1 строка |
| `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | **Pump.fun** (кривая) | 1 строка |
| `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` | **Raydium CP** | 1 строка в прогоне AMM v4 00:53Z |
| `gatorLx9aC1e5ZWAXscv5QRKiLXnLPLXjftVc81h1Hr` | **GatorSwap** | в словаре есть, в отказах ночи не встретился |

## Что с этим делать (моё предложение, решает владелец)

1. В сборах Code-2 рядом с адресом программы класть **имя из этого словаря** — тогда
   «сколько сигналов у какого типа» читается сразу, и видно, какой тип стоит следующего
   строителя по деньгам, а не по алфавиту.
2. Имени в словаре может не быть: тогда писать адрес и **пустое имя**, а не выдумывать.
   Словарь снят раньше, новые программы в нём не появятся сами.
3. Раскладку счетов не выводить из имени НИКОГДА — только из живых сделок. Ровно на этом
   можно потерять деньги молча: у Whirlpool счета делятся на A/B, а не на вход/выход, и
   имя об этом не говорит ничего.
