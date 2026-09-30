# Шреды: можно ли читать чужие UDP-шреды прокси Jito. Чтение кода, ничего не строил

Вопрос штаба: Triton может отдавать сырые UDP-шреды на наш порт; по транзакции их
читает только декодер. Смотрел `jito-labs/shredstream-proxy` (открытый, Rust, ветка
master, читал `proxy/src/main.rs` и `proxy/src/deshred.rs`). Ничего не запускал и не
собирал -- только исходники.

## 1. Принимает ли шреды от стороннего источника без аутентификации в block engine

**ДА.** У прокси два режима, и второй ровно про это:

* `Shredstream` -- просит шреды у Jito: требует `--block-engine-url`, `--auth-keypair`
  и `--desired-regions`;
* **`ForwardOnly`** -- doc-комментарий: *«Does not request shreds from Jito»*. Берёт
  ТОЛЬКО `CommonArgs`: ни адреса block engine, ни ключа, ни регионов.

Поток пульса (единственное место, где нужна аутентификация) запускается только в
первой ветке:

```rust
if let ProxySubcommands::Shredstream(args) = shredstream_args {
    let heartbeat_hdl = start_heartbeat(args, ...);
}
```

Слушает он свой порт в обоих режимах: `--src-bind-addr` / `--src-bind-port`
(по умолчанию `0.0.0.0:20000`) и групповую рассылку `--multicast-bind-ip` /
`--multicast-subscribe-port` (20001).

**Главное для нас:** флаг `--grpc-service-port` -- *«GRPC port for serving decoded
shreds as Solana entries»* -- лежит в `CommonArgs`, то есть **работает и в
`ForwardOnly`**. Значит путь «чужие шреды на наш порт → записи Solana по gRPC»
открывается без block engine и без ключа Jito вовсе.

**Оговорка, которую надо сказать вслух:** на приёмном порту аутентификации НЕ НИКАКОЙ.
Шреды примет кто угодно, кто до порта дотянется, -- и подложные тоже. Порт надо
закрывать снаружи (адрес Triton, файрвол), иначе мы читаем то, что нам подсунут.

## 2. Что из `solana-ledger` нужно, чтобы собрать записи из data-шредов

Из `deshred.rs` (~650 строк) используется:

```rust
use solana_ledger::{
    blockstore::MAX_DATA_SHREDS_PER_SLOT,
    shred::{merkle::{Shred, ShredCode}, ReedSolomonCache, ShredType, Shredder},
};
use solana_entry::entry::Entry;
```

Сама сборка -- две строки:

```rust
let deshredded_payload = Shredder::deshred(to_deshred.iter().map(|s| s.payload()))?;
let entries = bincode::deserialize::<Vec<solana_entry::entry::Entry>>(&deshredded_payload)?;
```

**Без восстановления по кодам достаточно:** `solana_ledger::shred` (`Shredder::deshred`,
`merkle::Shred` для разбора шреда из байтов, `ShredType`), `solana_entry::entry::Entry`
и `bincode`. `ReedSolomonCache`, `merkle::recover` и `ShredCode` нужны ТОЛЬКО для
восстановления недостающих data-шредов из кодовых.

**Чем за это платим.** Единица работы -- отрезок между флагами `DataComplete`, и
десериализация идёт только когда в отрезке нет пропусков: комментарий в файле --
*«attempting to deserialize into solana entries when there are no missing shreds»*.
То есть без восстановления мы получаем записи лишь тех FEC-наборов, что пришли
ЦЕЛИКОМ; неполные просто теряются. Сколько это в доле сделок -- из кода не видно, это
замер на нашем канале.

## 3. Оценка в днях

* **Взять готовый прокси** (`ForwardOnly` + `--grpc-service-port`, Triton пишет в наш
  порт, мы читаем `SubscribeEntries`): **1 день** на поднять и проверить, плюс день на
  замер «сколько записей доходит и на сколько раньше нашей подписки». Кода писать не
  надо -- это настройка.
* **Свой декодер на Python** (если зачем-то не брать прокси): **недели**, и это
  постоянный долг. Один `deshred.rs` -- 650 строк Rust поверх `solana-ledger`: разбор
  заголовков шреда, варианты merkle, группировка FEC, отрезки по `DataComplete`. При
  каждом изменении Agave раскладка шреда может поехать, и ломаться это будет молча.

**Что советую:** не писать декодер. Если штаб после числа Triton решит пробовать --
брать прокси в `ForwardOnly`, порт закрыть на адрес Triton, читать записи по gRPC.

## Чего в этом ответе нет

Не запускал прокси, не мерил задержку, не проверял, что именно и в каком виде отдаёт
Triton (это число штаба). Всё выше -- из исходников, с цитатами; где вывод мой, а не
код, так и написано.
