# Подбивка: куст DYAn4XpA / HT5EVAzf / Gf2wYM2k -- кто входит быстрее и есть ли уникальные

7 суток архива (окно 24.09 17:00Z → 01.10 17:00Z, файлы `kust_*T16`, порог сигнала 0.01 SOL): событий трёх адресов 981 (покупок 472, продаж 509), сигналов 304, ошибок чтения часов 0. Места в блоке -- отдельный проход по цепи (`getBlock`), в архиве их нет -- ещё не собраны, столбцы мест пустые. Ничего не рекомендуется.

## а. Общие минт + слот: кто раньше внутри блока

Случаев, где тот же минт в том же слоте купили хотя бы двое из трёх: **104**; из них одна и та же транзакция на двоих -- **0**, без мест в блоке (блок не отдан) -- 104.

| слот | минт | кто | размеры, SOL-экв | одна транзакция | места в блоке | разница | первый |
|---|---|---|---|---|---|---|---|
| 450097196 | `CARDSccU` | Gf2wYM2k, HT5EVAzf | Gf2wYM2k 0, HT5EVAzf 0 | нет | Gf2wYM2k —, HT5EVAzf — | — | — |
| 450098725 | `CARDSccU` | Gf2wYM2k, HT5EVAzf | HT5EVAzf 0, Gf2wYM2k 0 | нет | HT5EVAzf —, Gf2wYM2k — | — | — |
| 450102294 | `5fzMbRfA` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 3.957, HT5EVAzf 3.8404, Gf2wYM2k 3.7119 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 450153884 | `B2JBQ98Z` | Gf2wYM2k, HT5EVAzf | HT5EVAzf 2.0452, Gf2wYM2k 1.8659 | нет | HT5EVAzf —, Gf2wYM2k — | — | — |
| 450239630 | `FbGdsWgE` | Gf2wYM2k, HT5EVAzf | Gf2wYM2k 0.4887, HT5EVAzf 0.4851 | нет | Gf2wYM2k —, HT5EVAzf — | — | — |
| 450239730 | `2EZWjn3j` | Gf2wYM2k, HT5EVAzf | HT5EVAzf 1.8177, Gf2wYM2k 1.6332 | нет | HT5EVAzf —, Gf2wYM2k — | — | — |
| 450243489 | `9VREH2LV` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 2.0304, DYAn4XpA 1.8807 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450251171 | `5piXiXot` | Gf2wYM2k, HT5EVAzf | Gf2wYM2k 2.1678, HT5EVAzf 1.7922 | нет | Gf2wYM2k —, HT5EVAzf — | — | — |
| 450257569 | `26bJc6U5` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 5.3166, DYAn4XpA 6.4167 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450258421 | `26bJc6U5` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 0.0469, DYAn4XpA 0.0521 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450264385 | `MhtEiW13` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | HT5EVAzf 1.2204, DYAn4XpA 1.1604, Gf2wYM2k 1.1917 | нет | HT5EVAzf —, DYAn4XpA —, Gf2wYM2k — | — | — |
| 450266623 | `6uv5Qm6p` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 3.4824, HT5EVAzf 3.1617, Gf2wYM2k 3.1337 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 450285289 | `HXxBRPLZ` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | HT5EVAzf 1.7754, Gf2wYM2k 1.9232, DYAn4XpA 1.9991 | нет | HT5EVAzf —, Gf2wYM2k —, DYAn4XpA — | — | — |
| 450421123 | `EhR6VWs3` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 1.9686, DYAn4XpA 2.0746, HT5EVAzf 1.8235 | нет | Gf2wYM2k —, DYAn4XpA —, HT5EVAzf — | — | — |
| 450421964 | `qikeUfbJ` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 2.9306, HT5EVAzf 3.1282, DYAn4XpA 2.925 | нет | Gf2wYM2k —, HT5EVAzf —, DYAn4XpA — | — | — |
| 450422152 | `qikeUfbJ` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 0.0277, HT5EVAzf 0.0321, Gf2wYM2k 0.0286 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 450424027 | `FeSBGcJt` | DYAn4XpA, HT5EVAzf | HT5EVAzf 3.1591, DYAn4XpA 3.3032 | нет | HT5EVAzf —, DYAn4XpA — | — | — |
| 450427327 | `J9pJ964H` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 2.049, Gf2wYM2k 1.868, HT5EVAzf 1.9497 | нет | DYAn4XpA —, Gf2wYM2k —, HT5EVAzf — | — | — |
| 450433976 | `3o4hLASZ` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 1.5886, HT5EVAzf 1.6588, Gf2wYM2k 1.4573 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 450476328 | `8J69rbLT` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 1.7942, DYAn4XpA 2.1658 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450478224 | `FF6t1Uqg` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | HT5EVAzf 4.2356, Gf2wYM2k 4.446, DYAn4XpA 4.0295 | нет | HT5EVAzf —, Gf2wYM2k —, DYAn4XpA — | — | — |
| 450515670 | `9WsnqeW3` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 1.7515, HT5EVAzf 1.469, Gf2wYM2k 1.6685 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 450517132 | `6ruHjSeQ` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 2.8091, DYAn4XpA 2.5914, HT5EVAzf 2.4217 | нет | Gf2wYM2k —, DYAn4XpA —, HT5EVAzf — | — | — |
| 450517840 | `9WsnqeW3` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 2.6109, HT5EVAzf 2.7732, DYAn4XpA 2.4381 | нет | Gf2wYM2k —, HT5EVAzf —, DYAn4XpA — | — | — |
| 450525694 | `6ruHjSeQ` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 3.29, HT5EVAzf 3.2409, DYAn4XpA 3.2469 | нет | Gf2wYM2k —, HT5EVAzf —, DYAn4XpA — | — | — |
| 450526452 | `FQ4X5Zyq` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | HT5EVAzf 3.1105, Gf2wYM2k 2.6848, DYAn4XpA 3.1147 | нет | HT5EVAzf —, Gf2wYM2k —, DYAn4XpA — | — | — |
| 450527162 | `HdwhHdht` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 2.5038, DYAn4XpA 2.8226 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450527597 | `9GGVdGxG` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 4.6589, DYAn4XpA 4.2511 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450528534 | `HdwhHdht` | DYAn4XpA, Gf2wYM2k | DYAn4XpA 2.2638, Gf2wYM2k 2.6251 | нет | DYAn4XpA —, Gf2wYM2k — | — | — |
| 450550652 | `FL4UmMcs` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | HT5EVAzf 2.8368, DYAn4XpA 2.3709, Gf2wYM2k 2.6145 | нет | HT5EVAzf —, DYAn4XpA —, Gf2wYM2k — | — | — |
| 450551748 | `BMp3jDPE` | DYAn4XpA, Gf2wYM2k | DYAn4XpA 4.8806, Gf2wYM2k 5.0194 | нет | DYAn4XpA —, Gf2wYM2k — | — | — |
| 450554480 | `8cn4avN4` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 6.3443, DYAn4XpA 5.5357 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450555553 | `AMqJDyt4` | DYAn4XpA, Gf2wYM2k | DYAn4XpA 4.9875, Gf2wYM2k 4.9125 | нет | DYAn4XpA —, Gf2wYM2k — | — | — |
| 450556457 | `EGP1nZ7A` | DYAn4XpA, HT5EVAzf | DYAn4XpA 4.0392, HT5EVAzf 4.5964 | нет | DYAn4XpA —, HT5EVAzf — | — | — |
| 450559291 | `BVMWcqRK` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 2.4415, HT5EVAzf 2.1162, DYAn4XpA 2.2867 | нет | Gf2wYM2k —, HT5EVAzf —, DYAn4XpA — | — | — |
| 450563547 | `41g3m5wZ` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 3.1818, DYAn4XpA 2.7744, HT5EVAzf 2.8439 | нет | Gf2wYM2k —, DYAn4XpA —, HT5EVAzf — | — | — |
| 450563893 | `3NbEzSgg` | Gf2wYM2k, HT5EVAzf | Gf2wYM2k 3.3143, HT5EVAzf 3.5302 | нет | Gf2wYM2k —, HT5EVAzf — | — | — |
| 450564246 | `AfUbRxQZ` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 1.7166, Gf2wYM2k 1.6509, HT5EVAzf 1.5214 | нет | DYAn4XpA —, Gf2wYM2k —, HT5EVAzf — | — | — |
| 450565398 | `TtB1Gze4` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 2.5199, HT5EVAzf 2.5648, Gf2wYM2k 2.7375 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 450565769 | `D7vohC4w` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 1.5903, HT5EVAzf 1.5112, DYAn4XpA 1.7874 | нет | Gf2wYM2k —, HT5EVAzf —, DYAn4XpA — | — | — |
| 450765209 | `7dCbJXTA` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 1.2797, HT5EVAzf 1.4094, DYAn4XpA 1.2219 | нет | Gf2wYM2k —, HT5EVAzf —, DYAn4XpA — | — | — |
| 450767652 | `Edxe1mAe` | DYAn4XpA, Gf2wYM2k | DYAn4XpA 3.3882, Gf2wYM2k 3.4563 | нет | DYAn4XpA —, Gf2wYM2k — | — | — |
| 450771439 | `oPAiAikW` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 4.7906, DYAn4XpA 5.0189 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450772132 | `oPAiAikW` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 2.1972, DYAn4XpA 2.4886, HT5EVAzf 2.186 | нет | Gf2wYM2k —, DYAn4XpA —, HT5EVAzf — | — | — |
| 450772626 | `Gxoazs3H` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 2.136, HT5EVAzf 2.2992, DYAn4XpA 2.4093 | нет | Gf2wYM2k —, HT5EVAzf —, DYAn4XpA — | — | — |
| 450772804 | `7jVD3WnR` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 3.1506, DYAn4XpA 2.917, HT5EVAzf 2.7324 | нет | Gf2wYM2k —, DYAn4XpA —, HT5EVAzf — | — | — |
| 450773180 | `Ge3cA5kY` | DYAn4XpA, Gf2wYM2k | Gf2wYM2k 3.5376, DYAn4XpA 3.3069 | нет | Gf2wYM2k —, DYAn4XpA — | — | — |
| 450774179 | `88AmJzX1` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | Gf2wYM2k 2.6015, DYAn4XpA 2.6578, HT5EVAzf 2.5629 | нет | Gf2wYM2k —, DYAn4XpA —, HT5EVAzf — | — | — |
| 450898631 | `CC5D6puF` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 3.5825, Gf2wYM2k 3.7981, HT5EVAzf 3.375 | нет | DYAn4XpA —, Gf2wYM2k —, HT5EVAzf — | — | — |
| 450903292 | `AHp2e4b2` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | HT5EVAzf 2.1375, Gf2wYM2k 1.8576, DYAn4XpA 1.8715 | нет | HT5EVAzf —, Gf2wYM2k —, DYAn4XpA — | — | — |
| 450929381 | `ESC5ym7V` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 3.475, Gf2wYM2k 3.02, HT5EVAzf 3.2827 | нет | DYAn4XpA —, Gf2wYM2k —, HT5EVAzf — | — | — |
| 450933772 | `7qzph6zU` | DYAn4XpA, Gf2wYM2k | DYAn4XpA 6.4343, Gf2wYM2k 5.2991 | нет | DYAn4XpA —, Gf2wYM2k — | — | — |
| 450938635 | `CxfF3tvA` | DYAn4XpA, Gf2wYM2k | DYAn4XpA 2.3685, Gf2wYM2k 2.605 | нет | DYAn4XpA —, Gf2wYM2k — | — | — |
| 450940145 | `HEtmfSgc` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 0, HT5EVAzf 0, Gf2wYM2k 0 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 451388879 | `SPCXxcqX` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | HT5EVAzf 1.3693, Gf2wYM2k 1.5571, DYAn4XpA 1.6039 | нет | HT5EVAzf —, Gf2wYM2k —, DYAn4XpA — | — | — |
| 451390928 | `4KLjGoYR` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 0.6746, HT5EVAzf 0.6141, Gf2wYM2k 0.5854 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 451409702 | `APLwmQEy` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 3.5613, Gf2wYM2k 4.1853, HT5EVAzf 3.9868 | нет | DYAn4XpA —, Gf2wYM2k —, HT5EVAzf — | — | — |
| 451413885 | `Xs3oZwbH` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | HT5EVAzf 18.5941, Gf2wYM2k 19.0409, DYAn4XpA 21.46 | нет | HT5EVAzf —, Gf2wYM2k —, DYAn4XpA — | — | — |
| 451415220 | `5bKYTe4L` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 2.644, HT5EVAzf 2.7462, Gf2wYM2k 2.432 | нет | DYAn4XpA —, HT5EVAzf —, Gf2wYM2k — | — | — |
| 451416347 | `CLHfpxeL` | DYAn4XpA, Gf2wYM2k, HT5EVAzf | DYAn4XpA 3.0122, Gf2wYM2k 3.4149, HT5EVAzf 3.3507 | нет | DYAn4XpA —, Gf2wYM2k —, HT5EVAzf — | — | — |
| … ещё 44 случаев в `data/podbivka/kust_svod.json` | | | | | | | |

**Доля случаев, где первым каждый:** .

## б. Уникальные сигналы каждого (минт без двух других в окне 1800 слотов)

Результат при нашем входе «конец слота» за ним, билет 0.3, выход +108, п.п. чистыми. Первая строка -- по живому правилу (покупка источника от 2 SOL-экв), вторая -- по всем размерам, какие есть в проходе.

| адрес | от 2 SOL: n | среднее | медиана | в плюсе | все размеры: n | среднее | медиана | в плюсе |
|---|---|---|---|---|---|---|---|---|
| `DYAn4XpAkN5mhiXkRB7dGq4Jadnx6XYgu8L5b3WGhbrt` | n=6 (мало) | — | — | — | 26 | +1.00 | -0.05 | 50% |
| `HT5EVAzfn1CzBqbWtVKcC9CRdexcemactNQNt7tM7rSc` | n=8 (мало) | — | — | — | n=8 (мало) | — | — | — |
| `Gf2wYM2k5ojfPzN5Uqi3mbP1nWEdZhhzMyDBsKrHA3kC` | n=3 (мало) | — | — | — | n=3 (мало) | — | — | — |

## в. Похожи ли на «один трейдер -- разные кошельки»

| адрес | покупок / продаж | минтов | размер: медиана / среднее / макс | приоритет, медиана SOL | промежуток между своими покупками, медиана слотов | в общих минт+слот | своих минтов без двух других |
|---|---|---|---|---|---|---|---|
| `DYAn4XpAkN5mhiXkRB7dGq4Jadnx6XYgu8L5b3WGhbrt` | 259 / 184 | 94 | 0.1 / 1.7569 / 29.0086 | 0.000186 | 706 | 94 | 34 |
| `HT5EVAzfn1CzBqbWtVKcC9CRdexcemactNQNt7tM7rSc` | 98 / 154 | 76 | 2.5648 / 3.2946 / 21.4093 | 0.000168733 | 3297 | 73 | 20 |
| `Gf2wYM2k5ojfPzN5Uqi3mbP1nWEdZhhzMyDBsKrHA3kC` | 115 / 171 | 89 | 2.6145 / 3.6907 / 44.4935 | 0.0001734 | 2222 | 102 | 19 |

| адрес | пулы покупок | котировки |
|---|---|---|
| `DYAn4XpAkN5mhiXkRB7dGq4Jadnx6XYgu8L5b3WGhbrt` | pump 125, pump-amm 48, raydium-launchpad 31, meteora-dlmm 24, raydium-clmm 17, raydium-cpmm 11, meteora-damm-v2 3 | SOL 187, не SOL 72 |
| `HT5EVAzfn1CzBqbWtVKcC9CRdexcemactNQNt7tM7rSc` | pump 58, pump-amm 10, raydium-launchpad 10, raydium-clmm 8, raydium-cpmm 6, meteora-dlmm 5, meteora-damm-v2 1 | SOL 68, не SOL 30 |
| `Gf2wYM2k5ojfPzN5Uqi3mbP1nWEdZhhzMyDBsKrHA3kC` | pump 64, pump-amm 13, raydium-launchpad 12, raydium-clmm 10, meteora-dlmm 8, raydium-cpmm 7, meteora-damm-v2 1 | SOL 78, не SOL 37 |

| пара | общих минтов | жаккар | разрыв по общему минту, медиана слотов | из них в том же слоте |
|---|---|---|---|---|
| DYAn4XpA / HT5EVAzf | 64 (из 94 и 76) | 0.604 | 548 | 63 из 199 |
| DYAn4XpA / Gf2wYM2k | 83 (из 94 и 89) | 0.83 | 340 | 92 из 239 |
| HT5EVAzf / Gf2wYM2k | 67 (из 76 и 89) | 0.684 | 0 | 71 из 86 |

