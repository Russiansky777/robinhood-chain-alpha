# Пул отправителей: кандидаты по их документации

Всё ниже -- из страниц, сохранённых в `data/docs/senders/`. Ничего по памяти: у каждого значения есть цитата в `data/sender_pool_candidates.json`.

## Что скачать не удалось

* `astralane_regions.html.why_not.txt`: НЕ СКАЧАНО: https://astralane.gitbook.io/docs/low-latency/regions (HTTP 404), время UTC 2026-09-25T10:37:53Z
* `nozomi_llms.txt.why_not.txt`: НЕ СКАЧАНО: https://use.temporal.xyz/nozomi/llms.txt (HTTP 404), время UTC 2026-09-25T10:37:45Z
* `nozomi_regions.html.why_not.txt`: НЕ СКАЧАНО: https://use.temporal.xyz/nozomi/regions-and-endpoints (HTTP 404), время UTC 2026-09-25T10:37:44Z

| сервис | страниц | нужен ключ | минимум чаевых (из текста) | счетов чаевых | точки входа в EU |
|---|---|---|---|---|---|
| Astralane | 4 | да | не нашлось | 1 | не нашлось |
| BlockRazor | 5 | да | 100000 лампортов | 14 | 9 |
| Helius Sender | 1 | да | 5000 лампортов, 1000000 лампортов | 11 | 9 |
| Jito (нужен для опознания чужих чаевых) | 1 | да | 1000 лампортов, 300000000 лампортов, 700000000 лампортов, 1000000000 лампортов | 8 | 6 |
| Nozomi (Temporal) | 3 | да | 1000000 лампортов | 17 | не нашлось |
| 0slot.trade | 1 | да | 100000 лампортов, 1000000 лампортов, 1000000000 лампортов, 2000000000 лампортов | 21 | не нашлось |

## Astralane

Страницы: `astralane_quickstart.html`, `astralane_send_v2.html`, `astralane_submit_transactions.html`, `astralane_tip_refunds.html`

Счета чаевых (1):
* `astra4uejePWneqNaJKuFFA8oonqCE1sqF6b45kDMZm`

Ключ -- цитаты:
* api-key: «as Content-Type : text/plain Txn should be in Base64 encoded Compulsory to add api-key and method in URI params Send request on endpoint /iris2 URI Params Para»
* api-key: «in URI params Send request on endpoint /iris2 URI Params Param Type Description api-key String Mandatory , to set api key for authentication method String Manda»
* api key: «oint /iris2 URI Params Param Type Description api-key String Mandatory , to set api key for authentication method String Mandatory , to set method mev-protect B»

Про чужие чаевые и минимум -- цитаты:
* jito tip: «the segregation in validators as JITO validators and normal ones, traders are often conflicted between spending more on jito tips vs more in priority fees. Durable nonces offer a way to mitigate this issue. Our sendIdeal RPC method accepts»
* must include a valid tip: «n string[] Yes Array of base64-encoded serialized transactions. Maximum batch size is 25 transactions. Each transaction must include a valid tip. object No Optional configuration object. Configuration Object Field Type Default Description m»

## BlockRazor

Страницы: `blockrazor_authentication.md`, `blockrazor_js_example.html`, `blockrazor_solana_endpoint.md`, `blockrazor_solana_priority_fee_and_tip.md`, `blockrazor_solana_send_transaction.md`

Точки входа в EU:
* `http://frankfurt.solana.blockrazor.xyz:443/sendTransaction`
* `http://frankfurt.solana.blockrazor.xyz:443/health`
* `http://frankfurt.solana.blockrazor.xyz:443`
* `http://frankfurt-allnodes.solana.blockrazor.xyz:443`
* `http://frankfurt-cherryservers.solana.blockrazor.xyz:443`
* `http://amsterdam.solana.blockrazor.xyz:443`
* `http://amsterdam-cherryservers.solana.blockrazor.xyz:443`
* `http://london.solana.blockrazor.xyz:443`
* `https://frankfurt.solana.blockrazor.io`

Счета чаевых (14):
* `Gywj98ophM7GmkDdaWs4isqZnDdFCW7B46TXmKfvyqSm`
* `FjmZZrFvhnqqb9ThCuMVnENaM3JGVuGWNyCAxRJcFpg9`
* `6No2i3aawzHsjtThw81iq1EXPJN6rh8eSJCLaYZfKDTG`
* `A9cWowVAiHe9pJfKAj3TJiN9VpbzMUq6E4kEvf5mUT22`
* `68Pwb4jS7eZATjDfhmTXgRJjCiZmw1L7Huy4HNpnxJ3o`
* `4ABhJh5rZPjv63RBJBuyWzBK3g9gWMUQdTZP2kiW31V9`
* `B2M4NG5eyZp5SBQrSdtemzk5TqVuaWGQnowGaCBt8GyM`
* `5jA59cXMKQqZAVdtopv8q3yyw9SYfiE3vUCbt7p8MfVf`
* `BnGKHAC386n4Qmv9xtpBVbRaUTKixjBe3oagkPFKtoy6`
* `Dd7K2Fp7AtoN8xCghKDRmyqr5U169t48Tw5fEd3wT9mq`
* `AP6qExwrbRgBAVaehg4b5xHENX815sMabtBzUzVB4v8S`
* `5YktoWygr1Bp9wiS1xtMtUki1PeYuuzuCF98tqwYxf61`
* `295Avbam4qGShBYK7E9H5Ldew4B3WyJGmgmXfiWdeeyV`
* `EDi4rSy2LZgKJX74mbLTFk4mxoTgT6F7HxxzG2HBAFyK`

Минимум чаевых -- цитаты:
* at least 100,000 Lamports (100000 лампортов): «. BlockRazor does not charge service fees from Tips. The Tip transfer amount is at least 100,000 Lamports (0.0001 Sol) . It is recommended to set it to the valu»
* 0.0001 Sol (100000 лампортов): «e service fees from Tips. The Tip transfer amount is at least 100,000 Lamports (0.0001 Sol) . It is recommended to set it to the value returned by [`getTransact»

Ключ -- цитаты:
* Auth Token: «--- description: >- Learn how to create a BlockRazor account, obtain an Auth Token, and add the auth value to supported API requests before integrating»
* Auth Token: «k.com/s/QJcHRn7SY50Ny5UQhXHy/get-started/authentication --- # Get a BlockRazor Auth Token | API Authentication {% stepper %} {% step %} **Create Account** [S»
* auth token: «reate an account {% endstep %} {% step %} **Log in** Log in and obtain the auth token in the home page {% endstep %} {% endstepper %}»

## Helius Sender

Страницы: `helius_sender.html`

Точки входа в EU:
* `http://lon-sender.helius-rpc.com/fast`
* `http://fra-sender.helius-rpc.com/fast`
* `http://ams-sender.helius-rpc.com/fast`
* `http://lon-sender.helius-rpc.com/ping`
* `http://fra-sender.helius-rpc.com/ping`
* `http://ams-sender.helius-rpc.com/ping`
* `http://lon-sender.helius-rpc.com/fast?api-key=YOUR_SENDER_API_KEY`
* `http://fra-sender.helius-rpc.com/fast?api-key=YOUR_SENDER_API_KEY`
* `http://ams-sender.helius-rpc.com/fast?api-key=YOUR_SENDER_API_KEY`

Счета чаевых (11):
* `4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE`
* `D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ`
* `9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta`
* `signTransactionMessageWithSigners`
* `5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn`
* `2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD`
* `2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ`
* `wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF`
* `3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT`
* `4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey`
* `4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or`

Минимум чаевых -- цитаты:
* 0.000005
SOL (5000 лампортов): «block — tip more to land first. The cost-optimized SWQOS-only tier remains at 0.000005 SOL. Helius Sender is a specialized service for ultra-low latency tra»
* 0.001 SOL (1000000 лампортов): «Tier Routing Minimum tip Tip buffer Best for Sender Max All high-speed pathways 0.001 SOL Yes Highly contested transactions and bundles needing the fastest land»
* 0.000005 SOL (5000 лампортов): «QOS path 0.000005 SOL No Cost-optimized trading on one fast path Tips between 0.000005 SOL and 0.001 SOL are accepted but do not enter the priority tip buffer»
* 0.001 SOL (1000000 лампортов): «SOL No Cost-optimized trading on one fast path Tips between 0.000005 SOL and 0.001 SOL are accepted but do not enter the priority tip buffer — they are sent»
* at least 0.001
SOL (1000000 лампортов): «s through fewer pathways. To get buffer priority and every routing pathway, tip at least 0.001 SOL. Sender Max (fastest landing) Routed across every pathway a»
* 0.001 SOL (1000000 лампортов): «. random () * TIP_ACCOUNTS . length )]), amount: lamports ( 1_000_000 n ), // 0.001 SOL }), m ) ); const signedTx = await signTransactionMessageWi»

Ключ -- цитаты:
* API Key: «plans, including the free tier, and doesn’t consume any API credits. 2 Get Your API Key Go to the API Keys section and copy your key. Use this for getting block»
* API Key: «he free tier, and doesn’t consume any API credits. 2 Get Your API Key Go to the API Keys section and copy your key. Use this for getting blockhashes and transac»
* api-key: «ng > { const connection = new Connection ( 'https://mainnet.helius-rpc.com/?api-key=YOUR_API_KEY' ); const { value : { blockhash } } = await connectio»

Про чужие чаевые и минимум -- цитаты:
* must include a tip: «or speed — Sender also supports preflight checks ​ Requirements Mandatory requirements : Every Sender transaction must include a tip (minimum 0.001 SOL for Sender Max , or 0.000005 SOL for SWQOS-only ) and a priority fee. skipPrefli»

## Jito (нужен для опознания чужих чаевых)

Страницы: `jito_low_latency_txn_send.html`

Точки входа в EU:
* `https://mainnet.block-engine.jito.wtf`
* `https://amsterdam.mainnet.block-engine.jito.wtf`
* `https://dublin.mainnet.block-engine.jito.wtf`
* `https://frankfurt.mainnet.block-engine.jito.wtf`
* `https://london.mainnet.block-engine.jito.wtf`
* `https://ny.mainnet.block-engine.jito.wtf`

Счета чаевых (8):
* `96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5`
* `HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe`
* `Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY`
* `ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49`
* `DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh`
* `ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt`
* `jitodontfront111111111114511111111111111123`
* `jitodontfront111111111111234565432123456782`

Минимум чаевых -- цитаты:
* 1000 lamports (1000 лампортов): «ith providing MEV protection. Please note that Jito enforces a minimum tip of 1000 lamports for bundles. During high-demand periods, this minimum tip might no»
* 0.7 SOL (700000000 лампортов): «a 70/30 split between priority fee and jito tip(e.g.): Priority Fee ( 70 % ): 0.7 SOL + Jito Tip ( 30 % ): 0.3 SOL =========================== Total F»
* 0.3 SOL (300000000 лампортов): «e and jito tip(e.g.): Priority Fee ( 70 % ): 0.7 SOL + Jito Tip ( 30 % ): 0.3 SOL =========================== Total Fee : 1.0 SOL So, when using»
* 1.0 SOL (1000000000 лампортов): «OL + Jito Tip ( 30 % ): 0.3 SOL =========================== Total Fee : 1.0 SOL So, when using sendTransaction: You would allocate 0.7 SOL as th»
* 1000 lamports (1000 лампортов): «Tip Amount  Q: How to know how much to set a tip? The minimum tips is 1000 lamports Bundle Landing  Q: My bundle/transaction is not landing,»
* 1000 lamports (1000 лампортов): «hin the last 5 minutes through getInflightBundleStatuses . The minimum tip is 1000 lamports, but if you’re targeting a highly competitive MEV opportunity, you»

Ключ -- цитаты:
* Authorization: «Rate Limits Default Limits Rate Limit Operation Exceeding Limits Authorization Issues Custom Rate Limit Request Troubleshooting Priorit»
* API key: «lowing when authenticating with a URL using a UUID: Header Field: Include the API key in the x-jito-auth header. Example: x-jito-auth: <uuid> Query Paramet»
* API key: «-jito-auth header. Example: x-jito-auth: <uuid> Query Parameter: Include the API key as a query parameter. Example: api/v1/transactions?uuid=<uuid>»

Про чужие чаевые и минимум -- цитаты:
* Jito tip: «ht not be sufficient to successfully navigate the auction, so it’s crucial to set both a priority fee and an additional Jito tip to optimize your transaction’s chances. Please see Tip Amounts For cost efficiency, you can enable revert pro»
* jito tip: « sendTransaction  When using sendTransaction , it is recommended to use a 70/30 split between priority fee and jito tip(e.g.): Priority Fee ( 70 % ): 0.7 SOL + Jito Tip ( 30 % ): 0.3 SOL =========================== Total»
* Jito Tip: «it is recommended to use a 70/30 split between priority fee and jito tip(e.g.): Priority Fee ( 70 % ): 0.7 SOL + Jito Tip ( 30 % ): 0.3 SOL =========================== Total Fee : 1.0 SOL So, when using sendTransaction: Y»
* Jito tip: «ee : 1.0 SOL So, when using sendTransaction: You would allocate 0.7 SOL as the priority fee. And 0.3 SOL as the Jito tip. sendBundle  When using sendBundle , only the Jito tip matters. 💸 Get Tip Information »

## Nozomi (Temporal)

Страницы: `nozomi_json_rpc.html`, `nozomi_tipping_and_faq.html`, `nozomi_transaction_submission.html`

Счета чаевых (17):
* `TEMPaMeCRFAS9EKF53Jd6KpHxgL47uWLcpFArU1Fanq`
* `noz3jAjPiHuBPqiSPkkugaJDkJscPuRhYnSpbi8UvC4`
* `noz3str9KXfpKknefHji8L1mPgimezaiUyCHYMDv1GE`
* `noz6uoYCDijhu1V7cutCpwxNiSovEwLdRHPwmgCGDNo`
* `noz9EPNcT7WH6Sou3sr3GGjHQYVkN3DNirpbvDkv9YJ`
* `nozc5yT15LazbLTFVZzoNZCwjh3yUtW86LoUyqsBu4L`
* `nozFrhfnNGoyqwVuwPAW4aaGqempx4PU6g6D9CJMv7Z`
* `nozievPk7HyK1Rqy1MPJwVQ7qQg2QoJGyP71oeDwbsu`
* `noznbgwYnBLDHu8wcQVCEw6kDrXkPdKkydGJGNXGvL7`
* `nozNVWs5N8mgzuD3qigrCG2UoKxZttxzZ85pvAQVrbP`
* `nozpEGbwx4BcGp6pvEdAh1JoC2CQGZdU6HbNP1v2p6P`
* `nozrhjhkCr3zXT3BiT4WCodYCUFeQvcdUkM7MqhKqge`
* `nozrwQtWhEdrA6W8dkbt9gnUaMs52PdAv5byipnadq3`
* `nozUacTVWub3cL4mJmGCYjKZTnE9RbdY5AP46iQgbPJ`
* `nozWCyTPppJjRuw2fpzDhhWbW355fzosWSzrrMYB1Qk`
* `nozWNju6dY353eMkMqURqwQEoM3SFgEKC6psLCSfUne`
* `nozxNBgWohjR75vdspfxR5H9ceC7XXH99xpxhVGt3Bb`

Минимум чаевых -- цитаты:
* 0.001 SOL (1000000 лампортов): «sfer instruction to one of the Nozomi tip addresses. The default minimum tip is 0.001 SOL . Transactions that tip below the minimum are silently dropped : you w»

Ключ -- цитаты:
* API_KEY: «PC URL with the Nozomi endpoint. Request Field Value Method POST Path /?c=<YOUR_API_KEY> Content-Type application/json Encoding base64 (must be specified) Impor»

Про чужие чаевые и минимум -- цитаты:
* must include a tip: «llms.txt . This page is also available as Markdown . Copy On this page Nozomi Tipping Overview Every Nozomi transaction must include a tip: a standard Solana system transfer instruction to one of the Nozomi tip addresses. The default minimu»
* tip below the minimum: «ystem transfer instruction to one of the Nozomi tip addresses. The default minimum tip is 0.001 SOL . Transactions that tip below the minimum are silently dropped : you will not receive an error. If you are seeing transactions disappear wit»
* silently drop: «to one of the Nozomi tip addresses. The default minimum tip is 0.001 SOL . Transactions that tip below the minimum are silently dropped : you will not receive an error. If you are seeing transactions disappear with no response, check your»

## 0slot.trade

Страницы: `zeroslot_docs.html`

Счета чаевых (21):
* `6fQaVhYZA4w3MBSXjJ81Vf6W1EDYeUPXpgVQ6UQyU1Av`
* `4HiwLEP2Bzqj3hM2ENxJuzhcPCdsafwiet3oGkMkuQY4`
* `7toBU3inhmrARGngC7z6SjyP85HgGMmCTEwGNRAcYnEK`
* `8mR3wB1nh4D6J9RUCugxUpc6ya8w38LPxZ3ZjcBhgzws`
* `6SiVU5WEwqfFapRuYCndomztEwDjvS5xgtEof3PLEGm9`
* `TpdxgNJBWZRL8UXF5mrEsyWxDWx9HQexA9P1eTWQ42p`
* `D8f3WkQu6dCF33cZxuAsrKHrGsqGP2yvAHf8mX6RXnwf`
* `GQPFicsy3P3NXxB5piJohoxACqTvWE9fKpLgdsMduoHE`
* `Ey2JEr8hDkgN8qKJGrLf2yFjRhW7rab99HVxwi5rcvJE`
* `4iUgjMT8q2hNZnLuhpqZ1QtiV8deFPy2ajvvjEpKKgsS`
* `3Rz8uD83QsU8wKvZbgWAPvCNDU6Fy8TSZTMcPm3RB6zt`
* `DiTmWENJsHQdawVUUKnUXkconcpW4Jv52TnMWhkncF6t`
* `HRyRhQ86t3H4aAtgvHVpUJmw64BDrb61gRiKcdKUXs5c`
* `7y4whZmw388w1ggjToDLSBLv47drw5SUXcLk6jtmwixd`
* `J9BMEWFbCBEjtQ1fG5Lo9kouX1HfrKQxeUxetwXrifBw`
* `8U1JPQh3mVQ4F5jwRdFTBzvNRQaYFQppHQYoH38DJGSQ`
* `Eb2KpSC8uMt9GmzyAEm5Eb1AAAgTjRaXWFjKyFXHZxF3`
* `FCjUJZ1qozm1e8romw216qyfQMaaWKxWsuySnumVCCNe`
* `ENxTEjSQ1YabmUpXAdCgevnHQ9MHdLv8tzFiuiYJqa13`
* `6rYLG55Q9RpsPGvqdPNJs4z5WTxJVatMB8zV3WJhs5EK`

Минимум чаевых -- цитаты:
* 0.001 SOL (1000000 лампортов): «nti-MEV Anti-MEV Anti-MEV Anti-MEV Anti-MEV Transaction Tip 0.001 SOL 0.001 SOL 0.001 SOL 0.0001 SOL Note: The above free»
* 0.001 SOL (1000000 лампортов): «ti-MEV Anti-MEV Anti-MEV Anti-MEV Transaction Tip 0.001 SOL 0.001 SOL 0.001 SOL 0.0001 SOL Note: The above free plans are ac»
* 0.001 SOL (1000000 лампортов): «i-MEV Anti-MEV Anti-MEV Transaction Tip 0.001 SOL 0.001 SOL 0.001 SOL 0.0001 SOL Note: The above free plans are according to y»
* 0.0001 SOL (100000 лампортов): «-MEV Anti-MEV Transaction Tip 0.001 SOL 0.001 SOL 0.001 SOL 0.0001 SOL Note: The above free plans are according to your data, Pl»
* 15.sol (15000000000 лампортов): «ot_trade.sol) 4HiwLEP2Bzqj3hM2ENxJuzhcPCdsafwiet3oGkMkuQY4(0slot_dot_trade_tip15.sol) 7toBU3inhmrARGngC7z6SjyP85HgGMmCTEwGNRAcYnEK(0slot_dot_trade_tip16.sol»
* 16.sol (16000000000 лампортов): «de_tip15.sol) 7toBU3inhmrARGngC7z6SjyP85HgGMmCTEwGNRAcYnEK(0slot_dot_trade_tip16.sol) 8mR3wB1nh4D6J9RUCugxUpc6ya8w38LPxZ3ZjcBhgzws(0slot_dot_trade_tip17.sol»

Ключ -- цитаты:
* api-key: «l) Example for cmd: curl curl -X POST 'https://ny.0slot.trade?api-key=YOUR_API_KEY' \ -H 'Content-Type: application/json' \ -d '{ "jsonrpc":»
* API_KEY: «Example for cmd: curl curl -X POST 'https://ny.0slot.trade?api-key=YOUR_API_KEY' \ -H 'Content-Type: application/json' \ -d '{ "jsonrpc": "2.0", "id":»
* API Key: «btain the servers that best suits your needs. Special Error Codes API Key Expired {"id":"1","jsonrpc":"2.0","error":{"code":403,"message":"api-k»
