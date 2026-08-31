# Заметки к SQL

Все запросы написаны под **DuneSQL** (Trino-диалект Dune).

**Обновление 2026-08-31: реально прогнано на Dune, схема подтверждена.**
Первое предположение о названиях схем (см. историю git) было неверным —
поймали это через реальный `RuntimeError` от Dune (`Schema
'uniswap_v3_robinhood_chain' does not exist`), продиагностировали через
`analysis/_probe_schema.py` (запрос к `information_schema.tables`) и
поправили. Актуальное, подтверждённое:

1. **`dune.dex.trades`**: значение `blockchain` для этого чейна —
   **`'robinhood'`**, НЕ `'robinhood_chain'` (несмотря на официальное
   название сети и chain id 4663). Запрос `select count(*) from
   dex.trades where blockchain = 'robinhood_chain'` возвращает 0 строк;
   `'robinhood'` — корректное значение.
2. **Декодированные схемы контрактов Uniswap**: `uniswap_v3_robinhood` и
   `uniswap_v4_robinhood` (без суффикса `_chain`). Таблицы —
   `<contract>_evt_<event>` / `<contract>_call_<method>`, всё в нижнем
   регистре, без разделителей между словами названия контракта:
   - `uniswap_v3_robinhood.uniswapv3factory_evt_poolcreated`
   - `uniswap_v3_robinhood.uniswapv3pool_evt_swap` / `..._evt_initialize`
   - `uniswap_v4_robinhood.poolmanager_evt_initialize` / `..._evt_swap`
   - Также есть готовые `uniswap_v3_robinhood.base_trades` и
     `uniswap_v4_robinhood.swaps` / `base_trades` — Dune-агрегированные
     таблицы трейдов на уровне DEX-версии; не использовали (не
     диагностировали их колонки), но потенциально дешевле/проще, чем
     `dex.trades` — кандидат на упрощение в следующем спринте.
3. **Сырые таблицы чейна и ERC20-трансферы**: по аналогии с
   `blockchain='robinhood'` использую схемы `robinhood.transactions`,
   `robinhood.traces`, `erc20_robinhood.evt_transfer` — эта часть
   выведена по паттерну (`<protocol>_robinhood`), а не подтверждена
   отдельным probe-запросом. Если следующий прогон упадёт на этих
   именах — тот же процесс: `_probe_schema.py` → правим → перезапускаем.

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
