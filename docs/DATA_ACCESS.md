# Доступ к данным — статус и ограничения

## Что проверено в этой сессии (2026-08-31)

1. **Robinhood Chain существует и данные по ней доступны на Dune.**
   Подтверждено веб-поиском:
   - Dune объявил полную поддержку Robinhood Chain (transactions, decoded
     logs, bridge flows) — [dune.com/blog/robinhood-chain-is-live-on-dune](https://dune.com/blog/robinhood-chain-is-live-on-dune)
   - Есть минимум два community-дашборда как отправная точка:
     - **Entropy Advisors — "Robinhood Chain: Network Overview"**
       [dune.com/entropy_advisors/robinhood-chain-network-overview](https://dune.com/entropy_advisors/robinhood-chain-network-overview)
       (первый дашборд по чейну; на момент публикации — $55M TVL, 101
       stock-токен)
     - **"Robinhood Chain: The Full Trenches"**
       [dune.com/dch/robinhood-chain-the-full-trenches](https://dune.com/dch/robinhood-chain-the-full-trenches)
     - Блокчейн-хаб: [dune.com/blockchains/robinhood](https://dune.com/blockchains/robinhood)
   - Uniswap v2/v3/v4 и UniswapX — primary AMM на чейне с первого дня
     ([blog.uniswap.org/robinhood-chain-is-live](https://blog.uniswap.org/robinhood-chain-is-live)),
     так что декодированные таблицы Uniswap на Dune должны появиться
     достаточно быстро — это отправная точка для `sql/*.sql`.

2. **У этой исполняющей среды (агента) нет доступа к Dune/Alchemy.**
   - Нет `DUNE_API_KEY` / `ALCHEMY_API_KEY` — ни в env, ни в подключённых
     коннекторах (проверено — список коннекторов пуст).
   - Прямой сетевой запрос до `api.dune.com` и `*.alchemy.com` из этой
     среды получает `403` на уровне egress-прокси (`connect_rejected`,
     "policy denial or upstream failure") — то есть даже с ключом эта
     конкретная сессия физически не достучится до этих API.
   - Это **не** лимит бесплатного тарифа Dune (2500 кредитов) — до
     проверки лимита дело не дошло, заблокирован сам сетевой путь.

## Что это значит для пайплайна

Весь код (`sql/`, `analysis/`) написан, закоммичен и готов к запуску, но
**не выполнялся** — цифры в `docs/RESULTS.md` являются шаблоном/заглушкой,
а не результатом.

## Как это разблокировать — 3 варианта

### Вариант 1 (рекомендуется): GitHub Actions
GitHub-раннеры не сидят за этим ограниченным прокси и имеют обычный
интернет-доступ. План:
1. Получить `DUNE_API_KEY` на [dune.com/settings/api](https://dune.com/settings/api)
   (free tier: 2500 credits/мес).
2. Положить его в GitHub → Settings → Secrets and variables → Actions →
   `DUNE_API_KEY`. Опционально `ALCHEMY_API_KEY` туда же.
3. Запустить `.github/workflows/run_pipeline.yml` вручную
   (Actions → Run workflow) или дождаться push в `main`.
4. Workflow гоняет `analysis/run_pipeline.py`, кладёт результат в
   `docs/RESULTS.md` и коммитит его обратно в ветку.

Это не тратит кредиты «молча» — `run_pipeline.py` логирует количество
Dune credits, потраченных на каждый запрос (см. `analysis/dune_client.py`),
и падает с понятной ошибкой при 402/429 от Dune, вместо ретраев.

### Вариант 2: ключ через сессию агента
Если вы (пользователь) добавите `DUNE_API_KEY` в переменные окружения
именно этой remote-сессии — сеть всё равно заблокирована политикой
окружения. Нужно пересоздать окружение с сетевой политикой, разрешающей
`api.dune.com` (и `*.alchemy.com` как fallback), либо явно передать
разрешённые домены при создании environment. Без этого ключ не поможет.

### Вариант 3: запуск локально
`cp .env.example .env`, вписать ключ, `pip install -r
analysis/requirements.txt && python analysis/run_pipeline.py` на своей
машине — там нет ограничений этого прокси.

## Если упрётесь в лимит Dune free tier (2500 credits)

Free tier Dune SQL-запросы стоят кредиты за исполнение (не за строки
результата), крупные full-scan запросы по логам транзакций за месяц
активности могут быть дорогими. Стратегия экономии, уже заложенная в
`sql/`:
- Каждый запрос фильтрует по диапазону блоков/дат как можно раньше
  (partition pruning), а не тянет весь Uniswap-трейд-датасет и фильтрует
  потом.
- `sql/03_wallet_agg_july.sql` и `sql/06_wallet_agg_august.sql`
  агрегируют на стороне Dune (одна строка на кошелёк), а не тянут
  построчные свопы в Python — это на порядок дешевле по кредитам.
- `analysis/dune_client.py` кеширует результаты запросов на диск
  (`analysis/output/cache/`) по query_id+params, чтобы повторный прогон
  пайплайна не пережигал кредиты повторно.

Если и этого не хватит — **fallback на прямой RPC через Alchemy**:
`analysis/alchemy_fallback.py` (заготовка) тянет логи `Swap`-топиков
Uniswap v3 (`Pair`) и v4 (`PoolManager`) напрямую через
`eth_getLogs`/`alchemy_getAssetTransfers` за нужный диапазон блоков и
агрегирует их в Python тем же способом, что и SQL-версия. Это медленнее
и требует знать адреса пулов заранее (или предварительно найти их через
`PoolCreated`/`Initialize` события фабрики), но не завязано на
Dune-кредиты вообще — только на Alchemy compute units (щедрый free tier).

**Я не буду тратить Dune-кредиты молча**: если пайплайн упрётся в лимит,
`run_pipeline.py` останавливается на этом шаге, логирует остаток кредитов
и явно спрашивает, переключаться ли на Alchemy fallback, вместо того
чтобы тихо жечь оставшийся бюджет.
