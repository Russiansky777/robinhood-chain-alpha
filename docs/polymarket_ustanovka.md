# Регистратор Polymarket на lab-ams — установка

Писал CODE-3 (ветка `claude/stroiteli`). **Хост я не трогаю** — ставит Code-1
или владелец. Образец — `docs/podbivka_trending_ustanovka.md` (Code-2,
lab-miami), порядок тот же.

Скрипт — один: `analysis/polymarket_put.py`. Он же считает производные
(`--proizvodnye`), пушит (`--push`), чистит сырое (`--rotacija`) и ставит
отметку об артефакте (`--otmetit-artefakt`). Отдельных файлов нет нарочно.

**Решения владельца 16.10, по которым всё настроено:**

| | |
|---|---|
| рынков | **100** |
| сырое | на ящике **30 суток** + **суточный артефакт Actions** с бегунка lab-ams, retention **90** |
| в ветку | **только производные** минутные ряды (и отметки об артефактах) |
| ящик | **lab-ams, 216.73.157.61** |

**Ключей не нужно ни одного.** Ни кошелькового, ни API: всё опрашиваемое
Polymarket отдаёт без подписи — разбор по уровням подписи из их же клиента в
`docs/polymarket_put.md`, п. 2. Скрипт **не читает ни одной переменной
окружения** (это проверяет его самопроверка), значит `EnvironmentFile` юнитам
не нужен и ни одного секрета на машину не кладётся.

---

## 0. Сначала самопроверка — у меня она НЕ ПРОГНАНА

Предохранитель облачного окружения заблокировал в моей сессии запуск любых
команд: «auto mode could not evaluate this action... it reacts to earlier
conversation content... will keep firing for the rest of this conversation».
Переключение режима разрешений его не снимает. Поэтому `--self-test` и `ruff`
я выполнить не смог — **35 проверок объявлены, но не подтверждены**.

```bash
cd /opt/polymarket-put
python3 analysis/polymarket_put.py --self-test   # ждём «проверок 35, ждали 35, не прошло 0»
ruff check analysis/polymarket_put.py            # если ruff есть на ящике
```

**Таймеры до зелёной самопроверки не включать.** Если число проверок или число
упавших разойдётся — пришли вывод, поправлю.

## 1. Что пишется

9 списков, три API, все без подписи.

| список | каждые | метод | запрос |
|---|---|---|---|
| `gamma_events` | 5 мин | GET | `gamma-api.polymarket.com/events?active=true&closed=false&order=volume_24hr&ascending=false&limit=100` |
| `clob_books` | 1 мин | POST | `clob.polymarket.com/books`, тело `[{token_id}]` |
| `clob_prices` | 1 мин | POST | `/prices`, тело `[{token_id, side}]` — по две записи на токен |
| `clob_midpoints` | 1 мин | POST | `/midpoints` |
| `clob_spreads` | 1 мин | POST | `/spreads` |
| `data_trades` | 1 мин | GET | `data-api.polymarket.com/trades?limit=500` |
| `clob_fee_rate` | 5 мин | GET | `/fee-rate?token_id=…` |
| `data_holders` | 5 мин | GET | `/holders?market=<conditionId>&limit=100` |
| `clob_time` | 5 мин | GET | `/time` — контроль живости |

Состав рынков берётся **одним** запросом `/events` и кладётся в
`data/polymarket/vybor.json`; минутный круг читает этот файл. Выбор
перезаписывается **только из удачного снимка**: иначе один отказ сети стёр бы
состав, и минутный круг замолчал бы.

**Потолок — 125 рынков**, считается из их предела батча 500: на рынок два
токена, а `/prices` требует сторону, то есть четыре записи на рынок. При 100
рынках запас есть; выше 125 прогон отказывается по имени **до** первого
запроса.

## 2. Формат, ротация и что едет в ветку

Сырое: `data/polymarket/<список>/<YYYY-MM-DD>/<HH>.jsonl.gz`, строка = снимок,
дописыванием. Поля — `utc`, `список`, `метод`, `url`, `запрос`, `код`,
`ошибка`, `заголовки` (лимиты), `секунд`, `байт`, `тело` (СЫРОЕ), `разбор`.
Сырое не выбрасывается никогда; отказ — такая же строка с кодом и причиной.

**Час берётся из метки снимка**, а не из локальных часов: иначе снимок в
23:59:59Z, записанный в 00:00:00Z, лёг бы в чужой час.

Производные: `data/polymarket/proizvodnye/<YYYY-MM-DD>/<HH>.jsonl.gz`, строка =
(минута, токен):

```
utc  snimok_utc  token_id  conditionId  slug
luchshij_bid  luchshij_ask  mid  spred
razmer_luchshego_bid  razmer_luchshego_ask  urovnej_bid  urovnej_ask
glubina_bid_1c  glubina_ask_1c  glubina_bid_5c  glubina_ask_5c
sdelok_za_minutu  objom_za_minutu
nagrada_min_size  nagrada_max_spread  nagrada_sutochnaja_stavka
nagrada_zapisej  nagrada_forma
tick_size  min_order_size  poslednjaja_sdelka
```

Три вещи здесь сделаны нарочно, и каждая иначе молча врала бы:

1. **Глубина считается от СЕРЕДИНЫ, а не от лучшей цены.** Награда за
   ликвидность считается по спреду от середины (у рынка свой максимальный
   спред от середины, внутри него ордер зачитывается). При спреде 0.30/0.70 в
   ±1 цент от середины не входит ни одного уровня — а счёт от лучшей цены
   показал бы по 500 с каждой стороны.
2. **Минута сделки берётся из метки САМОЙ сделки, а не снимка**, и метки
   читаются и в секундах, и в миллисекундах.
3. **Дубли сделок снимаются по id.** `/trades?limit=500` каждую минуту отдаёт
   перекрывающиеся окна; без снятия дублей объём был бы завышен кратно числу
   снимков, где сделка засветилась.

**Производные пересчитываются из сырого и перезаписывают часовой файл**, а не
дописывают: прогнать `--proizvodnye` дважды безопасно, строки не удвоятся.

**В ветку едут только** `data/polymarket/proizvodnye` и
`data/polymarket/artefakty`. Сырое в ветку не пушится вовсе.

## 3. Установка (делает Code-1 на lab-ams)

```bash
# predohranitel: eto ne torgovyj host
test "$(hostname -I | awk '{print $1}')" != "45.59.170.76" || { echo STOP; exit 1; }

sudo git clone -b claude/stroiteli <origin> /opt/polymarket-put
sudo useradd -r -s /usr/sbin/nologin polymarket 2>/dev/null || true
sudo chown -R polymarket:polymarket /opt/polymarket-put
```

Из внешнего не нужно ничего: только стандартная библиотека Python
(`urllib.request`, `gzip`, `json`, `argparse`, `subprocess`, `calendar`,
`tempfile`). Пакеты не ставить.

### Четыре юнита

`/etc/systemd/system/polymarket-min.service`

```ini
[Unit]
Description=Polymarket: minutnye spiski (knigi, ceny, spredy, sdelki)
After=network-online.target

[Service]
Type=oneshot
User=polymarket
WorkingDirectory=/opt/polymarket-put
ExecStart=/usr/bin/python3 analysis/polymarket_put.py --kazhdye минута --rynkov 100 --pauza 0.4
TimeoutStartSec=50
Nice=10
# kljuchej net vovse; torgovogo puti ne kasaetsja
PrivateTmp=yes
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/opt/polymarket-put
```

`/etc/systemd/system/polymarket-min.timer` — `OnCalendar=*:0/1`,
`AccuracySec=1s`, `Persistent=false`.

`/etc/systemd/system/polymarket-5min.service` — то же, но
`--kazhdye пять --rynkov 100 --pauza 0.6`, `TimeoutStartSec=120`.
Таймер — `OnCalendar=*:0/5`, `AccuracySec=5s`.

`/etc/systemd/system/polymarket-proizvodnye.service`

```ini
[Unit]
Description=Polymarket: proizvodnye minutnye rjady i push v vetku

[Service]
Type=oneshot
User=polymarket
WorkingDirectory=/opt/polymarket-put
ExecStart=/usr/bin/python3 analysis/polymarket_put.py --proizvodnye
ExecStartPost=/usr/bin/python3 analysis/polymarket_put.py --push --vetka claude/stroiteli
TimeoutStartSec=1800
```

Таймер — `OnCalendar=*-*-* 03:23:00 UTC`, `Persistent=true`.
**03:23Z выбран нарочно:** не 02:17Z (там суточный пуш трендингов на
lab-miami) и не 07:17Z (тяжёлые проходы архива). Минута не круглая по той же
причине, по какой у трендингов 02:17.

`/etc/systemd/system/polymarket-rotacija.service`

```ini
[Unit]
Description=Polymarket: chistka syrogo starshe 30 sutok (tolko posle artefakta)

[Service]
Type=oneshot
User=polymarket
WorkingDirectory=/opt/polymarket-put
ExecStart=/usr/bin/python3 analysis/polymarket_put.py --rotacija --dnej 30
TimeoutStartSec=600
```

Таймер — `OnCalendar=*-*-* 05:11:00 UTC`, `Persistent=true`. **После** прогона
артефакта, не до.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-min.timer polymarket-5min.timer \
     polymarket-proizvodnye.timer polymarket-rotacija.timer
```

**Таймер пуша внутри `polymarket-proizvodnye` требует токена записи на
машине.** Это новый секрет на хосте, а слово владельца — «ключи и секреты —
никогда». Поэтому, как и с трендингами, два пути, и выбирает владелец:

* **убрать строку `ExecStartPost` и везти пуш отдельным прогоном на бегунке**
  (токен выдаёт сам GitHub на время прогона и на диске не остаётся) — так
  решили с трендингами, и я предлагаю так же;
* оставить `ExecStartPost` и положить токен — только по прямому слову.

Скрипт `--push` сам отказывается работать, если checkout стоит не на
`claude/stroiteli`, и кладёт в коммит только производные и отметки.

## 4. Суточный артефакт Actions (это пишет Code-1)

Образец — суточный пуш трендингов на ветке по умолчанию
(`.github/workflows/run_podbivka_trending_push_miami.yml`): прогон идёт **на
бегунке**, токен выдаёт сам GitHub на время прогона, на диске он не остаётся.
Здесь то же, только вместо пуша — загрузка артефакта.

**Порядок шагов важен, и менять его нельзя:**

1. упаковать сырое за сутки;
2. **загрузить артефакт** (`actions/upload-artifact@v4`,
   `retention-days: 90`);
3. **только если шаг 2 прошёл** — поставить отметку
   `--otmetit-artefakt <сутки> --artefakt <имя или id>`;
4. чистка (`--rotacija`) — отдельным таймером, позже.

Скрипт удаляет сырое **только там, где отметка есть**. Без отметки он не
удаляет ничего и называет сутки вслух («ЖДУТ АРТЕФАКТА, НЕ УДАЛЯЮ»). Пустая
отметка (без имени артефакта) — отказ по имени: иначе она открыла бы удаление,
ничего не подтвердив.

Набросок прогона (Code-1 доведёт под свои соглашения):

```yaml
name: "Polymarket: sutochnyj artefakt syrogo s lab-ams"
on:
  schedule:
    - cron: "41 4 * * *"      # posle proizvodnyh (03:23Z), do rotacii (05:11Z)
  workflow_dispatch:
jobs:
  artefakt:
    runs-on: [self-hosted, lab-ams]
    timeout-minutes: 90
    steps:
      - name: "Upakovat syroe za vcherashnie sutki"
        id: pack
        run: |
          set -e
          D=$(date -u -d yesterday +%F)
          cd /opt/polymarket-put
          tar -czf "$RUNNER_TEMP/polymarket-syroe-$D.tar.gz" \
            --exclude 'data/polymarket/proizvodnye' \
            --exclude 'data/polymarket/artefakty' \
            $(find data/polymarket -maxdepth 2 -type d -name "$D")
          echo "sutki=$D" >> "$GITHUB_OUTPUT"
          ls -la "$RUNNER_TEMP/polymarket-syroe-$D.tar.gz"

      - uses: actions/upload-artifact@v4
        id: up
        with:
          name: polymarket-syroe-${{ steps.pack.outputs.sutki }}
          path: ${{ runner.temp }}/polymarket-syroe-${{ steps.pack.outputs.sutki }}.tar.gz
          retention-days: 90
          if-no-files-found: error

      - name: "Otmetka -- TOLKO posle udachnoj zagruzki"
        run: |
          set -e
          cd /opt/polymarket-put
          python3 analysis/polymarket_put.py \
            --otmetit-artefakt "${{ steps.pack.outputs.sutki }}" \
            --artefakt "${{ steps.up.outputs.artifact-id }}"
```

### Что владельцу надо решить ДО установки: объём артефактов

Считаю, а не умалчиваю. При 100 рынках сырого выходит около **340 МБ в сутки**
(расчёт — `docs/polymarket_put.md`, п. 6), сжимать второй раз почти нечего:
тела уже в gzip. Значит:

**340 МБ × 90 суток retention ≈ 30 ГБ хранилища артефактов, которое держится
постоянно.**

Это много, и упереться можно в квоту аккаунта раньше, чем в 90 суток. Три
выхода, по убыванию цены:

1. **уменьшить retention** — 30 суток дают ≈ 10 ГБ, и это ровно столько же,
   сколько живёт сырое на ящике, то есть артефакт перестаёт быть вторым
   рубежом;
2. **класть в артефакт только `clob_books`** — это основная масса (203 из 340
   МБ), остальное (цены, спреды, сделки, события) и так есть в производных;
3. **класть всё, но резать сутки на четыре части по 6 часов** — не уменьшает
   объём, но снимает риск упасть на одном большом файле.

**Пока владелец не сказал — ставить retention 90, как велено, и смотреть на
квоту после первых суток.** Если квота не вытянет, загрузка упадёт, отметка не
встанет, и сырое **не удалится** — порядок шагов выше именно для этого.

## 5. Первый доклад (запускать на lab-ams)

```bash
cd /opt/polymarket-put

# 0. Samoproverka -- snachala ona
python3 analysis/polymarket_put.py --self-test

# 1. Kto otvechaet i s kakim kodom. Nichego ne pishet.
python3 analysis/polymarket_put.py --kazhdye все --probno

# 2. Ocenka objoma -- tem zhe kodom, chto schitaet v boju
python3 analysis/polymarket_put.py --ocenka

# 3. Chas raboty tajmerov, zatem:
python3 analysis/polymarket_put.py --svodka
python3 analysis/polymarket_put.py --proizvodnye --sutki $(date -u +%F)
python3 analysis/polymarket_put.py --rotacija --dnej 30 --probno   # dolzhno byt pusto
```

`--probno` ничего не пишет, только печатает по строке на список:
`список: код N байт B {разбор} <причина>` и `итого: ответили X из 9`.

**Что прислать в докладе** — именно эти числа, их я из контейнера получить не
могу:

1. вывод `--self-test` (ждём 35 из 35);
2. вывод `--probno`: кто ответил и с каким кодом;
3. **глубину книги** из `--svodka` (`clob_books … уровней N (в среднем M на
   книгу)`) — единственное неизвестное в расчёте объёма;
4. **заголовки лимитов** из любого снимка `clob_books`
   (`x-ratelimit-limit`/`-remaining`) — настоящие лимиты вместо пересказа
   официальной страницы;
5. **`base_fee`** из снимка `clob_fee_rate` и объект `feeSchedule` из записи
   рынка в `gamma_events` — настоящая комиссия вместо девяти расходящихся
   гайдов;
6. **`nagrada_forma`** из производных: Gamma кладёт параметры награды то
   плоско, то внутрь `rewards` — скрипт читает обе формы и пишет, какая
   встретилась. Из доклада станет ясно, какая настоящая;
7. фактический размер `data/polymarket` за час — сверить с расчётом.

## 6. Из контейнера Code-3 докладывать нечего

Сетевая политика облачного окружения закрывает все хосты Polymarket: DNS не
разрешается, прокси отвечает `403 Forbidden` на CONNECT (`connect_rejected`).
Проверено, не предположено — замеры в `docs/polymarket_put.md`, п. 7. Поэтому
`--probno` из контейнера даёт «итого: ответили 0 из 9», и это отказ окружения,
а не скрипта.

## 7. Чего в этой установке нет нарочно

* **Вебсокета.** Канал `market` отдал бы книгу один раз и дальше только
  изменения — объём упал бы в разы. Но это долгоживущий процесс
  (`Type=simple`, перезапуск, ответ `pong` на `ping` каждые 5 с), а не
  oneshot-таймер. Предлагаю следующим шагом, когда опрос покажет настоящую
  глубину и активность.
* **Ничего торгового.** Ни ордеров, ни подписи, ни ключей, ни баланса.
* **Удаления без подтверждения.** Ротация не удаляет ничего, пока отметка об
  артефакте не стоит, — и это не настройка, а поведение по умолчанию.
