# Котировки денежных групп 28–30.09 (архив PumpApi)

Адреса групп: lane_s0 10, batch5 12, leader 1 (data/sources_2026-09-25.json ветки Code-1, копия 28.09) и cand1 9 (data/gruppa_cand1.json, применено 30.09 21:02Z; считается за все трое суток). Окно 28.09 00:00Z → 30.09 24:00Z; сутки в архиве: 2026-09-28, 2026-09-29. Сигнал -- правило pyg7: первая покупка пары адрес + минт (нет покупки того же минта этим адресом в предыдущие 1800 слотов, любого размера), от 2 SOL-экв.; промежуточные ноги маршрута (SOL → USDC → токен: покупка USDC) и покупки самих WSOL / USDC / USDT не считаются. SOL-экв. -- quoteAmount × цена котировки в SOL по последней сделке архива пары котировка / WSOL. Тип пула -- по архиву PumpApi (Orca Whirlpool, Raydium AMM v4 и редких программ в архиве нет; 27.09 это ~2 % не-SOL покупок групп). Сбор -- analysis/podbivka_kotirovki_arhiv.py (бегунок lab-miami, без ключей), данные -- data/podbivka/kotirovki_grupp_2026-10-01.json. Ничего не рекомендуется.

## Итог (порог владельца: ≥ 1 в сутки USDC на других типах -- строим склейку)

| строка | за 3 суток | в сутки | по суткам | по группам |
|---|---|---|---|---|
| USDC на CPMM или Pump AMM | 4 | 1.3 | 09-28: 1, 09-29: 3, 09-30: 0 | cand1 3, batch5 1 |
| USDC на других типах | 18 | 6.0 | 09-28: 11, 09-29: 7, 09-30: 0 | leader 6, batch5 6, lane_s0 3, cand1 3 |
| GP / CRACKER всего | 0 | 0.0 | 09-28: 0, 09-29: 0, 09-30: 0 | — |

Сигналов всего (все котировки): 768 (256.0 в сутки); из них не WSOL: 221. Покупок без цены котировки в SOL (нет пары с WSOL в архиве к этому моменту) -- не оценены: 48.

## Котировка × тип пула × группа

| котировка | минт | тип пула | группа | n за 3 суток | в сутки |
|---|---|---|---|---|---|
| SPCXx | `Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8` | raydium-launchpad | cand1 | 9 | 3.0 |
| SPCXx | `Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8` | raydium-cpmm | cand1 | 8 | 2.7 |
| STONK | `6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx` | raydium-cpmm | leader | 7 | 2.3 |
| PUMP | `pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn` | pump | cand1 | 7 | 2.3 |
| NVDAx | `Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh` | raydium-cpmm | lane_s0 | 7 | 2.3 |
| CARDS | `CARDSccUMFKoPRZxt5vt3ksUbxEFEcnZ3H2pd3dKxYjp` | pump-amm | cand1 | 6 | 2.0 |
| SPCX | `SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb` | pump | cand1 | 6 | 2.0 |
| NVDAx | `Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh` | raydium-launchpad | cand1 | 6 | 2.0 |
| PUMP | `pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn` | pump-amm | cand1 | 5 | 1.7 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | raydium-clmm | batch5 | 5 | 1.7 |
| другое | `DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP` | raydium-launchpad | cand1 | 5 | 1.7 |
| NVDAx | `Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh` | pump | cand1 | 4 | 1.3 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | raydium-clmm | leader | 4 | 1.3 |
| CARDS | `CARDSccUMFKoPRZxt5vt3ksUbxEFEcnZ3H2pd3dKxYjp` | raydium-launchpad | cand1 | 4 | 1.3 |
| DJT | `DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF` | pump-amm | cand1 | 4 | 1.3 |
| другое | `NFLX7qV57zuVxCoHy3s1jiGZyALraLNwttbxdvmYJLJ` | raydium-cpmm | cand1 | 4 | 1.3 |
| STONK | `6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx` | raydium-cpmm | lane_s0 | 3 | 1.0 |
| другое | `N7Q5fYX7YRnDQksfdBKnoUb3awm92n7QNAD35X3Rq1X` | raydium-cpmm | leader | 3 | 1.0 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | raydium-clmm | cand1 | 3 | 1.0 |
| другое | `527PdUTGwcFxVEMXt8tyRJA1nYbVedgSiSfh4s2LWTWz` | raydium-cpmm | cand1 | 3 | 1.0 |
| NVDAx | `Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh` | raydium-cpmm | batch5 | 3 | 1.0 |
| другое | `RKLBnAXGqv31iZomqsuAWkQm1aqC7JwwvbCfzGdqAhz` | pump-amm | batch5 | 3 | 1.0 |
| NVDAx | `Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh` | pump-amm | cand1 | 2 | 0.7 |
| STONK | `6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx` | raydium-cpmm | batch5 | 2 | 0.7 |
| PUMP | `pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn` | pump-amm | batch5 | 2 | 0.7 |
| HIMS | `HiMSSzzwkZkrXJ4PGVJRdtfLaANeAztjjcgk5Dxe7Lwx` | pump-amm | cand1 | 2 | 0.7 |
| SPCXx | `Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8` | raydium-cpmm | lane_s0 | 2 | 0.7 |
| BOT | `BoTx8y9ynfdxf5ZjWtCoBVkff52qKA82ysaLU8ZM6d8T` | raydium-cpmm | leader | 2 | 0.7 |
| GOOGLx | `XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN` | pump | cand1 | 2 | 0.7 |
| другое | `cbADAmv9issuPfhFwyQG3xac4DGPd1LDSt1oz7vwJsg` | raydium-cpmm | cand1 | 2 | 0.7 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | meteora-dlmm | leader | 2 | 0.7 |
| DJT | `DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF` | pump | cand1 | 2 | 0.7 |
| другое | `A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump` | pump-amm | cand1 | 2 | 0.7 |
| METAx | `Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu` | pump-amm | cand1 | 2 | 0.7 |
| SPYx | `XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W` | raydium-launchpad | cand1 | 2 | 0.7 |
| DJT | `DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF` | raydium-launchpad | cand1 | 2 | 0.7 |
| NVDAx | `Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh` | raydium-cpmm | cand1 | 2 | 0.7 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | pump-amm | cand1 | 2 | 0.7 |
| METAx | `Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu` | raydium-cpmm | leader | 1 | 0.3 |
| METAx | `Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| другое | `7gYKUXFqbshpydGN6EQyMHUkeigak2J9MbfKCKyTWH73` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| MGM | `MGMuubtUEirmkhfEQdmGUh4pr7HuUdMWcZXFtpPbVJD` | raydium-cpmm | leader | 1 | 0.3 |
| MGM | `MGMuubtUEirmkhfEQdmGUh4pr7HuUdMWcZXFtpPbVJD` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| PEPE | `PEPEqnuuCDbBC89p1u9vpnP1KQ2oj1xTcQBsjt9X55m` | pump | cand1 | 1 | 0.3 |
| GLDx | `Xsv9hRk1z5ystj9MhnA7Lq4vjSsLwzL2nxrwmwtD3re` | raydium-launchpad | cand1 | 1 | 0.3 |
| другое | `4UHmZGe6X4DZ5dxYGiGXMhi3Sp34uPWtHB7qDMyvRbYB` | raydium-cpmm | batch5 | 1 | 0.3 |
| другое | `9GEUA6pXNmf1c8nujWRoJDq17K8gfMyWBNp5d9cfw8xG` | raydium-cpmm | batch5 | 1 | 0.3 |
| tOpenAI | `oPAiAikWTaFj9RYoRFD35ccfwhnMcB3ThgBZRHSkjTZ` | raydium-launchpad | lane_s0 | 1 | 0.3 |
| NEAR | `3ZLekZYq2qkZiSpnSvabjit34tUkjSwD1JFuW9as9wBG` | raydium-cpmm | batch5 | 1 | 0.3 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | raydium-cpmm | cand1 | 1 | 0.3 |
| другое | `SKHYhSjuRWHgikq8eRKbtBbpABgJSkd7ytQV14i9EQ3` | pump-amm | batch5 | 1 | 0.3 |
| другое | `7gYKUXFqbshpydGN6EQyMHUkeigak2J9MbfKCKyTWH73` | raydium-cpmm | leader | 1 | 0.3 |
| другое | `N7Q5fYX7YRnDQksfdBKnoUb3awm92n7QNAD35X3Rq1X` | pump-amm | cand1 | 1 | 0.3 |
| другое | `N7Q5fYX7YRnDQksfdBKnoUb3awm92n7QNAD35X3Rq1X` | raydium-cpmm | batch5 | 1 | 0.3 |
| DJT | `DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF` | raydium-cpmm | cand1 | 1 | 0.3 |
| другое | `ZesMGYmokFiEuDvNzWeMhB7jxF6eUW8c512vwSKSTNK` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| другое | `N7Q5fYX7YRnDQksfdBKnoUb3awm92n7QNAD35X3Rq1X` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| другое | `7XiFJgX4nkER8gTH1LXUX1VEBJrLRSZfFXuzDy8ccWGT` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| WBTC | `3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh` | pump-amm | cand1 | 1 | 0.3 |
| другое | `A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump` | pump | cand1 | 1 | 0.3 |
| INJ | `1NJMqVM4PadjuzYmeB7zV7q7DV8oB3ExaQCd9x6KsLz` | raydium-cpmm | cand1 | 1 | 0.3 |
| CARDS | `CARDSccUMFKoPRZxt5vt3ksUbxEFEcnZ3H2pd3dKxYjp` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | raydium-clmm | lane_s0 | 1 | 0.3 |
| CARDS | `CARDSccUMFKoPRZxt5vt3ksUbxEFEcnZ3H2pd3dKxYjp` | raydium-cpmm | cand1 | 1 | 0.3 |
| другое | `C1mBfBoDkwWfd6uTFZp62ARHLjeVp3bDpCDMfMZtPngE` | meteora-launchpad | lane_s0 | 1 | 0.3 |
| GMEx | `Xsf9mBktVB9BSU5kf4nHxPq5hCBJ2j2ui3ecFGxPRGc` | pump-amm | cand1 | 1 | 0.3 |
| другое | `7XiFJgX4nkER8gTH1LXUX1VEBJrLRSZfFXuzDy8ccWGT` | raydium-cpmm | leader | 1 | 0.3 |
| ZEC | `A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS` | pump | cand1 | 1 | 0.3 |
| PURR | `8RNUw4N655VSrZKuhGdywhbSMDTrheguFPfxbpE2NZHQ` | raydium-cpmm | leader | 1 | 0.3 |
| ZEC | `A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS` | pump-amm | cand1 | 1 | 0.3 |
| GMEx | `Xsf9mBktVB9BSU5kf4nHxPq5hCBJ2j2ui3ecFGxPRGc` | pump | cand1 | 1 | 0.3 |
| другое | `SH55hfaipFAbwT42nQYhRoM5o5t61QpkmJ6p62vXB3m` | pump | cand1 | 1 | 0.3 |
| HYPE | `98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g` | raydium-launchpad | cand1 | 1 | 0.3 |
| DOGE | `DoGEV7LASBkQbibMc5k5vKnTZoMg423GpJ5QtJEGfm7R` | pump-amm | cand1 | 1 | 0.3 |
| другое | `vAddewUHuYgRDrL6n3kXtBXMT635evJz8K93j6Wm1Ba` | raydium-clmm | leader | 1 | 0.3 |
| другое | `CTPoyCwkjMvoJwU4xvZZqoD8tiYk6yDchySiN5gGpump` | raydium-launchpad | lane_s0 | 1 | 0.3 |
| другое | `HGN8K3x5ZLfYNjvE8b1hCqqu7RwupuKU6EkEhemzpump` | meteora-launchpad | cand1 | 1 | 0.3 |
| WBTC | `3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh` | pump | cand1 | 1 | 0.3 |
| LMAO! | `H74CYmXgMkYHYuSRsZt6RJb4NYp2u72Vw8BS5huApump` | pump | cand1 | 1 | 0.3 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | meteora-dlmm | batch5 | 1 | 0.3 |
| WBTC | `3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh` | raydium-launchpad | cand1 | 1 | 0.3 |
| cbLTC | `cbLTC4T5NpzSUtQ7ekgEMZGaUPVJY1ko6BUikqa4gGf` | pump-amm | cand1 | 1 | 0.3 |
| OPENAI | `PreweJYECqtQwBtpxHL171nL2K6umo692gTm7Q3rpgF` | raydium-launchpad | cand1 | 1 | 0.3 |
| XMR | `WXMRyRZhsa19ety5erZhHg4N3xj3EVN92u94422teJp` | pump | cand1 | 1 | 0.3 |
| QQQx | `Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ` | pump-amm | cand1 | 1 | 0.3 |
| XMR | `WXMRyRZhsa19ety5erZhHg4N3xj3EVN92u94422teJp` | raydium-cpmm | batch5 | 1 | 0.3 |
| GOOGLx | `XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| MSFTx | `XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX` | raydium-cpmm | cand1 | 1 | 0.3 |
| другое | `4yqVrQt7Uvie3Dmtji3p7CDwqkBqjTfknzsFkHF7u5i9` | raydium-cpmm | cand1 | 1 | 0.3 |
| другое | `RKLBnAXGqv31iZomqsuAWkQm1aqC7JwwvbCfzGdqAhz` | raydium-cpmm | cand1 | 1 | 0.3 |
| другое | `RKLBnAXGqv31iZomqsuAWkQm1aqC7JwwvbCfzGdqAhz` | raydium-cpmm | batch5 | 1 | 0.3 |
| другое | `RKLBnAXGqv31iZomqsuAWkQm1aqC7JwwvbCfzGdqAhz` | pump-amm | lane_s0 | 1 | 0.3 |
| tOpenAI | `oPAiAikWTaFj9RYoRFD35ccfwhnMcB3ThgBZRHSkjTZ` | raydium-cpmm | cand1 | 1 | 0.3 |
| другое | `SATqS9DYpLQsM2z51P4QCoqJRHa5wboV4qjJerJRUSH` | raydium-cpmm | batch5 | 1 | 0.3 |
| ANTHROPIC | `Pren1FvFX6J3E4kXhJuCiAD5aDmGEb7qJRncwA8Lkhw` | raydium-cpmm | cand1 | 1 | 0.3 |
| другое | `EJguxbZRa9opvh1kBvYq21zwkXhttAD9YCqxBcaMvBi` | raydium-cpmm | cand1 | 1 | 0.3 |
| SPYx | `XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W` | pump | cand1 | 1 | 0.3 |
| tOpenAI | `oPAiAikWTaFj9RYoRFD35ccfwhnMcB3ThgBZRHSkjTZ` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| SPCXx | `Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8` | meteora-dlmm | lane_s0 | 1 | 0.3 |
| BABA | `BABANGA4JE7Kkam4nTrALAwAVgsNJUuFJnnkF7S16BZp` | raydium-launchpad | cand1 | 1 | 0.3 |
| SPCXx | `Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8` | raydium-cpmm | batch5 | 1 | 0.3 |
| другое | `NFLX7qV57zuVxCoHy3s1jiGZyALraLNwttbxdvmYJLJ` | pump | cand1 | 1 | 0.3 |
| XMR | `WXMRyRZhsa19ety5erZhHg4N3xj3EVN92u94422teJp` | pump-amm | lane_s0 | 1 | 0.3 |
| другое | `CFNRDaxFcvRwRSNnA5cHrCCr6AHhk9dNkHWpRUjNupFL` | raydium-clmm | lane_s0 | 1 | 0.3 |
| другое | `NFLX7qV57zuVxCoHy3s1jiGZyALraLNwttbxdvmYJLJ` | pump-amm | cand1 | 1 | 0.3 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | meteora-damm-v2 | lane_s0 | 1 | 0.3 |
| BP | `BPxxfRCXkUVhig4HS1Lh7kZqV6SPJhzfEk4x6fVBjPCy` | pump | cand1 | 1 | 0.3 |
| ZEC | `A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS` | raydium-launchpad | cand1 | 1 | 0.3 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | meteora-dlmm | lane_s0 | 1 | 0.3 |
| другое | `NFLX7qV57zuVxCoHy3s1jiGZyALraLNwttbxdvmYJLJ` | raydium-launchpad | cand1 | 1 | 0.3 |
| другое | `DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP` | raydium-launchpad | batch5 | 1 | 0.3 |
| IONQ | `NQ5hSuXQZrbnrwcDVk2qN73njjd3E3v3badYHnj5thF` | raydium-launchpad | batch5 | 1 | 0.3 |
| METAx | `Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu` | pump-amm | batch5 | 1 | 0.3 |
| xSOL | `4sWNB8zGWHkh6UnmwiEtzNxL4XrN7uK9tosbESbJFfVs` | raydium-launchpad | cand1 | 1 | 0.3 |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | pump-amm | batch5 | 1 | 0.3 |
| другое | `CFNRDaxFcvRwRSNnA5cHrCCr6AHhk9dNkHWpRUjNupFL` | raydium-clmm | batch5 | 1 | 0.3 |
| ANTHROPIC | `Pren1FvFX6J3E4kXhJuCiAD5aDmGEb7qJRncwA8Lkhw` | raydium-cpmm | lane_s0 | 1 | 0.3 |
| XMR | `WXMRyRZhsa19ety5erZhHg4N3xj3EVN92u94422teJp` | raydium-cpmm | cand1 | 1 | 0.3 |
| WSOL | `So11111111111111111111111111111111111111112` | pump | cand1 | 237 | 79.0 |
| WSOL | `So11111111111111111111111111111111111111112` | pump-amm | cand1 | 153 | 51.0 |
| WSOL | `So11111111111111111111111111111111111111112` | pump-amm | lane_s0 | 31 | 10.3 |
| WSOL | `So11111111111111111111111111111111111111112` | pump-amm | batch5 | 25 | 8.3 |
| WSOL | `So11111111111111111111111111111111111111112` | meteora-dlmm | batch5 | 22 | 7.3 |
| WSOL | `So11111111111111111111111111111111111111112` | meteora-dlmm | lane_s0 | 15 | 5.0 |
| WSOL | `So11111111111111111111111111111111111111112` | meteora-dlmm | cand1 | 13 | 4.3 |
| WSOL | `So11111111111111111111111111111111111111112` | meteora-damm-v2 | cand1 | 11 | 3.7 |
| WSOL | `So11111111111111111111111111111111111111112` | meteora-damm-v2 | lane_s0 | 8 | 2.7 |
| WSOL | `So11111111111111111111111111111111111111112` | meteora-launchpad | cand1 | 6 | 2.0 |
| WSOL | `So11111111111111111111111111111111111111112` | meteora-damm-v2 | batch5 | 6 | 2.0 |
| WSOL | `So11111111111111111111111111111111111111112` | pump | lane_s0 | 5 | 1.7 |
| WSOL | `So11111111111111111111111111111111111111112` | meteora-dlmm | leader | 4 | 1.3 |
| WSOL | `So11111111111111111111111111111111111111112` | raydium-cpmm | lane_s0 | 3 | 1.0 |
| WSOL | `So11111111111111111111111111111111111111112` | raydium-launchpad | cand1 | 2 | 0.7 |
| WSOL | `So11111111111111111111111111111111111111112` | raydium-cpmm | batch5 | 2 | 0.7 |
| WSOL | `So11111111111111111111111111111111111111112` | raydium-cpmm | cand1 | 2 | 0.7 |
| WSOL | `So11111111111111111111111111111111111111112` | raydium-launchpad | batch5 | 1 | 0.3 |
| WSOL | `So11111111111111111111111111111111111111112` | raydium-clmm | batch5 | 1 | 0.3 |

## Не WSOL: по источникам

| котировка | тип пула | группа | источник | n |
|---|---|---|---|---|
| STONK | raydium-cpmm | leader | `Beqv6dzT` лидер | 7 |
| другое | raydium-cpmm | cand1 | `5YRgrP3m`  | 7 |
| другое | raydium-cpmm | leader | `Beqv6dzT` лидер | 5 |
| CARDS | pump-amm | cand1 | `HmBmSYwY`  | 5 |
| NVDAx | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 4 |
| другое | pump-amm | batch5 | `4KFjw2xf` dreamloader | 4 |
| USDC | raydium-clmm | leader | `Beqv6dzT` лидер | 4 |
| USDC | raydium-clmm | batch5 | `F5MYbjEA` Omakase | 4 |
| другое | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 3 |
| PUMP | pump-amm | cand1 | `CAPn1yH4`  | 3 |
| другое | pump-amm | cand1 | `5YRgrP3m`  | 3 |
| другое | pump | cand1 | `5YRgrP3m`  | 3 |
| SPCXx | raydium-cpmm | cand1 | `5YRgrP3m`  | 3 |
| SPCXx | raydium-launchpad | cand1 | `GM7Hrz2b`  | 3 |
| PUMP | pump-amm | cand1 | `5YRgrP3m`  | 2 |
| SPCXx | raydium-cpmm | cand1 | `EjtQrPTb`  | 2 |
| PUMP | pump | cand1 | `CAPn1yH4`  | 2 |
| STONK | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 2 |
| STONK | raydium-cpmm | batch5 | `EC2f5DnH` Theo | 2 |
| другое | raydium-cpmm | batch5 | `9CNyLECt` rasmr | 2 |
| другое | raydium-cpmm | batch5 | `4KFjw2xf` dreamloader | 2 |
| HIMS | pump-amm | cand1 | `5YRgrP3m`  | 2 |
| SPCX | pump | cand1 | `5YRgrP3m`  | 2 |
| BOT | raydium-cpmm | leader | `Beqv6dzT` лидер | 2 |
| NVDAx | raydium-launchpad | cand1 | `GM7Hrz2b`  | 2 |
| SPCXx | raydium-launchpad | cand1 | `6S8Gezkx`  | 2 |
| USDC | meteora-dlmm | leader | `Beqv6dzT` лидер | 2 |
| DJT | pump-amm | cand1 | `CAPn1yH4`  | 2 |
| PUMP | pump | cand1 | `HmBmSYwY`  | 2 |
| NVDAx | raydium-launchpad | cand1 | `6S8Gezkx`  | 2 |
| другое | raydium-launchpad | cand1 | `6S8Gezkx`  | 2 |
| NVDAx | raydium-launchpad | cand1 | `HmBmSYwY`  | 2 |
| SPCXx | raydium-cpmm | cand1 | `HmBmSYwY`  | 2 |
| другое | raydium-launchpad | cand1 | `HmBmSYwY`  | 2 |
| NVDAx | raydium-cpmm | lane_s0 | `5pHeNsWM` 5pHeNs | 2 |
| METAx | raydium-cpmm | leader | `Beqv6dzT` лидер | 1 |
| METAx | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 1 |
| MGM | raydium-cpmm | leader | `Beqv6dzT` лидер | 1 |
| MGM | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 1 |
| PUMP | pump | cand1 | `6qudAN2k`  | 1 |
| NVDAx | pump | cand1 | `CAPn1yH4`  | 1 |
| NVDAx | pump-amm | cand1 | `2net6etA`  | 1 |
| CARDS | pump-amm | cand1 | `CAPn1yH4`  | 1 |
| SPCXx | raydium-launchpad | cand1 | `EjtQrPTb`  | 1 |
| PEPE | pump | cand1 | `6qudAN2k`  | 1 |
| GLDx | raydium-launchpad | cand1 | `HmBmSYwY`  | 1 |
| PUMP | pump | cand1 | `5YRgrP3m`  | 1 |
| tOpenAI | raydium-launchpad | lane_s0 | `7JVQMwRj`  | 1 |
| NEAR | raydium-cpmm | batch5 | `4KFjw2xf` dreamloader | 1 |
| USDC | raydium-cpmm | cand1 | `5YRgrP3m`  | 1 |
| DJT | raydium-cpmm | cand1 | `GM7Hrz2b`  | 1 |
| PUMP | pump-amm | batch5 | `4KFjw2xf` dreamloader | 1 |
| другое | raydium-cpmm | lane_s0 | `498g1rVn` frank | 1 |
| SPCXx | raydium-cpmm | lane_s0 | `7JVQMwRj`  | 1 |
| SPCX | pump | cand1 | `DYAn4XpA`  | 1 |
| STONK | raydium-cpmm | lane_s0 | `7JVQMwRj`  | 1 |
| WBTC | pump-amm | cand1 | `6qudAN2k`  | 1 |
| GOOGLx | pump | cand1 | `6qudAN2k`  | 1 |
| GOOGLx | pump | cand1 | `2net6etA`  | 1 |
| INJ | raydium-cpmm | cand1 | `GM7Hrz2b`  | 1 |
| SPCXx | raydium-launchpad | cand1 | `2net6etA`  | 1 |
| SPCXx | raydium-cpmm | cand1 | `6S8Gezkx`  | 1 |
| SPCXx | raydium-launchpad | cand1 | `DYAn4XpA`  | 1 |
| USDC | raydium-clmm | batch5 | `GAsnqm4X` Nach | 1 |
| CARDS | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 1 |
| USDC | raydium-clmm | lane_s0 | `498g1rVn` frank | 1 |
| CARDS | raydium-cpmm | cand1 | `HmBmSYwY`  | 1 |
| USDC | raydium-clmm | cand1 | `DYAn4XpA`  | 1 |
| USDC | raydium-clmm | cand1 | `CAPn1yH4`  | 1 |
| CARDS | raydium-launchpad | cand1 | `CAPn1yH4`  | 1 |
| CARDS | raydium-launchpad | cand1 | `DYAn4XpA`  | 1 |
| другое | meteora-launchpad | lane_s0 | `7JVQMwRj`  | 1 |
| CARDS | raydium-launchpad | cand1 | `HmBmSYwY`  | 1 |
| GMEx | pump-amm | cand1 | `HmBmSYwY`  | 1 |
| ZEC | pump | cand1 | `6qudAN2k`  | 1 |
| PURR | raydium-cpmm | leader | `Beqv6dzT` лидер | 1 |
| ZEC | pump-amm | cand1 | `HmBmSYwY`  | 1 |
| DJT | pump | cand1 | `5YRgrP3m`  | 1 |
| GMEx | pump | cand1 | `5YRgrP3m`  | 1 |
| CARDS | raydium-launchpad | cand1 | `5YRgrP3m`  | 1 |
| SPCX | pump | cand1 | `6qudAN2k`  | 1 |
| SPCXx | raydium-launchpad | cand1 | `HmBmSYwY`  | 1 |
| HYPE | raydium-launchpad | cand1 | `5YRgrP3m`  | 1 |
| DOGE | pump-amm | cand1 | `CAPn1yH4`  | 1 |
| другое | raydium-clmm | leader | `Beqv6dzT` лидер | 1 |
| DJT | pump-amm | cand1 | `6qudAN2k`  | 1 |
| DJT | pump-amm | cand1 | `2net6etA`  | 1 |
| другое | raydium-launchpad | lane_s0 | `Xk9onqHk` Xk9onq | 1 |
| другое | meteora-launchpad | cand1 | `5YRgrP3m`  | 1 |
| WBTC | pump | cand1 | `EjtQrPTb`  | 1 |
| LMAO! | pump | cand1 | `6qudAN2k`  | 1 |
| USDC | meteora-dlmm | batch5 | `BA3nKHc4` AviFelman | 1 |
| WBTC | raydium-launchpad | cand1 | `5YRgrP3m`  | 1 |
| cbLTC | pump-amm | cand1 | `5YRgrP3m`  | 1 |
| PUMP | pump | cand1 | `DYAn4XpA`  | 1 |
| NVDAx | pump | cand1 | `DYAn4XpA`  | 1 |
| NVDAx | pump | cand1 | `6qudAN2k`  | 1 |
| OPENAI | raydium-launchpad | cand1 | `DYAn4XpA`  | 1 |
| другое | raydium-cpmm | cand1 | `CAPn1yH4`  | 1 |
| XMR | pump | cand1 | `6qudAN2k`  | 1 |
| QQQx | pump-amm | cand1 | `CAPn1yH4`  | 1 |
| XMR | raydium-cpmm | batch5 | `EC2f5DnH` Theo | 1 |
| PUMP | pump-amm | batch5 | `EC2f5DnH` Theo | 1 |
| METAx | pump-amm | cand1 | `6S8Gezkx`  | 1 |
| SPYx | raydium-launchpad | cand1 | `6S8Gezkx`  | 1 |
| SPCX | pump | cand1 | `6S8Gezkx`  | 1 |
| GOOGLx | raydium-cpmm | lane_s0 | `Xk9onqHk` Xk9onq | 1 |
| DJT | pump | cand1 | `HmBmSYwY`  | 1 |
| MSFTx | raydium-cpmm | cand1 | `5YRgrP3m`  | 1 |
| DJT | raydium-launchpad | cand1 | `6S8Gezkx`  | 1 |
| DJT | raydium-launchpad | cand1 | `GM7Hrz2b`  | 1 |
| другое | raydium-cpmm | cand1 | `6S8Gezkx`  | 1 |
| NVDAx | raydium-cpmm | batch5 | `GAsnqm4X` Nach | 1 |
| NVDAx | pump | cand1 | `5YRgrP3m`  | 1 |
| другое | pump-amm | lane_s0 | `Xk9onqHk` Xk9onq | 1 |
| tOpenAI | raydium-cpmm | cand1 | `6S8Gezkx`  | 1 |
| другое | raydium-cpmm | batch5 | `F5MYbjEA` Omakase | 1 |
| ANTHROPIC | raydium-cpmm | cand1 | `HmBmSYwY`  | 1 |
| NVDAx | raydium-cpmm | batch5 | `4KFjw2xf` dreamloader | 1 |
| другое | raydium-cpmm | cand1 | `GM7Hrz2b`  | 1 |
| SPYx | pump | cand1 | `HmBmSYwY`  | 1 |
| SPYx | raydium-launchpad | cand1 | `HmBmSYwY`  | 1 |
| tOpenAI | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 1 |
| SPCXx | meteora-dlmm | lane_s0 | `7JVQMwRj`  | 1 |
| BABA | raydium-launchpad | cand1 | `HmBmSYwY`  | 1 |
| SPCXx | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 1 |
| NVDAx | pump-amm | cand1 | `HmBmSYwY`  | 1 |
| SPCXx | raydium-cpmm | batch5 | `GAsnqm4X` Nach | 1 |
| USDC | raydium-clmm | cand1 | `5YRgrP3m`  | 1 |
| XMR | pump-amm | lane_s0 | `4vER1GJQ` Bitman | 1 |
| другое | raydium-cpmm | cand1 | `EjtQrPTb`  | 1 |
| другое | raydium-cpmm | cand1 | `HmBmSYwY`  | 1 |
| SPCX | pump | cand1 | `HmBmSYwY`  | 1 |
| другое | raydium-clmm | lane_s0 | `3Um4qsYQ`  | 1 |
| другое | pump-amm | cand1 | `HmBmSYwY`  | 1 |
| NVDAx | raydium-cpmm | batch5 | `H3en1XWQ` MaxHuh | 1 |
| USDC | meteora-damm-v2 | lane_s0 | `4vER1GJQ` Bitman | 1 |
| BP | pump | cand1 | `5YRgrP3m`  | 1 |
| NVDAx | raydium-cpmm | cand1 | `6S8Gezkx`  | 1 |
| NVDAx | raydium-cpmm | lane_s0 | `Fvkc2thk` Brez | 1 |
| ZEC | raydium-launchpad | cand1 | `HmBmSYwY`  | 1 |
| USDC | meteora-dlmm | lane_s0 | `5pHeNsWM` 5pHeNs | 1 |
| другое | raydium-launchpad | cand1 | `5YRgrP3m`  | 1 |
| другое | raydium-launchpad | batch5 | `GAsnqm4X` Nach | 1 |
| IONQ | raydium-launchpad | batch5 | `F5MYbjEA` Omakase | 1 |
| METAx | pump-amm | cand1 | `HmBmSYwY`  | 1 |
| METAx | pump-amm | batch5 | `4KFjw2xf` dreamloader | 1 |
| USDC | pump-amm | cand1 | `HmBmSYwY`  | 1 |
| USDC | pump-amm | cand1 | `GM7Hrz2b`  | 1 |
| xSOL | raydium-launchpad | cand1 | `HmBmSYwY`  | 1 |
| USDC | pump-amm | batch5 | `4KFjw2xf` dreamloader | 1 |
| NVDAx | raydium-cpmm | cand1 | `HmBmSYwY`  | 1 |
| другое | raydium-clmm | batch5 | `9CNyLECt` rasmr | 1 |
| другое | raydium-launchpad | cand1 | `6qudAN2k`  | 1 |
| ANTHROPIC | raydium-cpmm | lane_s0 | `3Um4qsYQ`  | 1 |
| XMR | raydium-cpmm | cand1 | `EjtQrPTb`  | 1 |
