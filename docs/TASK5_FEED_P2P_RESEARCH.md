# Задача 5 -- фид секвенсера: P2P/альтернативные пути мимо Cloudflare

Владелец, 2026-09-12: "Ноду -- исследование по прошлому заданию (P2P
путь чтения мимо Cloudflare), пока только документация." Это
продолжение раздела PROJECT_STATE.md "Прямые пути мимо Cloudflare"
(предыдущий раунд, три под-проверки а/б/в). Всё ниже -- ТОЛЬКО
документация по реальным, открытым источникам (Arbitrum-документация,
Chainstack), НИЧЕГО не реализовано и не запущено.

**Граница, подтверждённая владельцем явно в предыдущем раунде и
сохраняемая здесь без изменений: поиск origin IP за Cloudflare и
прямое подключение в обход защиты -- НЕ исследуется и не
предпринимается.** Всё ниже -- официальные, документированные
Arbitrum/Robinhood механизмы, не bypass какой-либо защиты.

## 1. Реальный вывод: фид Nitro -- НЕ P2P/gossip, а client-server relay

Из официальной документации Arbitrum (docs.arbitrum.io, через WebSearch,
WebFetch на сам домен заблокирован сетевой политикой этой сессии,
как и ранее с binaries.soliditylang.org):

> "Arbitrum nodes prioritize data retrieval from the parent chain and
> rely on the sequencer for real-time updates, **deviating from the
> traditional P2P synchronization approach** used by Ethereum nodes."

> "A relay does not sequence, validate, or execute anything -- it reads
> an upstream feed and distributes it to more subscribers."

Т.е. "P2P" в смысле децентрализованного gossip-протокола между
произвольными узлами (как у Ethereum execution/consensus клиентов) --
**в Nitro не существует**. Единственный механизм -- **relay**: один
узел читает ОДИН upstream (сам секвенсер или другой relay) и раздаёт
дальше подписчикам через тот же WebSocket-протокол feed'а. Это
принципиально меняет постановку исходного вопроса: "P2P путь мимо
Cloudflare" в буквальном смысле недостижим, потому что такого пути в
самой архитектуре Nitro нет -- но **relay-механизм даёт реальный,
законный способ сократить число подключений к публичному
Cloudflare-хосту**, см. §2.

Источники:
- [Frequently asked questions: Run a node](https://docs.arbitrum.io/node-running/faq)
- [The Sequencer and Censorship Resistance](https://docs.arbitrum.io/how-arbitrum-works/deep-dives/sequencer)
- [Inside Arbitrum Nitro](https://docs.arbitrum.io/how-arbitrum-works/inside-arbitrum-nitro)

## 2. Реальный, законный путь: собственный feed relay сокращает число подключений к публичному фиду

Официальная команда запуска relay (Arbitrum One, из документации,
формат идентичен для любого Nitro-чейна, только `--node.feed.input.url`/
`--chain.id` меняются на робинхудовские):

```
docker run --rm -it -p 0.0.0.0:9642:9642 --entrypoint relay \
  offchainlabs/nitro-node:v3.11.2-3599aca \
  --node.feed.output.addr=0.0.0.0 \
  --node.feed.input.url=wss://arb1-feed.arbitrum.io/feed \
  --chain.id=42161
```

Relay поднимает broadcast-сервер на порту **9642**, отдаёт `/feed`
(WebSocket) локальным подписчикам. Для Robinhood Chain это было бы:

```
--node.feed.input.url=wss://feed.mainnet.chain.robinhood.com --chain.id=4663
```

**Что это реально даёт:** сейчас КАЖДЫЙ отдельный процесс/скрипт/GH
Actions запуск (structure probe, dry-run, бот) делает СВОЁ СОБСТВЕННОЕ
подключение к `wss://feed.mainnet.chain.robinhood.com` -- именно это,
по текущей рабочей гипотезе (см. PROJECT_STATE.md, "cooldown по IP"),
и провоцирует повторные 403. Если поднять ОДИН relay (например, на
Ohio, постоянно работающий процесс, не GH Actions job) и завести
ВСЕХ локальных потребителей на `ws://localhost:9642/feed` вместо
публичного URL напрямую -- к публичному Cloudflare-хосту идёт РОВНО
ОДНО подключение вместо N. Это НЕ обходит защиту Cloudflare (то самое
одно подключение всё ещё идёт туда же, через тот же edge, с тем же
риском 403 при реконнекте) -- это **сокращает частоту, с которой мы
вообще пытаемся переподключаться**, что прямо бьёт по симптому
(частые реконнекты = частые 403), не по причине.

**Честная оговорка:** relay сам по себе НЕ решает первопричину 403
(она по-прежнему не до конца понятна, см. PROJECT_STATE.md) -- если тот
единственный upstream-коннект relay'я тоже словит 403, все подписчики
останутся без данных одновременно. Reliability не выше, чем у одного
прямого подключения -- выгода ТОЛЬКО в снижении числа попыток
подключения при множестве параллельных потребителей (сейчас это не
основная проблема проекта: бот и так использует одно подключение per
процесс, а разные диагностические скрипты запускаются НЕ одновременно).
Наибольшая практическая польза была бы, если разные VPS (NL/Dallas/Ohio)
одновременно нуждались в фиде -- сейчас это не так (Ohio -- единственный
активный источник для фида в этом раунде).

Источники:
- [How to run a feed relay](https://docs.arbitrum.io/run-arbitrum-node/run-feed-relay)
- [How to read the sequencer feed](https://docs.arbitrum.io/run-arbitrum-node/sequencer/read-sequencer-feed)

## 3. `--node.feed.input.url` поддерживает список URL через запятую -- но альтернативного URL у Robinhood нет

Документация подтверждает: `node.feed.input.url` -- "a comma-separated
list of WebSocket URLs for all sequencer feed outputs" (можно указать
несколько upstream'ов для избыточности). **Реально проверено (WebSearch)
специально под этим углом: второй/резервный публичный URL фида
Robinhood Chain НЕ задокументирован нигде** -- ни в
`docs.robinhood.com/chain/run-a-full-node/`, ни у сторонних гайдов
(Quicknode, GetBlock, Titan Locker) -- везде фигурирует только
`wss://feed.mainnet.chain.robinhood.com`. Этот механизм остаётся
теоретически доступным, но СЕЙЧАС нечем воспользоваться -- честный
отрицательный результат, не гадаем про несуществующий второй URL.

## 4. Chainstack `rhfeed` -- повторно проверено: тот же самый хост, не альтернатива

Прошлый раунд уже установил, что Chainstack декодирует тот же публичный
relay, не даёт отдельный канал. Этот раунд подтвердил КОНКРЕТНО: сам
инструмент `chainstacklabs/robinhood-chain-sequencer-feed` подключается
к **`wss://feed.mainnet.chain.robinhood.com`** -- тому же хосту, что уже
используется этим проектом напрямую. Никакой альтернативной
инфраструктуры/хоста у Chainstack для ЭТОГО конкретного фида нет.

Источник:
- [chainstacklabs/robinhood-chain-sequencer-feed (GitHub)](https://github.com/chainstacklabs/robinhood-chain-sequencer-feed)

## 5. Полностью независимый от фида и Cloudflare путь: чтение батчей из L1 (медленно, но не про "блок 0")

Из документации:

> "A node with a working parent chain connection still syncs from
> batches without any feed. Sync is slower but correct, so a feed
> outage degrades latency rather than halting your node."

Секвенсер публикует данные ДВАЖДЫ: (1) real-time WS-фид (то, с чем мы
боремся) и (2) батчи, публикуемые в calldata на L1 (базовом слое).
Полноценный Nitro-узел может синхронизироваться ИСКЛЮЧИТЕЛЬНО из
батчей на L1, вообще не подключаясь к WS-фиду -- это **полностью
не зависит от Cloudflare** (обычный публичный L1 RPC/индексация
calldata, не WS, не тот хост, не тот протокол вообще).

**Компромисс:** задержка -- на уровне финальности батча на L1
(секунды-десятки секунд типично для Orbit-чейнов, точное число для
Robinhood Chain не измерено этой сессией), НЕ real-time. Это делает
путь **бесполезным для цели "блок 0"** (вся эта работа существует
именно ради минимальной задержки включения), но потенциально полезным
как **независимый, всегда доступный fallback** для менее
латентно-чувствительных задач этого проекта (например, периодическое
обновление состояния пулов раз в несколько секунд без риска 403 вообще,
если WS-фид снова окажется недоступен надолго).

Источники:
- [Data Availability](https://docs.arbitrum.io/run-arbitrum-node/data-availability)
- [Node Stops Syncing (OffchainLabs/nitro #1742)](https://github.com/OffchainLabs/nitro/issues/1742)

## Итог и статус

Ничего из этого не реализовано -- по прямому указанию владельца, это
раунд ТОЛЬКО документации. Если владелец решит двигаться дальше:

- **§2 (собственный relay)** -- самый конкретный, реализуемый шаг:
  разворачивается официальным Docker-образом `offchainlabs/nitro-node`
  на существующем VPS (Ohio), не требует нового кода бота -- только
  смена `FEED_URL` с публичного на `ws://localhost:9642/feed`. Даёт
  реальную, но ограниченную пользу (см. честную оговорку выше) -- не
  решает первопричину 403.
- **§5 (L1-батчи)** -- отдельная, гораздо более крупная задача (нужен
  полноценный синхронизированный Nitro full node, не просто relay) --
  имеет смысл только как медленный fallback, не как решение для "блок 0".
- **§3** -- ждёт появления второго документированного feed-URL (сейчас
  такого нет).

Cron `:51` на NL, SSH read-only на NL/Dallas, слепой OOS -- не
затронуты этим исследованием (чисто документация, ничего не менялось
ни на одном VPS).
