---
description: Explore Priority Fee & Tips for BlockRazor Solana Transaction Sending Mode.
metaLinks:
  canonical: priority-fee-and-tip.md
  alternates:
    - >-
      https://app.gitbook.com/s/QJcHRn7SY50Ny5UQhXHy/transaction-submission/transaction-sending/solana/priority-fee-and-tip
---

# Solana Transaction Sending Priority Fee & Tip

### **Priority** Fee <a href="#priority-fee-and-tip" id="priority-fee-and-tip"></a>

Priority Fee is an additional transaction fee charged by Solana on top of Base Fee (the minimum cost of sending a transaction, 5,000 lamports for each signature included in the transaction). Due to limited computing resources, Leader nodes order transactions mainly by transaction value when producing blocks. Transactions with higher Priority Fee have a higher probability of being included in the next block. The CU Price of Priority Fee is provided by [`getTransactionfee`](../../../streams/network-fee-stream/solana/get-transactionfee.md) and is recommended to be set at least 1,000,000 when conscructing transactions.

### Tip <a href="#priority-fee-and-tip" id="priority-fee-and-tip"></a>

When constructing a transaction, you need to add a instruction of Tip transfer into the transaction(preferably added at the front position) to further speed up the inclusion. BlockRazor does not charge service fees from Tips. The Tip transfer amount is at least 100,000 Lamports (0.0001 Sol) . It is recommended to set it to the value returned by [`getTransactionfee`](../../../streams/network-fee-stream/solana/get-transactionfee.md). The account to receive Tip is:

```
"FjmZZrFvhnqqb9ThCuMVnENaM3JGVuGWNyCAxRJcFpg9",
"6No2i3aawzHsjtThw81iq1EXPJN6rh8eSJCLaYZfKDTG",
"A9cWowVAiHe9pJfKAj3TJiN9VpbzMUq6E4kEvf5mUT22",
"Gywj98ophM7GmkDdaWs4isqZnDdFCW7B46TXmKfvyqSm",
"68Pwb4jS7eZATjDfhmTXgRJjCiZmw1L7Huy4HNpnxJ3o",
"4ABhJh5rZPjv63RBJBuyWzBK3g9gWMUQdTZP2kiW31V9",
"B2M4NG5eyZp5SBQrSdtemzk5TqVuaWGQnowGaCBt8GyM",
"5jA59cXMKQqZAVdtopv8q3yyw9SYfiE3vUCbt7p8MfVf",
"5YktoWygr1Bp9wiS1xtMtUki1PeYuuzuCF98tqwYxf61",
"295Avbam4qGShBYK7E9H5Ldew4B3WyJGmgmXfiWdeeyV",
"EDi4rSy2LZgKJX74mbLTFk4mxoTgT6F7HxxzG2HBAFyK",
"BnGKHAC386n4Qmv9xtpBVbRaUTKixjBe3oagkPFKtoy6",
"Dd7K2Fp7AtoN8xCghKDRmyqr5U169t48Tw5fEd3wT9mq",
"AP6qExwrbRgBAVaehg4b5xHENX815sMabtBzUzVB4v8S",
```

{% hint style="info" %}
To avoid the degradation of performance due to address occupation, causing transaction delay, please rotate the Tip account when sending transactions.
{% endhint %}
