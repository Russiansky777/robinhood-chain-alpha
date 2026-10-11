# Helius `preprocessedSubscribe` — фильтр по счёту не применяется (владельцу: отправить как есть)

**Что проверено и чем.** Зонд `analysis/preprocessed_ab_zond.py`, прогоны
11.10 с 01:1xZ по 03:0xZ, NL-хост, наш рабочий ключ Helius. Для каждой формы
параметров — окно 8 с, считались сообщения, их темп и число РАЗНЫХ слотов.

| форма `params` | подписка | сообщений | в секунду | разных слотов |
| --- | --- | --- | --- | --- |
| `{"accountInclude":[A],"accountExclude":[],"accountRequired":[]}` (как в документации) | принята | 527 | **65.12** | 38 |
| `{"accountInclude":[A]}` | принята | 223 | 27.78 | 20 |
| `[{"accountInclude":[A],...}]` (списком, как у `transactionSubscribe`) | принята | 641 | 79.67 | 34 |
| `{"filter":{"accountInclude":[A]}}` | **отказ** | — | — | — |
| `{"accountInclude":[],"accountExclude":[],"accountRequired":[A]}` | принята | 161 | 20.07 | 34 |

Адрес `A` = `4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9` — кошелёк, который за
этот период делал **единицы транзакций в час** (по обычному
`transactionSubscribe` с тем же фильтром пришло 4 сообщения за 445 с). Десятки
сообщений в секунду и десятки разных слотов за восемь секунд — это поток всей
сети, а не один счёт.

Форма с полем `filter` отвечает осмысленной ошибкой, то есть сервер фильтры
знает: `{"code": -32602, "message": "filter requires accountInclude or accountRequired"}`.

**Оговорка, без которой число соврёт.** В одном из прогонов та же первая форма
дала за своё окно 0 сообщений, и мы сначала приняли это за работающий фильтр.
Повторные прогоны показали 65 сообщений в секунду у той же формы: ноль был
холодным старом соединения, а не фильтром. Поэтому вывод делается по
повторяемым прогонам, а не по одному окну.

**Чем это важно для нас.** Поток нужен как «видеть раньше»: по 4 транзакциям,
попавшим в оба потока, `preprocessedSubscribe` приходит раньше обычного
`transactionSubscribe` на медиану 3.426 мс (p90 3.777, разброс 3.08–3.78, 4 из
4). Но без фильтра это 0.1 кредита за КАЖДОЕ сообщение сети — около
500 000 кредитов в сутки вместо считанных сотен.

---

## Текст письма (английский, отправлять как есть)

> Subject: preprocessedSubscribe ignores accountInclude / accountRequired — we
> receive the full network stream
>
> Hello,
>
> We are using `preprocessedSubscribe` on `wss://beta.helius-rpc.com/?api-key=…`
> and the account filter does not appear to be applied: whatever we pass, we
> receive what looks like the whole network.
>
> Test: we subscribe for a single wallet,
> `4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9`, which produces only a few
> transactions per hour (with the same filter on `transactionSubscribe` we got
> 4 notifications in 445 seconds). We then counted binary notifications for 8
> seconds per parameter shape:
>
> | params | subscribe | messages | per second | distinct slots |
> | --- | --- | --- | --- | --- |
> | `{"accountInclude":["4vw…"],"accountExclude":[],"accountRequired":[]}` | accepted | 527 | 65.12 | 38 |
> | `{"accountInclude":["4vw…"]}` | accepted | 223 | 27.78 | 20 |
> | `[{"accountInclude":["4vw…"],"accountExclude":[],"accountRequired":[]}]` | accepted | 641 | 79.67 | 34 |
> | `{"filter":{"accountInclude":["4vw…"]}}` | rejected | — | — | — |
> | `{"accountInclude":[],"accountExclude":[],"accountRequired":["4vw…"]}` | accepted | 161 | 20.07 | 34 |
>
> Dozens of distinct slots within an 8-second window for a single wallet is the
> whole network, not that account.
>
> The `filter`-wrapped shape is rejected with
> `{"code": -32602, "message": "filter requires accountInclude or accountRequired"}`,
> so the server clearly knows these fields.
>
> We parse the binary frames per your documentation (byte 0 = schema version 1,
> bytes 1–8 = slot as little-endian u64, bytes 9–72 = first signature, body from
> byte 73) and the frames decode correctly — the signatures are valid, they are
> simply not ours.
>
> Questions:
>
> 1. What is the exact `params` shape that applies the account filter for
>    `preprocessedSubscribe` on the beta endpoint?
> 2. Is filtering enabled for our plan/key on that endpoint, or does the beta
>    endpoint currently stream unfiltered regardless of the filter fields?
> 3. If filtering is not yet active, is there a way to subscribe for a small
>    account set without being billed 0.1 credit per network-wide message?
>
> Happy to send exact request/response pairs and a frame sample.
>
> Thank you.

---

## Что мы делаем до ответа

* Зонд не держит поток: предохранитель выключает его, если темп выше 5
  сообщений в секунду по одному адресу — иначе 0.1 кредита за сообщение
  превращаются в ~500 000 кредитов в сутки.
* Направление вопроса «видеть раньше» уже отвечено по 4 транзакциям: **3.426 мс
  по медиане, то есть НИЖЕ порога владельца 20 мс**. Добрать до 30 транзакций
  можно и на нефильтрованном потоке (пары сходятся по подписи), но это стоит
  ориентировочно 0.2–0.5 $ за окно в полтора-два часа по тарифу 0.1 кредита за
  сообщение и 5 $ за миллион. Премиса «бесплатно» перестала быть верной, поэтому
  без слова владельца я этого не включаю.
