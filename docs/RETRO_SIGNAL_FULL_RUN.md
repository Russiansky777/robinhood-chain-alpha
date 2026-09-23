# Ретро-сигнал: полный прогон после починки метода

Прогон `35778489729`, 2026-09-23T01:10:32Z, 301 мин, режим `full`.
Файл: `data/solana_retro_signal.json` (калибровка теперь в отдельном `data/solana_retro_signal_calibration.json`).

**Все прежние цифры ретро-сигнала недействительны** — метод изменён в трёх местах (вход, выход, состав кандидатов), поэтому сравнивать со старыми таблицами нельзя.

## Сводка

| сценарий | n | медиана, % | p25 | p75 | доля > +2.5% |
|---|---|---|---|---|---|
| E0 (слот лидера, только чужие покупки после него) | 443 | 3.99 | -4.96 | 19.46 | 53.9% |
| E0_s1 | 443 | 1.76 | -5.92 | 15.10 | 47.2% |
| E0_loose | 443 | 1.50 | -6.01 | 13.19 | 45.4% |
| E1 (S+1) | 443 | 1.23 | -6.74 | 12.44 | 44.9% |
| E2 (S+2) | 443 | 1.13 | -6.76 | 12.96 | 45.1% |

Симуляций всего 608, с посчитанным E2 до фильтров 602, в медианы попало 443. Отброшено пылевых (нога < 0.05 SOL): 131. Медленный выход (позже 45 с) исключён: 28 строк, их медиана E2 = 9.01%, медиана задержки 55 с.

Покупатель в слоте лидера после него был в 195 из 449 симуляций (43.43%); цена посчиталась в 168 (37.42%) — это и есть доля, где у E0 есть рыночный вход.

## Источник входа (главное для чтения таблицы)

| сценарий | n market | медиана market | n leader_price | медиана leader_price | доля leader_price |
|---|---|---|---|---|---|
| E0 | 168 | 0.69 | 275 | 5.26 | 62.1% |
| E1 | 330 | 0.98 | 113 | 1.94 | 25.5% |
| E2 | 305 | -0.31 | 138 | 3.24 | 31.1% |

Откат на цену лидера систематически даёт более высокий результат, чем реальный рыночный вход (E2: +3.24% против −0.31%). Это не открытие про рынок, а свойство отката: он берёт цену самого лидера, то есть вход без проскальзывания и без чужой очереди. Поэтому строку кошелька с высокой долей `leader_price` надо читать осторожнее, чем строку с долей 0.

Задержка выхода: n=445, мин=33, p25=33, медиана=33, p75=34, p95=39, макс=85 с.

## Строки без выхода

Статусы: ok — 602, `no_exit` — 4, `частично` — 2.

Из 4 `no_exit` двое упёрлись в потолок скана (420 блоков) и помечены `exit_incomplete` — это и есть **верхняя оценка обрыва по бюджету: 2 строки из 4**. Оставшиеся 2 просканировали 331 и 268 блоков и упёрлись не в потолок, а в отсутствие сделки — это честное «выхода не нашлось».

Поле `обрыв_по_бюджету` в файле пустое (`null`): прогон стартовал в 20:10, а `truncation_report` был закоммичен позже, и в этот прогон не попал. Цифра 2 посчитана вручную по полям `exit_scan_hit_cap` и `exit_incomplete` в `sims`, а не взята из отчёта.

## Самопроверка у двух провайдеров: НЕ выполнена

В файле стоит `self_check.ok = true`, и это ложный зелёный. Все 3 строки выборки получили `getBlock: бюджет времени прогона истёк` у обоих провайдеров, вернулись пустые словари, а сравнение `None` с `None` молча засчиталось как совпадение. Ни одной пары цен сверено не было.

Починено и пересверено по-настоящему (прогон `35805687394`, режим `--recheck`, 5 строк):
**22 пары цен сверено, расхождений 0**. Но честно про вклад: 20 пар дал публичный узел,
и только 2 — Helius; на 4 строках из 5 Helius отвечал `HTTP 429`. То есть это сверка
записанного (посчитанного через Helius в самом прогоне) против независимого публичного
узла — направление содержательное, но назвать её «у двух провайдеров» в полном смысле
нельзя. Добавил в вывод разбивку `сверено_пар_по_провайдерам` и оговорку, которая
выставляется автоматически, когда вклад дал только один узел.

Механика починки: `compare_prices()` считает сверенные пары, вердикт `совпало` становится `null` при нуле сверенных пар, `self_check.ok` становится `null` с явной запиской. Добавлен режим `--recheck` — пересверка по готовому файлу со своим коротким бюджетом, чтобы проверка не делила бюджет с расчётом.

## Кандидаты, прошедшие фильтр

Порог: медиана E2 ≥ 5.0%, n ≥ 4, толпа_2 ≥ 3.0, доля в плюс строго > 0.5. Кандидатов в отборе: 62.

| адрес | имя | толпа_2 | n | медиана E2 | доля в плюс | доля leader_price | задержка, с |
|---|---|---|---|---|---|---|---|
| `CFJr1M2z3SZpQpBMoY3nagpafvS7Uzanq4L7KVXVKi22` | Unicorn | 9.5 | 5 | 26.74 | 1.00 | 0.00 | 33 |
| `2E5rJKXz1n3bxn1ZnJvd77WC2SaeVxgDsa8SC33GYvyK` | nova6 | 3.5 | 7 | 16.32 | 0.71 | 0.43 | 34 |
| `BPjuxgARGFgBoMwJrMrhEmYat4TavCjW9wG9i3UZ1dbZ` | Value | 4.5 | 8 | 14.33 | 0.88 | 0.00 | 33.0 |
| `HTYho9ioTJcS4Dymi57HB65RBFu549gjyQ7kncfkknti` | BrazilZZ🇧🇷 | 4.0 | 7 | 13.88 | 0.86 | 0.14 | 34 |
| `CDNBnRyrXpBkh1R1iNpAHWn9BvCV4dHovMKBQsFusc8q` | Goosemanjones | 5.5 | 5 | 5.46 | 0.60 | 0.40 | 33 |

Отсев по каждому критерию поодиночке: не прошли E2 — 41, n — 13, толпу — 42, долю в плюс — 39.

## Полная таблица по всем кошелькам

| # | адрес | имя | статус | n | толпа_2 | E0 | E1 | E2 | доля>2.5% (E2) | доля в плюс E2 | доля leader_price | задержка выхода, с | окно |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit` | ПИЛОТ | в задаче | 1 | 12 | 134.41 | 271.91 | 271.91 | 1.00 | 1.00 | 1.00 | 34 | 72ч |
| 2 | `5opd5KBmodmoNuAThQ5cmXKbRxHbQDfomWGKAs3uEUP9` | soby | в задаче | 2 | 3.5 | 98.74 | 102.43 | 109.45 | 1.00 | 1.00 | 0.50 | 33.5 | 72ч |
| 3 | `DAejzMs5cUeCCENNvapy9KWFwzwegh7LvcgNkZ6hnf1y` | fomopumpguy | в задаче | 2 | 10.5 | 23.45 | 53.14 | 53.92 | 1.00 | 1.00 | 0.50 | 33.0 | 72ч |
| 4 | `HYh78tNpGBUcxoHgPU9v7VeP2wqSbkapuTzrfYHjfyLo` | CardinalSaint | в задаче | 2 | 2.0 | 47.19 | 49.65 | 49.65 | 1.00 | 1.00 | 0.50 | 34.0 | 72ч |
| 5 | `H3en1XWQHfbNWjEDRnRFi6HKVkZG1P7twdAHTvnCnYE3` | MaxHuh | в задаче | 6 | 6.5 | 30.41 | 33.05 | 32.67 | 0.67 | 0.67 | 0.00 | 33.0 | 72ч |
| 6 | `Et9jbvxvwKrKW5ZYJudU6HxLpHfCBVR4dR3yzWDqH1Zo` | Jarl Balgruuf | в задаче | 2 | 1.0 | 30.72 | 30.72 | 30.72 | 1.00 | 1.00 | 1.00 | 33.5 | 72ч |
| 7 | `CFJr1M2z3SZpQpBMoY3nagpafvS7Uzanq4L7KVXVKi22` | Unicorn | кандидат | 5 | 9.5 | 53.67 | 48.29 | 26.74 | 1.00 | 1.00 | 0.00 | 33 | 72ч |
| 8 | `9zZCjLr9xfXfp3qdqvPh6YeaEaagepz21khLhca69B18` | Pasterniq | в задаче | 1 | 4 | 35.69 | 25.62 | 25.62 | 1.00 | 1.00 | 0.00 | 33 | 72ч |
| 9 | `Cny7Brip3BgLYJi6MWn3oNw6NQvKQH25xSjFkKE2Ax2J` | vader007 | кандидат | 2 | 1.5 | 12.06 | 19.96 | 21.45 | 1.00 | 1.00 | 0.50 | 34.0 | расширенный |
| 10 | `Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB` | Brez | в задаче | 4 | 9 | 23.79 | 7.99 | 20.58 | 1.00 | 1.00 | 0.00 | 33.0 | 72ч |
| 11 | `Gh4YgP7RqtPZx87y88MB4bTWY1TJ2XMLp4itqUMHR83y` | ˚₊‧꒰ა yumie ໒꒱ ‧₊˚ | кандидат | 3 | 0 | 20.07 | 20.07 | 20.07 | 0.67 | 0.67 | 0.67 | 35 | расширенный |
| 12 | `2E5rJKXz1n3bxn1ZnJvd77WC2SaeVxgDsa8SC33GYvyK` | nova6 | кандидат | 7 | 3.5 | 11.98 | 6.03 | 16.32 | 0.71 | 0.71 | 0.43 | 34 | 72ч |
| 13 | `EC2f5DnHzuNRit1ExqghSifDbp1wgrzktsRRZCtU92MJ` | Theo | в задаче | 6 | 11 | 33.53 | 14.06 | 14.74 | 0.67 | 0.67 | 0.00 | 33.0 | 72ч |
| 14 | `BPjuxgARGFgBoMwJrMrhEmYat4TavCjW9wG9i3UZ1dbZ` | Value | кандидат | 8 | 4.5 | 22.68 | 17.78 | 14.33 | 0.88 | 0.88 | 0.00 | 33.0 | 72ч |
| 15 | `HTYho9ioTJcS4Dymi57HB65RBFu549gjyQ7kncfkknti` | BrazilZZ🇧🇷 | кандидат | 7 | 4.0 | 20.61 | 10.35 | 13.88 | 0.86 | 0.86 | 0.14 | 34 | 72ч |
| 16 | `7iPPqPyrqcmfenRs4xZ72ab4pyuUofXB5YaQB83WJmT9` | Wood | в задаче | 2 | 8.5 | 18.30 | 14.51 | 12.23 | 1.00 | 1.00 | 0.00 | 34.0 | 72ч |
| 17 | `Ak6gsstZwaRDYKnzdyNg2HvCXDFvv21afjVix9VGRMQv` | RugDalio | в задаче | 4 | 7.0 | 18.90 | 13.48 | 11.42 | 0.75 | 0.75 | 0.25 | 33.0 | 72ч |
| 18 | `HVfBxVKvGV56jGr4ormpkTuVp5nQtzNWBQqyKpVAhcq7` | topblaster | кандидат | 1 | 2 | 13.11 | 10.97 | 10.97 | 1.00 | 1.00 | 0.00 | 34 | расширенный |
| 19 | `F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv` | Omakase | в задаче | 5 | 3.5 | 16.88 | 10.45 | 10.45 | 0.80 | 0.80 | 0.20 | 33 | 72ч |
| 20 | `5RZPhPW9qGEd3hRGibgYaF5Yk2jBzXR1VZMKSP71C3Lb` | 333 | кандидат | 6 | 6.5 | 22.11 | 15.51 | 9.86 | 0.50 | 0.50 | 0.00 | 33.0 | 72ч |
| 21 | `CnEy6EVjK6bGMC9wxChibpkUQGNYPHWHttD2LAHJLggB` | MrMetavers3 | кандидат | 6 | 2.5 | 18.41 | 8.81 | 8.81 | 0.83 | 0.83 | 0.17 | 33.5 | 72ч |
| 22 | `FjC6L4aoBgqUpihguCWxcUgM7TCPj7Tvcr7iioSfkp6u` | PhoenixPlays | кандидат | 3 | 1.0 | 8.27 | 8.27 | 8.27 | 0.67 | 0.67 | 1.00 | 39 | расширенный |
| 23 | `GAsnqm4XkNkPVgrAofNQ65jWf8f3tKCLHhE9ZqSy2AP1` | Nach | в задаче | 4 | 9 | 9.75 | 2.59 | 8.27 | 0.75 | 0.75 | 0.00 | 33.0 | 72ч |
| 24 | `8GYbQxSrjEL1jMhNrUkbw2fVcn46uZEuVrJYyFoueAFe` | Tasso Lago | кандидат | 4 | 0.5 | 7.87 | 7.87 | 7.87 | 0.50 | 0.50 | 1.00 | 34.5 | расширенный |
| 25 | `CR8C7EkkYrxCSfAWV43pq82iMFyc7J794pyGN66qAjgb` | Fren0x | кандидат | 6 | 1.0 | 6.70 | 7.75 | 7.75 | 0.83 | 0.83 | 1.00 | 36.5 | расширенный |
| 26 | `78QUH7TPpzEVDts7jNf6Hjx3r3JQHF6qXaUxt3s74dcn` | smoothie | кандидат | 5 | 2 | 2.65 | 11.49 | 7.29 | 0.80 | 0.80 | 0.20 | 33 | расширенный |
| 27 | `2CYTJAU23a28o1Vitoi26qLqqF3UdnZ1CorWJBhTwLFc` | Ave | кандидат | 5 | 2.0 | 4.48 | 7.11 | 7.11 | 0.60 | 0.60 | 0.60 | 34 | расширенный |
| 28 | `7PFdKg9HgcjXh9fodFp6BCSGxqXcDSHAizotW6umE7aN` | L͓̽A͓̽D͓̽Y͓̽ ʟɛʍօռ 🍋 | кандидат | 6 | 2.0 | 8.02 | 4.56 | 6.83 | 0.67 | 0.67 | 0.33 | 34.0 | 72ч |
| 29 | `F5hkYsi8JxjyA2JHN5CA7MbnnhWubkXB2ZQB7Gkaxqs6` | Vee | в задаче | 1 | 12 | 22.44 | 1.72 | 6.82 | 1.00 | 1.00 | 0.00 | 33 | 72ч |
| 30 | `4XqYSrhf33K43SZxKmQ1DWRkdwDT4r4TuaV5yhUVA1jB` | gilly | кандидат | 5 | 0.0 | 13.65 | 6.25 | 6.25 | 0.60 | 0.60 | 0.40 | 33 | 72ч |
| 31 | `5sAQHDFzem7erKy5u3H6zTU8dbrYjfkEzhgn3dnek5qe` | FML | кандидат | 3 | 2.0 | 6.01 | -2.89 | 6.01 | 0.67 | 0.67 | 1.00 | 34 | расширенный |
| 32 | `woQZigHfLGHje85cjhXr5Y6uUiau2Q8p65N58EQj328` | heeshilio | кандидат | 4 | 2.0 | 8.05 | 5.93 | 5.93 | 0.50 | 0.50 | 0.75 | 34.0 | расширенный |
| 33 | `Xk9onqHkpULDEYYN9ZPyM7Q9AfTNYUrsCkzywyqdMeb` | N_Xk9o | в задаче | 8 | 3.0 | 24.54 | 8.78 | 5.79 | 0.50 | 0.50 | 0.12 | 34.5 | 72ч |
| 34 | `CDNBnRyrXpBkh1R1iNpAHWn9BvCV4dHovMKBQsFusc8q` | Goosemanjones | кандидат | 5 | 5.5 | 8.00 | 1.74 | 5.46 | 0.60 | 0.60 | 0.40 | 33 | 72ч |
| 35 | `8i3j6R4A4d7aNep9q6ZWsLhyNGU2YzU9GqgjrASxeLw9` | blur | кандидат | 3 | 3.5 | 10.39 | 6.59 | 5.38 | 1.00 | 1.00 | 0.00 | 33 | 72ч |
| 36 | `34F9PBzXrhBQMXwtKaXSuJyNWH75qFzgWLAoHKt2zmjH` | koosah | кандидат | 1 | 0.0 | 5.30 | 5.30 | 5.30 | 1.00 | 1.00 | 1.00 | 35 | расширенный |
| 37 | `4vER1GJQs73HtN9oYRswHZV4PSe2dvWQ8NLFoDhXeZjm` | Bitman | в задаче | 2 | 4.5 | 11.15 | 5.77 | 4.76 | 0.50 | 0.50 | 0.00 | 34.5 | 72ч |
| 38 | `k7vL5ZFYYrYH7uS5Am6GeFRCoKrmzDNFjyD7xwfTdMJ` | KOKO🫡 | кандидат | 8 | 2.0 | 8.83 | 8.83 | 4.00 | 0.50 | 0.50 | 0.75 | 33.0 | расширенный |
| 39 | `57aiHCweKvWmAspN26FVV6kKLY8opJGYgw5iN3nPESk9` | badtrader69 | в задаче | 5 | 2.0 | 3.79 | -2.64 | 3.79 | 0.60 | 0.60 | 0.40 | 35 | 72ч |
| 40 | `Ei1dmgYK1av31iuQHzpTx4e7vAg5nXxA9mE7x1Gr34Ao` | SoftMereElk | кандидат | 4 | 4.0 | 18.50 | 5.73 | 3.64 | 0.50 | 0.50 | 0.00 | 34.0 | 72ч |
| 41 | `FZ8ofuKKQbZ6QKRGYb75hD2Un56MKJzF6zErpcLn3Cma` | Dedrater | кандидат | 4 | 2.5 | 9.21 | 7.05 | 3.42 | 0.50 | 0.50 | 0.25 | 33.0 | 72ч |
| 42 | `BXLEfLN8bHsQCn81rFZebcavP1JSory1XbSDoeYQxJPc` | zakum | кандидат | 8 | 2.5 | 4.51 | 1.51 | 3.31 | 0.50 | 0.50 | 0.12 | 33.5 | 72ч |
| 43 | `6MwHc79vgWXeMVyZwgQ13bqqudRNCYkvjmgQzc294g8M` | Unmade | в задаче | 6 | 4 | 0.12 | -0.31 | 3.02 | 0.67 | 0.67 | 0.00 | 34.0 | 72ч |
| 44 | `DxFxvk9725aYNwgSGakh2p1S5UXFb5ozhbcoHAyEq1TF` | Spicy🌶️ | кандидат | 7 | 1.5 | 2.69 | 2.69 | 2.69 | 0.57 | 0.57 | 0.57 | 33 | 72ч |
| 45 | `7sQJttJLutWjHkxbusTgE4GpSj5z4fegouv2USHDFN2H` | eric_eth | в задаче | 1 | 9 | 4.06 | 4.03 | 2.64 | 1.00 | 1.00 | 0.00 | 35 | 72ч |
| 46 | `4XVFYAg8fHinxnNTjicpq2Xti5JGz5oJx3ZNbCvR9eTB` | 0xEly | кандидат | 4 | 1.0 | 5.89 | 2.42 | 2.42 | 0.50 | 0.75 | 0.75 | 33.0 | 72ч |
| 47 | `4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8` | jg | в задаче | 7 | 11.5 | 25.89 | 9.69 | 2.31 | 0.43 | 0.71 | 0.00 | 33 | 72ч |
| 48 | `5KVy7JCVrhJHXcDHmyUtWi2ri78KEhjfE3UZULyMjJE6` | Kohi | кандидат | 2 | 2.5 | 4.27 | 4.50 | 2.19 | 0.50 | 0.50 | 0.50 | 34.5 | расширенный |
| 49 | `5VRgqb2qbVqaWVGsM2k1b2bnPJk7up2xYbn4ziEjFgNt` | figaro | в задаче | 6 | 4 | 15.54 | 1.18 | 1.95 | 0.50 | 0.50 | 0.00 | 35.0 | 72ч |
| 50 | `9CNyLECt2j8tnDhqxtjYk5HUhZ2b8Nwnyb7sfYN7vND2` | rasmr | в задаче | 8 | 12.0 | 7.45 | -0.88 | 1.94 | 0.38 | 0.62 | 0.00 | 33.0 | 72ч |
| 51 | `5dB6rj9CoXMLQCAymoC5UXCb1LtFjbM5rbut3MNuj9Q` | Lasercat397 | в задаче | 1 | 9 | 1.55 | -0.69 | 1.79 | 0.00 | 1.00 | 0.00 | 33 | 72ч |
| 52 | `2QhcLuzPKGhnbaGCrC9XLXpfNS8uS1dWudrwM14A2s7N` | BonerMaxi | кандидат | 6 | 9.5 | -1.35 | 1.54 | 1.62 | 0.50 | 0.50 | 0.00 | 33.0 | 72ч |
| 53 | `ApUwcBwDnXN9Ttn8g7m9pnyDucSGpU8jknWfLxvX54J8` | derekz | кандидат | 5 | 2.5 | 0.64 | 0.64 | 0.76 | 0.20 | 0.60 | 0.00 | 34 | расширенный |
| 54 | `9QuHMjmYXxhqCPmNYnrjviD5SPNbFnXCt8SYDcZnhq8q` | trencHog | кандидат | 4 | 1.5 | 2.31 | 0.33 | 0.67 | 0.25 | 0.50 | 0.50 | 33.5 | 72ч |
| 55 | `5pHeNsWMVEi1cbMzLhgqABnhEUwRTSzy5vBfeGWyJfxS` | anon5pHe | в задаче | 6 | 7.0 | 20.09 | 1.53 | 0.44 | 0.33 | 0.50 | 0.17 | 33.0 | 72ч |
| 56 | `Gchh6bG7gqr14mjbKqUUA1qPSEdtHwYhUEwKrnSVeem6` | Joon | кандидат | 3 | 1.5 | 3.01 | -1.20 | 0.09 | 0.33 | 0.67 | 0.67 | 41 | расширенный |
| 57 | `DHnrkgve2u4nGAmzSXdWoZSib8ksFea2xxz7N4asSrvR` | Yeon | кандидат | 5 | 2 | 13.29 | 0.02 | 0.02 | 0.20 | 0.60 | 0.60 | 33 | 72ч |
| 58 | `DAz6dm7DvJtCy3H9XauujLtwBwzAG4cJtGdWNF6EYY42` | Otto | кандидат | 6 | 5 | 0.09 | -0.03 | -0.01 | 0.17 | 0.33 | 0.33 | 33.0 | 72ч |
| 59 | `7krCpC9dQgZVyRVv4W7okQHRdKD44ufEsnzgSAoYjLzZ` | 0x42 | кандидат | 4 | 0.0 | -0.79 | -1.43 | -0.16 | 0.25 | 0.50 | 0.75 | 33.5 | 72ч |
| 60 | `DyBKpVqM5GTXzd9w4V2xgHan5uurKbHzbUGduz2cxAha` | looc | кандидат | 5 | 0.0 | 0.04 | -0.70 | -0.70 | 0.20 | 0.40 | 0.40 | 33 | 72ч |
| 61 | `4KFjw2xfH4cXJJKjG1jDZRNphctZPMoFz3K6r3bAVtmD` | dreamloader | в задаче | 5 | 4.5 | -5.26 | -3.09 | -1.07 | 0.40 | 0.40 | 0.40 | 34 | 72ч |
| 62 | `HLLmfX1M39GuG5dZf4QEPknFZKtV1cWEwXR55PoyHPir` | Wizard Of SoHo (🍷,🍷) | кандидат | 8 | 4.5 | 3.89 | -0.46 | -1.24 | 0.38 | 0.50 | 0.62 | 34.0 | 72ч |
| 63 | `FWSPXhQAD57hXJBNsWRkYupwQ4grQrr75JiWM6CqzHva` | FullPinkYak | в задаче | 3 | 0 | 0.10 | -1.39 | -1.39 | 0.33 | 0.33 | 0.67 | 34 | 72ч |
| 64 | `G7PnVLZAviYw31LfQ54mmrUeMz6JzsX3b3Z9t7PPSDAo` | CateCatMfs | кандидат | 4 | 1.0 | -1.66 | -1.66 | -1.66 | 0.25 | 0.50 | 1.00 | 34.0 | расширенный |
| 65 | `BBcvxEyptYWSxgse7ESKWRxMRkpp9uE7wpCxUm6YqEhN` | 4EVER | в задаче | 2 | 2.5 | -0.10 | -1.86 | -1.72 | 0.00 | 0.50 | 0.00 | 33.5 | 72ч |
| 66 | `CzU8MaRcwvwUoNkwJFLbvtFWJugcEXAhDDQqNFE4ybb7` | Rowdy | в задаче | 6 | 10.0 | 28.03 | 13.63 | -1.94 | 0.33 | 0.50 | 0.00 | 33.0 | 72ч |
| 67 | `DYbZngFdHcaEEo4iLpjtAXKgXCECftbAhZeampomtCQc` | earn | в задаче | 4 | 2.5 | -3.52 | -2.96 | -2.02 | 0.25 | 0.25 | 0.25 | 33.5 | 72ч |
| 68 | `H6PEtpaDddtPEQjvGwEq5y7nF7WpNsTNy73m5h97oTye` | Carver | кандидат | 5 | 1.5 | -2.30 | -2.30 | -2.30 | 0.20 | 0.20 | 0.80 | 34 | 72ч |
| 69 | `rvg66dcUzSVwyu4k33Km6WHdrokwZ9Mc6BbfyLno2MD` | dave | кандидат | 3 | 2 | -2.99 | -2.99 | -2.99 | 0.33 | 0.33 | 1.00 | 35 | расширенный |
| 70 | `DZuhcnRgFNfxXhmKGJh6GRn8BkfzncMpiN5mBmSdpXxm` | mnysgud | кандидат | 6 | 3.0 | -3.05 | -4.38 | -3.05 | 0.33 | 0.50 | 0.33 | 33.0 | 72ч |
| 71 | `CbtBvpGhqX7kJ6eXkNWcy7ES9B7xK2HTfbpMqLojCRd5` | N_CbtB | в задаче | 2 | 2.5 | -2.04 | -4.97 | -3.28 | 0.50 | 0.50 | 0.50 | 33.5 | 72ч |
| 72 | `B9FJomxFnA5WauXkJdh4GBke3NAaGnC8nwEEW6sEGQ7y` | Lingard | кандидат | 5 | 2 | 2.20 | -3.44 | -3.44 | 0.20 | 0.40 | 0.40 | 33 | 72ч |
| 73 | `3PQgLybT4EaiePUPWG7S4mybFXQESUX18qQne7dhCDBD` | Mazino | кандидат | 4 | 1.0 | -5.97 | -9.56 | -3.75 | 0.00 | 0.25 | 0.50 | 33.0 | расширенный |
| 74 | `AMcHYqUj4HPUdkDkEatguugNSmKaQTdvxzexzqsXDU4r` | seb | в задаче | 5 | 2.0 | -16.21 | -14.29 | -4.04 | 0.20 | 0.40 | 0.20 | 34 | 72ч |
| 75 | `Gmywf4vuVYPvEtN1AjohuSrDsjcQGX4AbKDhdBHqASm1` | hungryghost | кандидат | 4 | 2 | -4.37 | -7.07 | -4.09 | 0.00 | 0.00 | 0.25 | 33.0 | 72ч |
| 76 | `Ev9AKa7WdY8D6kyCAsorBUyPoL8J5Rf84VBWfoS7UT9k` | wileEcoyote | кандидат | 6 | 2.0 | -0.59 | -4.98 | -4.23 | 0.17 | 0.17 | 0.50 | 34.0 | 72ч |
| 77 | `2bcvr5UJYHbMhM4LwgCqHYwDW5K6U39d79B9kJRBxxmZ` | TaobaoGCR | кандидат | 5 | 3.0 | -2.66 | -4.53 | -4.53 | 0.00 | 0.00 | 0.60 | 33 | 72ч |
| 78 | `QC9P3VEV5e4qei8XxXAeXCF5PwVa5y6z4EGx49n8zKD` | paidinfullintel | кандидат | 5 | 2.5 | -5.25 | -8.69 | -4.62 | 0.40 | 0.40 | 0.20 | 33 | 72ч |
| 79 | `Ekva8dVweVY5S3RV8MB11saFfeVcvpaxB5KmuSW3eecv` | Midcurver | кандидат | 6 | 5.5 | -4.69 | -5.02 | -5.16 | 0.00 | 0.33 | 0.00 | 33.0 | расширенный |
| 80 | `AgSELf8jkJYUfEpLNfUxpj3qf21tbLFdHsqvkHWjuYPC` | dtrains | кандидат | 3 | 1.0 | -5.20 | -5.20 | -5.20 | 0.00 | 0.33 | 0.67 | 35.5 | расширенный |
| 81 | `9mLaswxfkdjWcbfmotYRfBVSEfUddmbfebKEaNggfsSR` | N_9mLa | в задаче | 3 | 1 | -2.20 | -2.20 | -5.68 | 0.33 | 0.33 | 0.00 | 33 | 72ч |
| 82 | `HPZJPCpMKEY1mBfvLE7RrwcpiQDncLY575dBgc6S9sJ1` | spartee | кандидат | 2 | 2 | -6.50 | -8.93 | -6.33 | 0.50 | 0.50 | 0.50 | 33.0 | 72ч |
| 83 | `AUYLj5kLGUadmhn8kM91KnTJYfHiXGzcQ7TViP8uLLQv` | MrFernando | в задаче | 3 | 3 | -5.47 | -8.93 | -6.34 | 0.33 | 0.33 | 0.33 | 35 | 72ч |
| 84 | `ChKUC4WcY7JHPAJNE2yhcAtNwqxsejDjh4SfD6txaW4r` | BadGoodCoral | кандидат | 7 | 2.5 | -8.10 | -11.58 | -6.58 | 0.14 | 0.43 | 0.43 | 33 | 72ч |
| 85 | `DmopudSGaQQenK4cW5NgmHUsexMB3sEJNn6n1EfLKs28` | Werey | в задаче | 7 | 3.5 | 6.04 | 0.96 | -6.67 | 0.29 | 0.43 | 0.00 | 34 | 72ч |
| 86 | `4tWHFGC6iwzZm2VZaHZEiyvKMKjBArK424vMejgUUvTp` | insider | кандидат | 4 | 2.0 | -5.97 | -7.29 | -7.29 | 0.25 | 0.25 | 0.25 | 33 | 72ч |
| 87 | `27F9pmWwbEMmzBaj6gESiqxnVxAA6mo9pijFjiqjTgrx` | UBIK Maximalist | кандидат | 4 | 1 | -6.85 | -7.39 | -7.39 | 0.00 | 0.00 | 0.75 | 33.0 | 72ч |
| 88 | `ChgLZt4oJsgT1JEde8vmT2EcSJSqF38aB9PzYNtNDact` | xiaorenwu | в задаче | 3 | 3 | -6.29 | -9.37 | -7.39 | 0.00 | 0.33 | 0.33 | 33 | 72ч |
| 89 | `BMgsHTvcasRVtuevHJh8t6Vf5dmcWkDLAx6gSAQ3dsYm` | DipWheeler | в задаче | 7 | 8.5 | 18.55 | 6.74 | -7.43 | 0.43 | 0.43 | 0.00 | 33 | 72ч |
| 90 | `8sfbKCYFvKuzG6vE5dAyHrSVxEgRWgMrcjUEvfhYr4ng` | Koy | кандидат | 5 | 5 | 9.13 | -6.74 | -7.43 | 0.40 | 0.40 | 0.00 | 33 | 72ч |
| 91 | `9djgawmgpGrzt7DQoJ6tA2YW4gQyt3yH19uVZ3e2T3JJ` | retardedgains | в задаче | 4 | 2.0 | -0.54 | -7.57 | -7.57 | 0.25 | 0.50 | 0.25 | 33.5 | 72ч |
| 92 | `7UBjMaYsE3tKcanfgGrtso6aHAMhVNSVHkkX5j3t7f5y` | Tekkerrss | кандидат | 5 | 9.5 | 19.63 | 5.04 | -8.15 | 0.40 | 0.40 | 0.00 | 33 | 72ч |
| 93 | `DrXBn8eeRZXsc1h7qsj4V1W38at1sEynyZNC8mVrbuV5` | admiral | кандидат | 5 | 1.0 | -6.78 | 0.76 | -8.52 | 0.40 | 0.40 | 0.40 | 34 | 72ч |
| 94 | `7FPvbG2KAnSd5bpcdt5ZX638UhsvUnzafgmbLh3oEvND` | plottwist | кандидат | 5 | 7.5 | -8.78 | -8.95 | -10.12 | 0.20 | 0.20 | 0.20 | 33 | 72ч |
| 95 | `C7ajLwH7wywPEtpuTfp3QaV9QuYErZLShEPXaaaDrdxN` | solo6666 | кандидат | 5 | 4 | -9.08 | -10.65 | -10.65 | 0.20 | 0.20 | 0.20 | 34 | расширенный |
| 96 | `EURKJdQmbP2GqSBssKuhgLKz2XEwhUXnHL2oYd4eejMm` | N_EURK | в задаче | 1 | 5.0 | -8.92 | -11.12 | -11.68 | 0.00 | 0.00 | 0.00 | 33 | 72ч |
| 97 | `FWnbAT9w4GJRgWLd8oHPxQRjBStaKGSSCVdGYjG272y4` | Just Manny | кандидат | 2 | 0 | -6.48 | -10.80 | -11.73 | 0.00 | 0.00 | 0.00 | 34.5 | 72ч |
| 98 | `2L17Sw85Lh2Ca7ejEzWqyJsZ3yS4DxQhYr52X5H5xRns` | User | кандидат | 7 | 5.0 | 17.42 | 1.12 | -12.27 | 0.29 | 0.43 | 0.14 | 33 | 72ч |
| 99 | `6XpoayPqAE99XfCPARDig3CwU6Mg7pMdrTCCbqVidZMV` | lyx | кандидат | 4 | 1.0 | -10.96 | -17.10 | -13.28 | 0.25 | 0.25 | 0.50 | 33.0 | 72ч |
| 100 | `4rKj4Mpr3WD6GMCpbacfm4Kcj2r93XyjMqrekMFzh5DX` | bizz | кандидат | 4 | 4 | -13.38 | -25.67 | -19.83 | 0.00 | 0.00 | 0.00 | 33.0 | 72ч |
| 101 | `DPAghrNgkW4m5HMBL3kVsiCm9ywhrBtCutc6tSSvicx8` | buggyllama | в задаче | 2 | 1.0 | -19.33 | -21.05 | -20.43 | 0.00 | 0.00 | 0.50 | 34.0 | 72ч |
| 102 | `Bra5EH8vqm5bmGupdEHWcwNepetuvSuW2z4excQR5yNs` | ryan | в задаче | 1 | 1.0 | -25.42 | -28.86 | -28.86 | 0.00 | 0.00 | 0.00 | 34 | 72ч |
| 103 | `Hn5gVKAApv69t5HX7Q77uX7o5ayhEwArgYx7kukMLVGn` | gginvestments | в задаче | 0 | 1 | — | — | — | — | — | — | — | 72ч |
| 104 | `FF6vsaUcp4s95Q5NNm7VZ7deZU6gBU2vZmaiyQ36Ymrc` | CryptoChief | в задаче | 0 | 0 | — | — | — | — | — | — | — | 72ч |


## Оговорки

- Только чтение цепочки: ни одного вызова покупки/продажи.
- Окно выхода T+33..T+40с выбрано не на глаз: held_seconds наших 265 живых сделок дают медиану 35с, p25=33, p75=36, и 95.5% попадают в 30-45с.
- Секунды в названиях сценариев даны для слота 250 мс (так с 18.09). Для более ранних сделок слот был ~300 мс, то есть E1/E2 соответствуют ~0.30с/~0.60с, а не 0.25/0.50. Сами сценарии заданы В СЛОТАХ и от этого не зависят -- меняется только подпись в секундах.
- Цена берётся по дельтам балансов ТРЕЙДЕРА (подписанта), а не по состоянию пула: в неё входят проскальзывание и чаевые валидатору этого трейдера. У входа и выхода это разные люди, так что смещение частично гасится, но не исчезает -- это ограничение метода.
- Издержки не вычитаются: sim_* -- это уровень «сигнал», как в правиле владельца. Порог сравнения +2.5% -- издержки на 0.5 SOL.
- no_entry/no_exit и incomplete считаются и печатаются отдельно: недоступный блок нигде не превращается в тихий ноль.
