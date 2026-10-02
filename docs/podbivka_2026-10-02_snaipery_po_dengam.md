# Подбивка: снайперы по деньгам -- неделя 21.09 00Z – 28.09 00Z (архив PumpApi)

Сито: кошельки с ≥ 50 покупками за неделю и медианным билетом ≥ 1 SOL-экв. в пулах кривая pump.fun / LaunchLab / Pump AMM / CPMM, котировка WSOL -- 10485 кошельков из 1785351 просмотренных (в просмотр попали кошельки с ≥ 3 покупками в сутки: при двух в сутки 50 за неделю не собрать).

Итог -- по минтам, ЗАКРЫТЫМ в окне (продано от 99 % до 101 % купленных токенов -- верхняя граница отсекает минты, купленные ДО окна: их покупок архив недели не видит, и без неё такой минт давал выдуманную прибыль): сумма SOL за продажи минус SOL за покупки, минус приоритет (поле архива `priorityFee`). **Чаевых и комиссии сети в архиве нет** -- они считаются по цепи отдельной стадией по верхним 50; до неё итог здесь без них. Удержание -- медиана слотов от первой покупки минта до первой продажи. Доля частями -- доля минтов, где продаж было ≥ 2. «Ведущий» -- кошелёк, покупавший тот же пул за ≤ 3 слота до него и сделавший это ≥ 5 раз за неделю (пул, а не минт: в потоке архива ключ -- пул). Ничего не рекомендуется.

| № | кошелёк | оборот, SOL | итог после приоритета, SOL | покупок | билет, SOL | минтов (закрыто / продано больше) | удержание, слотов | части | за ведущим | ведущие (раз за неделю) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `GatgyE2SqnNNjNeNGR8MG1VSVxFGxgyjB111hYJRTkee` | 18400.0 | 6713.581 | 583 | 7.0 | 70 (70 / 0) | 1400.5 | 0.957 | 0.033 | `7W6dqaGS` 7, `AuGSXcEs` 5, `78zRDEe3` 5 |
| 2 | `AsA76pjNLKd1Es7g9Uo9yzbGk2y3Vy5b26kd4gjXMefv` | 12289.56 | 5488.341 | 182 | 5.0 | 55 (54 / 1) | 1538 | 0.909 | 0.027 | `A3YDEpfW` 5 |
| 3 | `cATq48AALCiD2hECKZDgKHp3VoSHbXc4aDxSzgQLbc3` | 9445.91 | 3909.863 | 368 | 6.0 | 39 (38 / 1) | 1449 | 0.974 | 0.0 | — |
| 4 | `bwamJzztZsepfkteWRChggmXuiiCQvpLqPietdNfSXa` | 11459.66 | 3804.083 | 1400 | 5.9259 | 1339 (1305 / 14) | 11 | 0.66 | 0.011 | `E3yE42dQ` 9, `MriyaNN8` 8, `AjkDC1Kd` 7 |
| 5 | `HHKuYTFSF4DnZxmUoWrJGwCK2iGVSPoVQF799UnfLKbs` | 10161.6 | 3532.576 | 252 | 5.0 | 41 (40 / 0) | 1270 | 0.878 | 0.0 | — |
| 6 | `9R3m89gXeC6BWc2aN9CFqWDZA5WUJP3umQUerC7gjoFs` | 8605.11 | 3264.18 | 195 | 5.0 | 50 (50 / 0) | 1428.5 | 0.88 | 0.0 | — |
| 7 | `BRjVGhFnXZ7wKcvgHa35hJuzGi8eTmHWcouhfZQKuJZg` | 7164.23 | 2450.204 | 280 | 5.8 | 28 (28 / 0) | 1578.0 | 0.929 | 0.0 | — |
| 8 | `GurWHaasjUmJDcu3qheVoDi5UegTQgrjupQtxRdnEqTo` | 11288.99 | 2420.637 | 407 | 27.0 | 285 (144 / 0) | 36.0 | 0.737 | 0.221 | `EQNoFbP9` 11, `GNukyR8A` 10, `8m4Tw67p` 9, `5fvVWFbz` 9, `8a2otEyB` 9, `8D7mAd8u` 8, `9nGFmAGj` 8, `451syhvs` 8 |
| 9 | `wae8YMC7PWjcPbM3mdx3CUDZEkkjaFAE4VxS5mF2YuN` | 7339.0 | 2389.702 | 111 | 15.0 | 39 (33 / 0) | 1061.5 | 0.769 | 0.0 | — |
| 10 | `E4FPggw9yDp9PNJDuRoLYDr6a5HKCsAterw3fygLpZEA` | 2629.32 | 1814.632 | 477 | 4.9383 | 443 (429 / 6) | 11.0 | 0.69 | 0.01 | `AjkDC1Kd` 5 |
| 11 | `2CQgjcdNEo7WtbQLpJTAVcC3Ga61pNvRDTgP5grzctFG` | 18838.78 | 1765.516 | 6993 | 1.9753 | 6350 (6310 / 8) | 7.0 | 0.632 | 0.493 | `6qudAN2k` 161, `GG8hd6XK` 161, `5U1abDdg` 142, `2ksQ77e9` 107, `3h65MmPZ` 105, `ardinRsN` 104, `BCagckXe` 101, `FLyBjfWG` 97 |
| 12 | `8TawwodB8Mkk1YnqJjEau5Cerix4wms61Yw1gTAc6n49` | 3789.0 | 1394.767 | 50 | 17.5 | 19 (19 / 0) | 912 | 1.0 | 0.0 | — |
| 13 | `kEFiAX3jo5NmemysQov342TZ9mGh6yp92GDRjhA8XDf` | 40842.18 | 1371.693 | 24464 | 1.2325 | 1458 (1402 / 29) | 73.5 | 0.88 | 0.476 | `ARu4n5mF` 1938, `2tgUbS9U` 1336, `BxMo47Jc` 1065, `7usDSTXm` 752, `5981EnT1` 586, `8JjVNyj6` 553, `Ar2Y6o1Q` 501, `MriyaNN8` 483 |
| 14 | `922M9fBTzwGfC4Gqu3WxB8S89WHaTfVAUZKwK5PNtZpk` | 32911.32 | 1349.197 | 8127 | 3.1012 | 355 (332 / 11) | 69.5 | 0.997 | 0.597 | `MriyaNN8` 1257, `ARu4n5mF` 612, `CzYQ2kFn` 344, `9EwQoN74` 337, `7dGrdJRY` 305, `DtvmxrTA` 297, `4BQ6ATUt` 263, `8LngKMjY` 251 |
| 15 | `8ZN71XTdVo8yRovnGLmNgW3Tgniw6A4J3JGLvPD686FP` | 1185.34 | 1254.128 | 275 | 4.0 | 245 (218 / 2) | 9.0 | 0.702 | 0.018 | `46qjgvZN` 5 |
| 16 | `Ge8ztT6Vv4MUQpHuQd732uQjFgGFaqDnAx4i5ymnfCBs` | 14005.65 | 1124.678 | 1618 | 4.95 | 74 (69 / 3) | 206.5 | 0.811 | 0.397 | `2FdJD3EV` 102, `AuNYDxqL` 101, `FB5jAs5Q` 76, `BKsaqqo3` 74, `2VS1k5m7` 73, `Cs4zpkT9` 61, `ARu4n5mF` 58, `mATtYiy3` 56 |
| 17 | `AV7PjXHL5JXZ1YoYRoN9Dsstg1x2UciBupMCXcJP8gUz` | 3067.05 | 985.329 | 370 | 7.0 | 368 (347 / 2) | 15 | 0.559 | 0.0 | — |
| 18 | `SQHK48QT8SY1vYN44iXji7wQ6CJek8AjfX6mBp47TZq` | 18175.42 | 984.927 | 15549 | 1.1078 | 4344 (4292 / 11) | 88.0 | 0.711 | 0.326 | `64hP97Bw` 1101, `DiJF3L37` 551, `omegoMAe` 515, `72NWWbch` 394, `2zMj6wNq` 386, `5veTCy9e` 310, `GVVP8N7j` 272, `sssssDdM` 256 |
| 19 | `75sowC62FiowyhAxyzSAhpxtYP1niqCrFsFrW43bYnLw` | 277.35 | 905.304 | 78 | 2.97 | 64 (35 / 0) | 2414.5 | 0.14 | 0.062 | `7vKGb6T8` 5 |
| 20 | `64hP97Bwr5PubotcTeGgfhkFrGiLVVxT2kVo9M9b4AEz` | 21010.52 | 895.981 | 17982 | 1.1002 | 4808 (4743 / 9) | 45 | 0.722 | 0.452 | `SQHK48QT` 1753, `omegoMAe` 840, `DiJF3L37` 772, `2zMj6wNq` 531, `72NWWbch` 500, `sssssDdM` 453, `GVVP8N7j` 426, `ssssswdk` 418 |
| 21 | `2JxPmU9UU5KhSQ7h5v8PfhgbxPQhHNyBqNrhPAjPiWCu` | 1510.94 | 885.081 | 321 | 4.8889 | 308 (306 / 0) | 13.0 | 0.987 | 0.0 | — |
| 22 | `5FqUo9aBjsp7QeeyN6Vi2ZmF2fjS4H5EU7wnAQwPy17z` | 1993.5 | 884.173 | 242 | 9.0 | 228 (219 / 2) | 17.0 | 0.57 | 0.0 | — |
| 23 | `5aLY85pyxiuX3fd4RgM3Yc1e3MAL6b7UgaZz6MS3JUfG` | 14357.61 | 870.426 | 7171 | 1.6202 | 1363 (1290 / 30) | 420.0 | 0.758 | 0.427 | `ARu4n5mF` 332, `2tgUbS9U` 274, `7usDSTXm` 253, `Ar2Y6o1Q` 231, `kEFiAX3j` 200, `BxMo47Jc` 197, `AeBgCFnk` 174, `5981EnT1` 149 |
| 24 | `4W3fiTa3Rai2dh5USiXA71EymrPCeLxVmSeXeU8his5C` | 1431.72 | 859.733 | 242 | 5.0 | 231 (213 / 1) | 7.0 | 0.661 | 0.021 | `g5HcBNwS` 5 |
| 25 | `Ar2Y6o1QmrRAskjii1cRfijeKugHH13ycxW5cd7rro1x` | 17250.6 | 812.277 | 8919 | 1.6302 | 1196 (1177 / 5) | 38 | 0.869 | 0.595 | `BxMo47Jc` 705, `kEFiAX3j` 665, `7usDSTXm` 579, `ARu4n5mF` 565, `5981EnT1` 420, `5WThXb2e` 313, `8JjVNyj6` 285, `2tgUbS9U` 281 |
| 26 | `5U1abDdg8CfRPRHpnMHiY7azkcwUZjP8Wj3fNmbXp3ys` | 6791.99 | 804.057 | 3234 | 2.7852 | 2860 (2858 / 0) | 3.0 | 0.106 | 0.382 | `2J2rtMBR` 103, `ardinRsN` 66, `bwamJzzt` 61, `AMDEmVoc` 55, `CyaE1Vxv` 43, `4vw54BmA` 30, `CFMra9xV` 30, `YARSciE3` 27 |
| 27 | `dtrzJPj7yDdvm6eRqBAgxsK2sMJeD9HhBEBB3XMedXy` | 1582.07 | 722.367 | 261 | 4.9529 | 240 (223 / 7) | 9 | 0.717 | 0.019 | `E3yE42dQ` 5 |
| 28 | `DmG8cLLkBy2ck6pAPGo6oqBaKT4fzQ584TLm9a4wTrZg` | 465.09 | 678.066 | 94 | 2.475 | 31 (24 / 1) | 1148.5 | 0.31 | 0.426 | `A92hHR4X` 34, `3wzr1Be3` 8, `ARu4n5mF` 7, `6eT6fShC` 7, `DPwH9aWr` 7, `6LRGHFDg` 6, `963iHBYA` 6, `BgqYVdyQ` 6 |
| 29 | `88887QrRZPZmsstEXsoXXB8E7nbmUDde4Gp5s7jG3ENu` | 14276.21 | 652.954 | 14429 | 1.0 | 9698 (9667 / 5) | 102 | 0.322 | 0.54 | `9999huSC` 1141, `omegoMAe` 557, `BDW8Bjoe` 502, `5QY3MBvL` 485, `AmwJToZR` 409, `CCCCQCrL` 367, `sssssDdM` 331, `nya666pQ` 311 |
| 30 | `D9gQ6RhKEpnobPBUdWY5bPQt2p3zGk3iVz6ChpUi2ArA` | 1049.32 | 645.494 | 363 | 2.963 | 327 (285 / 11) | 17 | 0.723 | 0.143 | `GZVSEAaj` 40, `4h4AT2Kh` 24, `8Y3KKHfx` 21, `BPDy8rdC` 15 |
| 31 | `9LXWa7V3AE15VfBupcx5gDts2ix3Y9NzbcKZKjkkq6hV` | 1017.03 | 644.649 | 314 | 2.963 | 337 (252 / 3) | 27 | 0.729 | 0.0 | — |
| 32 | `EBWkQGHPc4teggp5ok1oUmoGnbyx4W4bSYTo2ynMVw2z` | 2975.34 | 643.225 | 664 | 2.963 | 654 (634 / 0) | 1 | 0.963 | 0.422 | `6qudAN2k` 19, `9LXWa7V3` 15, `CBcNrPBR` 14, `3pSaSmMT` 14, `BugPnPKi` 13, `387FRwow` 13, `8tEjj2Kw` 13, `GijFWw4o` 12 |
| 33 | `GpTXmkdvrTajqkzX1fBmC4BUjSboF9dHgfnqPqj8WAc4` | 1108.27 | 632.736 | 198 | 5.0 | 188 (177 / 0) | 11 | 0.813 | 0.0 | — |
| 34 | `NULLioEUhd89Jo5Acm9sX88bwjNjrsAVy6KWkXD7qZh` | 24166.73 | 631.486 | 17844 | 1.2996 | 2806 (2798 / 3) | 15 | 0.447 | 0.708 | `AuNYDxqL` 2919, `2VS1k5m7` 2911, `FB5jAs5Q` 2209, `7JNNuQ2B` 1968, `GZnrnkZk` 1702, `mATtYiy3` 1508, `3b67692r` 1338, `g5HcBNwS` 1337 |
| 35 | `6wT8MSKLiXS26aZR1hTCE69MNGw3C6WRZmvdTtWmpHL8` | 1285.88 | 626.272 | 210 | 6.9136 | 187 (169 / 7) | 18 | 0.647 | 0.029 | `63dkU3Zh` 6, `8jd8CHjf` 5 |
| 36 | `DxhpC9c4kGkYJR34xUrfFeDx9vwb6yoHUMWrTCZybwtH` | 8049.2 | 624.863 | 4532 | 1.7778 | 4532 (4459 / 0) | 8.0 | 0.034 | 0.294 | `2CQgjcdN` 195, `Fi9fjEmz` 151, `53DnQU1d` 130, `FiFawHqx` 98, `DsGJkPzF` 98, `AMDEmVoc` 86, `H8bgvrbb` 60, `8ki679dZ` 53 |
| 37 | `9Bbw8zQvbt9rfTeNmAmhzK7yGB5y9f2CZtcR27Ws5qwX` | 1603.4 | 616.863 | 66 | 6.5 | 6 (6 / 0) | 775.5 | 1.0 | 0.0 | — |
| 38 | `whamNNP9tHoxLg92yHvJPdYhghEoCg1qYTsh5a2oLbx` | 1266.37 | 612.847 | 231 | 5.0 | 225 (217 / 1) | 9 | 0.612 | 0.0 | — |
| 39 | `7ZV54HcwtzRhZSEPskT8ox5hn9yNocK9xpe4BQXoziaP` | 2191.41 | 606.177 | 381 | 6.9136 | 343 (324 / 7) | 16.0 | 0.398 | 0.131 | `GcKSP83E` 37, `CpeBPwtA` 33, `B59c7WRT` 27, `922M9fBT` 6, `DuS2qVd9` 6 |
| 40 | `3D9bKicNaSRpXsWxDa4KMCNmDqVeSPYrgruVN4WMjVVi` | 1337.59 | 555.55 | 60 | 13.66 | 60 (7 / 0) | 259.5 | 0.214 | 0.0 | — |
| 41 | `3VUNtVtjjx5ckUojT7UocJ5fbuAJRsNUXNfTBnPte9vC` | 1213.92 | 554.782 | 437 | 2.963 | 319 (296 / 3) | 262.5 | 0.54 | 0.0 | — |
| 42 | `7BNaxx6KdUYrjACNQZ9He26NBFoFxujQMAfNLnArLGH5` | 7475.85 | 554.614 | 3249 | 1.7169 | 886 (849 / 11) | 793.5 | 0.652 | 0.216 | `ARu4n5mF` 99, `2tgUbS9U` 85, `kEFiAX3j` 44, `CzYQ2kFn` 33, `MriyaNN8` 31, `7usDSTXm` 29, `5aLY85py` 28, `2c4Nd25B` 27 |
| 43 | `C8BdBLW1GdnCR86nSpcfRMbshvHqwKq9qSTTdSn71AeV` | 850.23 | 536.658 | 188 | 4.9383 | 171 (166 / 5) | 9.0 | 0.655 | 0.0 | — |
| 44 | `F5XvCe4233m6mHRbkkq2ZsFvqrPAnRrDBExeQy2fwagQ` | 521.0 | 523.13 | 122 | 3.0 | 122 (108 / 0) | 4.0 | 0.951 | 0.0 | — |
| 45 | `FFdBLYqL9rs5fUT38ArE8GV2L3BziEgG5LJUu6sshfkF` | 1015.41 | 489.428 | 249 | 3.0 | 227 (219 / 0) | 6.0 | 0.881 | 0.036 | `97rkFX41` 7, `FGvcrgbG` 7, `5KQXNpDA` 6, `7MkKWyG7` 6, `J6mBr1NG` 5 |
| 46 | `9keCU8mgA23XV8LCgSCJEo4Lspmo9fRBA3MyxJmZKQSp` | 7451.11 | 483.21 | 4785 | 1.9753 | 3914 (3894 / 1) | 87 | 0.347 | 0.319 | `CSwo9FsN` 506, `HicFqmdY` 390, `EtZWfmdy` 183, `B1ZMARC6` 135, `FNvLdMfo` 107, `5pHk81ES` 87, `ELxcKrDw` 82, `Da1Erpm3` 77 |
| 47 | `29yFzeBZgxf5zqrAkKXwgZtQehRf4pL8WbV2nRJikbw8` | 1003.58 | 482.102 | 344 | 3.0 | 332 (317 / 0) | 8.0 | 0.885 | 0.0 | — |
| 48 | `4xY9T1Q7foJzJsJ6YZDSsfp9zkzeZsXnxd45SixduMmr` | 1340.79 | 472.679 | 256 | 4.9383 | 164 (159 / 3) | 19.0 | 0.204 | 0.199 | `Em8J3gBW` 9, `FZZJga8B` 8, `GfpZErvL` 7, `9kSyXPid` 6, `2FdJD3EV` 6, `2NmKaKBT` 6, `DqAf4YDu` 6, `kEFiAX3j` 6 |
| 49 | `96qVSY2nAVatHBVmTj3bpV9HQuQo2uM2Y4E6aRNV7gB4` | 1634.99 | 464.849 | 81 | 20.0 | 80 (8 / 0) | 12147 | 0.0 | 0.0 | — |
| 50 | `GeUnv1jmtviRbR7Gu1JnXSGkUMUgFVBHuEVQVpTaUX1W` | 1102.95 | 455.92 | 366 | 3.0617 | 530 (351 / 3) | 9.0 | 0.664 | 0.0 | — |

## Где в этом ряду 54ua и BomG

- `54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE` (54ua) -- место 163 из 10485 по итогу после приоритета: 188.636 SOL, оборот 987.15 SOL, покупок 200, билет 3.9506 SOL, минтов 198 (закрыто 193, продано больше купленного 0), удержание 10 слотов, части 0.96, за ведущим 0.435, ведущих 9.
- `BomGAZnAGwnjs3oaqNHm4Wk5sKctQi83PKVxjRuGGbrm` (BomG) -- в сито не попал: за неделю 4 покупок на 24.6 SOL в этих типах пулов, медианный билет 5.50 SOL -- порог 50 покупок не взят.

## Ведущие верхних 50, которых нет в наших списках

Всего ведущих у верхних 50: 128; нет в data/podbivka/arhiv_adresa.json: 113. В следующий проход сита -- они.

- `2FdJD3EVx4yv13157M9awWTxiU1ShtMMbwNZhyD3UySs`
- `2J2rtMBRLxAbFkSLgrsuSNN8N5KMTJVaRTdnGfbZ5Vjw`
- `2NmKaKBTN1iakMqB6NUgSnVNsr5QwJ2CwD2vSpV6up7A`
- `2VS1k5m7TVtjMzm5bEJcsFJYJJF4UDRyLSDqkxtuLvWh`
- `2c4Nd25BehQBNpug4E5DAVVSNmyYZxYBVUnjFMu19iMS`
- `2ksQ77e9e5SS6VA6poanRGWfkU3R4R5wZnptbJHb2nx9`
- `2tgUbS9UMoQD6GkDZBiqKYCURnGrSb6ocYwRABrSJUvY`
- `2zMj6wNqNKSa3dG8ioUDi71P2uijKiojLwEizVGKWRjG`
- `3b67692r29jDFd4Ud23xBTU7QJqKHAz2MApuc1mqWrjK`
- `3h65MmPZksoKKyEpEjnWU2Yk2iYT5oZDNitGy5cTaxoE`
- `3pSaSmMTfds773Xdzka4q4K4ygxLFoQFZQwtB88nKpWa`
- `3wzr1Be3XTL8cvgHvKpv4sKyeAzmW8oLEk2NEWHoKGRw`
- `451syhvspvKMRSZHX663Z323zfz1Tdacp2W1bvyYgypz`
- `46qjgvZNdiLxCqJ6oMBf8NoA3dZtC3xuagLpfMLYG8f9`
- `4BQ6ATUt26GFdiYQfht23iwfKyYD9D7XbL5ATqNGk3xK`
- `4h4AT2Khmix43PFKiMsRFLM6ZNCM7raoBWUcVrKEyKeF`
- `53DnQU1dJZNHfKBpv2Lhsd4EBGNpwsF4tS13Egc1m6Tv`
- `5981EnT1JLAfRqoBpn4jvd81ie5FXMgAJgkJnpYAsJs3`
- `5KQXNpDAkmngVTnYKm98D7V7ayWnAg5NWQwm3au13AQk`
- `5QY3MBvLEsP4xjE4SNYJYPuwvJgoBUrDnvSaZUNsNHZT`
- `5U1abDdg8CfRPRHpnMHiY7azkcwUZjP8Wj3fNmbXp3ys`
- `5WThXb2ere5hrMoKiY7tWEKi8bkbe9ESmWwRqEutHRbY`
- `5fvVWFbzPC4QdEWzfa715MWYGV5ammx2kTFS9hQjeK1Y`
- `5pHk81ESW7vUcMK61bRV1vFq7usygiYFjqgepmLY5ZZo`
- `5veTCy9eDaL66LqABfpywN6jr1s7zaXP4uit8sMRAaHA`
- `63dkU3ZhzXLin97bZ5SyXjWggLMZW1vfsANPBC2vDkj6`
- `64hP97Bwr5PubotcTeGgfhkFrGiLVVxT2kVo9M9b4AEz`
- `6LRGHFDgCPcTzBkrFU6gZo8iomkf3Kgy9imNHL198f8E`
- `6eT6fShCgFV75t2mp2g38h5cMhYErMtp8LSx52KHSWnx`
- `72NWWbchKdRJKvhG6U4f9363psUttkeEkE4yk15imy8u`
- `78zRDEe34XnLfSzgBtenpN7yijB4P51EKk6AQwzpXYa8`
- `7JNNuQ2BnBPWsVnhdAVZPjs1P5WdB8StaHQusz9JHmrQ`
- `7MkKWyG7nRxs1Qg4aGrAgk7JAyrQsh42C5BZuvRrFWgm`
- `7W6dqaGSvU5FtAEVGMQqrgk66Q6Dor4SjXKzpR3ZLGfR`
- `7dGrdJRYtsNR8UYxZ3TnifXGjGc9eRYLq9sELwYpuuUu`
- `7usDSTXmjAJzEV3AsT7UqG4oGWnMJ1VJH1FzhR3Sc6uV`
- `7vKGb6T8xsuQbtJvKmp3JdR6FZ5AdJYGmcoNz2tdkUcu`
- `8D7mAd8uGTyNKqPM2BNKeeSxrnow9XKsd4wjgyQj8vCs`
- `8JjVNyj65NyhCuit2n4xAPHaMJHvJsQarg23wFrhawAc`
- `8LngKMjY87P1fuUYhksSKdMN7yw8jaHEBxXb3CtguPRD`
- `8Y3KKHfxnx42Bope43V2FEcnx87nQn8gJQ6RAR33A5wp`
- `8a2otEyBzMakwdW46G8PVD5f3bay3WTNZ7WMmB6bfYrG`
- `8jd8CHjf8e4hc2RfFPfMyJ4mcL9L2VYPniU5X2rqjZz3`
- `8ki679dZgE8ozmGVKBzBbBqqby5CToRfqzAoKYx49aNX`
- `8m4Tw67p4EowDVgPkQxqRrb2nRxsATagjo9pstjkZCbN`
- `8tEjj2KwGBJwHWnQ36LYnkgUNUyrDzTKzNqJdHQ1nHiJ`
- `922M9fBTzwGfC4Gqu3WxB8S89WHaTfVAUZKwK5PNtZpk`
- `963iHBYAChpEJM3uHvWbEanddabyANsPoqWityDDeYJ9`
- `97rkFX41sz5vS3yfXZvyJaqd1CYhGW5sB6q3jBFoTko5`
- `9999huSCf6QpepPHQVFLZ9smhsbwkV4XWPLboiy9qqRj`
- `9EwQoN74Hzw747EM7wB3WtvgAdRGH1czaPtbqZj1qDj4`
- `9kSyXPidmHKvSaDr8Ym6NQide1ABFXW1SyWvrEJcSKsH`
- `9nGFmAGj7NDm1kk73CDVsLHUkoCGQSyR28tic3Fn3Yem`
- `A3YDEpfWTJskX1bn4ozVDDzgkhhJZimd1Nsq3GGTYxCt`
- `A92hHR4XMJNiJfBvg8sHKWiCivjxWjkFdokLEsayQwEb`
- `AMDEmVocLtN5ji2gBY7BrmSHEyM5vt6TGszBbg4crMXg`
- `ARu4n5mFdZogZAravu7CcizaojWnS6oqka37gdLT5SZn`
- `AeBgCFnkSWMAwv3zjaJPB93hxEBUEVkhEyKVFaxT84pJ`
- `AjkDC1KdkgPaFRtrCbeSh6B6dhVX2JM4o2XhPSs2qu4b`
- `AmwJToZR4YkawNquDbiUHbZnC2Myq1CLRRryUQ9FBf89`
- `AuGSXcEsRhatS3m4VyzS5Ty7L1jeCVnjPCPwqd3k1KFF`
- `AuNYDxqLav774fntdjAWNoqwNUZzojn7tgXkiNZ8yy6v`
- `B1ZMARC6qhJYJrvBJywUYdtf6hq2WBkTgvbbnGuQsQnX`
- `BDW8Bjoe9fgfRPAkSf3Kpnphdd4ugWwsLitSxvqo4gg4`
- `BKsaqqo39FGotJzoGKHnfNYckXdCwTBxnfFNbFAFdwnr`
- `BPDy8rdCEYjXfwZ7AfVVp9KoSoXA5ApxTsuRa6bg3dEK`
- `BgqYVdyQDBnSMJZo1W9efgX8aNsrF3JH5DCZGvS3G3MS`
- `BugPnPKiTGTJq6ki6Kt9PChhyAyqbKHCTDdcyPpRfJ1M`
- `BxMo47JcFjZFM9bpNCibdapL67PVBgTk6JJa6WVU7qwk`
- `CBcNrPBRRiMgihLCqbjfUDJGepyeYoeP5B8HrH2PD5vy`
- `CCCCQCrL6zVjnDeucDzcxJgxAs5ahNmrhw1CDexPhqrd`
- `CFMra9xVXTyPJBcFvgsTo1U3RmM4PrjmMw1taiy6QUAd`
- `CSwo9FsN2QYAdtfXN1bfWqbXFEPHC2mr1EiiZhijK2Wx`
- `CpeBPwtAEuyFh3Pt9mLyAocXmZQtBLE1HFqQseyGiUw4`
- `Cs4zpkT9sPWw26LWTU8UmLJLDmNrHWSJw6ZDZphykwkC`
- `CzYQ2kFnBxsNEt9Zy34vQ3n5fSDhvA4o4XaTnq1rLvyr`
- `DPwH9aWrYGSBqMiPNLMW4Hti1k1xgq64t781eycfLCp5`
- `Da1Erpm3FoS2f3Sto7FfVVnd8S2acvQyxY52VDmpH2iQ`
- `DiJF3L3796seiwyjmwfTz9wHUCCP5kmeMQM6kDzzjXax`
- `DqAf4YDudpzNHLZ5YXQXP5xRrwX4uX2KC4Y1AR9V5ikb`
- `DsGJkPzFEQZuwy7JjZzPcJEyEfdC6StV7rarXG4ftRSA`
- `DtvmxrTACskMG2W8a6KXgSemUfvyNVeQTgfpJoGvMVKx`
- `DuS2qVd9rVvFKo8m4HXDrPtnVUjurTtVC3D3vtyTwSJB`
- `E3yE42dQZCmwzsGBCZfFYPU9SaQjT5uR4WATdXDcf6tu`
- `ELxcKrDwPRKcKJNEMkqdUgHRtXhMtQsbT8KbtRh51SCC`
- `EQNoFbP9AtEsRE6Px5TUZy1yYnqEkQmpYEY2WhYwjdLx`
- `Em8J3gBWapfVBGVhVipwQnLrqCvnWBnLajw6XFsFECPF`
- `EtZWfmdyziLMK4kwYs56DWEz4Gfj1z7oDs4XPKGDyre`
- `FB5jAs5Q7XFLhoM6EePn1f8UqqvExnCiirdtEXpL7g9N`
- `FGvcrgbGfhUNU7NTPY7Z9Mapzw6NPYJZcWMMuQhtH6mT`
- `FLyBjfWGsxH4zDDfoUJHwA9774TmPVpR8kB5B46PtbaZ`
- `FNvLdMfo2aB9xf14x4TFN3dr2TDpKz78Rji1k8bTx4bs`
- `FZZJga8B5wTFBUf2GGH9yDke5jXmpKDHMZprG3Kzirxm`
- `Fi9fjEmzM7ieUEwT5LtFiYBrnz8EFYFYSqgZg5F2T8T8`
- `FiFawHqxeTVBhv6YbqbLwDVuvokRpPUM1bNwAyxhGc6W`
- `GG8hd6XKsDjLpviYEyt3EKaZs8WXmZ86uqVto6zVmsjt`
- `GNukyR8A2x14Z7NGTEUQmePb3KaSwCj5jmBPqRaiqz4u`
- `GVVP8N7jnxgr3QdtR461bsuCNNQmuNu4DBW2Ab4tzKVp`
- `GZnrnkZkdxWFmvEyBFWAobmcHUsNreXWAYVCKdyb3fFi`
- `GfpZErvLYqfCtRaKSyKxCCH9vVqH4rbnQ72RE2jJVtH1`

## Что считалось

Суток в фазе 2: 7 (2026-09-21, 2026-09-22, 2026-09-23, 2026-09-24, 2026-09-25, 2026-09-26, 2026-09-27); строк архива 482116907; ошибок чтения часов 0.

