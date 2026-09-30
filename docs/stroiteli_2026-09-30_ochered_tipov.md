# Какие типы пулов ещё без строителя -- по частоте, из журналов (28--29.09 и шире)

Читал только: `data/dvuhshagovyy_v_zhurnale.json`, `data/journal_stats.json`,
`data/bloom_regression_txs.json` и прочие файлы `data/` этого репозитория, плюс `c2_swap_build.SPECS`.
Ничего не запускал и ничего не менял.

## 1. Узкий счёт: бочка «строителя нет» за 28--29.09

Файл `data/dvuhshagovyy_v_zhurnale.json`, окно `since_utc 2026-09-28T00:00:00Z` →
`generated_utc 2026-09-29T02:13:28Z`, раздел `без_второй_ноги.по_типу_пула_источника`. Всего в
бочке «строителя нет» **21** сигнал:

| программа пула | сигналов | строитель |
|---|---|---|
| `LBUZKhRx…` Meteora DLMM | 9 | есть (Code-1) |
| `CAMMCzo5…` Raydium CLMM | 5 | есть (Code-1) + мой `c2_clmm_stroitel` |
| `whirLbMi…` Orca Whirlpool | 3 | **не было** -- мой `c2_whirlpool_stroitel` |
| `gatorLx9aC1e5ZWAXscv5QRKiLXnLPLXjftVc81h1Hr` | 2 | **нет** |
| `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` Raydium LaunchLab | 1 | **есть** (в `SPECS`) |
| `TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH` | 1 | **нет** |

**«Прочие 4» -- это последние три строки:** четыре сигнала сверх DLMM, CLMM и Whirlpool.

Важная оговорка, без которой число читается неверно: в журнале «строителя нет» -- это формулировка
для **«тип пула вне полосы»** (`deploy/checks/dvuhshagovyy_v_zhurnale.py`, `БЕЗ_ВТОРОЙ_НОГИ`), то
есть отказ **гейта**, а не обязательно отсутствие кода. У Raydium LaunchLab строитель есть -- в
`c2_swap_build.SPECS` он записан как `LAUNCHLAB`; его сигнал в этой бочке означает выключенный гейт,
а не пустое место. Настоящих «строителя нет» среди прочих четырёх **два**: `gatorLx9…` (2 сигнала) и
`TessVdML…` (1 сигнал).

Четыре сигнала за сутки -- это слишком мало, чтобы на них выбирать следующий тип: разница между «2» и
«1» здесь шум. Поэтому второй счёт, шире.

## 2. Широкий счёт: пять суток теневого разбора

Файл `data/journal_stats.json`, `shadow.not_built`, окно с `2026-09-24T18:49`. Это разбор ВСЕХ
сделок источников: «сборщик построил бы» против «не построил бы и почему». Строки
«тип пула не покрыт сборщиком: …» -- **1194 случая на 29 программах**. Вот они целиком, с ответом
на вопрос «а есть ли у полосы строитель»:

| случаев | программа | строитель |
|---|---|---|
| 368 | `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc` Orca Whirlpool | **мой модуль** |
| 195 | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` Pump.fun кривая | есть (`SPECS`) |
| **167** | `ojh19ojaKduoJZuaJADhcVGp4xt1TcdAvZmpVsCorch` | **НЕТ** |
| 112 | `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` Meteora DBC | есть + мой модуль |
| **103** | `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` Raydium AMM v4 | **НЕТ** |
| **66** | `TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH` | **НЕТ** |
| 30 | `BSwp6bEBihVLdqJRKGgzjcGLHkcTuzmSo1TQkHepzH8p` | НЕТ |
| 24 | `9H6tua7jkLhdm3w8BvgpTn5LZNU7g4ZynDmCiNN3q6Rp` | НЕТ |
| 17 | `gatorLx9aC1e5ZWAXscv5QRKiLXnLPLXjftVc81h1Hr` | НЕТ |
| 16 | `MNFSTqtC93rEfYHB6hF82sKdZpUDFWkViLByLd1k1Ms` | НЕТ |
| 13 | `QUayE6nexQWYNZAEqfN8FxoNwQDSu3CAzT2qq9J1ArG` | НЕТ |
| 11 | `REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2` | НЕТ |
| 10 | `HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq` | НЕТ |
| 8 | `DNL1tgEj3nJovHw9jtyCCQD3arssCJzkmpDizknwzey4` | НЕТ |
| 8 | `QuaNtZsgYRe5Z9Bk4LZ4cTD9tbkVoyCNf1R2BN9bBDv` | НЕТ |
| 6 | `BiSoNHVpsVZW2F7rx2eQ59yQwKxzU5NvBcmKshCSUypi` | НЕТ |
| 6 | `goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE` | НЕТ |
| 5 | `riptK81hDxhe5pW5jSzSM9iRA8azgEgLJ4dXkPtBS7j` | НЕТ |
| 5 | `ZERor4xhbUycZ6gb9ntrhqscUcZmAbQDjEAtCf4hbZY` | НЕТ |
| 5 | `omnixgS8fnqHfCcTGKWj6JtKjzpJZ1Y5y9pyFkQDkYE` | НЕТ |
| 5 | `Archer8kgiavM61GyusMzaaS2ft5sALtNsD1HxkUPMhy` | НЕТ |
| 3 | `9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP` | НЕТ |
| 3 | `B72M6nyCLFgWiJtAN4naUTminMiTmyGcEqQHXwVeRdht` | НЕТ |
| 2 | `swapFpHZwjELNnjvThjajtiVmkz3yPQEHjLtka2fwHW` | НЕТ |
| 2 | `fUSioN9YKKSa3CUC2YUc4tPkHJ5Y6XW1yz8y6F7qWz9` | НЕТ |
| 1 | `pegVkBpfR9GFi5Jaa9YXAHACyM9CzCNvhc5bf8GWNyW` | НЕТ |
| 1 | `swapNyd8XiQwJ6ianp9snpu4brUqFxadzvHebnAXjJZ` | НЕТ |
| 1 | `FUTARELBfJfQ8RDGhg1wdhddq1odMAJUePHFuBYfUxKq` | НЕТ |
| 1 | `FLUXubRmkEi2q6K3Y9kBPg9248ggaZVsoSFhtJHSrm1X` | НЕТ |

Whirlpool здесь первый с большим отрывом -- **368 случаев, треть всей бочки**. Это и подтверждает
порядок вводной: он и был самым дорогим пробелом.

Две оговорки: (1) окно этого файла -- с 24.09, то есть шире, чем 28--29.09, и это его достоинство,
а не недостаток -- на сутках разницы в единицы сигналов не видно; (2) «не покрыт сборщиком» у
`6EF8rrec` и `dbcij3LW` означает гейт теневого сборщика, а не пустое место: их раскладка в `SPECS`
есть.

## 3. Следующий по частоте -- и что по нему запросить у Code-2

По обоим счётам порядок один и тот же. Следующие три, у которых строителя нет НИГДЕ:

1. **`ojh19ojaKduoJZuaJADhcVGp4xt1TcdAvZmpVsCorch` (Scorch) -- 167 случаев.**
2. **`675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` (Raydium AMM v4) -- 103.**
3. **`TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH` (TesseraV) -- 66.**

**СДЕЛАНО 30.09: строитель Raydium AMM v4** -- `analysis/c2_ammv4_stroitel.py`, подробно в
`docs/ammv4_kak_vstroit.md`. Взят вторым, а не первым, по указанию владельца: Scorch (`ojh19oja…`) и
прочие неопознанные не трогать до опознания Code-2, а у AMM v4 раскладка и цена восстанавливаются
по живым сделкам, которые уже есть.

**ИСПРАВЛЕНО (первая редакция этого раздела была неверна).** Я сперва написал, что имён этих программ
в репозитории нет. Это моя ошибка поиска: я искал только по `*.py` и `*.md` и ИСКЛЮЧИЛ из поиска
каталог `data/`, а карта меток лежит именно там --
`data/solana_buyer_200/prior/current/buyer_100/dex_labels.json`, 107 записей. По ней:

| адрес | метка в карте |
|---|---|
| `ojh19ojaKduoJZuaJADhcVGp4xt1TcdAvZmpVsCorch` | **Scorch** |
| `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | Raydium (AMM v4 -- так его зовёт и `bloom_detector.py:183`) |
| `TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH` | **TesseraV** |
| `BSwp6bEBihVLdqJRKGgzjcGLHkcTuzmSo1TQkHepzH8p` | **Bonkswap** |
| `9H6tua7jkLhdm3w8BvgpTn5LZNU7g4ZynDmCiNN3q6Rp` | **HumidiFi** |
| `gatorLx9aC1e5ZWAXscv5QRKiLXnLPLXjftVc81h1Hr` | **GatorSwap** |
| `MNFSTqtC93rEfYHB6hF82sKdZpUDFWkViLByLd1k1Ms` | **Manifest** |
| `QUayE6nexQWYNZAEqfN8FxoNwQDSu3CAzT2qq9J1ArG` | **Quay** |
| `REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2` | **Byreal** |

Оговорка, которая от этого не снимается: **метка на карте -- это не опознание раскладки.** Имя
говорит, чья это биржа, и ничего не говорит о тегах инструкций, числе счетов и математике цены; в
логах их сделок строки `Program log: Instruction: …` нет, и по открытым источникам по адресу поиск
ничего не дал. Указание владельца «неизвестные программы не трогать до опознания Code-2» остаётся в
силе: опознание -- это раскладка и цена, а не название.

Что про `ojh19oja…` известно фактом цепи (по одной живой сделке в `data/bloom_regression_txs.json`):

* единственная инструкция этой программы во всех данных репозитория: **8 счетов, 25 байт данных**,
  первые восемь байт `d067becfc51b7495` -- то есть дискриминатор Anchor, а 25 = 8 + 8 + 8 + 1 (два u64
  и байт). Что это именно своп, по одной инструкции не доказано -- косвенно на это указывает
  возвращаемое значение (ниже);
* зовётся ВНУТРЕННЕЙ инструкцией с глубины 3 и потребила **47 816 CU**;
* возвращает значение: `Program return: … EJvJewAAAAA=` -- это ровно 8 байт, u64 LE **2 076 810 000**,
  то есть программа отдаёт вызывающему размер выхода.

Восемь счетов -- это очень компактная раскладка (у Whirlpool 11 и 15, у DBC 15), и массивов тиков там
скорее всего нет вовсе. Но по одной сделке раскладку не строят: у меня это и есть узкое место.

**Запрос Code-2 через владельца (то же, что по CLMM/Whirlpool/DBC, слово в слово по образцу
`docs/podbivka_dlmm_kak_vstroit.md`), в порядке `ojh19oja…` → `675kPX9M…` → `TessVdML…`:**

1. **Сбор живых сделок** этой программы у источников полосы: транзакция целиком, разобранные роли
   счетов инструкции, версия программы на цепи, слот.
2. **Снимки состояния**: в слоте S сырые байты счёта пула (и конфигурации, если она отдельная), затем
   СЛЕДУЮЩАЯ сделка этого пула целой транзакцией -- чтобы перенос было чем сверить.
3. **Перенос математики цены** -- модуль `analysis/podbivka_<тип>_quote.py` того же вида, что
   `podbivka_clmm_quote.py` / `podbivka_whirlpool_quote.py` / `podbivka_dbc_quote.py`: `разобрать_пул`,
   (`разобрать_конфиг`, если есть), `котировка_точный_вход`, и сверка «до единицы» на этих сделках.
   Мои строители ищут порт ровно по этому имени и проверяют, что эти функции в нём есть.

У `675kPX9M…` (Raydium AMM v4) пункт 3 проще прочих: это постоянное произведение с комиссией, без
тиков и корзин, -- но раскладка счетов у него длинная (в ней есть счета рынка OpenBook), и брать её
надо с живых сделок, как и у остальных.

## 4. Чего я по этому пункту НЕ делал

Строителя ни для одной из этих программ не писал: вводная -- CLMM → Whirlpool → DBC, а следующий тип
берётся «тем же порядком», то есть после заготовки цены от Code-2. Прогонов Actions не запускал,
симуляций не делал, чужих веток не трогал.
