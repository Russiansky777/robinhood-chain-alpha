# Подбивка: копировщики, не маркетмейкеры -- верхние 50 и их ведущие

Из 10485 кошельков сита (неделя 21.09 00Z – 28.09 00Z, `docs/podbivka_2026-10-02_snaipery_po_dengam.md`) отобраны те, у кого **доля покупок за ведущим ≥ 0.3** и **медианное удержание ≤ 50 слотов** -- это и отделяет копировщиков от маркетмейкеров, которые держат часами. Таких **1914**; ниже верхние 50 по итогу после приоритета. «Ведущий» -- кошелёк, покупавший тот же пул за ≤ 3 слота до него и сделавший это ≥ 5 раз за окно. Чаевых и комиссии сети в архиве нет -- итог после приоритета (`priorityFee`). Ничего не рекомендуется.

**Оговорка к столбцу «ведущие»: мера несимметрична только на вид.** Среди пар «верхний 50 -- его ведущий» ВЗАИМНЫХ (A зовёт B ведущим, и B зовёт A) -- 68. Пример: `64hP97Bw` считает `SQHK48QT` ведущим 1753 раза, а `SQHK48QT` его же -- 1365 раз; `Ar2Y6o1Q` и `kEFiAX3j` -- 665 против 661. Это значит, что оба постоянно сидят в одних пулах в пределах трёх слотов, и признак «за ведущим» у таких кошельков говорит о совместной толпе, а не о том, кто кого копирует. Для односторонних пар (как у 54ua: 9 ведущих, обратных ссылок нет) признак читается прямо.

## A. Верхние 50 копировщиков (неделя 21–28.09)

| адрес | оборот, SOL | итог после приоритета, SOL | покупок | билет, SOL | удержание, слотов | за ведущим | ведущие (раз за окно) |
|---|---|---|---|---|---|---|---|
| `2CQgjcdNEo7WtbQLpJTAVcC3Ga61pNvRDTgP5grzctFG` | 18838.78 | 1765.516 | 6993 | 1.9753 | 7.0 | 0.493 | `6qudAN2k` 161, `GG8hd6XK` 161, `5U1abDdg` 142, `2ksQ77e9` 107, `3h65MmPZ` 105, `ardinRsN` 104, `BCagckXe` 101, `FLyBjfWG` 97 |
| `64hP97Bwr5PubotcTeGgfhkFrGiLVVxT2kVo9M9b4AEz` | 21010.52 | 895.981 | 17982 | 1.1002 | 45 | 0.452 | `SQHK48QT` 1753, `omegoMAe` 840, `DiJF3L37` 772, `2zMj6wNq` 531, `72NWWbch` 500, `sssssDdM` 453, `GVVP8N7j` 426, `ssssswdk` 418 |
| `Ar2Y6o1QmrRAskjii1cRfijeKugHH13ycxW5cd7rro1x` | 17250.6 | 812.277 | 8919 | 1.6302 | 38 | 0.595 | `BxMo47Jc` 705, `kEFiAX3j` 665, `7usDSTXm` 579, `ARu4n5mF` 565, `5981EnT1` 420, `5WThXb2e` 313, `8JjVNyj6` 285, `2tgUbS9U` 281 |
| `5U1abDdg8CfRPRHpnMHiY7azkcwUZjP8Wj3fNmbXp3ys` | 6791.99 | 804.057 | 3234 | 2.7852 | 3.0 | 0.382 | `2J2rtMBR` 103, `ardinRsN` 66, `bwamJzzt` 61, `AMDEmVoc` 55, `CyaE1Vxv` 43, `4vw54BmA` 30, `CFMra9xV` 30, `YARSciE3` 27 |
| `EBWkQGHPc4teggp5ok1oUmoGnbyx4W4bSYTo2ynMVw2z` | 2975.34 | 643.225 | 664 | 2.963 | 1 | 0.422 | `6qudAN2k` 19, `9LXWa7V3` 15, `CBcNrPBR` 14, `3pSaSmMT` 14, `BugPnPKi` 13, `387FRwow` 13, `8tEjj2Kw` 13, `GijFWw4o` 12 |
| `NULLioEUhd89Jo5Acm9sX88bwjNjrsAVy6KWkXD7qZh` | 24166.73 | 631.486 | 17844 | 1.2996 | 15 | 0.708 | `AuNYDxqL` 2919, `2VS1k5m7` 2911, `FB5jAs5Q` 2209, `7JNNuQ2B` 1968, `GZnrnkZk` 1702, `mATtYiy3` 1508, `3b67692r` 1338, `g5HcBNwS` 1337 |
| `Fx2e5CAfq7Ds9Kg7Yk6BDN2wTr2uHN7gTJvKZVLg5TyT` | 721.72 | 444.925 | 248 | 2.4343 | 13 | 0.363 | `8vQZrLsK` 24, `J6ruBwRv` 12, `hYP39WZZ` 9, `C6LMdiDj` 8, `GhWRZ1RY` 8, `HoepM5Tj` 7, `BVs9hi5B` 7, `J1sLsSLB` 5 |
| `EnU1CPeCqFtVnqnYhd1JgfcPrhcDx4ffLZEnV9TnjJ4g` | 975.27 | 422.956 | 476 | 2.1728 | 29 | 0.958 | `E4FPggw9` 414, `3tqpS1u2` 35, `6Qa46jFT` 31, `9A7um9R3` 6, `G8gTguf7` 6, `HCsVftaM` 5 |
| `Fi9fjEmzM7ieUEwT5LtFiYBrnz8EFYFYSqgZg5F2T8T8` | 2707.79 | 391.447 | 2164 | 1.2642 | 8.0 | 0.412 | `DxhpC9c4` 160, `2CQgjcdN` 111, `DsGJkPzF` 84, `H8bgvrbb` 79, `FiFawHqx` 71, `AMDEmVoc` 62, `4yFAz7dp` 36, `8RvtT818` 35 |
| `BuekUk3YMmm7Agnb4ni1qMCmShVpeBTdQZYNyihyzr7u` | 3499.7 | 376.367 | 829 | 3.9506 | 11.0 | 0.849 | `ardinRsN` 57, `4vw54BmA` 55, `2CQgjcdN` 43, `3VwmH9qY` 40, `BCagckXe` 38, `CyaE1Vxv` 36, `4k92XBen` 36, `FLyBjfWG` 36 |
| `76rdHqaie4ooQ8ErhAgDyeifgVYauG2tBG2ofroNj6SG` | 1944.02 | 371.332 | 309 | 4.9383 | 4.0 | 0.388 | `JDFDma1T` 14, `ENGkTtUa` 11, `2kv8X2a9` 9, `C9s6hwgu` 9, `H9TMtTxB` 9, `6QGRJVuA` 9, `HSeCG7T2` 9, `GM7Hrz2b` 8 |
| `3VwmH9qYgqSSunvX1qQ3YNVMGsm4wnB6UYveH15hXK1Q` | 444.77 | 369.636 | 367 | 1.1232 | 30.0 | 0.605 | `4k92XBen` 152, `VJSDW6S7` 116, `H4dxJQN6` 41 |
| `9YdtQaDhsNwxSJaZ39HLCCMCVjgMNQscbd9f3UbqmFfB` | 1719.76 | 330.032 | 779 | 1.0 | 44.5 | 0.362 | `BwWK17cb` 277, `4e6M1w4N` 9, `JSWDiYE8` 5 |
| `G3fk9NykrzmcXr4YYmToftFayrC6Y6ACiuc6WCKq3f8B` | 915.71 | 322.29 | 240 | 4.0 | 24.5 | 0.992 | `8ZN71XTd` 237, `46qjgvZN` 16, `Cu1cXMvt` 10 |
| `81XychAw2StVtpTSp2CuoJxK9w1gv5d8r9taz2wzhvcV` | 1525.94 | 319.66 | 212 | 5.0 | 10.0 | 0.5 | `5YRgrP3m` 21, `CyaE1Vxv` 16, `ardinRsN` 11, `ARu4n5mF` 10, `JDFDma1T` 10, `BCrTEXmW` 9, `4vw54BmA` 8, `8efxAPpX` 5 |
| `GG8hd6XKsDjLpviYEyt3EKaZs8WXmZ86uqVto6zVmsjt` | 4018.56 | 319.204 | 1355 | 2.4691 | 8.0 | 0.861 | `3h65MmPZ` 137, `2CQgjcdN` 111, `FLyBjfWG` 74, `BCagckXe` 72, `GYieg7Sq` 71, `7R8mfCnA` 71, `6qudAN2k` 70, `GT2Y6qZD` 67 |
| `AyjjfuioEs341LrFaivCS6iPV9dXPyP4qCm4Fe2PRCPX` | 549.92 | 293.796 | 227 | 2.3584 | 10.0 | 0.742 | `HcoVmJC4` 32, `9tA3UaKm` 20, `Cc4xRSeo` 19, `zYVFeki3` 17, `DgBoXPU6` 17, `28cNdqYk` 16, `7tJPDTbz` 16, `7h8D59Qz` 10 |
| `EaaCfVxVboPx1wht6Xvqcmq6LVhDW5ouyCBMiu2tafBG` | 6480.35 | 280.452 | 5120 | 1.1852 | 16.0 | 0.519 | `8jsPACii` 202, `5QY3MBvL` 164, `nya666pQ` 145, `88887QrR` 131, `EDGEKeUN` 126, `AmwJToZR` 105, `CCCCQCrL` 102, `sssssDdM` 101 |
| `tBVFi3uhRxDtn4C5Sxy7D6yUQ2qUfp2qkjcoL6ttmFt` | 5056.78 | 275.413 | 2154 | 1.485 | 46 | 0.335 | `ARu4n5mF` 113, `5981EnT1` 46, `8JjVNyj6` 43, `kEFiAX3j` 40, `7usDSTXm` 38, `2tgUbS9U` 37, `sssssDdM` 37, `Ar2Y6o1Q` 32 |
| `89ooxuy4NTf3UNswCsjXx7fEd2WtJasaqUQbcrMUsPrm` | 769.18 | 266.509 | 372 | 2.0949 | 4 | 0.946 | `AdWbHbP5` 77, `DmRgL8wj` 47, `Fs2qhtSd` 33, `qUTKfuoA` 28, `Cv9FhYDP` 27, `GXcujphw` 18, `AdmwMEPV` 18, `3AkuuS27` 16 |
| `4Aktn51cRBEYXMrn2SDACPwXrKyz5oyXh4EUhCdiCjcG` | 6276.81 | 265.879 | 5056 | 1.1852 | 16 | 0.52 | `5QY3MBvL` 183, `5bb5kQKh` 159, `nya666pQ` 140, `88887QrR` 132, `EDGEKeUN` 123, `9A7um9R3` 115, `6yRBpeDD` 110, `AmwJToZR` 96 |
| `J3gZFpvsCsmA5zwqXgUpyqNLBoGGcJzmnfRwbHFR6KfU` | 6287.24 | 260.636 | 5056 | 1.1852 | 16 | 0.509 | `5QY3MBvL` 159, `nya666pQ` 154, `88887QrR` 153, `2k6aaxsz` 151, `BDW8Bjoe` 120, `AmwJToZR` 120, `9A7um9R3` 106, `EDGEKeUN` 105 |
| `64hJxoZzjoL6MW4uwdKDkaBiZdk87ZxmthHnLwaAKUgS` | 6382.02 | 259.055 | 5048 | 1.1852 | 16 | 0.515 | `3oN2NBiW` 185, `5QY3MBvL` 170, `88887QrR` 159, `nya666pQ` 144, `6yRBpeDD` 124, `CCCCQCrL` 118, `EDGEKeUN` 118, `BDW8Bjoe` 114 |
| `Dxis2F7p4LLbmA1XANAY6AVo4LQYoyqrgE42vRzXi9z8` | 458.7 | 223.618 | 155 | 2.388 | 4 | 0.873 | `Ae8QE7nv` 29, `Ho7VxRaB` 25, `HCfSSxk3` 19, `5dAxF9CG` 17, `An3x1q64` 17, `16MeFNrK` 13, `3g7hgDGr` 11, `JC1pZZMK` 9 |
| `HicFqmdYCxDLAPCszK5iyNZM2YkVHwJhFSJy9asgsr9m` | 2255.09 | 214.572 | 1718 | 1.4815 | 17 | 0.405 | `CSwo9FsN` 533, `9keCU8mg` 105, `FNvLdMfo` 50, `6MG3azei` 18, `HZGy7Kwh` 14, `9TBs4dci` 13, `5veTCy9e` 11, `7HmkKn8P` 11 |
| `J6ruBwRvV6hrwtHG2rPNUjFwikehz2nKEHSPR8vsnutP` | 626.59 | 213.626 | 249 | 2.451 | 14 | 0.956 | `Fx2e5CAf` 238, `8vQZrLsK` 24, `hYP39WZZ` 9, `C6LMdiDj` 8, `GhWRZ1RY` 8, `BVs9hi5B` 6, `HoepM5Tj` 6, `J1sLsSLB` 5 |
| `4wkzxPB2XGb632rbyE8ZPm9mTNN23aWKiYPxWssSg9un` | 14005.25 | 210.869 | 6528 | 1.0892 | 38 | 0.851 | `HWGoJ1Ha` 389, `Aa9mpEB7` 334, `5fCsf3UZ` 322, `HX6turSS` 318, `8xjSAk3p` 314, `794HyV3S` 311, `3nWLLAFp` 311, `E1SC85tH` 307 |
| `Akmwur3r9WhfFpy6yFUuDtgbQ4X4QUuif7xS7JfMNsxy` | 2265.78 | 197.02 | 1054 | 1.9753 | 3.0 | 0.556 | `8NY4eFow` 84, `EEHcVr9t` 64, `AJ5snxaH` 50, `9A7um9R3` 42, `HCsVftaM` 40, `nya666pQ` 39, `BsNemx1C` 39, `GfDh8LF5` 39 |
| `6Qa46jFT7NegJRKNGPMQudr9v1aNC73KqFzX7vuvwRcC` | 716.55 | 195.43 | 475 | 1.5802 | 29 | 0.966 | `EnU1CPeC` 452, `E4FPggw9` 413, `3tqpS1u2` 37, `G8gTguf7` 8, `9A7um9R3` 6, `HCsVftaM` 6, `CkLT4ADy` 5 |
| `3xvmSbDZY5vvKu2S9C2Ui9GjEAjwCrPevVSSSWu7JFac` | 1912.26 | 190.111 | 902 | 1.7778 | 1 | 0.78 | `6gAgy3F1` 89, `29yFzeBZ` 83, `GTnbqqrY` 82, `Gg3ska7S` 81, `HTrUmYyH` 76, `8NJ7Ujpj` 75, `4dmtSBr4` 70, `8TVKncCu` 70 |
| `54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE` (54ua) | 987.15 | 188.636 | 200 | 3.9506 | 10 | 0.435 | `6qudAN2k` 28, `387FRwow` 13, `4S9Vbao1` 12, `JDFDma1T` 10, `ARu4n5mF` 7, `AFmiexHw` 6, `3pSaSmMT` 6, `BCrTEXmW` 6 |
| `D6Fuvs4DATozxeV5fdr14xtZYCQDRfZzEmhGrjm3BgwG` | 875.56 | 184.564 | 295 | 1.9753 | 7 | 0.546 | `EM1ZyNA3` 33, `PMJA8UQD` 30, `J2L495ZP` 28, `CyaE1Vxv` 17, `5U1abDdg` 11, `Bi4rd5FH` 10, `ardinRsN` 10, `2CQgjcdN` 9 |
| `Dnq77xMNwqPTj8NRhXvD827HNJNgQvZpqob3oMFus7tE` | 10572.36 | 182.461 | 7425 | 1.0 | 3.0 | 0.762 | `FB5jAs5Q` 1027, `2FdJD3EV` 952, `DKvJD5BY` 724, `Cs4zpkT9` 686, `AuNYDxqL` 661, `mATtYiy3` 630, `2VS1k5m7` 563, `7JNNuQ2B` 530 |
| `9w5uJWRdLxhjtuMMppZMpGPNhTw5DCpDZ5om3yjqFGVj` | 192.3 | 182.388 | 64 | 2.963 | 25.0 | 0.938 | `6khARRdo` 13, `8KmGAnJ5` 12, `7oeoW4t2` 12, `5oNvozjo` 12, `9iRuqkAZ` 11 |
| `6ANGS6SSCxkv6hV3iymHHMESqDz7EFaecCwHA8qTswpr` | 933.08 | 180.204 | 170 | 4.8889 | 7 | 0.559 | `FFFz6LeD` 75, `sycaFCMF` 30, `66Fwv53d` 15, `G8gTguf7` 7, `92cwTJoG` 6, `CWiNFAEk` 6, `HCsVftaM` 5 |
| `B7v2xGaFp6h6iDAaDs4nDJb46o7v5244x6EkUi4WHBnB` | 769.26 | 175.704 | 371 | 2.1019 | 21 | 1.0 | `89ooxuy4` 371, `AdWbHbP5` 77, `DmRgL8wj` 47, `Fs2qhtSd` 33, `qUTKfuoA` 28, `Cv9FhYDP` 27, `GXcujphw` 18, `AdmwMEPV` 18 |
| `5Evyn77ib4bxpKAMNvzoNDgLhBjFSLxeePaZpWeCqTWW` | 609.29 | 175.158 | 232 | 2.3396 | 5.0 | 0.944 | `Dxis2F7p` 84, `Ae8QE7nv` 50, `9gYpkT6B` 48, `JC1pZZMK` 43, `An3x1q64` 32, `Ho7VxRaB` 32, `5dAxF9CG` 30, `2QRNKeqq` 22 |
| `8NY4eFowg7ESTFoeUtJJJuJwhiM919cXxckmy4CfSwVb` | 2226.02 | 174.905 | 1117 | 1.9753 | 2 | 0.594 | `Akmwur3r` 105, `EEHcVr9t` 102, `E35uoEGT` 67, `9A7um9R3` 67, `HCsVftaM` 67, `EDGEKeUN` 66, `2T3U2FzV` 64, `ApSqUG7V` 60 |
| `D9QP3Y9YJdwtpSMEUPELyYQUqScWPtMhXZLVqL3yuPhn` | 163.65 | 165.968 | 53 | 3.2593 | 25 | 0.849 | `2dp7WQhK` 6, `ExW6U7R9` 6, `5Wb9FHdW` 6, `7oeoW4t2` 6, `5oNvozjo` 6, `6khARRdo` 5, `9iRuqkAZ` 5, `8KmGAnJ5` 5 |
| `2SbcdgYR8Mp1e5bVMham6CkuKcPpJ93XxQ139D6dCGp4` | 1254.9 | 161.45 | 373 | 2.963 | 41 | 0.434 | `E35uoEGT` 20, `E4FPggw9` 19, `GijFWw4o` 18, `CFt926D6` 18, `3tqpS1u2` 18, `6Qa46jFT` 18, `EnU1CPeC` 18, `2Xjizmu5` 17 |
| `jhFR5Az8JWonCJQ7jagSR75UGgg7GLXG9BoCDBUE2Ws` | 689.41 | 159.667 | 73 | 10.8642 | 21 | 0.315 | `HNhuE4Y3` 17, `GtsZdFAW` 7 |
| `B8zn7SyWypomG5yMaep5DL2DUMKsyJCjCkKvd1AcLbxx` | 988.55 | 155.465 | 163 | 7.7 | 11 | 0.951 | `9KGv1vhe` 146, `7eTU6LbY` 29, `7wqrdPux` 25, `tAg2tgyH` 25, `5rFVvx9o` 23, `5kYkf8hw` 21 |
| `GJNd9DapidbEKxxTpNocs3Qcow3pwnmQRhzYn5ajEC6o` | 549.72 | 155.386 | 227 | 2.3392 | 14.0 | 0.991 | `Ayjjfuio` 227, `HcoVmJC4` 32, `9tA3UaKm` 20, `Cc4xRSeo` 19, `zYVFeki3` 17, `DgBoXPU6` 17, `28cNdqYk` 16, `7tJPDTbz` 16 |
| `9qcHMAe2TD1JXzztJrQdCCCcRoPM3CmGsZuP7zGJ3sdS` | 1409.0 | 151.205 | 175 | 2.837 | 45.0 | 0.503 | `7mGZh58m` 75, `J5t48c3u` 18, `Fz6ZLemd` 14, `5oeoKmts` 13, `2h6WT2yE` 8, `821JhhqD` 8, `DZAa55Hw` 7, `BAk7jy4p` 6 |
| `JC1pZZMKAFRLP3FmC2hWH8KfepRjBHxLE1MXmNM9ryb5` | 466.97 | 150.951 | 166 | 2.3472 | 6 | 0.928 | `Dxis2F7p` 54, `5Evyn77i` 47, `Ae8QE7nv` 38, `5dAxF9CG` 28, `16MeFNrK` 28, `9gYpkT6B` 27, `An3x1q64` 18, `HCfSSxk3` 17 |
| `5rBBZb5ShUpeYSrwK5tApe2vX9FQprG88oWqTfoaNzvn` | 4474.45 | 150.492 | 4052 | 1.0 | 11.0 | 0.807 | `FB5jAs5Q` 899, `2FdJD3EV` 742, `AuNYDxqL` 723, `Cs4zpkT9` 493, `mATtYiy3` 471, `2ykw4LcM` 467, `2VS1k5m7` 415, `8z9FqZed` 369 |
| `2LSncYjJrRsorTTG7UeTia9rmGo8pw8JtaAoYbq7CV5F` | 786.91 | 145.452 | 253 | 1.9753 | 7 | 0.526 | `EM1ZyNA3` 19, `J2L495ZP` 19, `PMJA8UQD` 16, `4vw54BmA` 11, `ardinRsN` 10, `CyaE1Vxv` 9, `Bi4rd5FH` 8, `5U1abDdg` 8 |
| `3tqpS1u2mXkfiGSCKhaEZUeFQa2yfqmpmp85ZTBMZCjk` | 802.91 | 145.286 | 476 | 1.7778 | 29 | 0.971 | `EnU1CPeC` 449, `6Qa46jFT` 447, `E4FPggw9` 415, `G8gTguf7` 8, `HCsVftaM` 6, `LpieurWu` 5, `9A7um9R3` 5, `CkLT4ADy` 5 |
| `3XSYLa8VqkTV4RxKGG92nQb3gPziFFFEVLCYkEaEtZ5k` | 8257.56 | 144.106 | 4959 | 1.2731 | 18 | 0.569 | `ARu4n5mF` 431, `8JjVNyj6` 252, `6ou4YPcD` 187, `5981EnT1` 180, `4993AyyD` 169, `7usDSTXm` 158, `MriyaNN8` 152, `EaBRrMav` 139 |
| `4k92XBen2ofaTYi5ntXX9TVjMPgFaaZRK5K8UmtLKqez` | 475.76 | 142.964 | 389 | 1.1485 | 25.5 | 0.519 | `3VwmH9qY` 139, `VJSDW6S7` 120, `H4dxJQN6` 20 |

## Б. Их ведущие, которых нет в нашем реестре, через то же сито на свежем окне

У верхних 50 ведущих всего 217; нет в `data/podbivka/arhiv_adresa.json` -- **189**. По ним прогнана фаза 2 сита на окне **24.09 – 01.10** (8 суток, 8 файлов, строк 545610230, ошибок чтения 0). Правило сита то же: **≥ 50 покупок за окно и медианный билет ≥ 1 SOL-экв.** Его проходят **72** из 175 ведущих, у которых в окне нашлись покупки. Покупок считается по разбору окна (до 1000 на кошелёк в сутки -- предел записи), поэтому у самых частых это нижняя оценка.

| адрес | оборот, SOL | итог после приоритета, SOL | покупок | билет, SOL | удержание, слотов | за ведущим | ведущие (раз за окно) |
|---|---|---|---|---|---|---|---|
| `bwamJzztZsepfkteWRChggmXuiiCQvpLqPietdNfSXa` | 13133.92 | 5510.314 | 1602 | 5.9259 | 11 | 0.008 | `4PkBsJpk` 8, `7WBNxvtP` 8, `8j5azKG3` 8, `wS9rKxir` 8, `7rRB4pFA` 7, `AYZryL5J` 7, `AdT2D4an` 7, `BPMpHRGV` 7 |
| `E4FPggw9yDp9PNJDuRoLYDr6a5HKCsAterw3fygLpZEA` | 3390.89 | 2529.412 | 580 | 4.9383 | 11.0 | 0.0 | — |
| `8ZN71XTdVo8yRovnGLmNgW3Tgniw6A4J3JGLvPD686FP` | 1638.15 | 1823.396 | 389 | 4.0 | 10 | 0.0 | — |
| `kEFiAX3jo5NmemysQov342TZ9mGh6yp92GDRjhA8XDf` | 49596.44 | 1688.848 | 8000 | 1.0441 | 84.0 | 0.505 | `ARu4n5mF` 2363, `2tgUbS9U` 1582, `BxMo47Jc` 1242, `7usDSTXm` 958, `5981EnT1` 955, `MriyaNN8` 686, `Ar2Y6o1Q` 661, `8JjVNyj6` 622 |
| `SQHK48QT8SY1vYN44iXji7wQ6CJek8AjfX6mBp47TZq` | 23689.92 | 1223.085 | 8000 | 1.1028 | 81 | 0.349 | `64hP97Bw` 1365, `DiJF3L37` 761, `omegoMAe` 618, `72NWWbch` 590, `2zMj6wNq` 551, `sssssDdM` 437, `HRBonvjq` 399, `GVVP8N7j` 333 |
| `5U1abDdg8CfRPRHpnMHiY7azkcwUZjP8Wj3fNmbXp3ys` | 6196.82 | 744.726 | 2541 | 2.7928 | 3.0 | 0.345 | `2J2rtMBR` 71, `ardinRsN` 63, `AMDEmVoc` 43, `CyaE1Vxv` 38, `4vw54BmA` 30, `CFMra9xV` 30, `YARSciE3` 27, `Bi4rd5FH` 25 |
| `88887QrRZPZmsstEXsoXXB8E7nbmUDde4Gp5s7jG3ENu` | 15611.23 | 708.776 | 8000 | 1.0 | 103 | 0.588 | `9999huSC` 1395, `5QY3MBvL` 763, `BDW8Bjoe` 559, `AmwJToZR` 501, `omegoMAe` 487, `dsegg9CX` 464, `sssssDdM` 447, `CCCCQCrL` 425 |
| `29yFzeBZgxf5zqrAkKXwgZtQehRf4pL8WbV2nRJikbw8` | 1407.04 | 702.373 | 472 | 3.0 | 8 | 0.0 | — |
| `DxhpC9c4kGkYJR34xUrfFeDx9vwb6yoHUMWrTCZybwtH` | 9268.98 | 682.687 | 5182 | 1.7778 | 8.0 | 0.349 | `2CQgjcdN` 222, `Fi9fjEmz` 192, `53DnQU1d` 150, `DsGJkPzF` 129, `AEgTuy8L` 128, `FiFawHqx` 104, `H8bgvrbb` 86, `AMDEmVoc` 81 |
| `EnU1CPeCqFtVnqnYhd1JgfcPrhcDx4ffLZEnV9TnjJ4g` | 1320.27 | 639.985 | 651 | 2.1728 | 28 | 0.96 | `E4FPggw9` 560, `3tqpS1u2` 55, `6Qa46jFT` 46, `GGCXeUae` 8, `CKpnHDYQ` 7, `54BKdQGW` 7, `EcSmhrdY` 7, `24VSRRkQ` 7 |
| `5981EnT1JLAfRqoBpn4jvd81ie5FXMgAJgkJnpYAsJs3` | 25511.2 | 620.736 | 7638 | 1.0072 | 36.0 | 0.658 | `ARu4n5mF` 1645, `7usDSTXm` 1246, `8JjVNyj6` 1044, `922M9fBT` 966, `g5HcBNwS` 934, `7y5aRdzh` 813, `BxMo47Jc` 752, `EaBRrMav` 683 |
| `Fx2e5CAfq7Ds9Kg7Yk6BDN2wTr2uHN7gTJvKZVLg5TyT` | 1096.34 | 618.74 | 615 | 1.4508 | 16.0 | 0.657 | `4GfKXzTF` 103, `CoCZxCTD` 98, `FRykLCqT` 76, `84RNkj2Q` 67, `9c1qCCs3` 51, `457D7HB5` 48, `EEmDBj9g` 37, `EFdojdDp` 37 |
| `7usDSTXmjAJzEV3AsT7UqG4oGWnMJ1VJH1FzhR3Sc6uV` | 20003.63 | 575.941 | 6705 | 1.0394 | 63.0 | 0.699 | `5981EnT1` 1843, `ARu4n5mF` 1551, `8JjVNyj6` 1130, `g5HcBNwS` 867, `922M9fBT` 815, `6ou4YPcD` 716, `Aceu1has` 686, `2tgUbS9U` 615 |
| `9keCU8mgA23XV8LCgSCJEo4Lspmo9fRBA3MyxJmZKQSp` | 8116.48 | 505.143 | 5164 | 1.9753 | 78.0 | 0.331 | `CSwo9FsN` 489, `HicFqmdY` 458, `EtZWfmdy` 168, `4F8hmnhN` 112, `HVE8996r` 92, `B1ZMARC6` 86, `FNvLdMfo` 77, `5xFhXswZ` 69 |
| `9KGv1vhepXtMeAxnvVf9wqEq5RbXaZioeZmPan4aDWcM` | 1177.65 | 485.385 | 219 | 4.1 | 10.0 | 0.114 | `B8zn7SyW` 16, `tAg2tgyH` 14, `7eTU6LbY` 12, `7wqrdPux` 11, `5kYkf8hw` 10, `5rFVvx9o` 9 |
| `4yFAz7dp5WwuZs3vbWKedbRiUmSFxTVGCdsLAxfEQrG3` | 3316.98 | 461.007 | 1390 | 2.1531 | 1.0 | 0.258 | `2CQgjcdN` 90, `2J2rtMBR` 21, `5U1abDdg` 17, `9LMeW9Sx` 17, `Etgb4nx8` 17, `5fMFE7rb` 16, `8YcbyX92` 16, `BddzswyZ` 16 |
| `FiFawHqxeTVBhv6YbqbLwDVuvokRpPUM1bNwAyxhGc6W` | 3962.53 | 449.033 | 2097 | 1.9357 | 10 | 0.159 | `49fAsUF3` 97, `7R8mfCnA` 34, `FLyBjfWG` 34, `GYieg7Sq` 34, `GT2Y6qZD` 33, `BCagckXe` 32, `5ZdusGZM` 24, `862hNEUq` 22 |
| `8RvtT8189KpAq5MkXGVHFn6LdZpxq9PkvDeAvs1g5Yj4` | 3287.27 | 403.903 | 1395 | 2.0697 | 2 | 0.242 | `2CQgjcdN` 84, `2J2rtMBR` 22, `BC7hKBso` 16, `7ZAv8MpN` 15, `9aXdFgVP` 14, `5U1abDdg` 13, `9eZU9vEv` 13, `2EsRiaPa` 13 |
| `89ooxuy4NTf3UNswCsjXx7fEd2WtJasaqUQbcrMUsPrm` | 1085.51 | 374.505 | 527 | 2.0854 | 2 | 0.865 | `AdWbHbP5` 139, `AdmwMEPV` 29, `DmRgL8wj` 28, `GXcujphw` 26, `Fs2qhtSd` 21, `HSwJ9b4M` 21, `3DrM4K8K` 19, `Cv9FhYDP` 15 |
| `FNvLdMfo2aB9xf14x4TFN3dr2TDpKz78Rji1k8bTx4bs` | 5261.03 | 333.479 | 1705 | 3.0 | 66.0 | 0.184 | `D4pycXUX` 141, `V4xHVomb` 75, `HicFqmdY` 28, `ARu4n5mF` 26, `7HmkKn8P` 24, `CSwo9FsN` 23, `DkudZy3Z` 12, `2kN7bb7s` 8 |
| `GG8hd6XKsDjLpviYEyt3EKaZs8WXmZ86uqVto6zVmsjt` | 3737.23 | 304.447 | 1194 | 2.963 | 8.0 | 0.853 | `3h65MmPZ` 94, `2CQgjcdN` 78, `GrCixK6b` 62, `4yo9CUuT` 59, `3ALh2uMT` 56, `EUvQT21t` 50, `5hAgYC8T` 49, `BsvjGudt` 48 |
| `6Qa46jFT7NegJRKNGPMQudr9v1aNC73KqFzX7vuvwRcC` | 978.51 | 284.848 | 653 | 1.5802 | 28 | 0.975 | `EnU1CPeC` 614, `E4FPggw9` 560, `3tqpS1u2` 69, `CWiNFAEk` 17, `AEgTuy8L` 9, `4iMb6hLj` 8, `G8gTguf7` 8, `EcSmhrdY` 7 |
| `J6ruBwRvV6hrwtHG2rPNUjFwikehz2nKEHSPR8vsnutP` | 844.7 | 266.597 | 494 | 2.1017 | 15.0 | 0.915 | `Fx2e5CAf` 304, `CpK1zVzp` 33, `8vQZrLsK` 30, `C3Q5S6xe` 28, `GmzJPDDk` 28, `BB4uUTDu` 25, `CenHCRNJ` 24, `KcBEDyQg` 22 |
| `8NY4eFowg7ESTFoeUtJJJuJwhiM919cXxckmy4CfSwVb` | 3030.62 | 231.633 | 1520 | 1.9753 | 2.0 | 0.607 | `EEHcVr9t` 141, `Akmwur3r` 116, `EDGEKeUN` 109, `HCsVftaM` 99, `E35uoEGT` 94, `9A7um9R3` 93, `6AHvixSi` 81, `nya666pQ` 79 |
| `3tqpS1u2mXkfiGSCKhaEZUeFQa2yfqmpmp85ZTBMZCjk` | 1099.53 | 225.965 | 652 | 1.7778 | 28 | 0.954 | `EnU1CPeC` 604, `6Qa46jFT` 593, `E4FPggw9` 560, `CWiNFAEk` 17, `AEgTuy8L` 10, `G8gTguf7` 8, `HCsVftaM` 7, `CKpnHDYQ` 5 |
| `Akmwur3r9WhfFpy6yFUuDtgbQ4X4QUuif7xS7JfMNsxy` | 2432.69 | 216.784 | 1146 | 1.9753 | 3.0 | 0.558 | `EEHcVr9t` 82, `8NY4eFow` 79, `EDGEKeUN` 77, `HCsVftaM` 61, `FmsUqgMj` 46, `vEM7CDGg` 45, `AJ5snxaH` 44, `9xhhrTFe` 43 |
| `2Xjizmu5FwVX36JyVGcWMDaNoQN1VWU25tz1TFk24nd7` | 391.41 | 168.25 | 271 | 1.395 | 477.5 | 0.649 | `BprdjjAz` 141, `GijFWw4o` 107 |
| `AdWbHbP5CJsMQC78hdsnueDYCA2J1jkdy2tzXA3VZGsD` | 645.45 | 164.938 | 340 | 2.0 | 9 | 0.229 | `89ooxuy4` 42, `B7v2xGaF` 35, `7DhxyVUU` 15, `A5hp2pzB` 15, `ARu4n5mF` 11, `5981EnT1` 7, `8JjVNyj6` 6, `HSwJ9b4M` 6 |
| `Dxis2F7p4LLbmA1XANAY6AVo4LQYoyqrgE42vRzXi9z8` | 297.04 | 143.256 | 103 | 2.4173 | 5 | 0.854 | `Ae8QE7nv` 29, `HCfSSxk3` 26, `Ho7VxRaB` 11, `3g7hgDGr` 11, `5Evyn77i` 9, `2QRNKeqq` 6, `8VkbbcXk` 6, `5wZeB3Hs` 5 |
| `Ae8QE7nvpZBAjAJQw8JAcMbBb29eFJ51G6UAGkBE9BD2` | 131.0 | 125.853 | 72 | 2.0 | 10.0 | 0.0 | — |
| `46qjgvZNdiLxCqJ6oMBf8NoA3dZtC3xuagLpfMLYG8f9` | 1355.5 | 120.564 | 359 | 4.0 | 25.0 | 0.992 | `8ZN71XTd` 355, `Cu1cXMvt` 168, `G3fk9Nyk` 167 |
| `5Evyn77ib4bxpKAMNvzoNDgLhBjFSLxeePaZpWeCqTWW` | 412.94 | 107.556 | 155 | 2.3354 | 6 | 0.935 | `Dxis2F7p` 57, `Ae8QE7nv` 50, `HCfSSxk3` 42, `9gYpkT6B` 35, `JC1pZZMK` 35, `3g7hgDGr` 21, `Ho7VxRaB` 17, `2QRNKeqq` 17 |
| `JC1pZZMKAFRLP3FmC2hWH8KfepRjBHxLE1MXmNM9ryb5` | 294.64 | 105.499 | 106 | 2.332 | 6.0 | 0.925 | `Ae8QE7nv` 38, `Dxis2F7p` 31, `HCfSSxk3` 30, `5Evyn77i` 25, `16MeFNrK` 14, `9gYpkT6B` 13, `3g7hgDGr` 12, `8VkbbcXk` 6 |
| `GtsZdFAWMirv7akKP5Vx55M4c4sZLvTgnzcdebedGNWy` | 803.38 | 100.153 | 222 | 2.4691 | 4.0 | 0.527 | `2CQgjcdN` 23, `5B52w1ZW` 17, `2mqrindM` 11, `HPjJ1AwV` 10, `jhFR5Az8` 10, `4yFAz7dp` 9, `9RG3YAui` 9, `7RB7CqR5` 7 |
| `92cwTJoGDFKSnp6Zr2taFr3sFWqs8jRvtU73qbut2win` | 611.67 | 98.943 | 59 | 9.0 | 15 | 0.0 | — |
| `BAk7jy4pPS9u4tBsbGWVJstrbami8UuxQtRktVFnfx9k` | 2578.79 | 95.133 | 1479 | 1.0226 | 91.5 | 0.253 | `EFzoqYkN` 129, `UCLXpESE` 57, `ARu4n5mF` 43, `EeXvxkcG` 16, `4HSvNQnb` 13, `7usDSTXm` 13, `4dmtSBr4` 12, `8TVKncCu` 12 |
| `tAg2tgyHmkGTsmq8wBSKsGvUUgoH37cxmCfRUZSXdtB` | 3848.33 | 90.361 | 637 | 4.4978 | 25.0 | 0.14 | `9KGv1vhe` 17, `B8zn7SyW` 11, `MriyaNN8` 11, `5kYkf8hw` 10, `7eTU6LbY` 10, `7wqrdPux` 8, `ARu4n5mF` 7, `5p2bA4wz` 7 |
| `8vQZrLsKuZDtY3fWowqGz4LNtFeEVDE9m6mg7zc6Z6hQ` | 132.14 | 84.855 | 56 | 2.0 | 9 | 0.0 | — |
| `HCfSSxk3jXuQmhSAenFLZuNdo7dHYpDhqoSeGQhu8Spn` | 108.57 | 75.553 | 63 | 2.0 | 10 | 0.0 | — |
| `AyjjfuioEs341LrFaivCS6iPV9dXPyP4qCm4Fe2PRCPX` | 171.88 | 74.602 | 76 | 2.2595 | 2.0 | 0.421 | `DgBoXPU6` 17, `ByS7vqwu` 9, `7xFKhU14` 6 |
| `J5t48c3uwLN69tRcF2fUBFjCez7JrzmegcGGTgs3nfdk` | 443.87 | 71.804 | 119 | 3.0 | 18.0 | 0.294 | `7mGZh58m` 21, `5oeoKmts` 21, `Fz6ZLemd` 21, `9qcHMAe2` 19, `821JhhqD` 18 |
| `9gYpkT6B9YZcXXkpN1zkLgtBFgzK23pzHwEq8DaoG3Jx` | 313.08 | 67.192 | 122 | 2.2934 | 7 | 0.943 | `JC1pZZMK` 54, `5Evyn77i` 53, `Ae8QE7nv` 43, `HCfSSxk3` 30, `Dxis2F7p` 24, `2QRNKeqq` 22, `3g7hgDGr` 13, `Ho7VxRaB` 11 |
| `DKvJD5BYRE1nNVULwst1T8hTvEgs8HP7TnGeM2G9dwqQ` | 3969.3 | 54.823 | 3129 | 1.0 | 3.0 | 0.773 | `2FdJD3EV` 534, `FB5jAs5Q` 446, `Cs4zpkT9` 332, `Dnq77xMN` 294, `mATtYiy3` 248, `98XhbDKZ` 246, `AuNYDxqL` 241, `ARu4n5mF` 218 |
| `8TVKncCuwQGDTSQZiPxXtNHFE2QMn6KDLez49mcndhxv` | 624.22 | 49.635 | 551 | 1.2148 | 30.0 | 0.848 | `EdNcBDUF` 288, `6f9KuZVZ` 133, `6jTpjNsc` 130, `5U1abDdg` 8, `EeXvxkcG` 5, `7qaQFEd4` 5 |
| `2QRNKeqqRgkdMFdVJLNhCo5cM6MSwGQdiBjJDaP7VUcD` | 208.46 | 41.345 | 85 | 2.3247 | 6 | 0.965 | `5Evyn77i` 57, `Dxis2F7p` 36, `HCfSSxk3` 25, `Ae8QE7nv` 22, `Ho7VxRaB` 14, `9gYpkT6B` 14, `3g7hgDGr` 12, `JC1pZZMK` 8 |
| `CkLT4ADywSJkXPhwjfnQSd2ujWv8eb5FLKxN1FejvELr` | 681.52 | 38.582 | 312 | 2.0991 | 4.0 | 0.497 | `AMDEmVoc` 38, `AEgTuy8L` 21, `apexpDoP` 18, `2CQgjcdN` 18, `53DnQU1d` 13, `DxhpC9c4` 13, `G8gTguf7` 13, `DsGJkPzF` 10 |
| `FFFz6LeDp41sqe5VusaLH7V2HFhidCnqTiYQhE6V2Zco` | 557.37 | 37.714 | 115 | 4.8889 | 11.0 | 0.661 | `6ANGS6SS` 70, `G8gTguf7` 8, `CWiNFAEk` 7, `HCsVftaM` 5 |
| `7HmkKn8P6aZSMg7jn9esjdPQ9cBqg9LQgNECzoDeJiGQ` | 970.32 | 31.315 | 150 | 6.4198 | 56 | 0.533 | `5t4Tz7qe` 37, `D4pycXUX` 21, `8X5pJ2J3` 16, `8D8FmQE2` 14, `FNvLdMfo` 12, `2kN7bb7s` 10, `FYbJyijr` 5 |
| `ENGkTtUanFtqD5avHk5jJARwZNz5DX1AZ8TGQqASw8CT` | 205.64 | 30.808 | 158 | 1.1021 | 168 | 0.886 | `HSeCG7T2` 110, `6QGRJVuA` 101, `5m1GCXJq` 100, `BAr5csYt` 19, `AcgXgEfi` 12, `CieVs3Te` 11, `HaNmBDom` 10, `6Eegkyd2` 9 |
| `C9s6hwgurdYfmnCNgdRou44ihPcssZmu7X45wBQ3y5t3` | 374.42 | 28.943 | 227 | 1.5061 | 262.0 | 0.454 | `2kv8X2a9` 97, `ARu4n5mF` 6, `GVVP8N7j` 5, `sssssDdM` 5 |
| `7mGZh58meFf8xsES37bc2hdffEmh8aTDcB2i725Zivyg` | 454.41 | 28.071 | 167 | 1.5747 | 61 | 0.701 | `9qcHMAe2` 92, `J5t48c3u` 27, `Fz6ZLemd` 26, `5oeoKmts` 22, `821JhhqD` 18, `B9kJYdzb` 9, `DZAa55Hw` 9, `2h6WT2yE` 8 |
| `CBcNrPBRRiMgihLCqbjfUDJGepyeYoeP5B8HrH2PD5vy` | 204.32 | 25.149 | 119 | 1.3136 | 76.0 | 0.395 | `BugPnPKi` 25, `EqL7Tdci` 22, `6y4Tpshh` 9 |
| `BugPnPKiTGTJq6ki6Kt9PChhyAyqbKHCTDdcyPpRfJ1M` | 179.57 | 24.917 | 113 | 1.2642 | 94.5 | 0.504 | `CBcNrPBR` 39, `EqL7Tdci` 10, `6y4Tpshh` 10, `pK4Fa1TM` 9 |
| `EM1ZyNA3g4rr9MBzSEA36QqWd7t7cp54zDidm3YskyeU` | 1757.26 | 24.306 | 1297 | 1.2781 | 64 | 0.763 | `J2L495ZP` 758, `PMJA8UQD` 644, `BiQPccjN` 38, `5U1abDdg` 32, `ARu4n5mF` 31, `EDGEKeUN` 27, `nya666pQ` 20, `64hJxoZz` 14 |
| `6QGRJVuAGg9AGLLSf5HvFd7KCYV1Hfb43xkGjo8eHcYx` | 205.24 | 22.642 | 158 | 1.1088 | 146.0 | 0.829 | `5m1GCXJq` 107, `HSeCG7T2` 102, `ENGkTtUa` 93, `BAr5csYt` 17, `AcgXgEfi` 13, `GG8hd6XK` 12, `HaNmBDom` 10, `CieVs3Te` 8 |
| `CFMra9xVXTyPJBcFvgsTo1U3RmM4PrjmMw1taiy6QUAd` | 149.23 | 12.062 | 120 | 1.3254 | 30 | 0.225 | `4mugTfk3` 26, `AJ4gVBsc` 26, `CUHXeExx` 26, `6iDTRg5J` 8, `4Fgyz6ux` 7, `AMDEmVoc` 5, `BLm7PT4i` 5, `2CQgjcdN` 5 |
| `5oeoKmtsuDv29B33bPFihEcTN3X9GE3R7Jf4R98rF4ch` | 156.13 | 2.803 | 104 | 1.67 | 43.5 | 0.913 | `J5t48c3u` 84, `7mGZh58m` 21, `Fz6ZLemd` 21, `9qcHMAe2` 19, `821JhhqD` 17 |
| `8xjSAk3p7zvp2HphRmMXK65q9BfAYfLticFzRUkHaJu1` | 2659.77 | 2.603 | 1589 | 1.0973 | 16.5 | 0.784 | `5fCsf3UZ` 140, `BbC38fPT` 136, `HX6turSS` 134, `HJzaC8kp` 130, `AqFJkRcJ` 124, `EUK7VSfy` 118, `4wkzxPB2` 118, `HX6HmEgW` 115 |
| `HX6turSSAdQzQs7GPRSKwtqat5VYeWtAm71Jy9PjgTth` | 3423.82 | 2.422 | 1819 | 1.1714 | 24.0 | 0.733 | `HJzaC8kp` 135, `F8WZGj48` 130, `AqFJkRcJ` 129, `AJaRDhFe` 128, `HX6HmEgW` 128, `4FUzhtwz` 127, `5fCsf3UZ` 123, `57YYnFt2` 117 |
| `E1SC85tHSc9PVznau421HE2pQWGv81b6UpTGAHeMwZJz` | 2116.79 | 1.658 | 1497 | 1.1033 | 18.0 | 0.781 | `555AbGZ6` 104, `4wkzxPB2` 99, `7qYKzox8` 97, `4FUzhtwz` 97, `8xjSAk3p` 96, `HX6turSS` 96, `B9P3rFpE` 95, `9bQAATVq` 95 |
| `3nWLLAFpo8NN86WK1KX5fc1iPCUcvgqUN27Qthfcz4tN` | 2535.42 | 0.763 | 1512 | 1.1084 | 11.5 | 0.776 | `4wkzxPB2` 124, `AqFJkRcJ` 118, `HX6turSS` 115, `9v5rqhwZ` 114, `C1qyS19J` 109, `HJzaC8kp` 104, `E1SC85tH` 103, `8xjSAk3p` 102 |
| `HTrUmYyHJqyVv73hYSEtgFNFPNGzb22rMdxBqdXMxdA5` | 896.5 | 0.078 | 264 | 3.3877 | 71 | 0.989 | `6gAgy3F1` 261 |
| `5fCsf3UZ3yzGr8bo65gpva5BfG69PaukrxcdmEHcZUqz` | 3717.9 | -0.275 | 1778 | 1.3677 | 11 | 0.709 | `HX6turSS` 128, `BbC38fPT` 124, `9v5rqhwZ` 121, `GyzRWe28` 121, `F8WZGj48` 118, `4wkzxPB2` 113, `HX6HmEgW` 112, `AqFJkRcJ` 110 |
| `H4dxJQN6DRqCPva9yaX2Nt3mAkvgo7pyQZr93fLke1U5` | 219.11 | -3.399 | 167 | 1.1262 | 44.5 | 0.353 | `3VwmH9qY` 42, `4k92XBen` 20, `VTd8fTsC` 13, `VJSDW6S7` 5 |
| `794HyV3SyKY3HoCitpytgJQJk5c6nqyTntBXFVQa8pA1` | 3513.16 | -3.684 | 1984 | 1.3433 | 15 | 0.707 | `GyzRWe28` 106, `HX6turSS` 101, `C1qyS19J` 101, `4FUzhtwz` 100, `HJzaC8kp` 99, `5fCsf3UZ` 98, `6TdieMMN` 97, `BbC38fPT` 97 |
| `Fz6ZLemdq3J7Fn24t9XZjT91bH4j8ZdPxnVSPoa9MKXt` | 158.69 | -4.299 | 105 | 1.55 | 45 | 0.952 | `5oeoKmts` 87, `J5t48c3u` 84, `9qcHMAe2` 19, `7mGZh58m` 18, `821JhhqD` 17 |
| `Gg3ska7ScdMZX5nPgsM7wBfprRhNp6cuS2zuG7UFXcDv` | 1100.41 | -5.138 | 450 | 2.58 | 54.0 | 0.978 | `29yFzeBZ` 412, `GTnbqqrY` 27, `7CvsrUrc` 5 |
| `Aa9mpEB7GrAs4H2J4tydFUyLvvnMwoeJRpRmmp9y57f5` | 2460.46 | -5.424 | 1569 | 1.1815 | 23.5 | 0.784 | `BbC38fPT` 128, `HX6turSS` 122, `AqFJkRcJ` 119, `HGmudUwt` 119, `4FUzhtwz` 113, `HX6HmEgW` 112, `HJzaC8kp` 112, `9bQAATVq` 107 |
| `821JhhqD2fA7mEvMpiHUuqmHhGFiuFRKPsCCdEnoVpDo` | 92.89 | -11.506 | 65 | 1.8 | 50.5 | 0.892 | `Fz6ZLemd` 51, `5oeoKmts` 51, `J5t48c3u` 48, `7mGZh58m` 11, `9qcHMAe2` 10 |
| `J2L495ZPcJ6Btuyd4YqXz5uw7hpKiQXGcLvALwumabED` | 1737.57 | -15.651 | 1293 | 1.2874 | 64 | 0.72 | `EM1ZyNA3` 663, `PMJA8UQD` 607, `BiQPccjN` 36, `5U1abDdg` 33, `ARu4n5mF` 29, `EDGEKeUN` 27, `nya666pQ` 20, `sssssDdM` 14 |
| `Cu1cXMvtXdEafDoPjtvjxgvyXHAK7Yh8AFvuKUVDy6yG` | 1355.44 | -17.571 | 359 | 4.0 | 25.0 | 0.994 | `8ZN71XTd` 357, `G3fk9Nyk` 347, `46qjgvZN` 191 |
| `GTnbqqrYg8tv9gsievtfDGxMi9FVGikNu3HiSxb4Wukr` | 1019.68 | -132.484 | 441 | 2.44 | 62 | 0.971 | `Gg3ska7S` 427, `29yFzeBZ` 406, `7CvsrUrc` 5 |

## Где в этом ряду 54ua

- `54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE` (54ua) -- **место 31 из 1914** копировщиков по итогу после приоритета: 188.636 SOL, оборот 987.15 SOL, покупок 200, билет 3.9506 SOL, удержание 10 слотов, за ведущим 0.435, ведущих 9 -- в таблице A он 31-й.
- `BomGAZnAGwnjs3oaqNHm4Wk5sKctQi83PKVxjRuGGbrm` (BomG) -- в сито недели не попал, в этот ряд не входит.

