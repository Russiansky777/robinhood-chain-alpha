# Обращение в поддержку DBot — текст для отправки

Готово к отправке как есть. Ниже — сначала пояснение для владельца
(что именно утверждается и чем это подтверждено), затем сам текст на
английском.

## Что мы утверждаем

`POST /coin_tools/distribute` строит перевод SPL-токена устаревшей
инструкцией `Transfer` вместо `TransferChecked`. Token-2022 такие
переводы отклоняет для СВОИХ минтов — симуляция падает с
`custom program error: 0x1f` (`MintRequiredForTransfer`) и явным
указанием в логах: «use `transfer_checked` or `transfer_checked_with_fee`».

Проверено на четырёх реальных вызовах в один день, одним и тем же
ключом, между кошельками одного аккаунта:

| что переносили | программа минта | комиссия за перевод | результат |
|---|---|---|---|
| нативный SOL | — | — | успех, `95knwrxb…JoZs` |
| STONK | SPL Token | нет | успех, `3FTbLqxn…qwY9`, удержано 0% |
| NEURALINK | Token-2022 | 100 bps | отказ `0x1f` |
| PUMP | Token-2022 | нет | отказ `0x1f` |

Ключевое: PUMP — Token-2022 БЕЗ комиссии за перевод, и он отказал так же.
Значит дело не в расширении `TransferFeeConfig`, а в самой программе
Token-2022. Свопы DBot с этими минтами работают нормально — позиции
покупались и продавались через ваш же движок; сломан именно инструмент
рассылки.

Полные подписи успешных транзакций:
* SOL: `95knwrxb5jYdLcqiGqpjkUJQqMRNR4Ny1TosepAhuof58Bc85JgoCfZynextzTDJTLkKDPC9RNJ8zrMc4P8JoZs`
* STONK: `3FTbLqxnGuYsM7tjpre74YaofKHpQLqRbLVNb9hr2LseT9kCeS5cHw3bPdNpB3g5ufMmmKHZpZXRCxiHX5r5qwY9`

---

## Текст обращения (английский)

**Subject:** `/coin_tools/distribute` cannot send any Token-2022 mint — uses legacy `Transfer` instead of `TransferChecked` (error `0x1f`)

Hello,

`POST /coin_tools/distribute` fails for **every Token-2022 mint** we tried,
while working correctly for native SOL and for classic SPL Token mints.
The API returns HTTP 200 with the outer `err: false`, but the inner result
carries `err: true` and a simulation failure from the chain.

**Error returned by the chain**

```
Transaction simulation failed: Error processing Instruction 1:
custom program error: 0x1f

Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb invoke [1]
Program log: Instruction: Transfer
Program log: Mint required for this account to transfer tokens,
             use `transfer_checked` or `transfer_checked_with_fee`
Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb failed:
             custom program error: 0x1f
```

`0x1f` is `TokenError::MintRequiredForTransfer`. The log states the cause
directly: the transaction uses the deprecated `Transfer` instruction, and
Token-2022 requires `TransferChecked` (or `TransferCheckedWithFee` when
the mint has the transfer-fee extension), because those variants pass the
mint account and the expected decimals.

**What we tested (chain: `solana`, same API key, transfers between wallets
of the same account, all on 2026-09-22)**

| Asset | Token program | Transfer fee | `token` field | Result |
|---|---|---|---|---|
| Native SOL | — | — | `""` | **OK** — tx `95knwrxb5jYdLcqiGqpjkUJQqMRNR4Ny1TosepAhuof58Bc85JgoCfZynextzTDJTLkKDPC9RNJ8zrMc4P8JoZs` |
| STONK `6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx` | SPL Token | none | mint | **OK** — tx `3FTbLqxnGuYsM7tjpre74YaofKHpQLqRbLVNb9hr2LseT9kCeS5cHw3bPdNpB3g5ufMmmKHZpZXRCxiHX5r5qwY9`, full amount delivered |
| NEURALINK `PrekqLJvJ3qVdXmBGDiexvwUTF4rLFDa6HWS4HJbw9S` | Token-2022 | 100 bps | mint | **FAILS** — `0x1f` |
| PUMP `pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn` | Token-2022 | **none** | mint | **FAILS** — `0x1f` |

The PUMP case is the important one: it is a Token-2022 mint **without**
the transfer-fee extension and it fails identically. So this is not about
transfer fees — it affects the Token-2022 program as a whole.

**Full inner response for the PUMP attempt**

```json
{"err": false, "res": [{
  "err": true,
  "msg": "Simulation failed. Message: Transaction simulation failed:
          Error processing Instruction 1: custom program error: 0x1f. Logs: [
    \"Program log: Instruction: InitializeAccount3\",
    \"Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb consumed 2802 of 385229 compute units\",
    \"Program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL success\",
    \"Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb invoke [1]\",
    \"Program log: Instruction: Transfer\",
    \"Program log: Mint required for this account to transfer tokens,
                   use `transfer_checked` or `transfer_checked_with_fee`\",
    \"Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb failed: custom program error: 0x1f\"
  ]",
  "toList": [{"address": "…", "amountUI": 119.947159}]
}]}
```

Note that the associated-token-account creation step (`InitializeAccount3`
via `ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL`) succeeds — only the
transfer instruction itself is rejected.

**Requested fix**

Build the transfer with `TransferChecked` (passing the mint account and
its decimals), and with `TransferCheckedWithFee` when the mint carries the
`TransferFeeConfig` extension. This is backward compatible: classic SPL
Token accepts `TransferChecked` as well, so a single code path covers both
programs.

**Why this matters to us**

Your swap engine handles these same Token-2022 mints correctly — we buy
and sell them through DBot every day. Only the multisender is affected, so
tokens that our copy-trading tasks hold cannot be moved off the task
wallets at all. A fix would unblock every Token-2022 position, which in
our current book is 9 of 11 stuck positions.

**Secondary observation (not a bug report, just a note):** the outer
`err` field is `false` on these failed calls while the inner per-recipient
`err` is `true`. Integrators reading only the outer field will record a
failed transfer as a success. It would help if the outer `err` reflected
the outcome, or if the documentation stated this explicitly.

Thank you.
