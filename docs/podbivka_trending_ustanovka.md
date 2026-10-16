# Регистратор трендингов на lab-miami — установка

Писал CODE-2 (ветка `claude/podbivka`). **Хост я не трогаю** — ставит Code-1 или владелец.
Кошельковых ключей скрипт не просит и не читает: все запросы — публичные GET.

Скрипт: `analysis/podbivka_trending.py`. Суточный пуш: `analysis/podbivka_trending_push.sh`.

## 1. Что пишется

9 источников, 16 списков. Частота — колонка «каждые».

| источник | список | каждые | URL |
|---|---|---|---|
| jupiter | toptrending_5m | 1 мин | `lite-api.jup.ag/tokens/v2/toptrending/5m?limit=100` |
| jupiter | toptrending_1h | 1 мин | `lite-api.jup.ag/tokens/v2/toptrending/1h?limit=100` |
| jupiter | toptraded_5m | 1 мин | `lite-api.jup.ag/tokens/v2/toptraded/5m?limit=100` |
| jupiter | toptraded_1h | 1 мин | `lite-api.jup.ag/tokens/v2/toptraded/1h?limit=100` |
| geckoterminal | trending_pools | 1 мин | `api.geckoterminal.com/api/v2/networks/solana/trending_pools` |
| geckoterminal | new_pools | 1 мин | `api.geckoterminal.com/api/v2/networks/solana/new_pools` |
| dexscreener | token_boosts_latest | 1 мин | `api.dexscreener.com/token-boosts/latest/v1` |
| dexscreener | token_boosts_top | 1 мин | `api.dexscreener.com/token-boosts/top/v1` |
| dexscreener | token_profiles_latest | 1 мин | `api.dexscreener.com/token-profiles/latest/v1` |
| gmgn | trending_5m | 1 мин | `gmgn.ai/defi/quotation/v1/rank/sol/swaps/5m` |
| gmgn | new_pairs | 1 мин | `gmgn.ai/defi/quotation/v1/pairs/sol/new_pairs?limit=50` |
| pumpfun | koth | 1 мин | `frontend-api-v3.pump.fun/coins/king-of-the-hill` |
| pumpfun | currently_live | 1 мин | `frontend-api-v3.pump.fun/coins/currently-live?limit=50` |
| coingecko | search_trending | 5 мин | `api.coingecko.com/api/v3/search/trending` |
| coinmarketcap | spotlight | 5 мин | `api.coinmarketcap.com/data-api/v3/cryptocurrency/spotlight` |
| birdeye | token_trending | 5 мин | `public-api.birdeye.so/defi/token_trending` — **только если задан `BIRDEYE_API_KEY`** |

DexScreener трендинг Solana со страницы (`dexscreener.com/solana?rankBy=trendingScoreH6`) в набор **не
включён**: страница отдаёт не JSON, а приложение, список подгружается отдельным запросом за
Cloudflare. Обход защиты не делаю — вместо него стоят три открытых эндпоинта boosts/profiles.
CoinMarketCap взят с их же data-api, без ключа. GMGN и pump.fun — их публичные frontend-API, без
обхода защиты: если Cloudflare ответит 403, это ляжет в снимок кодом, а не пропуском.

## 2. Формат

**`data/trending/<источник>/<YYYY-MM-DD>/<ЧЧ>.jsonl.gz` — файл на ЧАС** (с 16.10; было
по файлу на сутки). Причина названа замером: суточный файл Jupiter весит около 490 МБ, а
GitHub не принимает файл больше 100 МБ — суточный пуш 13, 14 и 15.10 отбивался именно на
этом. Часовой файл Jupiter — около 20 МБ.

Потолок проверяется перед каждой записью: если час подошёл к 95 МБ, снимок уходит в
`<ЧЧ>.part2.jsonl.gz` и далее по счёту. То есть в ветку влезает любой час при любом росте
списков, и чинить это второй раз не придётся.

Старые суточные файлы (12.10) остаются как есть: читалки берут оба раскроя.

Одна строка = один снимок, дописыванием (`gzip` в режиме `at`). В строке:

```
utc          метка снимка, YYYY-MM-DDTHH:MM:SSZ
источник     список   url
код          HTTP-код; null, если соединение не состоялось
ошибка       причина, если была (http 429, Cloudflare, таймаут, «нет ключа BIRDEYE_API_KEY»)
секунд       время ответа
байт         длина сырого тела
заголовки    content-type, x-ratelimit-remaining, retry-after, cf-ray, server
тело         СЫРОЕ тело как пришло (если не JSON — первые 4000 знаков в «не_json»)
строки       разбор: по_порядку, ранг, адрес, объём, ликвидность — и рядом ИМЯ ПОЛЯ источника
элементов    длина «строки»
```

Сырое не выбрасывается никогда: разбор может отстать от смены формата, снимок обязан остаться
годным. Отказ пишется такой же строкой — с кодом и причиной. Пустой список — тоже снимок.
Правок ради «красоты» нет.

### Суточная таблица первых появлений

`data/trending/pervye/<YYYY-MM-DD>.jsonl.gz` — строка на пару (список, адрес):

```
дата  список (источник/имя)  источник  имя_списка
адрес           минт либо адрес пула; GeckoTerminal отдаёт в id пул «solana_<адрес>»,
                префикс снят, рядом адрес_как_отдан
вид             «минт» / «пул» / «иное» (CoinGecko отдаёт свои имена вроде bitcoin)
utc             первый снимок суток, где адрес встретился
ранг  по_порядку  объём  ликвидность  и имена этих полей у источника
в_первом_снимке_суток   true -- настоящее первое появление РАНЬШЕ и неизвестно
от_предыдущего_снимка_с  неопределённость момента: 60 с у минутных списков, 300 с у пятиминутных
снимков_в_сутках  первый_снимок_суток
```

Считается по уже записанным снимкам, сеть не нужна, файл суток перезаписывается целиком
(можно пересчитать любую дату сколько угодно раз):

```bash
python3 analysis/podbivka_trending.py --pervye 2026-10-16
```

Замер на снимках 12.10 (2 ч 13 мин записи): 4671 строка, 0.34 МБ — то есть суточная
таблица остаётся мелкой даже при полных сутках.

## 3. Установка (делает Code-1 на lab-miami)

Checkout ветки `claude/podbivka` в отдельной папке — в торговый checkout не класть.

```bash
sudo git clone -b claude/podbivka <origin> /opt/robinhood-chain-alpha
sudo chown -R podbivka:podbivka /opt/robinhood-chain-alpha
```

`/etc/systemd/system/podbivka-trending-min.service`

```ini
[Unit]
Description=Трендинги: минутные списки
After=network-online.target

[Service]
Type=oneshot
User=podbivka
WorkingDirectory=/opt/robinhood-chain-alpha
ExecStart=/usr/bin/python3 analysis/podbivka_trending.py --kazhdye минута --pauza 0.4
TimeoutStartSec=50
Nice=10
# кошельковых ключей нет; торгового пути не касается
PrivateTmp=yes
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/opt/robinhood-chain-alpha
```

`/etc/systemd/system/podbivka-trending-min.timer`

```ini
[Unit]
Description=Трендинги каждую минуту

[Timer]
OnCalendar=*:0/1
AccuracySec=1s
Persistent=false

[Install]
WantedBy=timers.target
```

`/etc/systemd/system/podbivka-trending-5min.service` — то же, но
`ExecStart=/usr/bin/python3 analysis/podbivka_trending.py --kazhdye пять --pauza 0.6`,
`TimeoutStartSec=120`. Если ключ Birdeye появится, добавить в этот юнит:
`Environment=BIRDEYE_API_KEY=…` (лучше `EnvironmentFile=/etc/podbivka-trending.env`, режим 600).

`/etc/systemd/system/podbivka-trending-5min.timer` — `OnCalendar=*:0/5`, `AccuracySec=5s`.

`/etc/systemd/system/podbivka-trending-pervye.service`

```ini
[Unit]
Description=Трендинги: суточная таблица первых появлений

[Service]
Type=oneshot
User=podbivka
WorkingDirectory=/opt/robinhood-chain-alpha
# За ВЧЕРА: сутки уже закрыты. Таблицу можно пересчитать за любую дату повторно --
# файл суток перезаписывается целиком.
ExecStart=/bin/sh -c '/usr/bin/python3 analysis/podbivka_trending.py --pervye "$(date -u -d yesterday +%%F)"'
TimeoutStartSec=1800
Nice=10
```

`/etc/systemd/system/podbivka-trending-pervye.timer` — `OnCalendar=*-*-* 02:05:00 UTC`,
`Persistent=true`. 02:05Z — до пуша (02:17Z), чтобы таблица уехала в ветку тем же пушем.

`/etc/systemd/system/podbivka-trending-push.service`

```ini
[Unit]
Description=Трендинги: суточный пуш в ветку

[Service]
Type=oneshot
User=podbivka
WorkingDirectory=/opt/robinhood-chain-alpha
Environment=PODB_KOREN=/opt/robinhood-chain-alpha
Environment=PODB_VETKA=claude/podbivka
ExecStart=/opt/robinhood-chain-alpha/analysis/podbivka_trending_push.sh
TimeoutStartSec=900
```

`/etc/systemd/system/podbivka-trending-push.timer` — `OnCalendar=*-*-* 02:17:00 UTC`,
`Persistent=true`. 02:17Z выбран, чтобы не попадать на 07:17Z (тяжёлые проходы архива).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now podbivka-trending-min.timer \
     podbivka-trending-5min.timer podbivka-trending-pervye.timer \
     podbivka-trending-push.timer
```

Пуш-скрипт работает только если checkout стоит на `claude/podbivka`, кладёт в коммит **только**
`data/trending`, и повторяет пуш 4 раза с задержками 2/4/8/16 с.

## 4. Первый доклад (запускать на lab-miami)

```bash
cd /opt/robinhood-chain-alpha
python3 analysis/podbivka_trending.py --kazhdye все --probno    # кто доступен и с каким кодом
# час работы таймеров, затем:
python3 analysis/podbivka_trending.py --svodka                  # снимков и места по источникам
```

`--probno` ничего не пишет, только печатает по строке на список: `источник/список: код N
элементов M байт B <причина>` и последнюю строку `итого: отдали список X из 16`.

Перед установкой — самопроверка без сети (часовой раскрой, потолок файла, таблица первых
появлений); пишет в свою папку и сама её убирает:

```bash
python3 analysis/podbivka_trending.py --samoproverka   # код 0 и «самопроверка прошла»
```

## 5. Из моего контейнера докладывать нечего

Сетевая политика этого облачного окружения запрещает все 7 хостов из таблицы (15 списков): прокси отвечает
`403 Forbidden` на CONNECT (`connect_rejected`), то есть запрос не доходит до источника. Проверка
`--probno` из контейнера даёт 16 строк «код None … Tunnel connection failed: 403 Forbidden»
(у Birdeye — «нет ключа»), `итого: отдали список 0 из 16`. Это отказ окружения, а не источников:
кто доступен и с какой частотой, покажет только запуск на lab-miami — либо владелец откроет
хосты в настройках окружения (Network access).

## 6. План проверки — на потом, сейчас не делать

Первое появление токена в списке (по `utc` первой строки, где он есть) → путь цены из архива
PumpApi на +10 с / +1 / +5 / +15 мин; ликвидность пула на момент появления; разрез по спискам и
по рангу появления; при нашей задержке (вход в начале следующего слота после появления); счёт по
стандарту — подбор 7 → проверка 4, сдвиг 4, подряд идущие сутки, n≥100, mean>0, CI-low>0,
усечённое-5 %>0, ≥60 % суток в плюсе, обе половины в плюсе, знаковый тест и поправка на все
сочетания (p < 0.05/число сочетаний).
