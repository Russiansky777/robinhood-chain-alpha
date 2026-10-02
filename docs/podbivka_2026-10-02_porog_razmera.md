# Подбивка: Правило 14 -- порог размера покупки источника = билет группы

Числа ниже -- **в клетке: n / среднее, п.п. / медиана, п.п. / доля в плюсе**; где n < 20, стоит только `n=… (мало)` -- средние по такому числу сделок не считаются (слово владельца). Выход -- главный +108 слотов, вход «конец слота» за источником, билет -- билет группы (`lane_size` политики Code-1), п.п. чистыми (0.002 SOL на круг), Pump AMM по модели v6, котировка WSOL. Билет группы -- живой на 02.10 (`lane_size` политики Code-1): lane_s0 0.5 (переставлен 02.10 02:05Z), batch5 0.3, cand1 0.1, cand1_03 0.3, cand1_05 0.5, cand2 0.1; те же корзины на двух других билетах лежат в `data/podbivka/porog_razmera.json`. Корзины -- по размеру покупки источника в SOL-экв. Ничего не рекомендуется, решение владельца.

## 1. Отбор cand1 и cand2 считался только по покупкам источника ≥ 2 SOL

**Только по ним.** В архиве сигнал пишется лишь когда `sol >= порог_t` (`analysis/podbivka_arhiv_den.py:586`, порог собирается строкой выше: `порог_t = porog_доп if (t in ист and porog_доп is not None) else porog`, `analysis/podbivka_arhiv_den.py:574`), а во всех прогонах отбора порог стоял `--porog 2` без `--porog-dop`: `data/podbivka/zapusk/zadacha_pyg10.json` (cand1), `data/podbivka/zapusk/zadacha_cand2.json` и `miami_cand2.json` (cand2), `zadacha_vne11.json` (вне выборки). Поэтому n, среднее и медиана при нашем входе в отборе -- это **только первые покупки от 2 SOL-экв**; покупки меньше 2 SOL в счёт не входили вовсе. Правило «первой покупки» при этом от размера не зависит: окно докупки 1800 слотов обновляется на КАЖДОЙ покупке пары адрес+минт, до проверки порога (`analysis/podbivka_arhiv_den.py:576-584`), так что мелкая покупка и раньше «съедала» право крупной считаться первой.

## 2. Результат при нашем входе по корзинам размера покупки источника

Проход архива: 7 суток (2026-09-24, 2026-09-25, 2026-09-26, 2026-09-27, 2026-09-28, 2026-09-29, 2026-09-30), сигналов в окне 7136 (порог сигнала 0.01 SOL вместо 2), дублей 148, ошибок чтения часов 0. Окно -- 24.09 17:00Z → 01.10 17:00Z, то же, на котором шёл отбор cand1 / cand2.

### Первой строкой -- cand1_03

| адрес | группа | билет | < 0.1 SOL-экв | 0.1–0.3 SOL-экв | 0.3–1 SOL-экв | 1–2 SOL-экв | ≥ 2 SOL-экв | сигналов |
|---|---|---|---|---|---|---|---|---|
| `6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy` | cand1_03 | 0.3 | n=15 (мало) | n=4 (мало) | n=4 (мало) | 47 / +7.60 / +0.04 / 51% | 451 / +6.82 / +3.54 / 58% | 566 |
| `DYAn4XpAkN5mhiXkRB7dGq4Jadnx6XYgu8L5b3WGhbrt` | cand1_03 | 0.3 | n=18 (мало) | n=3 (мало) | — | n=15 (мало) | 54 / +9.56 / +3.93 / 65% | 122 |

### По группам (сумма адресов группы)

| адрес | группа | билет | < 0.1 SOL-экв | 0.1–0.3 SOL-экв | 0.3–1 SOL-экв | 1–2 SOL-экв | ≥ 2 SOL-экв | сигналов |
|---|---|---|---|---|---|---|---|---|
| **cand1_03** (2) | — | 0.3 | 33 / -2.47 / -1.34 / 46% | n=7 (мало) | n=4 (мало) | 62 / +7.53 / -0.31 / 48% | 505 / +7.11 / +3.58 / 59% | 688 |
| **lane_s0** (10) | — | 0.5 | n=1 (мало) | n=3 (мало) | 98 / -1.38 / -1.75 / 44% | 1354 / +2.40 / -0.16 / 50% | 205 / +11.48 / +1.59 / 58% | 2549 |
| **cand1** (5) | — | 0.1 | 34 / -0.85 / -3.74 / 21% | 33 / -2.32 / -2.58 / 15% | 87 / +4.03 / -1.73 / 42% | 174 / +2.28 / -2.66 / 36% | 593 / +3.04 / -2.29 / 44% | 1249 |
| **cand2** (12) | — | 0.1 | n=13 (мало) | n=19 (мало) | 77 / +8.34 / +0.69 / 51% | 264 / -0.16 / -3.20 / 44% | 944 / +5.15 / +1.72 / 55% | 1878 |
| **cand1_05** (2) | — | 0.5 | n=12 (мало) | n=1 (мало) | n=13 (мало) | 112 / +3.78 / -0.02 / 50% | 185 / +4.73 / -1.68 / 45% | 530 |
| **batch5** (12) | — | 0.3 | n=3 (мало) | n=2 (мало) | 24 / +2.09 / -1.09 / 38% | n=3 (мало) | 102 / +10.18 / +0.05 / 50% | 242 |

### cand1_03 (2 адресов, билет 0.3)

| адрес | группа | билет | < 0.1 SOL-экв | 0.1–0.3 SOL-экв | 0.3–1 SOL-экв | 1–2 SOL-экв | ≥ 2 SOL-экв | сигналов |
|---|---|---|---|---|---|---|---|---|
| `6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy` | cand1_03 | 0.3 | n=15 (мало) | n=4 (мало) | n=4 (мало) | 47 / +7.60 / +0.04 / 51% | 451 / +6.82 / +3.54 / 58% | 566 |
| `DYAn4XpAkN5mhiXkRB7dGq4Jadnx6XYgu8L5b3WGhbrt` | cand1_03 | 0.3 | n=18 (мало) | n=3 (мало) | — | n=15 (мало) | 54 / +9.56 / +3.93 / 65% | 122 |

### lane_s0 (10 адресов, билет 0.5)

| адрес | группа | билет | < 0.1 SOL-экв | 0.1–0.3 SOL-экв | 0.3–1 SOL-экв | 1–2 SOL-экв | ≥ 2 SOL-экв | сигналов |
|---|---|---|---|---|---|---|---|---|
| `7JVQMwRj82STgsG57spj6vpE6XY3RqG8B64PczVc7jJr` | lane_s0 | 0.5 | — | — | 80 / -2.05 / -2.32 / 48% | 1311 / +2.36 / -0.13 / 50% | n=8 (мало) | 2189 |
| `3Um4qsYQKYULYSJwRChReZtgsXu3kiGm6HNTvZpy9dYy` | lane_s0 | 0.5 | — | — | n=3 (мало) | n=5 (мало) | 56 / +9.33 / +3.25 / 55% | 116 |
| `5pHeNsWMVEi1cbMzLhgqABnhEUwRTSzy5vBfeGWyJfxS` | lane_s0 | 0.5 | — | n=3 (мало) | n=5 (мало) | n=3 (мало) | 50 / -1.05 / -1.26 / 36% | 73 |
| `B8m6fDRcw9VnkqWBXo5MEh9uY8HSPCkFuH4NNv5pCiPk` | lane_s0 | 0.5 | — | — | n=8 (мало) | 34 / +4.30 / -1.01 / 44% | n=13 (мало) | 63 |
| `Xk9onqHkpULDEYYN9ZPyM7Q9AfTNYUrsCkzywyqdMeb` | lane_s0 | 0.5 | — | — | — | — | 32 / +12.28 / +5.72 / 72% | 43 |
| `498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ` | lane_s0 | 0.5 | — | — | — | — | n=12 (мало) | 18 |
| `Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB` | lane_s0 | 0.5 | — | — | — | — | n=16 (мало) | 18 |
| `4vER1GJQs73HtN9oYRswHZV4PSe2dvWQ8NLFoDhXeZjm` | lane_s0 | 0.5 | n=1 (мало) | — | n=2 (мало) | n=1 (мало) | n=7 (мало) | 18 |
| `8RCEq8RrBJ1G6eji9vZqjtjQgkDjUHMysPtynZENWo7S` | lane_s0 | 0.5 | — | — | — | — | n=11 (мало) | 11 |
| `4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8` | lane_s0 | 0.5 | — | — | — | — | — | 0 |

### cand1 (5 адресов, билет 0.1)

| адрес | группа | билет | < 0.1 SOL-экв | 0.1–0.3 SOL-экв | 0.3–1 SOL-экв | 1–2 SOL-экв | ≥ 2 SOL-экв | сигналов |
|---|---|---|---|---|---|---|---|---|
| `5YRgrP3mjGzrzirYYN5HAQH19cTYREYwGxW6XRJQUzij` | cand1 | 0.1 | n=16 (мало) | n=10 (мало) | 42 / +8.58 / -3.11 / 38% | 69 / -0.02 / -2.84 / 33% | 297 / +3.47 / -1.81 / 45% | 666 |
| `CAPn1yH4oSywsxGU456jfgTrSSUidf9jgeAnHceNUJdw` | cand1 | 0.1 | — | n=9 (мало) | n=8 (мало) | n=9 (мало) | 158 / +2.68 / -1.31 / 48% | 208 |
| `EjtQrPTbcMevStBkpnjsH23NfUCMhGHusTYsHuGVQZp2` | cand1 | 0.1 | n=17 (мало) | — | n=15 (мало) | 67 / +3.11 / -2.66 / 40% | 31 / +0.09 / -2.92 / 42% | 164 |
| `6S8GezkxYUfZy9JPtYnanbcZTMB87Wjt1qx3c6ELajKC` | cand1 | 0.1 | — | n=8 (мало) | n=1 (мало) | n=14 (мало) | 60 / +2.66 / -2.57 / 35% | 114 |
| `2net6etAtTe3Rbq2gKECmQwnzcKVXRaLcHy2Zy1iCiWz` | cand1 | 0.1 | n=1 (мало) | n=6 (мало) | 21 / +0.10 / +1.01 / 57% | n=15 (мало) | 47 / +3.97 / -2.58 / 43% | 97 |

### cand2 (12 адресов, билет 0.1)

| адрес | группа | билет | < 0.1 SOL-экв | 0.1–0.3 SOL-экв | 0.3–1 SOL-экв | 1–2 SOL-экв | ≥ 2 SOL-экв | сигналов |
|---|---|---|---|---|---|---|---|---|
| `DsqRyTUh1R37asYcVf1KdX4CNnz5DKEFmnXvgT4NfTPE` | cand2 | 0.1 | — | n=1 (мало) | n=3 (мало) | 153 / -3.48 / -5.37 / 40% | 73 / +3.00 / +1.02 / 55% | 327 |
| `JDFDma1TMb1tWNFY1pruwCsHBybMdzwxveythZB2dcaG` | cand2 | 0.1 | — | — | n=2 (мало) | n=3 (мало) | 193 / +5.04 / +0.90 / 51% | 255 |
| `2kv8X2a9bxnBM8NKLc6BBTX2z13GFNRL4oRotMUJRva9` | cand2 | 0.1 | n=2 (мало) | n=1 (мало) | n=10 (мало) | 45 / +8.33 / -3.09 / 44% | 80 / +0.82 / +1.78 / 55% | 192 |
| `AFmiexHwMBFjKY7N9spbYjeKCr2nmj6k7zmAaamcjkVy` | cand2 | 0.1 | — | — | — | — | 143 / +7.32 / +2.72 / 56% | 191 |
| `7zyowp3jJHuVTm5VBkht21EgZHmbFTZR3edTAeqcy8Da` | cand2 | 0.1 | n=6 (мало) | n=11 (мало) | 21 / -1.03 / +0.69 / 52% | n=19 (мало) | 63 / +2.51 / +4.38 / 59% | 167 |
| `6yfEx8iX7g7WMsUd7Auybn7e1etwxH17T53MM1adtX87` | cand2 | 0.1 | — | n=2 (мало) | n=16 (мало) | 27 / +3.48 / +2.75 / 70% | 50 / +4.32 / +4.24 / 64% | 132 |
| `83Y8aqfDxz8XSerVACBubyinHm8rQ6REg9j5YXvT8b57` | cand2 | 0.1 | — | — | n=12 (мало) | n=3 (мало) | 65 / +6.04 / +2.29 / 57% | 120 |
| `AuPp4YTMTyqxYXQnHc5KUc6pUuCSsHQpBJhgnD45yqrf` | cand2 | 0.1 | n=2 (мало) | n=4 (мало) | n=1 (мало) | n=3 (мало) | 53 / +7.75 / +6.34 / 57% | 118 |
| `DrJ6SnDXkEsPeGdmSs93v5rwWumv5QMvAGSZjAyWSd5o` | cand2 | 0.1 | n=1 (мало) | — | n=1 (мало) | n=3 (мало) | 59 / +5.07 / +0.87 / 52% | 116 |
| `3JJzmDLp33hFG8KeNghcYM9Hyr8pJuWeTr3Rr8suP1tQ` | cand2 | 0.1 | — | — | n=5 (мало) | n=7 (мало) | 50 / +3.83 / +0.85 / 52% | 102 |
| `HTYho9ioTJcS4Dymi57HB65RBFu549gjyQ7kncfkknti` | cand2 | 0.1 | n=2 (мало) | — | n=6 (мало) | n=1 (мало) | 61 / +7.95 / +1.38 / 59% | 96 |
| `AxtZoNYhAxvpv416hW7yzpTdcJeY45t8JyGUYMJXL9G` | cand2 | 0.1 | — | — | — | — | 54 / +7.51 / +0.94 / 52% | 62 |

### cand1_05 (2 адресов, билет 0.5)

| адрес | группа | билет | < 0.1 SOL-экв | 0.1–0.3 SOL-экв | 0.3–1 SOL-экв | 1–2 SOL-экв | ≥ 2 SOL-экв | сигналов |
|---|---|---|---|---|---|---|---|---|
| `GM7Hrz2bDq33ezMtL6KGidSWZXMWgZ6qBuugkb5H8NvN` | cand1_05 | 0.5 | n=10 (мало) | n=1 (мало) | n=13 (мало) | 54 / +6.73 / +3.67 / 59% | 102 / +7.60 / -0.69 / 49% | 285 |
| `HmBmSYwYEgEZuBUYuDs9xofyqBAkw4ywugB1d7R7sTGh` | cand1_05 | 0.5 | n=2 (мало) | — | — | 58 / +1.03 / -4.13 / 41% | 83 / +1.19 / -1.96 / 40% | 245 |

### batch5 (12 адресов, билет 0.3)

| адрес | группа | билет | < 0.1 SOL-экв | 0.1–0.3 SOL-экв | 0.3–1 SOL-экв | 1–2 SOL-экв | ≥ 2 SOL-экв | сигналов |
|---|---|---|---|---|---|---|---|---|
| `4KFjw2xfH4cXJJKjG1jDZRNphctZPMoFz3K6r3bAVtmD` | batch5 | 0.3 | n=3 (мало) | n=2 (мало) | 23 / +2.15 / -1.11 / 35% | n=2 (мало) | 44 / +1.55 / -1.47 / 41% | 130 |
| `GAsnqm4XkNkPVgrAofNQ65jWf8f3tKCLHhE9ZqSy2AP1` | batch5 | 0.3 | — | — | n=1 (мало) | n=1 (мало) | 21 / +2.51 / +0.88 / 57% | 32 |
| `F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv` | batch5 | 0.3 | — | — | — | — | n=13 (мало) | 24 |
| `9CNyLECt2j8tnDhqxtjYk5HUhZ2b8Nwnyb7sfYN7vND2` | batch5 | 0.3 | — | — | — | — | n=14 (мало) | 21 |
| `EC2f5DnHzuNRit1ExqghSifDbp1wgrzktsRRZCtU92MJ` | batch5 | 0.3 | — | — | — | — | — | 18 |
| `9zZCjLr9xfXfp3qdqvPh6YeaEaagepz21khLhca69B18` | batch5 | 0.3 | — | — | — | — | n=4 (мало) | 7 |
| `HYh78tNpGBUcxoHgPU9v7VeP2wqSbkapuTzrfYHjfyLo` | batch5 | 0.3 | — | — | — | — | n=2 (мало) | 3 |
| `5opd5KBmodmoNuAThQ5cmXKbRxHbQDfomWGKAs3uEUP9` | batch5 | 0.3 | — | — | — | — | n=2 (мало) | 3 |
| `H3en1XWQHfbNWjEDRnRFi6HKVkZG1P7twdAHTvnCnYE3` | batch5 | 0.3 | — | — | — | — | n=1 (мало) | 2 |
| `8xL8S7P4QLdTGRquHas8NP5EVjp2qUGbmSgrkh97mvmq` | batch5 | 0.3 | — | — | — | — | n=1 (мало) | 1 |
| `BA3nKHc4DoSANRrx4FcCupExzs6cWzw1wkPpqjnqJaCN` | batch5 | 0.3 | — | — | — | — | — | 1 |
| `Ak6gsstZwaRDYKnzdyNg2HvCXDFvv21afjVix9VGRMQv` | batch5 | 0.3 | — | — | — | — | — | 0 |

## 3. События, где адрес упомянут, но не подписант и не владелец получившего токен-счёт

**В архиве PumpApi таких событий нет ни по одному источнику -- и быть не может.** Событие архива привязывается к кошельку только двумя полями: `txSigner` (подписант транзакции) и `breakdown[].trader` (кошелёк ноги свопа); списка счетов транзакции, pre/postTokenBalances и владельцев токен-счетов в событии нет вовсе. Пример сырого события -- `data/podbivka/arhiv_den/lider_celi_2026-10-01T21.json.gz` (поля: `signature`, `action`, `pool`, `poolId`, `mint`, `quoteMint`, `txSigner`, `tokenAmount`, `quoteAmount`, резервы, `poolFeeRate`, `priorityFee`, `block`, `timestamp`, `трейдер`, `breakdown`). Поэтому «упомянут, но не подписант и не владелец» в архиве не считается никак: такие транзакции в него просто не попадают. Мера берётся по цепи.

По цепи за сутки 2026-10-01T06:00:00Z → 2026-10-02T06:00:00Z по 44 адресам живых групп (те же, что в таблице «архив N / цепь N»): транзакций с пуловой программой, где адрес **подписант** -- 4222; где он только **упомянут** -- 2273, из них владелец получившего токен-счёта 749, владелец токен-счёта без прихода токена 11 и **ни подписант, ни владелец -- 1513** (из них с приходом SOL на его счёт 1468, всего +13.985856 SOL -- это и есть доля создателя и подобное). Ниже -- по адресу за сутки.

| адрес | группы | подписант | упомянут: владелец получившего | владелец без прихода | только упомянут | из них с приходом SOL | SOL за сутки | архив покупок |
|---|---|---|---|---|---|---|---|---|
| `CAPn1yH4oSywsxGU456jfgTrSSUidf9jgeAnHceNUJdw` | 543, cand1 | 188 | 2 | 0 | 259 | 257 | +3.742125 | 121 |
| `5pHeNsWMVEi1cbMzLhgqABnhEUwRTSzy5vBfeGWyJfxS` | lane_s0, 543, 133 | 182 | 6 | 0 | 240 | 240 | +3.546494 | 97 |
| `4KFjw2xfH4cXJJKjG1jDZRNphctZPMoFz3K6r3bAVtmD` | batch5, 543, 133 | 53 | 56 | 0 | 175 | 175 | +0.113384 | 29 |
| `6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy` | 543, sniper_src, cand1 | 310 | 0 | 11 | 164 | 164 | +0.560111 | 221 |
| `AFmiexHwMBFjKY7N9spbYjeKCr2nmj6k7zmAaamcjkVy` | снайперские источники, sniper_src, cand2 | 178 | 25 | 0 | 161 | 161 | +1.550224 | 96 |
| `HYh78tNpGBUcxoHgPU9v7VeP2wqSbkapuTzrfYHjfyLo` | batch5, 543, 133 | 0 | 10 | 0 | 147 | 147 | +0.077567 | 0 |
| `8RCEq8RrBJ1G6eji9vZqjtjQgkDjUHMysPtynZENWo7S` | lane_s0, 133 | 3 | 0 | 0 | 130 | 130 | +0.601209 | 0 |
| `Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB` | lane_s0, 543, 133 | 30 | 0 | 0 | 130 | 130 | +0.859671 | 12 |
| `7JVQMwRj82STgsG57spj6vpE6XY3RqG8B64PczVc7jJr` | lane_s0, 133 | 1240 | 3 | 0 | 32 | 10 | +0.026957 | 435 |
| `83Y8aqfDxz8XSerVACBubyinHm8rQ6REg9j5YXvT8b57` | cand2 | 87 | 0 | 0 | 18 | 11 | +0.153141 | 0 |
| `498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ` | lane_s0, 133 | 24 | 67 | 0 | 11 | 0 | +0.000000 | 32 |
| `GM7Hrz2bDq33ezMtL6KGidSWZXMWgZ6qBuugkb5H8NvN` | 543, cand1 | 170 | 4 | 0 | 8 | 8 | +0.234466 | 133 |
| `AuPp4YTMTyqxYXQnHc5KUc6pUuCSsHQpBJhgnD45yqrf` | 543, cand2 | 21 | 1 | 0 | 6 | 6 | +0.034402 | 28 |
| `DrJ6SnDXkEsPeGdmSs93v5rwWumv5QMvAGSZjAyWSd5o` | 543, cand2 | 85 | 11 | 0 | 6 | 6 | +1.353528 | 44 |
| `7zyowp3jJHuVTm5VBkht21EgZHmbFTZR3edTAeqcy8Da` | снайперские источники, cand2 | 126 | 2 | 0 | 5 | 5 | +0.088941 | 41 |
| `EjtQrPTbcMevStBkpnjsH23NfUCMhGHusTYsHuGVQZp2` | 543, cand1 | 176 | 9 | 0 | 4 | 4 | +0.131480 | 148 |
| `JDFDma1TMb1tWNFY1pruwCsHBybMdzwxveythZB2dcaG` | снайперские источники, sniper_src, cand2 | 90 | 0 | 0 | 4 | 3 | +0.292362 | 61 |
| `2kv8X2a9bxnBM8NKLc6BBTX2z13GFNRL4oRotMUJRva9` | 543, cand2 | 59 | 0 | 0 | 2 | 2 | +0.200278 | 32 |
| `3Um4qsYQKYULYSJwRChReZtgsXu3kiGm6HNTvZpy9dYy` | lane_s0, 133 | 98 | 0 | 0 | 2 | 2 | +0.080159 | 43 |
| `6yfEx8iX7g7WMsUd7Auybn7e1etwxH17T53MM1adtX87` | cand2 | 70 | 14 | 0 | 2 | 2 | +0.019666 | 0 |
| `9CNyLECt2j8tnDhqxtjYk5HUhZ2b8Nwnyb7sfYN7vND2` | batch5, 543, 133 | 20 | 0 | 0 | 2 | 0 | +0.000000 | 4 |
| `DsqRyTUh1R37asYcVf1KdX4CNnz5DKEFmnXvgT4NfTPE` | 543, cand2 | 102 | 3 | 0 | 2 | 2 | +0.096928 | 60 |
| `HmBmSYwYEgEZuBUYuDs9xofyqBAkw4ywugB1d7R7sTGh` | 543, cand1 | 473 | 3 | 0 | 2 | 2 | +0.190115 | 401 |
| `4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8` | lane_s0, 543, 133 | 0 | 0 | 0 | 1 | 1 | +0.032646 | 0 |
| `2net6etAtTe3Rbq2gKECmQwnzcKVXRaLcHy2Zy1iCiWz` | 543, cand1 | 73 | 0 | 0 | 0 | 0 | +0.000000 | 37 |
| `3JJzmDLp33hFG8KeNghcYM9Hyr8pJuWeTr3Rr8suP1tQ` | cand2 | 56 | 1 | 0 | 0 | 0 | +0.000000 | 0 |
| `4vER1GJQs73HtN9oYRswHZV4PSe2dvWQ8NLFoDhXeZjm` | lane_s0, 543, 133 | 14 | 0 | 0 | 0 | 0 | +0.000000 | 6 |
| `5opd5KBmodmoNuAThQ5cmXKbRxHbQDfomWGKAs3uEUP9` | batch5, 543, 133 | 2 | 247 | 0 | 0 | 0 | +0.000000 | 1 |
| `8xL8S7P4QLdTGRquHas8NP5EVjp2qUGbmSgrkh97mvmq` | batch5, 133 | 0 | 12 | 0 | 0 | 0 | +0.000000 | 0 |
| `Ak6gsstZwaRDYKnzdyNg2HvCXDFvv21afjVix9VGRMQv` | batch5, 543, 133 | 4 | 14 | 0 | 0 | 0 | +0.000000 | 0 |
| `AxtZoNYhAxvpv416hW7yzpTdcJeY45t8JyGUYMJXL9G` | 543, cand2 | 29 | 0 | 0 | 0 | 0 | +0.000000 | 18 |
| `B8m6fDRcw9VnkqWBXo5MEh9uY8HSPCkFuH4NNv5pCiPk` | lane_s0, 133 | 48 | 1 | 0 | 0 | 0 | +0.000000 | 32 |
| `Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit` | leader, 543, 133 | 0 | 18 | 0 | 0 | 0 | +0.000000 | 0 |
| `EC2f5DnHzuNRit1ExqghSifDbp1wgrzktsRRZCtU92MJ` | batch5, 543, 133 | 38 | 12 | 0 | 0 | 0 | +0.000000 | 15 |
| `F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv` | batch5, 543, 133 | 28 | 0 | 0 | 0 | 0 | +0.000000 | 15 |
| `GAsnqm4XkNkPVgrAofNQ65jWf8f3tKCLHhE9ZqSy2AP1` | batch5, 543, 133 | 50 | 228 | 0 | 0 | 0 | +0.000000 | 20 |
| `HTYho9ioTJcS4Dymi57HB65RBFu549gjyQ7kncfkknti` | cand2 | 30 | 0 | 0 | 0 | 0 | +0.000000 | 0 |
| `Xk9onqHkpULDEYYN9ZPyM7Q9AfTNYUrsCkzywyqdMeb` | lane_s0, 543, 133 | 65 | 0 | 0 | 0 | 0 | +0.000000 | 8 |

Адресов без пуловых транзакций за сутки: 6.


