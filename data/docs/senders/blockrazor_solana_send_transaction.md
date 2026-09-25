---
description: >-
  Use the BlockRazor Solana Send Transaction API. Review the endpoint,
  parameters, request example, and integration example.
metaLinks:
  canonical: ./
  alternates:
    - >-
      https://app.gitbook.com/s/QJcHRn7SY50Ny5UQhXHy/transaction-submission/transaction-sending/solana/send-transaction
---

# Solana Send Transaction API

{% hint style="warning" %}
Solana's transaction sending service is not bound to the subscription plan, with rate limit default to 3 TPS. API key could be required from [Authentication](../../../../get-started/authentication.md). If you need to increase the TPS limit, please [contact](https://discord.com/invite/qqJuwRb8Nh) us and we will handle it as soon as possible.
{% endhint %}

`Send Transaction` is a fast transaction sending interface provided by BlockRazor for Solana, used to send signed transactions to the blockchain with lower latency. This service offers access via HTTP and gRPC, making it suitable for users with high requirements for transaction inclusion speed.

Send Transaction, while maintaining compatibility with existing sending methods, leverages [BEF](../../../../core-technology/blockchain-edge-fabric.md) to shorten the transaction path from the client to the Leader node, thereby improving transaction inclusion speed. For Send Transaction's inclusion routing, testing instructions, and Benchamark results, please refer to [BenchMarking Solana Send Transaction Service](https://blockrazor.io/blog/20250801Benchmarking/).

Currently, Send Transaction supports two modes: fast and sandwichMitigation.

* Fast mode is suitable for scenarios with higher requirements for sending speed.
* The sandwichMitigation model is suitable for scenarios where a balance needs to be struck between speed and specific risk control.

### Endpoint <a href="#xian-liu" id="xian-liu"></a>

* `POST /sendTransaction`
* `gRPC`

### Request Example <a href="#jiao-yi-gou-jian-dai-ma-shi-li" id="jiao-yi-gou-jian-dai-ma-shi-li"></a>

* [Curl](request-example/curl.md)
* [Go](request-example/go.md)
* [Rust](request-example/rust.md)
* [JS](request-example/js.md)

### Request Parameter

<table><thead><tr><th width="111.44921875">Parameters</th><th width="115.421875">Mandatory</th><th width="109.98046875">Example</th><th>Description</th></tr></thead><tbody><tr><td>transaction</td><td>Mandatory</td><td>"4hXTCk……tAnaAT"</td><td>Fully signed transactions, compatible with encoding protocal Base64 and Base58, with Base64 being recommended</td></tr><tr><td>mode</td><td>Optional</td><td>"fast"<br>"sandwichMitigation"</td><td>BlockRazor offers two modes: Fast and SandwichMitigation, with Fast as the default.<br><br>In fast mode, transactions are sent based on globally distributed high-performance network and high-quality SWQoS, reaching the Leader node with the lowest latency.<br><br>In sandwichMitigation mode, BlockRazoz will route transactions to the trusted SWQoS and skip the slot of the blacklisted Leader (dynamically identified by the BlockRazor sandwich monitoring mechanism). In this mode, <strong>DO NOT</strong> send transactions using durable nonce, as it will cause the sandwich protection to become ineffective.</td></tr><tr><td>safeWindow</td><td>Optional</td><td>3</td><td>safeWindow is used to determine the timing of transaction sending in sandwichMitigation mode and represents the number of consecutive slots of  whitelist validators. For example, if it is set to 3, the transaction will only be sent when 3 consecutive slots from the current slot belong to whitelist validators.<br><br>The range of safeWindow is 3-13. The larger the number, the better the effect of mitigating the sandwich attack, but it may have a certain impact on the rate of inclusion. If not set, the default is 3.</td></tr><tr><td>revertProtection</td><td>Optional</td><td>false</td><td>The default value is false. If set to true, the transaction will not fail on chain, but the speed of inclusion will be affected and there is a possibility that it cannot be included. Please choose to enable it carefully according to actual needs.</td></tr></tbody></table>
