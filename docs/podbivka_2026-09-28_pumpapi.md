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
## 6. Сделки полосы 27.09 15:10–15:30 (час архива 27.09 15)

Источники: 6 из 6 в архиве (все pump.fun buy, слот совпадает). Наши севшие покупки (lane_landed_signature или
lane_signature, если она и села): 4 из 4 проверенных; наши продажи (jup_signature): 5 из 5. Подписи из журнала,
которые не сели (другой вариант той же сделки), в архиве нет -- как и должно быть.

## 7. Итог: что даёт / чего нет / глубина / рекомендация

| вопрос | что даёт | чего нет | глубина | вывод |
|---|---|---|---|---|
| архив как история сделок | каждая сделка покрытых пулов: signature, txSigner, block, poolId, mint, quoteMint, суммы, резервы после сделки (для кривой -- виртуальные и реальные), poolFeeRate, priorityFee, timestamp мс; наших покупок 25.09 найдено 98 %; резерв котировки после сделки совпал с нашим (m4) в 341 из 349 до 0.1 % | индекса в блоке (порядок внутри слота -- только по timestamp мс, оценка); чаевых Jito отдельным полем; пулов Raydium CLMM, AMM v4, Orca Whirlpool как событий пула | с 18.04.2026, по часам; ~400 МБ сжатых на час, час разбирается облаком за ~20 с | годится как источник истории пулов для режима 1 |
| симулятор режима 1 на архиве | состояние пула после каждого свопа без листания истории пула через RPC -- снимает отказы «история длиннее 150 страниц» и нехватку Helius для старых дат; час файла на все покупки этого часа | порядок внутри слота (S+0 «сразу за ним» -- по timestamp, оценка); свопы s0+1/s0+2 совпали с нашей историей в 79 %, архив меньше в 136 из 710 -- причина не выяснена | как архив | рекомендую: пересчитать режим 1 на архиве для пулов pump / pump-amm / raydium-cpmm / raydium-launchpad / meteora-*, сверить с m4 на тех же покупках |
| симулятор режима 2 | нога токена в покрытых пулах (CPMM, LaunchLab, Pump AMM) | ноги SOL↔котировка часто в CLMM / Whirlpool / DLMM-агрегаторах: CLMM и Whirlpool в архиве нет | как архив | частично; курс котировки -- по-прежнему из цепи |
| поток wss://stream.pumpapi.io | ~750 событий/с, те же поля, что архив; задержка приёма к timestamp события на раннере GitHub (не Франкфурт): медиана 56 мс (часы не сверены -- оценка) | индекса в блоке; 1 соединение на IP; фильтр только на клиенте | -- | как второй фид оценивать на машине Франкфурта против нашего детектора -- это сторона Code-1; с раннера задержку относительно цепи не измерить |
| наши кошельки в архиве | покупок 133 за те же часы в архиве больше, чем в наших файлах: из «нет у нас» 1962 -- кошельки без файла скана, остальное -- покупки за другой котировочный токен и мелкие (< 2 SOL) покупки, которые скан не считает покупками от 2 SOL | -- | -- | архив может заменить скан кошельков источников (все покупки за день -- один проход по 24 файлам) |
