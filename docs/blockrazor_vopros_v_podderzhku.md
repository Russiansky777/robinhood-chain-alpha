# BlockRazor — вопрос в их поддержку (владельцу: отправить как есть)

Слово владельца 10.10, п.0: «Если и это нет — "причина не найдена", blockrazor
из набора, текст вопроса в их поддержку — владельцу».

**Причина не найдена.** Три подозреваемых проверены и сняты пробами, которые
ничего не стоили (отказанная отправка не платит ни комиссии, ни чаевых):

| что проверяли | как | ответ |
| --- | --- | --- |
| сумма чаевых | самопереводы с 100 000, 101 000, 200 000 и 1 000 000 лампортов | один и тот же отказ с одним и тем же числом в тексте — то есть и при десятикратном минимуме |
| адрес чаевых | все 14 счетов из их документации проверены ПО ЦЕПИ | все 14 живы: по 25 свежих подписей на каждом, все позже 13:49:37Z, последние за минуту до проверки. Наши четыре — это первые четыре того же списка |
| порядок инструкции | проба с инструкцией чаевых ПЕРВОЙ (их документация просит «at the front position») | тот же отказ |

Ключ при этом принимается: без него сервис отвечает `403 Authentication
information is missing`, а с ним — разбирает тело и отвечает про чаевые.

Текущее состояние: `blockrazor` выключен в нашем реестре отправителей
(`data/senders.json`, поле `vyklyuchen` с этой же причиной). Вернуть — убрать
одно поле; проводка его протокола (тело `mode=fast`, заголовок `apikey`,
разбор ответа) остаётся проверенной самопроверкой, чтобы возврат был правкой
одного поля, а не новой работой.

---

## Текст письма (английский, отправлять как есть)

> Subject: Fast mode keeps rejecting with "Insufficient tip ... at least 100000
> lamports" although the tip is present and ≥ the stated minimum
>
> Hello,
>
> Since 2026-10-10 13:49:37 UTC every transaction we submit to
> `POST amsterdam.solana.blockrazor.xyz:443/sendTransaction` (Fast mode,
> `{"transaction": <base64>, "mode": "fast", "revertProtection": false}`,
> header `apikey: <our key>`) is rejected with exactly:
>
> `rpc error: code = Unknown desc = Insufficient tip, please increase the tip
> amount and try again, at least 100000 lamports`
>
> Before that timestamp the same code path was accepted 11 times in a row
> (HTTP 200) with a 100 000 lamport tip, and those transactions landed.
>
> We have ruled out the three obvious causes:
>
> 1. **Amount.** We retried with 100 000, 101 000, 200 000 and 1 000 000
>    lamports. All four were rejected with the identical message and the
>    identical "at least 100000 lamports", i.e. the message does not change at
>    ten times the stated minimum.
> 2. **Tip account.** The tip is a plain SystemProgram transfer from the fee
>    payer to one of the 14 tip accounts listed in your documentation (we used
>    `FjmZZrFvhnqqb9ThCuMVnENaM3JGVuGWNyCAxRJcFpg9`). We verified on-chain that
>    all 14 of those accounts are receiving transfers right now — 25 recent
>    signatures each, all after 13:49:37 UTC — so the list we hold is current.
> 3. **Instruction position.** Your documentation says the tip instruction is
>    "preferably added at the front position" and your Go example puts `tipIx`
>    first. We retried with the tip instruction placed first (immediately after
>    the compute-budget instructions). Same rejection.
>
> Authentication is clearly fine: without the key we get
> `403 Authentication information is missing`, with it we get the tip error.
>
> Our transactions are versioned (v0) messages. The test transaction that
> reproduces this is a 0.000001 SOL self-transfer plus the tip, no address
> lookup tables, 4 instructions, ~290 bytes.
>
> Questions:
>
> 1. Is there an account-level or plan-level minimum tip that differs from the
>    documented 100 000 lamports, and if so, what is it for our key?
> 2. Does Fast mode require the tip to be denominated or placed differently
>    than our request above (for example, a specific instruction index, or the
>    tip account that `getTransactionfee` returns rather than an arbitrary one
>    from the list)?
> 3. Did anything change on your side around 2026-10-10 13:49 UTC — a new
>    minimum, a parser change, or versioned-transaction handling?
>
> Happy to send a base64 sample transaction and the exact request/response
> pair on request.
>
> Thank you.

---

## Что ещё можно проверить, если поддержка не ответит

* `getTransactionfee` — их документация (раздел Tip, нынешняя редакция) просит
  ставить чаевые «to the value returned by getTransactionfee». У нас этого
  вызова нет вовсе: мы берём минимум из реестра. Если минимум у них стал
  динамическим, их текст ошибки мог остаться с прежней константой. Это
  следующая бесплатная проба, но она требует их метода, которого у нас в
  оснастке нет.
* `send-transaction-v2` — в нынешней документации есть и такой путь, у нас
  настроен первый. Проба стоит ровно столько же (отказ бесплатен).

Сделать это без слова владельца я не буду: каждая такая проба — правка
денежного пути (тело запроса и адрес точки входа), а не замер.
