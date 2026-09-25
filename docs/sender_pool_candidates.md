# Пул отправителей: кандидаты по их документации

Всё ниже -- из страниц, сохранённых в `data/docs/senders/`. Ничего по памяти: у каждого значения есть цитата в `data/sender_pool_candidates.json`.

| сервис | страниц | нужен ключ | минимум чаевых (из текста) | счетов чаевых | точки входа в EU |
|---|---|---|---|---|---|
| BlockRazor | 4 | да | 100000 лампортов | 14 | 7 |

## BlockRazor

Страницы: `blockrazor_authentication.md`, `blockrazor_solana_endpoint.md`, `blockrazor_solana_priority_fee_and_tip.md`, `blockrazor_solana_send_transaction.md`

Точки входа в EU:
* `http://frankfurt.solana.blockrazor.xyz:443`
* `http://frankfurt-allnodes.solana.blockrazor.xyz:443`
* `http://frankfurt-cherryservers.solana.blockrazor.xyz:443`
* `http://amsterdam.solana.blockrazor.xyz:443`
* `http://amsterdam-cherryservers.solana.blockrazor.xyz:443`
* `http://london.solana.blockrazor.xyz:443`
* `https://frankfurt.solana.blockrazor.io`

Счета чаевых (14):
* `FjmZZrFvhnqqb9ThCuMVnENaM3JGVuGWNyCAxRJcFpg9`
* `6No2i3aawzHsjtThw81iq1EXPJN6rh8eSJCLaYZfKDTG`
* `A9cWowVAiHe9pJfKAj3TJiN9VpbzMUq6E4kEvf5mUT22`
* `Gywj98ophM7GmkDdaWs4isqZnDdFCW7B46TXmKfvyqSm`
* `68Pwb4jS7eZATjDfhmTXgRJjCiZmw1L7Huy4HNpnxJ3o`
* `4ABhJh5rZPjv63RBJBuyWzBK3g9gWMUQdTZP2kiW31V9`
* `B2M4NG5eyZp5SBQrSdtemzk5TqVuaWGQnowGaCBt8GyM`
* `5jA59cXMKQqZAVdtopv8q3yyw9SYfiE3vUCbt7p8MfVf`
* `5YktoWygr1Bp9wiS1xtMtUki1PeYuuzuCF98tqwYxf61`
* `295Avbam4qGShBYK7E9H5Ldew4B3WyJGmgmXfiWdeeyV`
* `EDi4rSy2LZgKJX74mbLTFk4mxoTgT6F7HxxzG2HBAFyK`
* `BnGKHAC386n4Qmv9xtpBVbRaUTKixjBe3oagkPFKtoy6`
* `Dd7K2Fp7AtoN8xCghKDRmyqr5U169t48Tw5fEd3wT9mq`
* `AP6qExwrbRgBAVaehg4b5xHENX815sMabtBzUzVB4v8S`

Минимум чаевых -- цитаты:
* at least 100,000 Lamports (100000 лампортов): «. BlockRazor does not charge service fees from Tips. The Tip transfer amount is at least 100,000 Lamports (0.0001 Sol) . It is recommended to set it to the valu»
* 0.0001 Sol (100000 лампортов): «e service fees from Tips. The Tip transfer amount is at least 100,000 Lamports (0.0001 Sol) . It is recommended to set it to the value returned by [`getTransact»

Ключ -- цитаты:
* Auth Token: «--- description: >- Learn how to create a BlockRazor account, obtain an Auth Token, and add the auth value to supported API requests before integrating»
* Auth Token: «k.com/s/QJcHRn7SY50Ny5UQhXHy/get-started/authentication --- # Get a BlockRazor Auth Token | API Authentication {% stepper %} {% step %} **Create Account** [S»
* auth token: «reate an account {% endstep %} {% step %} **Log in** Log in and obtain the auth token in the home page {% endstep %} {% endstepper %}»
