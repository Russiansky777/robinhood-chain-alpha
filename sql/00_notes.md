# Заметки к SQL

Все запросы написаны под **DuneSQL** (Trino-диалект Dune). Не выполнялись
вживую — нет сетевого доступа к `api.dune.com` из текущей среды (см.
`docs/DATA_ACCESS.md`). Перед первым реальным запуском проверьте:

1. **Название схемы для Robinhood Chain.** Использую `blockchain =
   'robinhood_chain'` в `dune.dex.trades` (унифицированная кросс-чейн
   таблица трейдов, которую Dune поддерживает для всех подключённых
   чейнов) — это самый устойчивый способ не зависеть от точного имени
   декодированных таблиц конкретного контракта. Если `dex.trades` ещё не
   покрывает Robinhood Chain на момент запуска (индексация нового чейна
   иногда отстаёт от появления сырых данных на несколько дней/недель),
   фолбэк — сырые декодированные логи:
   - Uniswap v3: `uniswap_v3_robinhood_chain.Pair_evt_Swap`
   - Uniswap v4: `uniswap_v4_robinhood_chain.PoolManager_evt_Swap`
   - Фабрики (для гейта снайперов): `uniswap_v3_robinhood_chain.Factory_evt_PoolCreated`,
     `uniswap_v4_robinhood_chain.PoolManager_evt_Initialize`
   Точные имена нужно свериться со списком таблиц в Dune UI
   (Data Explorer → search "robinhood") — они могут отличаться на
   момент запуска, чейн подключён недавно (июль 2026).

2. **Base/quote токены** (WETH, USDC, USDT и т.п.) сейчас матчатся по
   `token_bought_symbol`/`token_sold_symbol` — это удобно, но
   спуфабельно (любой токен может назвать себя "USDC"). Перед боевым
   прогоном замените на матчинг по адресу контракта
   (`token_bought_address IN (...)`) — впишите реальные адреса
   канонических WETH/USDC/USDT на Robinhood Chain (chain id 4663) из
   официального bridge-реестра, см.
   [docs.robinhood.com/chain](https://docs.robinhood.com/chain/).

3. **Realized PnL** считается методом **weighted-average cost basis**
   (не строгий FIFO) — стандартный компромисс для чистого SQL без
   построчного процедурного кода. Подробно — в комментариях
   `03_wallet_agg_july.sql`. Открытые (нереализованные) позиции на конец
   месяца в PnL не входят по определению "реализованного" PnL.

4. Даты как параметры Dune (`{{start_date}}`/`{{end_date}}`) — задавайте
   при вызове через API (`analysis/dune_client.py` подставляет их из
   `config.py`), чтобы один и тот же запрос переиспользовался для июля и
   августа без дублирования SQL.
