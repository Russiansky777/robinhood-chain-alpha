# P4 recon — перп-рынки на сток-токены (Lighter, Aster), без Dune

**Дата:** 2026-09-01. **Статус:** разведка (не дизайн). По явному указанию
владельца `docs/P4_DESIGN.md` в этом проходе НЕ создаётся — дизайн
замораживается в штабе после того, как этот файл прочитан. Ничего ниже
не является зафиксированным критерием.

## Метод и ограничение среды (важно для доверия к цифрам)

Задание — "публичные API/документация, без Dune". В этой рабочей среде
(Claude Code on the web, сетевая политика окружения) прямой исходящий
доступ к произвольным доменам **заблокирован egress-прокси** —
проверено эмпирически: `fapi.asterdex.com`, `docs.asterdex.com`,
`apidocs.lighter.xyz`, `mainnet.zklighter.elliot.ai` и даже
`en.wikipedia.org` возвращают `EGRESS_BLOCKED` при прямом запросе
(curl и WebFetch одинаково). Работают только: (а) веб-поиск
(агрегированные сводки со ссылками на источники) и (б) `github.com` /
`raw.githubusercontent.com` — по ним удалось получить **официальную,
первоисточную** документацию API (репозиторий `asterdex/api-docs`,
SDK `elliottech/lighter-python`).

Следствие: часть цифр ниже (эндпоинты, поля схемы, пути) —
**первоисточник, дословно из официальной документации на GitHub**.
Другая часть (актуальный список сток-тикеров прямо сейчас, текущие
значения funding rate, точные лимиты плеча/позиции по акциям) —
**вторичные источники** (новости, агрегаторы, блог-посты), т.к. живые
REST-вызовы (`GET /fapi/v3/exchangeInfo`, `GET /api/v1/orderBooks` и
т.д.) из этой среды не прошли. Каждый пункт ниже помечен явно
(**[первоисточник]** / **[вторично, требует live-проверки]**).
Перед заморозкой дизайна P4 в штабе рекомендуется повторить live-пуллы
из окружения с открытым egress (например, GH Actions runner этого
репозитория, как уже делается для Alchemy RPC в Sprint R1) — это
дёшево (публичные API, без ключа или с бесплатным ключом) и снимет
вторичность.

## Lighter (zkLighter, Robinhood Chain's perps partner)

**Базовая инфраструктура [первоисточник, `elliottech/lighter-python`,
официальный Python SDK]:**

- Base URL (mainnet): `https://mainnet.zklighter.elliot.ai`
- `GET /api/v1/orderBooks` — метаданные всех торговых пар (market_id,
  комиссии, минимальные размеры, precision). Без авторизации.
- `GET /api/v1/orderBookDetails`, `GET /api/v1/orderBookOrders`,
  `GET /api/v1/assetDetails` — детали книги/актива.
- `GET /api/v1/funding-rates` — funding rates. Схема ответа
  (`FundingRates`) в SDK-доках не детализирована построчно (нужен
  live-вызов, чтобы увидеть поля).
- Официальный веб-документ `docs.lighter.xyz/trading/funding` описывает
  механику (см. ниже) — сам домен `docs.lighter.xyz` заблокирован для
  прямого фетча в этой среде, содержание — по сводке веб-поиска.

**Рынки на сток-токены [вторично, требует live-проверки]:**

- Lighter — партнёр перпетуалов Robinhood Chain (50/50 revenue split с
  Robinhood), листинг на mainnet с октября 2025.
- Принимает токенизированные акции (Robinhood Stock Tokens: NVDA, GOOG,
  AAPL и др.) как **маржинальное обеспечение** для перпетуалов — это
  отдельно от того, есть ли сами акции как БАЗОВЫЙ актив перпа.
- Отдельно запущены **equity-перпетуалы** (сами акции как underlying):
  "24-часовая, будни" торговля с анонсированным расширением до полного
  24/7; упоминаются как минимум Apple и S&P 500 (индекс), общий тон
  источников — "крупные технологические акции и индексы". **Полного
  построчного списка тикеров не найдено** ни через поиск, ни через
  документацию — заблокированный `orderBooks` дал бы точный список за
  один бесплатный вызов.
- Общий масштаб биржи: ~206 торговых пар всего (крипто+прочее),
  BTC/USDC — самая активная пара (для контекста величины биржи, не
  специфично для акций).

**Funding — механика [вторично, по сводке `docs.lighter.xyz/trading/funding`]:**

- Премия считается **почасово** (time-weighted average 60 поминутных
  премий за последний час).
- Выплата funding распределена по компонентам, привязанным к 8-часовому
  циклу премии (аналог стандартной перп-механики), но **платежи
  peer-to-peer без биржевой комиссии**.
- Ставка = премия + фиксированная процентная компонента (разница ставок
  base/quote валют).
- Явного отдельного значения funding-интервала ИМЕННО для
  equity-перпов Lighter не найдено — нужна live-проверка per-symbol
  (по аналогии со схемой Aster ниже, где интервал конфигурируется
  per-symbol).

**Лимиты (плечо/позиция) [не найдено]:** ни через поиск, ни через
документацию SDK не нашлось числового значения макс. плеча/позиции для
equity-перпов Lighter конкретно. `docs.lighter.xyz` (заблокирован для
фетча) — вероятное место, где это документировано; альтернативно —
`GET /api/v1/orderBooks` вернул бы лимиты per-market живым вызовом.

## Aster (мультичейн перп-DEX)

**Базовая инфраструктура [первоисточник, репозиторий
`asterdex/api-docs`, файл `V3(Recommended)/EN/aster-finance-futures-api-v3.md`]:**

- `GET /fapi/v3/exchangeInfo` — полный список символов, правила,
  rate limits. Поле `underlyingType` в объекте символа (пример в доке
  — `"COIN"`; для акций ожидается отдельное значение типа `"STOCK"` —
  не подтверждено дословно, пример в доке был не по акции) +
  `underlyingSubType` (массив, доп. категоризация).
- `GET /fapi/v3/fundingInfo` — конфиг funding по символу: **поле
  `fundingIntervalHours` есть и явно конфигурируется per-symbol**
  (примеры в доке: `8` и `4` для двух разных крипто-пар) + `fundingFeeCap`/
  `fundingFeeFloor` (потолок/пол ставки).
- `GET /fapi/v3/fundingRate` — история funding: параметры `symbol`,
  `startTime`, `endTime`, `limit` (default 100, **max 1000** записей за
  вызов). Без авторизации.
- `GET /fapi/v3/premiumIndex` — live mark price + `lastFundingRate` +
  `nextFundingTime`.
- `GET /fapi/v3/leverageBracket` (USER_DATA — **требует API-ключ**) —
  notional/leverage brackets; детальная схема не выведена полностью в
  доступной части документа.
- Rate limits (пример из документации, testnet-файл — числа могут
  отличаться на mainnet, не подтверждено live-вызовом):
  `REQUEST_WEIGHT` 2400/мин, `ORDERS` 1200/мин; заголовок ответа
  `X-MBX-USED-WEIGHT-(interval)` даёт текущий расход веса.

**Рынки на сток-токены [вторично, по сводке новостей/`asterpedia.com/markets`]:**

- **8 токенизированных сток-перпов + 3 товарных перпа** (золото, нефть
  и др.), торгуются 24/7 (не только часы рынка), цена — от
  third-party оракулов (не собственных фидов Aster).
- Подтверждено поимённо (7 из 8): **AMZN, AAPL, GOOG, META, MSFT,
  NVDA, TSLA**. 8-й тикер **не подтверждён** ни одним найденным
  источником — нужен live `GET /fapi/v3/exchangeInfo` с фильтром по
  `underlyingType`, чтобы закрыть список точно.
- Промо: 0% maker/taker комиссия на NVDA и TSLA (и, по части
  источников, шире — на все сток-тикеры) — тактическая акция, не
  вечная величина, надо перепроверять на момент дизайна P4.

**Funding [частично первоисточник + вторично]:**

- Схема API подтверждает **per-symbol конфигурируемый интервал**
  (`fundingIntervalHours`, первоисточник выше).
- Конкретно для сток-перпов агрегатор `asterpedia.com/markets`
  описывает витрину как показывающую "8-hour funding rates" —
  **вторично**, трактуем как «скорее всего 8ч для сток-группы», не
  как подтверждённый факт per-symbol (в примере доки сам интервал
  4ч тоже встречается у других инструментов — то есть per-symbol
  дизайн подтверждён, но конкретное значение для каждого из 8
  сток-тикеров требует live `fundingInfo`-вызова).
- История funding доступна публично и без ключа
  (`GET /fapi/v3/fundingRate`, до 1000 записей за вызов,
  фильтр по времени) — **дёшево докачать реальную историю ставок**
  без Dune при живом вызове.

**Лимиты [вторично]:**

- Плечо на сток-перпы: **до 50x** (long/short, выбирается при открытии
  позиции) — по нескольким независимым вторичным источникам
  (в отличие от «Simple/Degen Mode» с плечом до 1001x, которое
  относится к крипто-парам, не к акциям).
- Точный лимит позиции (notional cap) по каждому сток-тикеру —
  не найден; API-путь для точного ответа существует
  (`GET /fapi/v3/leverageBracket`, но требует авторизованный ключ) и
  сам `exchangeInfo` (публичный, без ключа) содержит per-symbol
  фильтры лимитов ордера/позиции — не вытянут live в этой сессии.

## Сводная таблица (агрегат)

| | Lighter | Aster |
|---|---|---|
| Роль на Robinhood Chain | официальный партнёр перпетуалов (50/50 revenue split) | сторонняя мультичейн площадка, отдельно листит токенизированные акции |
| Сток-токены Robinhood Chain как **обеспечение** | да (NVDA/GOOG/AAPL и др., подтверждено прессой) | не подтверждено (Aster торгует токенизированными акциями как **базовым активом** перпа, оракул — third-party, не обязательно тот же реестр, что на Robinhood Chain) |
| Сток-перпы (акция как underlying) | да, экспансия к 24/7, список тикеров не подтверждён построчно | да, **8 подтверждено как количество**, 7/8 тикеров названы поимённо |
| Публичный список рынков (без ключа) | `GET /api/v1/orderBooks` (не вызван live в этой сессии) | `GET /fapi/v3/exchangeInfo` (не вызван live в этой сессии) |
| История funding (без ключа) | `GET /api/v1/funding-rates` (схема не детализирована) | `GET /fapi/v3/fundingRate`, до 1000 записей/вызов, фильтр по времени — **первоисточник, подтверждено** |
| Частота выплат funding | почасовой расчёт премии, распределение похоже на 8ч-цикл (не подтверждено числом именно для equity) | **per-symbol конфигурируемо** (`fundingIntervalHours`, подтверждено в схеме API); для сток-группы вторично сообщается 8ч |
| Плечо на сток-перпы | не найдено числом | до 50x (вторично) |
| Комиссии | не найдено отдельно для equity | 0% на NVDA/TSLA (промо, вторично) |

## Итог разведки (не вердикт, не дизайн)

Обе площадки реально существуют, имеют документированные публичные
(без ключа) эндпоинты для списка рынков и истории funding, что
подтверждает техническую возможность собрать P4 без Dune и почти без
затрат (публичные REST-вызовы, не блокчейн-индексация). Но эта сессия
не смогла выполнить ни одного живого вызова из-за сетевой политики
окружения — все цифры о ТЕКУЩЕМ списке тикеров, точных ставках
funding и лимитах плеча/позиции нужно **перепроверить живым вызовом**
(рекомендация: GH Actions runner репозитория, как уже используется для
Alchemy RPC в Sprint R1 — `.github/workflows/run_sprint_r1.yml`) перед
тем, как штаб замораживает дизайн P4 на конкретных числах.

## Источники

- [Lighter Makes Stock Tokens Eligible Collateral on Robinhood Chain — The Defiant](https://thedefiant.io/news/defi/lighter-makes-stock-tokens-eligible-collateral-on-robinhood-chain)
- [Robinhood Chain goes live on mainnet alongside 24/7 tokenized stocks, Lighter perps — The Block](https://www.theblock.co/news/business/2026-07-01-robinhood-chain-goes-live-mainnet-alongside-24-7-tokenized-stocks-lighter-perps-planned-crypto-agentic-trading-406918)
- [Lighter DEX Launches 24/5 Equity Perps Trading — CoinMarketCap Academy](https://coinmarketcap.com/academy/article/lighter-dex-launches-245-equity-perps-trading)
- [Lighter Perpetual Futures: 24-Hour Weekday Trading for Stock Perpetuals — CryptoRank](https://cryptorank.io/news/feed/f5397-lighter-24-hour-stock-perpetuals-trading)
- [Funding | Lighter Docs (via web search summary)](https://docs.lighter.xyz/trading/funding)
- [elliottech/lighter-python — official Python SDK](https://github.com/elliottech/lighter-python)
- [lighter-python/docs/OrderApi.md](https://github.com/elliottech/lighter-python/blob/main/docs/OrderApi.md)
- [lighter-python/docs/FundingApi.md](https://github.com/elliottech/lighter-python/blob/main/docs/FundingApi.md)
- [asterdex/api-docs — official Aster API documentation repo](https://github.com/asterdex/api-docs)
- [aster-finance-futures-api-v3.md](https://github.com/asterdex/api-docs/blob/master/V3(Recommended)/EN/aster-finance-futures-api-v3.md)
- [Aster Markets — Live Perp Prices, Funding & Volume — Asterpedia](https://asterpedia.com/markets)
- [Stock Perpetuals — Aster docs (via web search summary)](https://docs.asterdex.com/trading/perpetuals/stock-perpetuals)
- [Aster Launches 24/7 Stock Perpetual Contracts Trading — etf.com](https://www.etf.com/sections/news/aster-launches-247-stock-perpetual-contracts-trading-exposure-us-equities)
- [Aster DEX eliminates trading fees on NVDA and TSLA stock perpetuals — Cryptopolitan](https://www.cryptopolitan.com/aster-dex-eliminate-trading-fees-nvda-tsla/)
- [What Is Aster Crypto (ASTER)? Complete 2026 Guide — Coin Bureau](https://coinbureau.com/review/what-is-aster-crypto)
