# Развёртывание с нуля: новый VPS, новый аккаунт GitHub

Документ для того, кто поднимает этот стенд ПЕРВЫЙ РАЗ и кода не знает.
Всё собрано чтением ветки `claude/nifty-sagan-r0polg`: `.github/workflows/*.yml`,
`analysis/`, `deploy/`, `data/`. SSH к живому хосту не использовался.

**Ни одного значения секрета в этом документе нет и быть не должно.** Ключи, приватные
ключи, сиды и токены названы только ИМЕНАМИ переменных и назначением. Значения по
умолчанию приводятся только для НЕсекретных настроек.

Ссылки вида `analysis/bloom_seller.py:298` — это файл и строка в этой ветке; по ним
проверяется каждое число в документе. Где разведка ничего не дала, так и написано:
«в коде не нашёл — проверить на месте».

---

## 1. Что это за система

Это копировальщик сделок на Solana. Служба `bloom-detector` держит подписку Helius
(`transactionSubscribe`, `analysis/bloom_detector.py:7746-7754`) на адреса-источники из
файла групп, разбирает каждую их транзакцию, определяет минт, пул и маршрут, прогоняет
через фильтры (размер цели, налог маршрута, отставание слотов, дубль минта, пределы) и
при проходе покупает тот же токен. Покупка идёт двумя путями: через API площадки Bloom
(`analysis/bloom_executor.py`) и «полосой своей отправки» — мы сами собираем
транзакцию и рассылаем её параллельно пулом отправителей (`analysis/bloom_own_send.py`,
`analysis/bloom_senders.py`). Продажу ведёт отдельная служба `bloom-seller`: ордер по
сроку `BLOOM_SELL_AFTER_S` (28.8 с), затем повторы каждые `BLOOM_SELL_RETRY_EVERY_S`
(45 с) до предела `BLOOM_SELL_GIVE_UP_AFTER_S` (600 с), через Bloom, свой путь или
Jupiter. Третья служба `dbot-sold-guard` дожимает проваленные продажи DBot и спасает
зависшие остатки кошельком-утилизатором.

Живёт это на ОДНОМ VPS. На нём шесть юнитов systemd (`bloom-detector`, `bloom-seller`,
`dbot-sold-guard`, `dbot-detect-probe`, `grpc-feed-probe`, `shred-listener`), пять пар
хостовых таймеров (учёт, живучесть, суточная выгрузка, уборщик ренты и его итог,
`deploy/hostovye_tajmery.py:103-221`) и две пары таймеров строк владельцу. Клона
репозитория у боевых служб нет: код доставляется деплоем списком модулей в
`/home/bot/bloom_executor` со сверкой sha256 каждого файла, а единственный venv лежит в
`/home/bot/robinhood-chain-alpha/venv`. Весь ввод в систему — прогоны GitHub Actions:
часть на облачном бегунке `ubuntu-latest` по ssh, часть на своём раннере с метками
`[self-hosted, nl-host]`, который стоит на этом же хосте. Доклады идут в Telegram,
состояние — в `/home/bot/bloom_executor_live_data`, окружение и рубильники — в
`/etc/bloom-executor`.

---

## 2. Что надо завести заранее

### 2.1. Аккаунт GitHub и репозиторий

Нужен аккаунт (или организация) с репозиторием, куда положена эта ветка, и включёнными
Actions. **Прогоны жёстко прописывают имена ветки, адрес хоста и метки раннера** — на
новом аккаунте это надо заменить ДО первого запуска, иначе прогоны пойдут не туда:

| Что зашито | Где и сколько | Чем это грозит |
|---|---|---|
| `ref: claude/nifty-sagan-r0polg` в шаге `actions/checkout` | 200 файлов в `.github/workflows/` | прогон возьмёт код не из вашей ветки |
| адрес хоста `45.59.170.76` | 324 строки в 121 файле `.github/workflows/` | ssh пойдёт на чужую машину |
| метка раннера `nl-host` | 98 строк в 95 файлах | задание повиснет в очереди навсегда |
| URL репозитория `github.com/russiansky777/robinhood-chain-alpha` | `run_vps_bootstrap_nl.yml:116`, `run_vps_bootstrap.yml:113`, `run_vps_bootstrap_ohio.yml:113`, `run_task5_deploy_executor.yml:66`, `run_task5_deploy_executor_v4.yml:86`, `run_task5_test5_revert.yml:67` | клон на хост не пройдёт |
| ветка клона на хост `claude/sc1-pons-v2-launcher-efdbyt` | `run_vps_bootstrap_nl.yml:112-116` | на хосте окажется старый код и старый `requirements.txt` |
| публичный адрес кошелька исполнителя — КОНСТАНТА `EXECUTOR_WALLET` | `analysis/bloom_exec_state.py:45` | адрес сверяется перед каждой отправкой; с новым кошельком строку надо заменить, иначе полоса сочтёт себя «на кошельке исполнителя» неверно (`analysis/bloom_own_send.py:131,142`) |

### 2.2. VPS

* ОС: Ubuntu 24.04. Важно: на ней служба ssh зовётся `ssh`, не `sshd` —
  `systemctl reload ssh` (`run_vps_bootstrap_nl.yml:91`).
* Сеть: наружу открывается ТОЛЬКО 22/tcp, остальное входящее закрывается ufw,
  плюс fail2ban и `PasswordAuthentication no` (`run_vps_bootstrap_nl.yml:71-91`).
* **Ядра и диск в прогонах торгового хоста не проверяются — в коде не нашёл, проверить
  на месте.** Гейт по `nproc` есть только у лабораторных машин
  (`run_lab_bootstrap_nl.yml:208-222`: у miami 6 ядер, у ams 2, расхождение роняет
  прогон как «адрес не тот»). Для торгового хоста такой проверки нет вовсе.
* Память — считается по пределам юнитов, и это не формальность: 08.10 детектор убил
  OOM-killer при `MemoryMax=512M` и пике 515.1M, предел поднят до 1G
  (`run_vps_health_nl.yml:100-112`, `run_vps_deploy_bloom_detector_nl.yml:1140,1151`).

  | Юнит / таймер | `MemoryMax` | Где задано |
  |---|---|---|
  | `bloom-detector` | 1G | `run_vps_deploy_bloom_detector_nl.yml:1151` |
  | `bloom-seller` | 512M | `run_vps_deploy_bloom_seller_nl.yml:655` |
  | `shred-listener` | 400M (+`CPUQuota=35%`) | `run_vps_deploy_shred_listener_nl.yml:267-268` |
  | таймер `bloom-uchet-chas` (в `*:23`) | 1G | `deploy/hostovye_tajmery.py:118` |
  | `bloom-vygruzka-sutki`, `bloom-uborshchik-chas` | 512M | `deploy/hostovye_tajmery.py:139,215` |
  | `bloom-zhivuchest-chas`, `bloom-uborshchik-itog-sutki` | 256M | `deploy/hostovye_tajmery.py:128,158` |
  | таймеры строк владельцу | 256M | `run_vps_stroki_tajmery_nl.yml:153` |

  Сумма одновременно работающих в худший час (детектор + продавец + учёт + слушатель
  шредов) — около 3 ГБ без системы. Отсюда ОЦЕНКА «не меньше 4 ГБ ОЗУ»; это вывод из
  пределов юнитов, а не требование из кода.
* Диск: требований в коде нет. Что известно точно — ротации:
  `/home/bot/bloom_executor_live_data/*.jsonl` — `daily`, `rotate 14`, `compress`,
  `copytruncate` (`run_vps_deploy_bloom_seller_nl.yml:663-674`); журнал сторожа —
  `size 100M`, `rotate 5` (`run_vps_deploy_sold_position_guard_nl.yml:184-198`); зонд
  обнаружения — `size 200M` (`run_vps_deploy_detect_probe_nl.yml:140-145`). Плюс venv и
  кэши цепи. Конкретный объём — проверить на месте.
* Часовой пояс: у таймеров пояс назван в самой строке `OnCalendar` и установщик
  ОТКАЗЫВАЕТСЯ, если пояса нет (`deploy/hostovye_tajmery.py:284,349`); сутки учёта
  считаются по `Europe/Madrid` (`analysis/bloom_exec_state.py:48`).

### 2.3. Helius

План **Developer**: 10 млн кредитов в месяц, 50 запросов/с к узлу, 10/с к расширенным
API, 150 подключений WebSocket, отправка 5/с (`analysis/solana_rpc_client.py:8-11`). Наш
общий клиент нарочно ходит на 40 и 8 вместо 50 и 10 (там же:13-15). Перерасход сверх
плана — 5 долларов за миллион кредитов (`analysis/solana_rpc_client.py:178-182`).

Суточные бюджеты служб (`analysis/solana_rpc_client.py:187-193`): `bloom_detector`
2 500 000, `bloom_seller` 60 000, `ledger` 50 000, `зонд` 150 000, `grpc_feed_probe`
300 000, `bloom_send_race` 20 000, `c2_followers_growth` 40 000; общий потолок
`ОБЩИЙ_ПОТОЛОК_СУТКИ = 2 600 000` (`analysis/solana_rpc_client.py:208`), и самопроверка
требует, чтобы потолок был не меньше суммы детектора и продавца.

Чего план НЕ даёт: `blockSubscribe` отвечает `-32601 Method not found`
(`data/blocksubscribe_proba.json`); Enhanced Websockets в тариф не входят. Нужны **два**
ключа: боевой и второй под зонды и замеры, чтобы разведка не ела кредиты боевого.

### 2.4. Провайдеры отправки

Реестр — `data/senders.json`, он единственный источник адресов, минимальных чаевых и
ИМЁН секретов; нет адреса или нет нужного секрета — отправитель не подключается и в пул
не идёт (`готов`, `analysis/bloom_senders.py:83-113`).

| Отправитель | Точка | ИМЯ ключа в env | Мин. чаевые, лампортов | Где взять ключ |
|---|---|---|---|---|
| Helius Sender | `http://ams-sender.helius-rpc.com/fast` | ключ не нужен | 1 000 000 | — (сам Sender кредитов не тратит) |
| Jito Block Engine | `https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/transactions` | ключ не нужен | 1 000 | UUID запрашивается у Jito только под повышенные пределы |
| 0slot.trade | `http://ams1.0slot.trade` | `ZEROX_API` | 1 000 000 | панель 0slot.trade; ключ в строке запроса `api-key=` |
| BlockRazor | `http://amsterdam.solana.blockrazor.xyz:443/sendTransaction` | `BLOCKRAZOR_AUTH_TOKEN` | 100 000 | регистрация на blockrazor.io, токен на главной панели |
| Nozomi (Temporal) | `https://ams1.nozomi.temporal.xyz/` | `NOZOMI_API_KEY` | 1 000 000 | панель Temporal/Nozomi; ключ как `?c=<ключ>` |
| Astralane | `http://fr.gateway.astralane.io/iris` | `ASTRALANE_API_KEY` | 1 000 000 | панель Astralane (точка французская, не ams) |

Своего узла-отправителя нет. Triton в реестре отсутствует намеренно: он у нас только
фид предподтверждений (`data/senders.json`, поле `_triton_pochemu_ne_v_spiske`).

### 2.5. Бот Telegram

BotFather → `/newbot` → токен. Нужны два-три чата: основной чат владельца (покупки,
продажи, круг, итоги, тревоги, сводка часа) и ЖУРНАЛЬНАЯ группа под построчные решения
детектора. ID основного чата добывает прогон `run_telegram_getupdates_chat_id.yml`; ID
журнальной группы берётся из поля `seen_chats` признака жизни через
`run_vps_telegram_log_chat_nl.yml`. Второй `getUpdates` руками запускать нельзя: два
клиента одного бота отбирают обновления друг у друга.

### 2.6. Кошельки и счёт nonce

| Что | Зачем | Ключ (ИМЯ) | Чем создаётся |
|---|---|---|---|
| кошелёк исполнителя | покупки и продажи через Bloom, подпись продаж Jupiter | секрет `EXEC_WALLET_KEY`, на хосте `EXEC_WALLET_KEY`, откат `BLOOM_WALLET_KEY` | `solana-keygen new` или Phantom; публичный адрес — в константу `analysis/bloom_exec_state.py:45` |
| кошелёк полосы | своя отправка: свои позиции, свой стоп, свои деньги | секрет `LIVE_PRIVATE`, на хосте `OWN_SEND_WALLET_KEY`; адрес — `OWN_SEND_WALLET` | `solana-keygen new`; адрес проверяется прогоном `run_lane_wallet_addr_nl.yml` до записи |
| кошелёк-утилизатор | спасение зависших остатков сторожем | секрет `RESCUE_WALLET_KEY`; адрес — ПЕРЕМЕННАЯ `vars.RESCUE_WALLET_ADDRESS` | `solana-keygen new` |
| счёт долговечного nonce | замок от двойной покупки в nonce-варианте пула и обход предела 1232 байта | своего ключа нет: распорядитель — кошелёк полосы | прогон `run_vps_lane_nonce_nl.yml`, `mode=create` |

Формат ключа кошелька: base58 (как отдаёт Phantom) ЛИБО JSON-массив байт (как пишет
`solana-keygen`) — разбирается оба (`analysis/dbot_rescue.py:391-405`).

Деньги, которые нужны заранее: рента счёта nonce (размер аккаунта 80 байт,
`analysis/lane_nonce.py:34`; рента спрашивается у узла через
`getMinimumBalanceForRentExemption`, `analysis/lane_nonce.py:125`; фактически узел
ответил 1 056 640 лампортов = 0.00105664 SOL, `data/lane_nonce_state.txt`; в докстринге
стоит ориентир 0.00144768 SOL, `analysis/lane_nonce.py:9-12` — верить надо ответу узла),
плюс стартовый баланс кошелька исполнителя не ниже
`BLOOM_MIN_START_BALANCE_SOL` (по умолчанию 0.3 SOL,
`analysis/bloom_reconcile.py:45`), плюс запас на приоритетку, чаевые и ренту счетов
(`DEFAULT_FEE_RESERVE_SOL = 0.02`, `analysis/bloom_exec_state.py:55`).

### 2.7. Площадки и прочие API

* **Bloom** (`eu.solana.bloombot.app`) — покупка и продажа через API, ключ
  `BLOOM_API_KEY`, берётся в панели/боте Bloom.
* **DBot** (`api-bot-v1.dbotx.com`) — источник сделок копирования (задачи
  `BATCH-5,BATCH-3`) и учёт, ключ `DBOT_API_KEY`, берётся в панели dbotx.
* **Jupiter** (portal.jup.ag) — второй путь выхода. Ключ есть → план `swap-v2`, Ultra
  запасной; ключа нет → только Ultra (`analysis/bloom_jupiter_sell.py:263-272`).
* **Файл групп источников** — чем вообще торгуем. В ветке лежат `data/sources_live.json`
  (17 групп: `leader`, `batch5`, `lane_s0`, `off`, `log_only`, `sniper_src`,
  `kandidaty`, `cand1`, `cand1_03`, `cand1_05`, `cand2`, `cand3`, `cand2_03`,
  `krug_001`, `konveyer`, `vol_4vw`, `nabor`),
  `data/sources_2026-09-25.pered_finalom.json` и
  `data/sources_2026-09-25_USTAREL_2809.json`. **Файла `data/sources_2026-09-25.json`,
  которого ждёт прогон замены (`run_vps_gruppy_zamena_nl.yml:45-46`), в ветке нет** — на
  новом стенде либо положить свой файл под этим именем, либо поправить в прогоне `LOCAL`
  и `FILE`.

---

## 3. Секреты GitHub

Заводятся в Settings → Secrets and variables → Actions → Secrets. **Значений здесь нет.**
Таблица — минимум, без которого торговый хост не поднимается.

| ИМЯ секрета | Назначение | Формат | Где взять |
|---|---|---|---|
| `VPS_SSH_KEY` | ssh на торговый хост под `root`: все деплои и все снятия состояния (`run_vps_deploy_bloom_detector_nl.yml:308`, `run_vps_deploy_bloom_seller_nl.yml:189`) | приватный ключ ssh ЦЕЛИКОМ, со строкой `-----BEGIN ... KEY-----` и переводом строки в конце | `ssh-keygen -t ed25519`; публичная половина — в `~root/.ssh/authorized_keys` на VPS |
| `HELIUS_API` | ключ RPC и WebSocket Helius: чтение цепи, балансы, подтверждения, отправка через Helius Sender. **Это и есть боевое имя**, его читают все деплои | ключ-строка | панель Helius, раздел API Keys |
| `HELIUS_API_KEY2` | второй ключ Helius под зонды и замеры, чтобы разведка не ела кредиты боевого | ключ-строка | второй ключ в той же панели |
| `DBOT_API_KEY` | API DBot: источник сделок копирования и учёт | ключ-строка | панель dbotx, раздел API |
| `BLOOM_API_KEY` | API площадки Bloom: покупка и продажа | ключ-строка | панель/бот Bloom |
| `EXEC_WALLET_KEY` | приватный ключ кошелька ИСПОЛНИТЕЛЯ | base58 или JSON-массив байт | владелец кошелька / `solana-keygen new` |
| `LIVE_PRIVATE` | приватный ключ кошелька ПОЛОСЫ; на хост пишется под именем `OWN_SEND_WALLET_KEY` (`run_lane_wallet_env_nl.yml:88`) | base58 или JSON-массив байт | владелец кошелька полосы |
| `RESCUE_WALLET_KEY` | ключ кошелька-утилизатора для сторожа продаж | base58 или JSON-массив байт | отдельный кошелёк |
| `JUPITER_API_KEY` | ключ Jupiter Swap V2 (второй путь выхода) | ключ-строка | portal.jup.ag, раздел API keys |
| `BLOCKRAZOR_AUTH_TOKEN` | отправитель BlockRazor | токен-строка | панель BlockRazor |
| `ASTRALANE_API_KEY` | отправитель Astralane | ключ-строка | панель Astralane |
| `ZEROX_API` | отправитель 0slot. **Имя именно такое** — оно стоит в реестре `data/senders.json` полем `key_env` у `zeroslot`, а значение код берёт из окружения ПО ЭТОМУ ИМЕНИ (`analysis/bloom_senders.py:73-80`) | ключ-строка | панель 0slot.trade |
| `NOZOMI_API_KEY` | отправитель Nozomi (Temporal) | ключ-строка | панель Temporal/Nozomi |
| `TELEGRAM_BOT_TOKEN` | бот, который пишет доклады | `<числа>:<строка>` | BotFather, `/newbot` |
| `TELEGRAM_CHAT_ID` | основной чат владельца | число (у групп с минусом) | прогон `run_telegram_getupdates_chat_id.yml` покажет сам |
| `GH_RUNNER_PAT` **или** `GH_RUNNER_TOKEN` | регистрация своего раннера на хосте. С PAT прогон сам добудет токен регистрации (`run_vps_install_gh_runner.yml:62-63`) | PAT с правом на репозиторий / короткий токен, живёт около часа | Settings → Developer settings → Personal access tokens, либо Settings → Actions → Runners → New self-hosted runner |

Переменная (НЕ секрет), Settings → Variables → Actions:

| ИМЯ | Назначение |
|---|---|
| `RESCUE_WALLET_ADDRESS` | публичный адрес кошелька-утилизатора; читается как `vars.RESCUE_WALLET_ADDRESS` (`run_vps_deploy_sold_position_guard_nl.yml:123`). Если завести секретом — прогон молча подставит пустую строку |

`GITHUB_TOKEN` выдаётся Actions сам, заводить не нужно.

Необязательные для подъёма торговли (нужны только отдельным прогонам): `VPS_SSH_KEY_MIAMI`,
`VPS_SSH_KEY_AMS`, `VPS_SSH_KEY_OHIO` (лаборатории и стенды), `GRPC_FEED_URL`,
`GRPC_FEED_TOKEN`, `GRPC_FEED_AUTH_HEADER`, `GRPC_FEED2_*`, `GRPC_FEED3_URL`,
`GRPC_FEED4_URL` (зонд гонки потоков), `TRITON_X_TOKEN` (фид предподтверждений),
`LAB_DISPATCH_TOKEN` (запуск прогона из прогона), `LIVE_TESTS` (разрешение на живые
опыты). Ключи исследовательских веток (`ALCHEMY_*`, `DUNE_*`, `BITQUERY_*`,
`BLOCKSCOUT_API_KEY`, `FOMO_API_KEY`, `THE_ODDS_API`, `LIGHTER_*`, `PRIVATE_KEY_NOX`) к
торговле на Solana отношения не имеют.

Те из необязательных, которые ЧИТАЮТСЯ прогонами этой ветки — полным списком, с
назначением и источником. Торговый хост без них поднимается; без них просто не работают
названные прогоны:

| ИМЯ секрета | Назначение | Кто читает | Где взять |
|---|---|---|---|
| `TRITON_DESHRED_URL` | точка потока дешреда Triton (endpoint tier3) для часового зонда «даёт ли дешред транзакцию источника раньше Helius WS» | `run_zond_deshred_chas_nl.yml:96,178` (на хост кладётся файлом `$ZOND_DIR/deshred_url`, `:183`) | кабинет Triton, бета дешреда (плата по балансу кабинета) |
| `TRITON_DESHRED_TOKEN` | допуск к тому же потоку | `run_zond_deshred_chas_nl.yml:97,179` (файл `$ZOND_DIR/deshred_token`, `:184`) | там же, вместе с URL |
| `LAB_TOKEN` | допуск на `workflow_dispatch` из прогона — расписание лабораторий. Одно из пяти имён, которые прогон перебирает: `LAB_DISPATCH_TOKEN`, `LAB_DISPATCH`, `LAB_TOKEN`, `DISPATCH_TOKEN`, `GH_DISPATCH_TOKEN` (`run_lab_raspisanie_nl.yml:221`) | `run_lab_raspisanie_nl.yml:191` (и `vars.LAB_TOKEN`, `:201`) | Settings → Developer settings → Personal access tokens, право только на Actions |
| `GH_DISPATCH_TOKEN` | то же назначение, пятое имя в том же переборе | `run_lab_raspisanie_nl.yml:193` | там же |
| `BLOOM_API_KEY_PUBLIC` | исторический дубль имени ключа Bloom; проверяется ТОЛЬКО на «задан/не задан» | `run_secret_presence_check.yml:22` | не заводить на новом аккаунте |
| `BLOOM_API_KEY_PRIVATE` | исторический дубль; кроме наличия, его ещё прогоняют через разборщик ключей — не окажется ли под этим именем ключ кошелька | `run_secret_presence_check.yml:23`, `run_sekrety_chey_adres_nl.yml:63,69` | не заводить на новом аккаунте |
| `BLOOM_KEY` | исторический дубль; тоже проверяется разборщиком «чей кошелёк в секрете» | `run_secret_presence_check.yml:25`, `run_sekrety_chey_adres_nl.yml:61,69` | не заводить на новом аккаунте |

Четыре ловушки имён, на которых легко встать:

1. **Боевой секрет Helius называется `HELIUS_API`, и ТОЛЬКО он.** Это не догадка по
   имени, а замер прогоном 12.10: `HELIUS_API_KEY` пришёл в прогон ПУСТЫМ, работал
   `HELIUS_API`. Счёт по ветке подтверждает то же: `secrets.HELIUS_API` стоит в
   **120 местах** (74 файла), `secrets.HELIUS_API_KEY` — в **4**, `secrets.HELIUS_KEY` —
   в **2** (отдельно `secrets.HELIUS_API_KEY2` — в 3: это второй ключ под зонды).
   На хост ключ приходит так: деплой читает `secrets.HELIUS_API` в свою переменную
   `HELIUS_API_KEY` (`run_vps_deploy_bloom_seller_nl.yml:436`, гейт наличия — `:177,182`)
   и пишет в env сразу ДВА имени, `HELIUS_API_KEY` и `HELIUS_API` (`:468-469`), потому
   что код спрашивает оба. Секреты `HELIUS_API_KEY` и `HELIUS_KEY` существуют как
   исторические дубли и проверяются только на «задан/не задан» — на новом аккаунте их
   заводить не надо, а если завести пустыми, они ничего не починят и запутают разбор.
2. **У Bloom та же история дублей имён, и она опаснее.** Боевое имя одно —
   `BLOOM_API_KEY`. Рядом живут `BLOOM_API_KEY_PUBLIC`, `BLOOM_API_KEY_PRIVATE`,
   `BLOOM_API` и `BLOOM_KEY`: ни одно из них боевым путём не читается, все четыре
   проверяются только на «задан/не задан» (`run_secret_presence_check.yml:21-30`). Хуже
   того, под именами `BLOOM_KEY` и `BLOOM_API_KEY_PRIVATE` когда-то мог оказаться
   ПРИВАТНЫЙ КЛЮЧ КОШЕЛЬКА, а не ключ API, — поэтому отдельный прогон выводит из каждого
   такого секрета публичный адрес и сверяет, чей он
   (`run_sekrety_chey_adres_nl.yml:61,63,69`). На новом аккаунте заводить ровно
   `BLOOM_API_KEY` и ни одного дубля.
3. **Ключ 0slot зовётся `ZEROX_API`.** Имя `ZEROSLOT_API_KEY` читает только зонд
   `run_sender_key_probe_nl.yml:111`; боевой деплой читает `ZEROX_API`. Если заводите
   оба — держите синхронно.
4. **Пустым ключ не писать никогда.** Пустая строка в env означает «ключ есть, но
   пустой», и путь продажи сочтёт себя готовым
   (`run_vps_deploy_bloom_seller_nl.yml:490-502`).

---

## 4. ENV хоста

### 4.1. Кто вообще пишет окружение

Файл `/etc/bloom-executor/env`, права `600 root:root`, подключён обеим боевым службам
как `EnvironmentFile=` (`run_vps_deploy_bloom_detector_nl.yml:1123`,
`run_vps_deploy_bloom_seller_nl.yml:640`). Писателей четыре с половиной:

| Прогон | Что делает с env |
|---|---|
| `run_vps_deploy_bloom_seller_nl.yml` | ЕДИНСТВЕННЫЙ, кто пишет файл ЦЕЛИКОМ (`:432-553`, запись `:549`). Только при `keep_env=no` и `perepisat_env=PEREPISAT` |
| `run_vps_deploy_bloom_detector_nl.yml` | дописывает и переписывает ОТДЕЛЬНЫЕ строки: ключи отправителей (`:646`), `DBOT_API_KEY` (`:685`), флаги полосы, тени, покупки |
| `run_lane_wallet_env_nl.yml` | три строки полосы: `OWN_SEND_WALLET_KEY`, `OWN_SEND_WALLET`, `BLOOM_LANE_NONCE` (`:87-88`), с откатом из `env.bak.*` при потере имени (`:108-118`) |
| `run_vps_set_jupiter_key_nl.yml`, `run_vps_telegram_log_chat_nl.yml` | по одной строке: `JUPITER_API_KEY`, `TELEGRAM_LOG_CHAT_ID` |
| `run_vps_env_flag_nl.yml` | одна строка по БЕЛОМУ СПИСКУ имён (`:96`), в том числе `BLOOM_SOURCE_GROUPS` |
| `run_vps_env_bloom_nl.yml` | ТОЛЬКО ЧТЕНИЕ: печатает безопасные значения `BLOOM_*` по белому списку (`:51`); имя, похожее на секрет, отбрасывается второй проверкой |

Отдельные файлы окружения у остальных служб: `/etc/dbot-guard/env`
(`run_vps_deploy_sold_position_guard_nl.yml:145`), `/etc/dbot-detect-probe/env`,
необязательные добавки `feed_probe_env` и `shred_listener_env`.

### 4.2. Секретные имена env (значения не печатать никогда)

| ИМЯ env | Из какого секрета | Кто читает | Что меняет |
|---|---|---|---|
| `BLOOM_API_KEY` | `BLOOM_API_KEY` | `analysis/bloom_notify.py:138`, `analysis/bloom_detector.py:14963` | без него Bloom не отвечает — ни покупок, ни продаж через площадку |
| `HELIUS_API_KEY` | `HELIUS_API` | `analysis/bloom_seller.py:298`, `analysis/bloom_notify.py:139` | чтение цепи, балансы, подписка |
| `HELIUS_API` | `HELIUS_API` | `analysis/bloom_seller.py:299`, `analysis/bloom_notify.py:140` | второе имя того же ключа, код спрашивает оба |
| `DBOT_API_KEY` | `DBOT_API_KEY` | `analysis/dbot_sold_position_guard.py:923`, `analysis/bloom_notify.py:141` | источник сделок и учёт |
| `EXEC_WALLET_KEY` | `EXEC_WALLET_KEY` (или переносится с хоста) | `analysis/bloom_jupiter_sell.py:656`, `analysis/bloom_detector.py:15054` | подпись продаж кошельком исполнителя |
| `JUPITER_API_KEY` | `JUPITER_API_KEY` | `analysis/bloom_lane_jupiter.py:83` | есть → `swap-v2`, нет → только Ultra |
| `OWN_SEND_WALLET_KEY` | `LIVE_PRIVATE` | `analysis/bloom_own_send.py:125,137` | ключ кошелька полосы |
| `BLOCKRAZOR_AUTH_TOKEN` | одноимённый | `ключ_отправителя`, `analysis/bloom_senders.py:73-80` — ПО ИМЕНИ из реестра | подключает BlockRazor в пул |
| `ASTRALANE_API_KEY` | одноимённый | там же, `:73-80` | подключает Astralane |
| `ZEROX_API` | одноимённый | там же, `:73-80` | подключает 0slot |
| `NOZOMI_API_KEY` | одноимённый | там же, `:73-80` | подключает Nozomi |
| `TELEGRAM_BOT_TOKEN` | одноимённый | `analysis/bloom_notify.py:137`, `analysis/bloom_seller.py:476` | без него доклады молчат |
| `RESCUE_WALLET_KEY` | одноимённый, в `/etc/dbot-guard/env` | `analysis/dbot_rescue.py` | спасение зависших остатков |

**Как читаются ключи отправителей — это важнее номеров строк.** Ни одно из четырёх имён
(`BLOCKRAZOR_AUTH_TOKEN`, `ASTRALANE_API_KEY`, `ZEROX_API`, `NOZOMI_API_KEY`) в коде
денежного пути НЕ НАЗВАНО жёстко. Имя секрета лежит в реестре `data/senders.json` полем
`key_env` у отправителя; функция `ключ_отправителя`
(`analysis/bloom_senders.py:73-80`) берёт это имя из записи реестра и читает значение из
окружения — `os.environ.get(имя)`, пустое и пробельное значение считается отсутствием.
Дальше `готов` (`analysis/bloom_senders.py:83-113`) по тому же имени решает, подключать
отправителя или нет, и причину пишет словами: «секрет `<имя>` не задан в окружении —
отправитель не подключён». Следствие для нового стенда: ПЕРЕИМЕНОВАТЬ секрет можно, но
менять надо в реестре, а не в коде; а строки вида `os.environ["ZEROX_API"] = ...` внутри
`bloom_senders.py` (`:805`, `:849`, `:881`, `:1012-1015`, `:1196`) — это САМОПРОВЕРКИ: они
ставят подставное значение в окружение, а не читают боевой ключ.

**Второе имя ключа DBot: `DBOT_APIKEY`** (без подчёркивания). Боевое имя —
`DBOT_API_KEY`; разведочные и зондовые модули берут первое непустое из пары
(`analysis/dbot_pair_pool_probe.py:228`,
`analysis/solana_task_wallets_illiquid_positions.py:58,252`,
`analysis/solana_fee_experiment_compare.py:206`,
`analysis/dbot_rescue_stage_b_tests.py:400`). На новом стенде заводить только
`DBOT_API_KEY`: про запасное имя надо знать, чтобы не искать «второй ключ», когда зонд
говорит, что ключ взят.

**Второй способ подать ключ Helius — ФАЙЛОМ, а не строкой: `HELIUS_API_KEY_FILE`.**
Переменная несёт ПУТЬ к файлу с ключом; читается ПЕРВОЙ, и только если её нет, берётся
`HELIUS_API_KEY` (`analysis/preprocessed_ab_zond.py:673-686`). Важно: если путь задан, а
файл не читается, отката к переменной НЕТ — зонд отвечает «файл ключа не читается» и
останавливается (`:678-680`); нет ни файла, ни переменной — «ключа Helius нет ни в файле,
ни в переменной» (`:684-685`). Так ключ
подают временным службам зондов, чтобы он не оседал в `Environment=` юнита и в
`systemctl show`: `--setenv=HELIUS_API_KEY_FILE=$ZOND_DIR/helius_key`
(`run_zond_deshred_chas_nl.yml:209`, `run_triton_zond_24h_nl.yml:174`,
`run_bonding_sim_check_nl.yml:594`). В env боевых служб его нет и быть не должно — там
ключ строкой.

Не секрет, но тоже адрес: `OWN_SEND_WALLET` (публичный адрес полосы,
`analysis/bloom_own_send.py:124,128`), `BLOOM_LANE_NONCE` (адрес счёта nonce,
`analysis/bloom_own_send.py:1774`), `RESCUE_WALLET_ADDRESS` и **`BLOOM_LANE_WALLET`** —
запасное имя публичного адреса кошелька полосы для хостовых счётчиков и разборов, когда
`OWN_SEND_WALLET` не задан: `analysis/bloom_lane_sell.py:491` (и отказ словами на `:516`),
`deploy/checks/sutochnyj_schet_polosy.py:107`,
`deploy/checks/zapisat_vyvod_vladelca.py:252`,
`deploy/checks/dlmm_vosstanovlenie.py:455`, плюс
`deploy/checks/priemka_stroitelya.py:885` и `deploy/checks/pravilo11_po_cepi.py:975`;
в прогонах — `run_dvizhenie_koshelka_nl.yml:68` (третий по счёту источник адреса). Ключа
под этим именем быть НЕ ДОЛЖНО: `BLOOM_LANE_WALLET_KEY` стоит в списке образцов
«похоже на ключ» у стража отчётов (`deploy/checks/arhiv_bez_klyuchey.py:177`).

### 4.3. Настроечные имена env (значения по умолчанию приводятся)

Что пишет деплой сторожа при `PEREPISAT` (`run_vps_deploy_bloom_seller_nl.yml:467-545`):
`BLOOM_STATE_DIR=/home/bot/bloom_executor_live_data`,
`BLOOM_KILL_FILE=/etc/bloom-executor/KILL`,
`BLOOM_KILL_SELL_FILE=/etc/bloom-executor/KILL_SELL`,
`BLOOM_JUP_FLOOR_PCT=70`, `BLOOM_JUP_MIN_QUOTE_PCT=30`, `BLOOM_SELL_SLIPPAGE_PCT=40`,
`BLOOM_SELL_GRACE_S=15`, `BLOOM_SELL_RETRY_EVERY_S=45`,
`BLOOM_SELL_GIVE_UP_AFTER_S=600`, `BLOOM_SELLER_LOOP_S=3`, `BLOOM_PRIORITY_FEE=0.001`,
`BLOOM_PROCESSOR_TIP=0.001`, `BLOOM_MAX_OPEN=5`, `BLOOM_DAILY_LOSS_SOL=1`,
`BLOOM_BUY_SOL` (переносом с хоста, иначе `0.05`), плюс значения из входов прогона.
Деплой детектора добавляет `BLOOM_MODE=dry`, `BLOOM_TASKS`, `BLOOM_EXEC=1`, если их в
файле ещё нет.

Главные настройки и их умолчания В КОДЕ (код сильнее деплоя только когда строки в env
нет вовсе):

| ИМЯ env | Читается в | Умолчание в коде | Что меняет |
|---|---|---|---|
| `BLOOM_MODE` | `analysis/bloom_detector.py:14969` | `dry` | `dry` — только журнал, POST в Bloom не уходит |
| `BLOOM_TASKS` | `analysis/bloom_detector.py:14900` | `BATCH-5,BATCH-3` | какие задачи DBot слушаем |
| `BLOOM_LIVE_BUY` | `analysis/bloom_executor.py:118` | нет строки = нет боевых покупок | боевые покупки через Bloom |
| `BLOOM_BUY_SOL` | `analysis/bloom_exec_state.py:603` | `DEFAULT_BUY_SOL = 0.2` (`:54`) | размер боевой покупки |
| `BLOOM_LIVE_SELL` | `analysis/bloom_seller.py:1587` | `"0"` | боевые продажи |
| `BLOOM_MAX_OPEN` | `analysis/bloom_exec_state.py:601` | `DEFAULT_MAX_OPEN = 5` (`:52`) | потолок открытых позиций |
| `BLOOM_DAILY_LOSS_SOL` | `analysis/bloom_exec_state.py:602` | `DEFAULT_DAILY_LOSS_SOL = 1.0` (`:53`) | суточный предел убытка площадки |
| `BLOOM_FEE_RESERVE_SOL` | `analysis/bloom_exec_state.py:604` | `DEFAULT_FEE_RESERVE_SOL = 0.02` (`:55`) | приоритетка + чаевые + рента счёта |
| `BLOOM_MIN_START_BALANCE_SOL` | `analysis/bloom_reconcile.py:45` | `0.3` | ниже этого баланса не стартуем |
| `BLOOM_MAX_BUYS_PER_MINT` | `analysis/bloom_exec_state.py:610` | `2` (`:72`) | покупок на один минт |
| `BLOOM_MINT_COOLDOWN_S` | `analysis/bloom_exec_state.py:609` | `900.0` (`:59`) | пауза по тому же минту |
| `BLOOM_API_ERROR_STREAK` | `analysis/bloom_exec_state.py:605` | `5` | подряд отказов API → пауза |
| `BLOOM_UNSOLD_STREAK` | `analysis/bloom_exec_state.py:606` | `3` | подряд непроданных → пауза |
| `BLOOM_SELL_AFTER_S` | `analysis/bloom_executor.py:67` | `28.8` | срок таймерного ордера на продажу |
| `BLOOM_SELL_MAX_ATTEMPTS` | `analysis/bloom_seller.py:1268` | `2` | две неудачи → `unsold` |
| `BLOOM_SELLER_LOOP_S` | `analysis/bloom_seller.py:9659` | `DEFAULT_LOOP_EVERY_S = 15.0` (`:207`) | период круга продавца. **На хосте 3**: при 15 закрытие выходило ровно через 15.0 с, удержание 43-52 с вместо 25-37 |
| `BLOOM_SELL_VIA_JUPITER` | `analysis/bloom_seller.py:1605` | `"0"` | второй путь выхода |
| `BLOOM_JUP_API` | `analysis/bloom_jupiter_sell.py:263` | пусто | **ЯВНО перебивает выбор пути продажи через Jupiter и СИЛЬНЕЕ наличия ключа.** `ultra` → план только `["ultra"]` (`:265-266`); `v2`, `swap-v2` или `swapv2` → `["swap-v2","ultra"]`, то есть swap-v2 с Ultra в запасе (`:267-269`); и ТОЛЬКО при пустом значении решает наличие ключа V2: есть → `["swap-v2","ultra"]`, нет → `["ultra"]` (`:270-272`). Значение приводится к нижнему регистру и обрезается по краям; любое другое слово читается как пустое. Выбор делает ОДНА функция на продавца и сторожа — два места с одним правилом однажды разошлись бы в деньгах |
| `BLOOM_DUST_RAW` | `analysis/bloom_seller.py:1594` | `1000` | меньше — крошка, продавать нечего |
| `BLOOM_OWN_SEND` | `analysis/bloom_own_send.py:109` | `0` | полоса своей отправки: сборка и симуляция |
| `BLOOM_OWN_SEND_LIVE` | `analysis/bloom_own_send.py:114` | `0` | полоса отправляет живьём |
| `BLOOM_OWN_SEND_SOL` | `analysis/bloom_own_send.py:319` | `0.01` | размер покупки полосой |
| `BLOOM_SENDER_REGION` | `analysis/bloom_own_send.py:173` | `"ams"` | регион точки; неизвестное значение МОЛЧА падает в `ams` (`:171-173`) |
| `BLOOM_POOL_SEND` / `BLOOM_POOL_NONCE` | `analysis/bloom_own_send.py:1754,1784` | — | отправка пулом; nonce-вариант. `pool_senders` задаётся СПИСКОМ |
| `BLOOM_LANE_NONCE_WARM_S` | `analysis/bloom_own_send.py:1847` | `30.0` | прогрев nonce |
| `BLOOM_LANE_DAY_LIMIT` | `analysis/bloom_own_send.py:5139` | `50` | сделок полосой в сутки |
| `LANE_DAILY_LOSS_SOL` / `LANE_KILL_DROP_SOL` | `analysis/bloom_own_send.py:5175,5180` | `0.0` / `0.5` | суточный убыток полосы; обвал → стоп |
| `LANE_STOP_CHAIN` / `LANE_CHAIN_MAX_AGE_S` / `LANE_CHAIN_VS_LEDGER_SOL` | `analysis/bloom_own_send.py:3833-3837` | `"1"` / `300.0` / `0.05` | сверка по цепи против журнала |
| `LANE_BONDING_CURVE`, `LANE_DAMM2`, `LANE_DBC`, `LANE_DLMM`, `LANE_CLMM`, `LANE_AMMV4`, `LANE_WHIRLPOOL`, `LANE_LAUNCHLAB` и парные `*_GROUPS` (в том числе `LANE_AMMV4_GROUPS`) | набор — `ДОП_ТИПЫ_ПОЛОСЫ`, `analysis/bloom_own_send.py:1333-1336`; функции флагов — `:1154` (кривая, историческое имя флага `LANE_BONDING_CURVE`, читается на `:1176`), `:1246`, `:1256`, `:1261`, `:1266`, `:1285`, `:1302`, `:1316`; общий разбор — `_тип_включён` `:1238` и `_тип_по_флагу` `:1179` | — | **флагов типов ВОСЕМЬ**: `BONDING`, `DAMM2`, `DBC`, `DLMM`, `CLMM`, `LAUNCHLAB`, `WHIRLPOOL`, `AMMV4`. Типы включаются ЯВНО, по флагу на тип; молчаливого «все» нет. Имён в поле `lane_pools` файла групп 11 — десять типов из `ИМЕНА_ТИПОВ` (`:1193-1196`) плюс `two_step` (`:1200`, это не тип пула, а способ сборки); у `pump_amm` и `cpmm` своей функции-флага нет. Файл групп СИЛЬНЕЕ окружения: есть непустой `lane_pools` — флаги env не спрашиваются вовсе (`:1238-1243`) |
| `BLOOM_STALE_SLOTS` | `analysis/bloom_detector.py:353` | `10` | STALE-догон: отставание слотов, после которого сделка просрочена |
| `BLOOM_MIN_TARGET_SOL` / `BLOOM_THRESHOLD_EDGE_PCT` | `analysis/bloom_detector.py:347,239` | `2.0` / `0.9` | порог размера цели, край порога |
| `BLOOM_DETECTOR_PULSE_S` | `analysis/bloom_detector.py:8108` | `60.0` | как часто пишется признак жизни детектора |
| `BLOOM_SOURCE_GROUPS` | `analysis/bloom_detector.py:14931-14943` | нет умолчания | путь к файлу групп. **Пусто → ВСЕМ адресам группа `off`, покупок нет** |
| `BLOOM_ZABOR_KOLCO_S` / `BLOOM_ZABOR_KOLCO_TEMPOM` | `analysis/bloom_detector.py:8165,8286` | `0.2176` / выключено | забор S+0 задаётся ЯВНО; кольцо по темпу — 397 059 вызовов в сутки |
| `BLOOM_TELEGRAM_LIVE_TEST` / `BLOOM_TELEGRAM_REAL` | `analysis/bloom_notify.py:81,101` | `"0"` / `"0"` | строки о стенде и о боевых |
| `TELEGRAM_LOG_CHAT_ID` | `analysis/bloom_notify.py:927` | пусто | журнальный чат. Пусто → построчные решения не идут НИКУДА и в основной чат не подмешиваются |
| `BLOOM_CLOSE_ON_SELL` / `CLOSE_ACCOUNTS_LIVE` | `analysis/bloom_close_on_sell.py:51,328` | — | закрывать токен-счёт после продажи. **Рента возвращается только при продаже через Jupiter** |

Настроечные сторожа продаж (`/etc/dbot-guard/env`, значения прописаны в
`run_vps_deploy_sold_position_guard_nl.yml:130-141`): `GUARD_LIVE_SELL=1`,
`GUARD_INTERVAL_S=45` (умолчание `analysis/dbot_sold_position_guard.py:90`),
`GUARD_STUCK_THRESHOLD=10` (`:91`), `GUARD_API_DOWN_ALERT_S=600` (`:92`),
`GUARD_STATE_DIR=/home/bot/dbot_sold_position_guard_live_data`,
`GUARD_TRADES_PATH=/home/bot/robinhood-chain-alpha/data/solana_trades_all.json` (`:136`,
только на чтение — так и сказано в самом прогоне на `:35`),
`RESCUE_LIVE=0`, `RESCUE_MAX_QUOTE_SOL=3.0`, `RESCUE_MIN_SHARE=0.30`,
`RESCUE_RETURN_RESERVE_SOL=0.002`, `RESCUE_SLIPPAGE_BPS=3000`.

### 4.4. Грабли окружения, которые стоили денег

1. **Деплой сторожа переписывает env ЦЕЛИКОМ** (`run_vps_deploy_bloom_seller_nl.yml:549`).
   Переносятся с хоста ровно девять имён (`:383`): `BLOOM_LIVE_BUY`, `BLOOM_BUY_SOL`,
   `TELEGRAM_LOG_CHAT_ID`, `JUPITER_API_KEY`, `EXEC_WALLET_KEY`, `BLOOM_OWN_SEND`,
   `BLOOM_OWN_SEND_LIVE`, `BLOOM_OWN_SEND_SOL`, `BLOOM_SENDER_REGION`. Без переноса любой
   деплой сторожа молча выключал бы боевые покупки и возвращал размер к `0.2 SOL`.
2. **`BLOOM_SOURCE_GROUPS` в этот перенос НЕ входит и деплоем не пишется.** После
   каждого `PEREPISAT` его надо вернуть прогоном `run_vps_env_flag_nl.yml`
   (`imya=BLOOM_SOURCE_GROUPS`). Именно на этом 03.10 с 18:33Z до 20:26Z стояли покупки:
   окружения не было, и по правилу «молчание значит не торгуем» всем адресам встала
   группа `off` (`deploy/checks/pered_perezapuskom_sluzhby.py:41-61`).
3. **Отсутствие секрета отправителя = удаление строки из env и смена состава пула**:
   шаг записи делает `sed -i '/^NAME=/d'` (`run_vps_deploy_bloom_detector_nl.yml:646-676`).
   Поэтому перед ним стоит гейт на четыре ключа (`:577`).
4. **Копия env снимается ДО первой правки** (`run_vps_deploy_bloom_detector_nl.yml:625`).
   Раньше `cp -a` стоял после шага ключей отправителей — удалённая им строка в копию не
   попадала, и сверка «копия против нового» молчала.
5. **Кошелёк полосы и его ключ сверяются перед КАЖДОЙ подписью**, не один раз при
   деплое: несовпадение `OWN_SEND_WALLET` и `OWN_SEND_WALLET_KEY` — запрет, а не
   предупреждение (`analysis/bloom_own_send.py:145-160`).
6. **`RESCUE_WALLET_ADDRESS` — переменная, а не секрет** (`vars.`,
   `run_vps_deploy_sold_position_guard_nl.yml:123`).
7. **Каталог `/etc/bloom-executor` имеет права 711, а не 750** — служба работает от `bot`
   и обязана войти в каталог за файлом рубильника; при 750 `exists()` бросал
   `PermissionError`, fail-closed запрещал покупки, и снаружи это выглядело как тишина
   (`run_vps_deploy_bloom_seller_nl.yml:200-214`).
8. **Сторож продаж живёт в своём env** `/etc/dbot-guard/env` и своём каталоге состояния,
   а журнал решений и позиции у него общие с детектором.
9. `BLOOM_LANE_NONCE` со значением-мусором однажды МОЛЧА выключил nonce-вариант —
   поэтому прогон nonce отдельным шагом вычищает негодное значение
   (`run_vps_lane_nonce_nl.yml`).
10. **`BIRDEYE_API_KEY` торговым путём не ЧИТАЕТСЯ нигде, но в прогонах это имя ЕСТЬ.**
    В `analysis/` и `deploy/` его нет ни одного раза — ни в детекторе, ни в продавце, ни
    в полосе. Зато он назван в установщике регистратора трендингов Code-2:
    `run_podbivka_trending_miami_ustanovka.yml:5` («Без `BIRDEYE_API_KEY` — слово
    владельца») и `:198-201` — «`BIRDEYE_API_KEY` НЕ СТАВИТСЯ (слово владельца):
    `Environment=` и `EnvironmentFile=` с ним здесь нет». То есть ключ НАРОЧНО НЕ
    СТАВИТСЯ на `lab-miami` по слову владельца от 12.10, а не «отсутствует как имя».
    Регистратор трендингов взять его умеет, и без него Birdeye ложится в снимок строкой
    «нет ключа `BIRDEYE_API_KEY`» — это снимок с НАЗВАННОЙ ПРИЧИНОЙ, а не молчаливый
    пропуск списка. Для торгового хоста вывод прежний: в env `/etc/bloom-executor/env`
    этого имени быть не должно.

---

## 5. Прогоны и параметры

Раннеры: `cloud` = `ubuntu-latest`; `nl` = `[self-hosted, nl-host]`; `nl|cloud` = выбор
входом `runner`. **Ни у одного прогона ИЗ ТАБЛИЦЫ НИЖЕ нет `type:` у входов — все входы
это СТРОКИ**, и сравнения идут словами (`yes`, `PEREPISAT`, `ZAMENIT`, `SNYAT`, `1`).

Но по репозиторию в целом это НЕ единственное исключение, и лабораторией дело не
ограничивается: `grep -rn "type: choice" .github/workflows` даёт **11 совпадений в
9 файлах**, и среди них есть ХОСТОВЫЕ прогоны — `run_gruppa_cand1_nl.yml:25` (вход
`rezhim`: `pokazat`/`primenit`/`otkatit`) и `run_zabor_s0_opyt_nl.yml:38,48` (входы
`otpravitel` и `rezhim`), оба с `runs-on: [self-hosted, nl-host]`. Остальные восемь
совпадений в семи файлах — лаборатория и старые спринты:
`run_lab_bootstrap_nl.yml:41`, `run_p3_guard.yml:20`,
`run_sprint_g1.yml:10`, `run_sprint_r1.yml:10`, `run_sprint_sc1.yml:10`,
`run_task1_oos_check4.yml:43,59`, `run_task1_weekend_gap.yml:16`. Вывод для нового
стенда: правило «входы — строки, сравнения словами» верно для прогонов подъёма, но
проверять надо КАЖДЫЙ прогон, который берёте в работу: у `choice` набор значений задан
списком `options:`, и слово не из списка просто не передать.

| Прогон | Дело | Входы = умолчание | Раннер | Опасность |
|---|---|---|---|---|
| `run_vps_bootstrap_nl.yml` | голый VPS: apt, ufw, fail2ban, пользователь `bot`, клон, venv | нет | cloud | **ОПАСЕН**: правит `sshd_config`, пишет `/etc/bot/env`. Гейт — шаг «3б. НОВОЕ соединение после смены sshd_config» |
| `run_vps_install_gh_runner.yml` | свой раннер пользователем `ghrunner` | `labels`=`nl-host,solana`; `runner_user`=`ghrunner`; `remove`=`no` | cloud | трогает системные юниты; версия раннера `RUNNER_VERSION: "2.321.0"` (`:38`) |
| `run_nl_runner_smoke.yml` | замер круга + проверка, что раннер НЕ читает env служб | нет | cloud и nl | безопасен |
| `run_helius_key_check.yml` | 12 вызовов `getSlot` ключом из секрета и ключом с хоста | `deploy`=`""` | cloud | **ОПАСЕН** при `deploy`: пишет ключ в env и перезапускает сторожа |
| `run_telegram_getupdates_chat_id.yml` | `getUpdates` → `chat_id` → тестовое сообщение | нет | cloud | пишет в чат, денег не трогает |
| `run_vps_deploy_bloom_seller_nl.yml` | деплой сторожа продаж; ЕДИНСТВЕННЫЙ, кто создаёт env целиком | `keep_env`=`yes`; `perepisat_env`=`""`; `enable_service`=`no`; `live_sell`=`0`; `sell_after_s`=`28.8`; `max_attempts`=`2`; `via_jupiter`=`0`; `telegram_live_test`=`1`; `telegram_log_chat_id`=`""`; `runner`=`nl`; `allow_lane_restart`=`no`; `allow_live_test_restart`=`no`; `vtoroe_mnenie_pervyj_deploj`=`no`; `zavisshie_fantomy`=`""` | nl\|cloud | **САМЫЙ ОПАСНЫЙ по env** |
| `run_vps_deploy_bloom_detector_nl.yml` | деплой детектора и исполнителя, аварии групп, хостовые таймеры | 25 входов, см. раздел 7 | nl\|cloud | **ОПАСЕН**: деньги (`live_buy`, `own_send_live`, `buy_sol`), env, службы |
| `run_lane_wallet_addr_nl.yml` | публичный адрес из секрета + остатки по цепи | адреса кошелька и nonce | cloud | чтение; гейт «ключей в отчёте быть не должно» |
| `run_lane_wallet_env_nl.yml` | ключ и адрес полосы в env, перезапуск двух служб | нет | cloud | **ОПАСЕН**: ключ кошелька |
| `run_vps_lane_nonce_nl.yml` | счёт долговечного nonce: проверить или создать | `mode`=`check` | nl | **ОПАСЕН при `create`**: тратит ренту |
| `run_sender_key_probe_nl.yml` | какую схему ключа ждёт сервер отправителя | `servisy`=`blockrazor,astralane,zeroslot`; `dochitat`=`yes` | nl | проба без денег |
| `run_sender_pool_docs_nl.yml` | адреса чаевых и минимумы отправителей | `servis`=`""` | nl | чтение |
| `run_vps_env_flag_nl.yml` | одна строка env по белому списку + перезапуск одной службы | `imya`(req)=`CLOSE_ACCOUNTS_LIVE`; `znachenie`(req)=`1`; `sluzhba`=`none` | cloud | **ОПАСЕН**: правит env |
| `run_vps_env_bloom_nl.yml` | показать безопасные значения `BLOOM_*` | нет | cloud | только чтение |
| `run_vps_telegram_log_chat_nl.yml` | журнальный чат: найти ID / положить в env | `mode`(req)=`read`; `chat_id`=`""`; `restart`=`yes` | nl | **ОПАСЕН**: env + перезапуск |
| `run_vps_set_jupiter_key_nl.yml` | `JUPITER_API_KEY` на хост | `mode`(req)=`read`; `restart`=`yes` | nl | **ОПАСЕН**: env + перезапуск |
| `run_vps_gruppy_zamena_nl.yml` | заменить живой файл групп целиком | `confirm`=`net` (нужно `ZAMENIT`); `hash`=`""`; `gruppy`=`batch5,lane_s0,leader` | cloud | **ОПАСЕН**: это «чем торгуем» |
| `run_vps_pravka_grupp_nl.yml` | правка полей политики групп, переносы адресов | `pokazat`=`yes`; `plan`=`no`; остальные пустые; `runner`=`nl` | nl\|cloud | **ОПАСЕН**: `lane_open_max`, `lane_sol`, `day_cap_sol`, `lane_pools` — это деньги |
| `run_vps_group_policy_nl.yml` | одно поле одной группы | `group`=`bloom_lane`; `field`=`bloom_sol`; `set`=`""`; `restart_detector`=`no` | nl | **ОПАСЕН** |
| `run_vps_deploy_sold_position_guard_nl.yml` | служба `dbot-sold-guard`, свой env | `enable_service`=`""` | cloud | **ОПАСЕН**: ключ спасения |
| `run_vps_deploy_detect_probe_nl.yml` | служба `dbot-detect-probe` | `enable_service`, `only_report`, `recount`, `missed` = `""` | cloud | не торгует |
| `run_vps_deploy_feed_probe_nl.yml` | служба `grpc-feed-probe` | `enable_service`=`no`; `razgonnye`=`3`; `zadachi`=`BATCH-5,BATCH-3`; `potolok_kreditov`=`300000`; `okno_s`=`3600`; `potolok_na_okno`=`30000`; `potolok_prognoza`=`200000`; `tx_v_minutu`=`5`; `helius_ws`=`1` | nl\|cloud | не торгует, но ЖРЁТ КРЕДИТЫ |
| `run_vps_deploy_shred_listener_nl.yml` | служба `shred-listener`: замер, насколько наш сигнал по WS Helius отстаёт от последнего шреда слота | `port`=`0`; `enable_service`=`no`; `ws`=`1`; `max_sources`=`70`; `open_firewall`=`no`; `allow_from`=`""`; `rmem_max`=`0` | cloud | не торгует, не подписывает, кошельков не знает. **ОПАСНОСТЬ не в деньгах, а в сети и ресурсах**: `open_firewall=yes` открывает входящий UDP (только с адреса из `allow_from`, пусто — открывать отказывается), `rmem_max` правит `net.core.rmem_max`. Юнит зажат: `Nice=15`, `CPUWeight=20`, `IOWeight=20`, `CPUQuota=35%`, `MemoryMax=400M` (`:264-268`) |
| `run_vps_stroki_tajmery_nl.yml` | таймеры строк владельцу | `vkljuchit`=`no`; `probnaja_otpravka`=`no`; `minuty`=`14,44`; `minuta`=`10` | nl | таймеры |
| `run_vps_autoupdate_guard_nl.yml` | `needrestart` не трогает службы, окно обновлений | `rezhim`=`config`; `okno`=`03:20`; `ozhidanie`=`240` | cloud | **ОПАСЕН**: может перезагрузить хост |
| `run_vps_bloom_kill_nl.yml` | рубильник покупок | `mode`(req)=`status`; `kakoj`=`common`; `prichina`=`""`; `tg`=`no`; `confirm`=`""` | cloud | снятие — деньги |
| `run_vps_lane_kill_nl.yml` | рубильник полосы `KILL_OWN_SEND` | `mode`(req)=`status`; `prichina`=`""`; `tg`=`no`; `confirm`=`""`; `sbros`=`no`; `bez_samoproverok`=`no` | nl | снятие — деньги |
| `run_vps_health_nl.yml` | живучесть служб и забор работы таймеров в ветку | `tail_decisions`=`0`; `diag_min`=`0`; `runner`=`nl`; плюс cron `38 * * * *` | nl\|cloud | только чтение, но ПАДАЕТ при мёртвой службе |
| `run_vps_priznak_zhizni_nl.yml` | хэш файла групп, подписка, полоса, Telegram | нет | cloud | чтение |
| `run_vps_env_v_pamjati_nl.yml` | имена env ЖИВОГО процесса против файла | нет | cloud | чтение |
| `run_vps_open_positions_nl.yml` | что считается открытым; дожать застрявшие | `pokazat`=`no`; `zapisat`=`no` | nl | `zapisat=yes` пишет закрытие в журнал |
| `run_klyuchi_pochistit_nl.yml` | стереть `env.bak.*`, убрать/заменить `EXEC_WALLET_KEY` | `steret`=`net`; `ubrat`=`net`; `svezhij_kljuch`=`net`; `restart`=`no` | nl | **ОПАСЕН**: уносит копии env и ключ |
| `run_sverka_storozha_nl.yml`, `run_vps_guard_state_check.yml`, `run_avariya_prodazh_nl.yml`, `run_vps_detector_subscription_audit.yml`, `run_progony_na_hoste_nl.yml` | разборы и сверки | см. файлы | nl / cloud | чтение |

---

## 6. Порядок подъёма

Шаги идут строго по порядку: каждый следующий опирается на проверку предыдущего.

**Шаг 0. Подготовить ветку под себя.**
Заменить зашитые значения из таблицы в разделе 2.1: имя ветки в `ref:`, адрес хоста,
метку раннера, URL репозитория, ветку клона в bootstrap, константу `EXECUTOR_WALLET`
(`analysis/bloom_exec_state.py:45`). Проверка: `grep -rn "45.59.170.76" .github/workflows
| wc -l` даёт 0. При отказе — прогон уйдёт на чужую машину, это самый дорогой промах.

**Шаг 1. Голый VPS.**
Запуск: `run_vps_bootstrap_nl.yml` (вручную, входов нет; секрет `VPS_SSH_KEY` должен быть
заведён). Делает: apt upgrade, ufw только 22/tcp, fail2ban, `PasswordAuthentication no`,
пользователя `bot` без sudo, `python3-venv`, клон репозитория в
`/home/bot/robinhood-chain-alpha`, venv и `pip install -r analysis/requirements.txt`.
Проверка: шаг «3б. НОВОЕ соединение после смены sshd_config» прошёл и шаг 9 печатает
`df -h /`. При отказе: если прогон упал ПОСЛЕ правки sshd и до проверки — не запускать
повторно слепо, сначала убедиться, что ssh по ключу работает.
Зачем это нужно боевым службам: ради единственного venv
`/home/bot/robinhood-chain-alpha/venv/bin/python` — им работают ВСЕ юниты и все таймеры.

**Шаг 2. Свой раннер GitHub на хосте.**
Запуск: `run_vps_install_gh_runner.yml`, `labels=nl-host,solana`, `runner_user=ghrunner`,
`remove=no`; нужен `GH_RUNNER_PAT` либо `GH_RUNNER_TOKEN`. Раннер ставится отдельным
пользователем `ghrunner` — не `bot` и не `root`.
Проверка: шаг «раннер виден и активен» и Settings → Actions → Runners показывает метки.
При отказе: `GH_RUNNER_TOKEN` живёт около часа — взять свежий; токен передаётся через
stdin, не аргументом (`:133`).

**Шаг 3. Проверить, что раннер не видит лишнего.**
Запуск: `run_nl_runner_smoke.yml` (две работы — облачная и хостовая).
Проверка: хостовая работа подтверждает, что раннер НЕ читает `/etc/bloom-executor/env`
(`:43-47`). При отказе: закрыть права ДО того, как в env появится хоть один ключ.

**Шаг 4. Ключ Helius.**
Запуск: `run_helius_key_check.yml` с `deploy` ПУСТЫМ (ничего не кладёт), затем
`run_helius_key_probe.yml`. Проверка: 12 из 12 `getSlot` отвечают, хвост ключа из 4
символов совпадает у секрета и у хоста. При отказе: ключ не тот или план не тот — в env
его не писать.

**Шаг 5. Telegram, основной чат.**
Запуск: написать боту любое сообщение, затем `run_telegram_getupdates_chat_id.yml`.
Проверка: прогон печатает `chat_id` и присылает тестовое сообщение. Это значение
заводится секретом `TELEGRAM_CHAT_ID`. При отказе: `getUpdates` пуст — бот не получал
сообщений; второй `getUpdates` одновременно не запускать.

**Шаг 6. СОЗДАНИЕ окружения и каталогов.**
Запуск: `run_vps_deploy_bloom_seller_nl.yml` с `keep_env=no` и `perepisat_env=PEREPISAT`,
`enable_service=no`, `live_sell=0`. Это единственный прогон, который создаёт
`/etc/bloom-executor/env` целиком и заводит каталоги с правами (`:200-214`): `CODE_DIR` и
`STATE_DIR` — `bot:bot`, `ENV_DIR` — `root:root 711`, сам env — `600 root:root`.
Проверка: шаг «ГЕЙТ: ни один ключ env не потерян правкой» (`:554`) зелёный, и прогон
печатает число строк в env. При отказе: без слова `PEREPISAT` гейт (`:109`) не пустит —
это защита, а не сбой. Деплой детектора без существующего env ОТКАЗЫВАЕТСЯ
(`run_vps_deploy_bloom_detector_nl.yml:326`: «нет $ENV_DIR/env — сначала деплой сторожа»).

**Шаг 7. Журнальный чат Telegram.**
Запуск: `run_vps_telegram_log_chat_nl.yml`, `mode=read` (посмотреть `seen_chats`), затем
`mode=set` с найденным `chat_id`. Проверка: имя `TELEGRAM_LOG_CHAT_ID` появилось в env
(значение не печатается). При отказе: пусто = построчные решения не идут НИКУДА и в
основной чат не подмешиваются — это тихая потеря журнала, не ошибка службы.

**Шаг 8. Кошелёк полосы.**
Запуск: сначала `run_lane_wallet_addr_nl.yml` — он выводит ПУБЛИЧНЫЙ адрес из секрета
`LIVE_PRIVATE` и остатки по цепи; убедиться, что ключ даёт ожидаемый адрес. Затем
`run_lane_wallet_env_nl.yml` — он пишет три строки: `OWN_SEND_WALLET_KEY`,
`OWN_SEND_WALLET`, `BLOOM_LANE_NONCE` (`:87-88`).
Проверка: гейт «Ключ даёт ИМЕННО подтверждённый адрес» и сверка множества имён env.
При отказе: прогон сам откатывает env из `env.bak.*`, если имя потерялось (`:108-118`).

**Шаг 9. Счёт долговечного nonce.**
Запуск: `run_vps_lane_nonce_nl.yml`, `mode=check` (бесплатно), затем `mode=create`.
Создание — одна транзакция `CreateAccountWithSeed` + `InitializeNonceAccount`,
распорядитель = кошелёк полосы (`analysis/lane_nonce.py:88-180`). Адрес воспроизводим из
кошелька полосы и семени `bloom-lane-nonce` (`analysis/lane_nonce.py:32-34,54`), поэтому
отдельного файла-списка нет.
Проверка: `mode=check` печатает `{address, exists, is_nonce, blockhash, authority,
lamports}`; адрес пишется в env только после `"is_nonce": true` на цепи.
При отказе: без этой проверки транзакции ушли бы недействительными, потратив чаевые.
Деньги: рента списывается с кошелька полосы и возвращается при закрытии счёта.

**Шаг 10. Отправители.**
Запуск: `run_sender_key_probe_nl.yml` (какую схему ключа ждёт сервер) и
`run_sender_pool_docs_nl.yml` (адреса чаевых, минимумы). Заводить надо те ИМЕНА, которые
читает ДЕПЛОЙ: `BLOCKRAZOR_AUTH_TOKEN`, `ASTRALANE_API_KEY`, `ZEROX_API`,
`NOZOMI_API_KEY`. Проверка: проба отвечает «ключ принят», и гейт «значения секретов не
попали в файлы отчёта» зелёный. При отказе: истёкший ключ 0slot виден как
`403 API key has expired`; без ключа Astralane отдаёт 401.

**Шаг 11. Деплой детектора.**
Запуск: `run_vps_deploy_bloom_detector_nl.yml` с `enable_service=no`, `live_buy=0`,
`own_send=0`, `own_send_live=0`, `keep_env=yes` (первый прогон — ничего не трогать в
env), затем второй прогон с `keep_env=no` и `perepisat_env=PEREPISAT`, чтобы положить
ключи отправителей и DBot. Делает: 17 самопроверок до сети, доставку кода со сверкой
sha256, юнит с ужесточением, запуск, и УСТАНОВКУ ПЯТИ ХОСТОВЫХ ТАЙМЕРОВ (`:1284`).
Проверка: шаг «Признак жизни детектора должен появиться» (`:1264`) и финальный гейт
хостовых таймеров — каждое имя из установщика есть среди живых таймеров.
При отказе: гейты разбираются в разделе 7, каждый называет причину словами.

**Шаг 12. Файл групп источников.**
Запуск: положить свой файл групп в ветку, затем `run_vps_gruppy_zamena_nl.yml` с
`confirm=ZAMENIT`, ожидаемым `hash` (12 знаков) и списком торгующих групп в `gruppy`.
После этого ОБЯЗАТЕЛЬНО прописать путь в env: `run_vps_env_flag_nl.yml`,
`imya=BLOOM_SOURCE_GROUPS`, `znachenie=/home/bot/data/<ваш файл>.json`.
Точечные правки потом — `run_vps_pravka_grupp_nl.yml` и `run_vps_group_policy_nl.yml`.
Проверка: `jq '.source_groups' detector_status.json` показывает группы и живой путь
файла; `run_vps_priznak_zhizni_nl.yml` сверяет хэш.
При отказе: без `BLOOM_SOURCE_GROUPS` служба ставит ВСЕМ адресам группу `off` —
покупок не будет, и гейт перезапуска не пустит службу дальше
(`deploy/checks/pered_perezapuskom_sluzhby.py:59-61`).

**Шаг 13. Второй путь выхода (Jupiter).**
Запуск: `run_vps_set_jupiter_key_nl.yml`, `mode=set`, затем деплой сторожа с
`via_jupiter=1` (он же доставит `solders` в venv, `:606`).
Проверка: гейт «Путь через Jupiter: ключ обязателен и должен быть НАШЕГО кошелька»
(`run_vps_deploy_bloom_seller_nl.yml:280`).
При отказе: ключа нет → останется только Ultra; пустая строка хуже отсутствия.

**Шаг 14. Остальные службы.**
Запуск: `run_vps_deploy_sold_position_guard_nl.yml` (нужны `RESCUE_WALLET_KEY` и
переменная `RESCUE_WALLET_ADDRESS`), `run_vps_deploy_detect_probe_nl.yml`,
`run_vps_deploy_feed_probe_nl.yml`. Вместе с детектором и продавцом это те ПЯТЬ служб,
которых ждёт живучесть (`deploy/zhivuchest_na_hoste.py:38-39`).
Проверка: `--check-only` до боевого включения; юнит ставится без запуска, если
`enable_service != yes`. При отказе: отсутствие службы для живучести — ФАКТ, а не сбой
(`deploy/zhivuchest_na_hoste.py:56`), тревога только если юнит есть и не активен.

ШЕСТАЯ служба — `shred-listener`, и она ставится ПОСЛЕДНЕЙ из юнитов и только если
владелец купил поток шредов. Запуск: `run_vps_deploy_shred_listener_nl.yml`. Порядок
внутри шага: сначала с `port=0` — прогон только доставляет код, гоняет самопроверку НА
ХОСТЕ и печатает готовность, ни юнита, ни запуска; затем, когда порт от BlockRazor на
руках, `port=<порт>`, `enable_service=yes`, `open_firewall=yes` и ОБЯЗАТЕЛЬНО
`allow_from=<адрес источника шредов>` — с пустым `allow_from` прогон открывать порт
отказывается, и это защита, а не сбой. `rmem_max` трогать только если слушатель теряет
пакеты. Проверка: самопроверка на хосте зелёная и `systemctl is-active shred-listener`.
В список живучести эта служба НЕ входит (там пять имён,
`deploy/zhivuchest_na_hoste.py:38-39`), её отсутствие тревоги не даёт. Денег она не
касается вовсе: не торгует, не подписывает, кошельков не знает, из шреда берёт только
слот и индекс из заголовка.

**Шаг 15. Таймеры строк владельцу.**
Запуск: `run_vps_stroki_tajmery_nl.yml`, `vkljuchit=yes`, `minuty=14,44`, `minuta=10`.
Проверка: `systemctl list-timers 'bloom-*'` показывает `bloom-stroka-balansov` и
`bloom-samoproverka-chas`; прогон делает пробную отправку по входу.
При отказе: без `SupplementaryGroups=systemd-journal` `journalctl` чужого юнита отдаёт
пустоту и код 1 — это уже учтено в юните.

**Шаг 16. Автообновления.**
Запуск: `run_vps_autoupdate_guard_nl.yml`, `rezhim=config`. Ставит врезку, чтобы
`needrestart` не трогал наши службы, и сдвигает окно обновлений прочь от торгового.
Проверка: врезка `/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf` на месте.
При отказе: `rezhim`, требующий перезагрузки, проходит только через гейт «рубильник
стоит и позиций 0».

**Шаг 17. Рубильники ДО денег.**
Запуск: `run_vps_bloom_kill_nl.yml`, `mode=set`, `kakoj=common`, с причиной — поставить
общий `KILL` ЗАРАНЕЕ; и `run_vps_lane_kill_nl.yml`, `mode=status`.
Проверка: финальный шаг «Служба видит рубильник так же, как файл на диске».
При отказе: гейт «рубильник читается ОТ ПОЛЬЗОВАТЕЛЯ СЛУЖБЫ, а не от root» — проверка от
`root` бесполезна, первый живой прогон детектора встал на `PermissionError` именно так.

**Шаг 18. Признаки жизни.**
Запуск: `run_vps_health_nl.yml` (он же сам по cron `38 * * * *`),
`run_vps_priznak_zhizni_nl.yml`, `run_vps_env_v_pamjati_nl.yml`,
`run_sverka_storozha_nl.yml`. Проверка: прогон живучести зелёный — ни одной мёртвой
службы и ни одного признака старше 10 минут. При отказе: смотреть раздел 8.

---

## 7. Гейты: что не пускает деплой и как читать отказ

Гейт — это отдельный шаг прогона, который РОНЯЕТ прогон и называет причину словами.
Зелёный деплой без пройденных гейтов невозможен. Ниже — предохранители главного прогона
`run_vps_deploy_bloom_detector_nl.yml` (имена шагов ровно как в файле).

| Строка | Шаг | Что не пускает |
|---|---|---|
| `:160` | `ГЕЙТ: переписывать env можно только по явному слову` | любой деплой с `keep_env != yes` без слова ровно `PEREPISAT`. Умолчание `keep_env=yes` = env не трогаем вовсе |
| `:213` | `Самопроверки ДО доставки (без сети)` | код с провалившейся самопроверкой. Гоняются `bloom_trevogi`, `bloom_exec_state`, `c2_swap_build` (БАЙТЫ транзакции покупки), `bloom_api`, `bloom_seller`, `bloom_vtoroe_mnenie` (80 проверок правила 4), `bloom_jupiter_sell` (65), `bloom_detector`, `bloom_executor`, `bloom_reconcile`, `bloom_senders`, `bloom_own_send` (759 проверок), `bloom_source_groups`, `check_shell_ascii`, `check_workflow_quotes`, `check_workflow_bash`, `deploy/hostovye_tajmery.py`, `deploy/zhivuchest_na_hoste.py`. `set -o pipefail` обязателен: две строки идут через `| tail -1`, без него код шага брался у `tail` |
| `:293` | `Секреты не пусты (значения не печатаются)` | деплой без `HELIUS_API`, `DBOT_API_KEY`, `VPS_SSH_KEY`. Печатается только длина |
| `:318`, отказ на `:326` | `Каталоги на месте (сторож их уже создал -- проверяем, не ломаем)` | деплой детектора на хост без `/etc/bloom-executor/env` |
| `:330` | `Все нужные модули есть в списке доставки` | `ModuleNotFoundError` на хосте: импорты ищутся транзитивно по объединению списков двух деплоев |
| `:337` | `Доставка кода + СВЕРКА sha256 (расхождение = падение)` | молча обрезанный или пустой файл (есть отдельная проверка на сумму пустого файла) |
| `:577` | `ГЕЙТ: четыре ключа отправителей не пусты (иначе PEREPISAT сотрёт их строки)` | `PEREPISAT`, если пуст хоть один из `BLOCKRAZOR_AUTH_TOKEN`, `ASTRALANE_API_KEY`, `ZEROX_API`, `NOZOMI_API_KEY`: запись делает `sed -i '/^NAME=/d'` и молча сменила бы состав пула |
| `:625` | `env: резервная копия ДО любой правки (имя копии -- в журнал)` | правку без копии `env.bak.<UTC>` |
| `:930` | `ГЕЙТ: рубильник читается ОТ ПОЛЬЗОВАТЕЛЯ СЛУЖБЫ, а не от root` | запуск, если `kill_readable()` от имени `bot` говорит НЕДОСТУПЕН |
| `:970` | `ГЕЙТ: каталог состояния принадлежит службе, а не root` | состояние, в которое служба от `bot` писать не сможет. Так уже потерялся весь учёт кредитов: каталог создал `--check-only` от `root` |
| `:985` | `СВЕРКА env с копией: весь файл как МНОЖЕСТВО строк (порядок не считается)` | деплой, потерявший или изменивший ЛЮБУЮ строку env. Допустимое различие ровно одно — `BLOOM_REGION_SEND`. В вывод идут только ИМЕНА разошедшихся строк |
| `:1058` | `Сверка при старте (bloom_reconcile): можно ли включать деньги` | (а) `Traceback` в выводе — вердикта НЕТ ВОВСЕ, и это не повод включать деньги; (б) `live_test=yes` и код не 0. Порог баланса печатается числом |
| `:1159` | `НУЛЬ ПОЗИЦИЙ перед перезапуском` | перезапуск, пока в воздухе позиция. Охрана ОДНА на все прогоны — `deploy/checks/pered_perezapuskom_sluzhby.py`, и стоит вплотную к перезапуску, потому что между постановкой в очередь и перезапуском проходят минуты |
| `:1193` | `Запуск и ЖЁСТКАЯ проверка, что поднялось` | `systemctl is-active` не `active` или `NRestarts >= 3`. Момент перезапуска берётся часами ХОСТА |
| `:1212` | `ВСЕ службы, делящие каталог кода, перезапущены (не только детектор)` | состояние, где детектор и продавец работают на разных версиях ОБЩИХ модулей. Старт каждой службы обязан быть позже самого свежего `*.py` в каталоге, иначе `STARYJ_KOD_V_PAMJATI` |
| `:1264` | `Признак жизни детектора должен появиться` | «зелёный» деплой, у которого `detector_status.json` СТАРШЕ момента перезапуска |
| `:1284` | `ХОСТОВЫЕ ТАЙМЕРЫ` | внутри три предохранителя: сверка sha256 каждого доставленного файла; самопроверки `hostovye_tajmery.py`, `zhivuchest_na_hoste.py`, `sdelki_polosy_vygruzka.py` НА ХОСТЕ тем питоном, которым пойдёт таймер; гейт «каждое имя таймера ИЗ УСТАНОВЩИКА есть среди живых таймеров». Кэш цепи только ЗАСЕВАЕТСЯ. Последняя строка шага — `deploy/checks/bez_klyuchey_v_otchete.py` |

Гейты деплоя сторожа (`run_vps_deploy_bloom_seller_nl.yml`): `:109` слово `PEREPISAT`,
`:174` секреты не пусты, `:280` «Путь через Jupiter: ключ обязателен и должен быть
НАШЕГО кошелька», `:554` «ни один ключ env не потерян правкой» (вторая половина ловит
ключ, который остался именем, но опустел), `:612` каталог состояния принадлежит службе,
`:676` рубильник читается от `bot`, `:692` открытых позиций нет, `:733` «НУЛЬ ПОЗИЦИЙ
перед перезапуском ПРОДАВЦА».

Общая охрана перед перезапуском любой службы —
`deploy/checks/pered_perezapuskom_sluzhby.py`. Она требует: открытых позиций 0 по ВСЕМ
группам (не пропускается ничем), зависших 0 (разовое исключение
`ZAVISSHIE_FANTOMY=yes` только для фантомов), рубильник продаж `KILL_SELL` СНЯТ (`:37`),
возраст признаков жизни не больше `ПРЕДЕЛ_ВОЗРАСТА_S = 600` (`:38`, применяется к
детектору `:244-250` и отдельно к сторожу `:312-319`), загруженный модуль второго мнения
у сторожа, и непустой `BLOOM_SOURCE_GROUPS` с существующим файлом (`:59-61`).

Как читать отказ: имя упавшего шага — это и есть диагноз. Все гейты печатают ПРИЧИНУ
словами и ни один не печатает значений секретов (только длины, хвосты из 4 символов и
ИМЕНА строк).

---

## 8. Признаки жизни

Четыре файла-признака, пять служб, один порог на всех — **600 секунд**.

| Файл | Кто пишет | Как часто | Кто читает |
|---|---|---|---|
| `/home/bot/bloom_executor_live_data/detector_status.json` | `bloom-detector` (`analysis/bloom_detector.py:3374-3418`, запись `:3709`) | раз в пульс, `BLOOM_DETECTOR_PULSE_S = 60.0` (`analysis/bloom_detector.py:8108`) | гейт деплоя (`run_vps_deploy_bloom_detector_nl.yml:1264`), `deploy/zhivuchest_na_hoste.py:44`, `deploy/checks/pered_perezapuskom_sluzhby.py:34`, сторож продаж (`analysis/bloom_seller.py:767`) |
| `/home/bot/bloom_executor_live_data/seller_heartbeat.json` | `bloom-seller` (`analysis/bloom_seller.py:5088-5140`) | каждый круг, `BLOOM_SELLER_LOOP_S=3` | `deploy/zhivuchest_na_hoste.py:45`, `deploy/checks/pered_perezapuskom_sluzhby.py:35` |
| `/home/bot/dbot_detect_probe/status.json` | `dbot-detect-probe` (`analysis/solana_detect_probe.py:62`) | своим кругом | `deploy/zhivuchest_na_hoste.py:46` |
| `/home/bot/grpc_feed_probe_data/heartbeat.json` | `grpc-feed-probe` | своим кругом | `deploy/zhivuchest_na_hoste.py:47` |

Пороги и правила с номерами строк:

* `deploy/zhivuchest_na_hoste.py:51` — `ПОРОГ_ПРИЗНАКА_S = 600`; тревога строго при `>`
  (`:158`), «ровно порог — ещё не тревога» закреплено самопроверкой (`:257-259`).
* `deploy/zhivuchest_na_hoste.py:38-39` — список пяти служб: `dbot-sold-guard`,
  `dbot-detect-probe`, `bloom-seller`, `bloom-detector`, `grpc-feed-probe`.
* `deploy/zhivuchest_na_hoste.py:56` — `НЕТ_ЮНИТА = ("not-found",)`: отсутствие службы это
  ФАКТ, а не сбой; тревога только если юнит установлен и не активен. `LoadState`, который
  не `loaded` и не `not-found`, — отдельная тревога «юнит есть, но systemd его не принял»
  (`:111`).
* `run_vps_health_nl.yml:384` — шаг «Свежесть признаков жизни (порог 10 минут)», метка
  `СТАРШЕ_10_МИНУТ` → `STALE=1` (`:410`), итог валит прогон при `STALE != 0` (`:657`).
  Возраст считается НА ХОСТЕ: часы бегунка и хоста разные.
* `analysis/bloom_seller.py:769` — `СЛОТ_СЕТИ_СВЕЖЕСТЬ_S = 5.0`, порог свежести
  `net_slot.json` для решения об удержании; `analysis/bloom_detector.py:199` —
  `СЛОТ_ФАЙЛ_КАЖДЫЕ_S = 0.25` (реже 0.5 с нельзя: возраст числа должен быть заметно
  меньше порога 5 с и меньше одного слота).
* Сводный доклад живучести пишет ТАЙМЕР хоста `bloom-zhivuchest-chas` в
  `/home/bot/host_tajmery/zhivuchest/zhivuchest.json` (`deploy/hostovye_tajmery.py:121-129`,
  `OnCalendar=*:38 UTC`, предел 120 с, память 256M).

Грабля расписания: таймер пишет файл в `*:38`, а прогон `run_vps_health_nl.yml` с cron
`38 * * * *` в ту же минуту его ЗАБИРАЕТ — копия в ветке регулярно отстаёт на час от
чисел на хосте. Сами числа не теряются: они лежат на хосте и догоняются следующим
забором. Хост на ветку ПУШИТЬ НЕ МОЖЕТ — токена записи у него нет и заводить его рядом с
ключами кошельков нельзя (`run_vps_health_nl.yml:254-266`), поэтому последняя миля до
ветки едет только прогоном.

Быстрая проверка руками на хосте:

```bash
cd /home/bot/bloom_executor_live_data
for F in detector_status.json seller_heartbeat.json; do
  echo "$F: $(( $(date -u +%s) - $(stat -c %Y $F) )) c nazad"
done
jq '{updated_utc, mode, positions_in_cycle, pozicii_why_not, last_cycle}' seller_heartbeat.json
jq '{utc, итог, тревоги}' /home/bot/host_tajmery/zhivuchest/zhivuchest.json
systemctl list-timers 'bloom-*' --no-pager
```

---

## 9. Первый запуск денег

Деньги включаются ПОСЛЕДНИМИ и только по отдельному решению владельца, по одному
рубильнику за раз. До этого стенд живёт в `dry`: журнал пишется, POST в Bloom не уходит.

Порядок включения:

1. Чистая сверка: `run_vps_health_nl.yml` зелёный, `bloom_reconcile` в деплое дал вердикт
   БЕЗ `Traceback`, баланс кошелька исполнителя выше `BLOOM_MIN_START_BALANCE_SOL`
   (умолчание 0.3 SOL), открытых позиций 0.
2. **Продажи раньше покупок.** Деплой сторожа с `live_sell=1` и `enable_service=yes`.
   Иначе можно купить то, что никто не продаст.
3. **Покупки.** Деплой детектора с `live_buy=1`, `buy_sol=<размер>` и
   `enable_service=yes`. `BLOOM_LIVE_BUY` деплой детектора по умолчанию ВСЕГДА вычищает
   из env (`run_vps_deploy_bloom_detector_nl.yml:4-7` и шаг записи env) — включается
   только явным входом, и забытый вход не оставит боевые покупки включёнными случайно.
4. **Полоса своей отправки — отдельно и в два приёма**: сначала `own_send=1` (сборка и
   симуляция, без отправки), потом `own_send_live=1`.
5. Размер билета: первый боевой размер ставится входом `buy_sol` (у прогона умолчание
   `0.05`, в коде `DEFAULT_BUY_SOL = 0.2`), размер полосой — `own_send_sol`
   (умолчание `0.01`). Предел открытых позиций `BLOOM_MAX_OPEN=5`, суточный убыток
   площадки `BLOOM_DAILY_LOSS_SOL=1`, сделок полосой в сутки `BLOOM_LANE_DAY_LIMIT=50`.

Рубильники (любой файл на месте = запрет):

| Файл | Что глушит | Чем ставится и снимается |
|---|---|---|
| `/etc/bloom-executor/KILL` | ВСЁ, включая полосу | `run_vps_bloom_kill_nl.yml`, `mode=set/clear`, `kakoj=common`. Снятие — только слово ровно `SNYAT` плюс обязательная причина |
| `/etc/bloom-executor/KILL_BLOOM` | только площадку Bloom, полоса торгует | тот же прогон, `kakoj=bloom` |
| `/etc/bloom-executor/KILL_SELL` | продажи (`BLOOM_KILL_SELL_FILE`) | прогона, который его СТАВИТ, в ветке не нашёл — он только читается (`run_vps_bloom_kill_nl.yml:106`) и требуется СНЯТЫМ гейтом перезапуска (`deploy/checks/pered_perezapuskom_sluzhby.py:37`) |
| `$STATE_DIR/KILL_OWN_SEND` | полосу своей отправки | `run_vps_lane_kill_nl.yml`. ТРИ гейта: слово `SNYAT` и причина; самопроверки полосы и сторожа НА ХОСТЕ; «ключ полосы даёт именно её адрес» |
| `$STATE_DIR/KILL_BY_TELEGRAM`, `$STATE_DIR/KILL_SELL_BY_TELEGRAM` | покупки / продажи, поставленные из Telegram | команды владельца `/kill`, `/kill_sell`; `/resume` снимает ТОЛЬКО их и рубильник в `/etc` не трогает никогда (`analysis/bloom_telegram_cmd.py:17-21,50`) |

Команды из Telegram принимаются только от ID владельца и сверяются И по `chat.id`, И по
`from.id` (`analysis/bloom_telegram_cmd.py:11`); остальное молча идёт в счётчик чужих.

Как убедиться, что рубильник ДЕЙСТВУЕТ, а не просто лежит: финальный шаг прогона
«Служба видит рубильник так же, как файл на диске» и гейт чтения от пользователя службы.

---

## 10. Чего не делать

1. **Не запускать деплой детектора с пустыми входами «чтобы просто обновить код».**
   Входы с умолчаниями — это деньги: `buy_sol` по умолчанию `0.05`, а на хосте может
   жить `0.2`; `own_send` по умолчанию `0`, а полоса работает. Сначала
   `run_vps_env_bloom_nl.yml` — посмотреть, что стоит сейчас.
2. **Не писать `keep_env=no` без `PEREPISAT` и не писать `PEREPISAT` без нужды.**
   Деплой сторожа перезаписывает env ЦЕЛИКОМ и переносит с хоста только девять имён;
   `BLOOM_SOURCE_GROUPS` среди них нет.
3. **Не писать в env пустой ключ.** Пустая строка = «ключ есть, но пустой», и путь
   продажи сочтёт себя готовым.
4. **Не удалять секрет отправителя, чтобы «временно его выключить»** — пропадёт строка
   env и МОЛЧА сменится состав пула. Выключение — полем `vyklyuchen` в `data/senders.json`
   (строго `is True`), а не удалением записи.
5. **Не перезапускать службы при непустых позициях.** Охрана
   `deploy/checks/pered_perezapuskom_sluzhby.py` одна на все прогоны; обходить её
   входами нельзя, `ZAVISSHIE_FANTOMY=yes` — разовое исключение только для фантомов.
6. **Не перезапускать детектор без продавца** (и наоборот), если они делят каталог кода:
   две версии общих модулей в памяти дают `STARYJ_KOD_V_PAMJATI` и падение.
7. **Не считать `lane_pools` «всеми типами по умолчанию»** — типы включаются
   ЯВНО, по флагу на тип: флагов **восемь** (`ДОП_ТИПЫ_ПОЛОСЫ`,
   `analysis/bloom_own_send.py:1333-1336`). То же с `zabor_s0` и `pool_senders`: последний задаётся СПИСКОМ.
8. **Не рассчитывать на возврат ренты при продаже через Bloom** — рента возвращается
   только при продаже через Jupiter (`analysis/bloom_close_on_sell.py`).
9. **Не подменять точку 0slot другим регионом** — у нас работает только `ams1`; при
   неизвестном `BLOOM_SENDER_REGION` код молча падает в `ams`
   (`analysis/bloom_own_send.py:171-173`).
10. **Не включать `BLOOM_TELEGRAM_REAL` «чтобы видеть боевые»** — живым путём он не
    зовётся, и его включение погасило бы строки по боевым группам
    (`analysis/bloom_notify.py:84-104`).
11. **Не запускать второй `getUpdates` того же бота** — два клиента отбирают обновления
    друг у друга.
12. **Не держать на хосте токен записи в GitHub.** Хост пушить не может и не должен:
    последняя миля до ветки едет прогоном (`run_vps_health_nl.yml:254-266`).
13. **Не копировать на лабораторные машины ключи кошельков, env полосы и торговые
    службы** — это проверкой, а не обещанием: страж в `run_lab_bootstrap_nl.yml` роняет
    прогон и называет находку.
14. **Не печатать значения ключей ни в один отчёт.** Для этого есть
    `deploy/checks/bez_klyuchey_v_otchete.py` и `deploy/checks/arhiv_bez_klyuchey.py`; в
    докладах наружу идут только имена, длины и хвосты из 4 символов.
15. **Три незакрытых дефекта денежного пути** (списание упавшей позиции; состояние
    «списана» вне круга и гейта; вызовы без ловли исключения) записаны как ИЗВЕСТНЫЕ в
    `docs/HOST_INVENTAR.md` и НЕ чинятся при консервации. Поднимая стенд с нуля, считать
    их действующими: позиция в состоянии `spisana` не видна ни кругу продавца, ни
    суточному стопу (`analysis/bloom_exec_state.py:180`), а `STATES_OPEN` её не включает
    (`analysis/bloom_exec_state.py:86`).
