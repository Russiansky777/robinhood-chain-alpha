# Подбивка: PumpApi -- архив и поток против наших данных

Скачано часов архива: 27 (имя файла -- час начала; проверено по timestamp событий). Наших подписей в этих часах: 10477, найдено в архиве: 10260.

## 1. Покрытие наших сделок

| программа пула (наша) | найдено / всего | доля |
|---|---|---|
| не известна (без симулятора) | 8009 / 8200 | 98% |
| Pump AMM | 668 / 674 | 99% |
| кривая pump.fun | 510 / 515 | 99% |
| Raydium LaunchLab | 404 / 409 | 99% |
| Raydium CPMM | 384 / 387 | 99% |
| Meteora DAMM v2 | 107 / 107 | 100% |
| Meteora DLMM | 61 / 61 | 100% |
| Raydium CLMM | 38 / 40 | 95% |
| Meteora DBC | 35 / 35 | 100% |
| Orca Whirlpool | 18 / 21 | 86% |
| Raydium AMM v4 | 15 / 15 | 100% |
| прочие | 11 / 13 | 85% |

| группа | найдено / всего |
|---|---|
| наши 133 | 8329 / 8524 |
| 543 | 1931 / 1953 |

Найденные по pool / action архива: pump-amm/buy 3600, pump/buy 1816, raydium-cpmm/buy 1155, raydium-launchpad/buy 1017, meteora-dlmm/buy 731, raydium-cpmm/sell 654, meteora-damm-v2/buy 454, None/transfer 276, meteora-dlmm/sell 151, meteora-launchpad/buy 122, raydium-launchpad/sell 114, pump/sell 52, pump/create 47, pump-amm/sell 34, meteora-damm-v2/sell 25, raydium-launchpad/create 6, meteora-damm-v1/buy 2, meteora-launchpad/sell 2, pump-amm/migrate 1, meteora-damm-v1/sell 1

## 2. Резерв котировки после сделки источника: архив (quoteInPool) против m4

Пар: 349. Отклонение архива от m4, %: медиана +0.000, p10 +0.000, p90 +0.034; |отклонение| < 0.1 %: 341 из 349.

## 3. Свопы пула в слотах s0+1 и s0+2: архив против m4 (наша история пула)

Сравнений: 710; совпало: 562 (79%); разница архив − m4: медиана +0.0, архив больше в 12, меньше в 136.

## 4. Покупки наших 133 в архиве против наших файлов (те же часы)

Покупок наших 133 в архиве (txSigner -- наш, action buy): 11358; из них в наших файлах: 6776; нет у нас: 4582 (по pool: pump 2027, pump-amm 1097, raydium-launchpad 714, raydium-cpmm 583, meteora-dlmm 100, meteora-damm-v2 45).
Наших покупок 133 в этих часах: 8524; в архиве: 8329.

## 5. Поток wss://stream.pumpapi.io (раннер GitHub Actions, не Франкфурт)

451393 событий за 600 с (752/с); ошибка: None. По pool / action: -|transfer 210709, pump-amm|buy 93797, pump-amm|sell 52884, meteora-damm-v2|buy 19942, pump|buy 14983, meteora-damm-v2|sell 14900, pump|sell 13634, meteora-dlmm|sell 5959, meteora-dlmm|buy 5785, pump|distributeFees 4294.
Задержка приёма к timestamp события (каждое 50-е, 9027 шт.), мс: медиана 56, p10 55, p90 61. Часы раннера и сервера не сверены -- оценка.
Поля событий: action, addressLookupTables, baseFeeMode, binStep, block, breakdown, burnedLiquidity, cashbackEnabled, creatorFeeAddress, creatorFeeShare, creatorLockedLiquidityAfterMigration, creatorMigrationFeeShare, creatorUnlockedLiquidityAfterMigration, creatorVestingDuration, creatorVestingTokens, curveType, decimals, dynamicFeeEnabled, feeAmount, feeCollectedIn, feeMint, feeScheduleDuration, feeScheduleUnit, firstSwapWithMinFee, fixedSupply, freezeAuthority, holderRewardsRate, initialBuy, initialMarketCap, initialPrice, launchpadConfig, leftoverReceiver, leftoverTokens, liquidityVestingDuration, lockedLiquidityAfterMigration, marketCapQuote, maxPrice, mayhemMode, metadataAuthority, migratesTo, migrationFee, migrationMarketCap, migrationPrice, migrationThresholds, minPoolFeeRate, minPrice, mint, mintAuthority, mintAuthorityRetained, name, platform, platformFeeAddress, platformLockedLiquidityAfterMigration, platformUnlockedLiquidityAfterMigration, pool, poolCreatedBy, poolCreationFee, poolFeeRate, poolFeeRateAfterMigration, poolId, postBalances, price, priorityFee, programsUsed, quoteAmount, quoteInPool, quoteMint, quoteSupply, recipients, signature, supply, supplyAfterMigration, symbol, timestamp, tokenAmount, tokenExtensions, tokenProgram, tokensInPool, tokensInPoolAfterMigration, tokensSoldBeforeMigration, tradersInvolved, transferHookProgram, transfers, txSigner, type, uri, vQuoteInBondingCurve, vTokensInBondingCurve, vestedLiquidityAfterMigration, virtualQuoteInPool, virtualTokensInPool.
