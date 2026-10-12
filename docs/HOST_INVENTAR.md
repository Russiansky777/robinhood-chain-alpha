# HOST_INVENTAR — что крутится на хостах, где файлы, как читать журналы, все грабли

Документ для человека, который видит систему ПЕРВЫЙ РАЗ и кода не знает. Каждый
путь, каждое число и каждая ссылка на код взяты чтением ветки
`claude/nifty-sagan-r0polg` (`.github/workflows/*.yml`, `deploy/`, `analysis/`).
Ссылки кликабельны в виде `analysis/file.py:123`.

**Про секреты.** Ни одного значения ключа, приватного ключа, сида или токена в
этом документе нет и быть не должно. Переменные окружения и секреты названы
ТОЛЬКО ИМЕНАМИ и назначением. Значения НЕсекретных настроек приводятся.
Публичные адреса кошельков приводятся — они публичны по определению.

**Состояние проекта на 12.10.2026: консервируется.** Три известных дефекта
(раздел 7) не чинятся по слову владельца. Денежный путь не правится.

---

## Оглавление

1. [КАРТА МАШИН](#1-карта-машин)
2. [ЧТО КРУТИТСЯ: службы и таймеры systemd](#2-что-крутится-службы-и-таймеры-systemd)
3. [ГДЕ ФАЙЛЫ](#3-где-файлы)
4. [СТРУКТУРА КАТАЛОГОВ НА ХОСТЕ](#4-структура-каталогов-на-хосте)
5. [КАК ЧИТАТЬ ЖУРНАЛЫ](#5-как-читать-журналы)
6. [ГРАБЛИ](#6-грабли)
7. [ИЗВЕСТНЫЕ НЕЗАКРЫТЫЕ ДЕФЕКТЫ](#7-известные-незакрытые-дефекты)

---

## 1. КАРТА МАШИН

| машина | адрес | роль | ключ доступа (ИМЯ секрета) |
|---|---|---|---|
| **торговый хост NL** | `45.59.170.76` | ВСЯ торговля: детектор, продавец, сторожа, зонды, хостовые таймеры, раннер GitHub | `VPS_SSH_KEY` (имя; значение не приводится) |
| **lab-miami** | `169.128.191.158` | лабораторная, НЕ торговая. Сюда ставятся замеры и регистраторы, чтобы не отнимать ресурс и лимиты у торговли | `VPS_SSH_KEY_MIAMI` |
| **lab-ams** | `216.73.157.61` | лабораторная. Европейская точка — единственная, где отвечает ключ 0slot (грабля 8) | `VPS_SSH_KEY_AMS` (проверить имя на месте) |

### 1.1. Торговый хост NL — общие константы

Одинаковы во всех прогонах, напр. блок `env` в
`.github/workflows/run_backup_hosta_polnyy.yml:70-81`, где пять каталогов стоят
на `:76-80` (`CODE_DIR`, `STATE_DIR`, `DATA_DIR`, `ENV_DIR`, `TAJM_DIR`; `:55-61`
— это ВХОДЫ прогона, а не константы):

| имя | значение |
|---|---|
| `CODE_DIR` | `/home/bot/bloom_executor` |
| `STATE_DIR` | `/home/bot/bloom_executor_live_data` |
| `ENV_DIR` | `/etc/bloom-executor` |
| `DATA_DIR` | `/home/bot/data` |
| `TAJM_DIR` | `/home/bot/host_tajmery` |
| питон служб | `/home/bot/robinhood-chain-alpha/venv/bin/python` — **единственный** venv на все службы и все таймеры (`deploy/hostovye_tajmery.py:75-77`) |
| раннер GitHub | метки `[self-hosted, nl-host]` |
| часовой пояс хоста | `Europe/Amsterdam` (+0200) — врезка `deploy/hostovye_tajmery.py:81-83` |
| сутки учёта | `Europe/Madrid` — `DAY_TZ` в `analysis/bloom_exec_state.py:48`; граница `22:00Z` летом |
| пользователь служб | `bot:bot` |

**Полного клона ветки на хосте НЕТ** (`deploy/hostovye_tajmery.py:23-29`): пушить
хост не может, последняя миля до ветки — прогон `run_vps_health_nl.yml`.

### 1.2. Чего на торговом хосте быть не должно

Регистраторы, замеры и долгие опросы. Они ставятся на lab-машины. Пример:
`lab-potolok-diska.service/.timer` ставится ТОЛЬКО на lab
(`run_lab_bootstrap_nl.yml:709-722`, `run_lab_uborka_nl.yml:211-224`), на торговом
хосте его нет.

На хосте висит наследие старого стенда — `p5-live-bot.service`
(`run_vps_bootstrap_nl.yml:140`). К bloom-хозяйству отношения не имеет, трогать
не надо.

---

## 2. ЧТО КРУТИТСЯ: службы и таймеры systemd

### 2.1. Постоянные службы (`Restart=always`)

| юнит | ExecStart | польз. | ставит прогон |
|---|---|---|---|
| `bloom-detector.service` | `venv/bin/python -u /home/bot/bloom_executor/bloom_detector.py --serve --config /home/bot/bloom_executor/konfig_snapshot.json` | `bot:bot` | `run_vps_deploy_bloom_detector_nl.yml:1110-1157` |
| `bloom-seller.service` | `venv/bin/python -u /home/bot/bloom_executor/bloom_seller.py` | `bot:bot` | `run_vps_deploy_bloom_seller_nl.yml:627-661` |
| `dbot-sold-guard.service` | `venv/bin/python -u /home/bot/dbot_sold_guard/dbot_sold_position_guard.py` | `bot:bot` | `run_vps_deploy_sold_position_guard_nl.yml:154-182` |
| `dbot-detect-probe.service` | `venv/bin/python -u /home/bot/dbot_detect_probe/solana_detect_probe.py` | `bot:bot` | `run_vps_deploy_detect_probe_nl.yml:115-138` |
| `grpc-feed-probe.service` | `venv/bin/python /home/bot/feed_probe/grpc_feed_probe.py --state-dir /home/bot/grpc_feed_probe_data --config .../konfig.json --boosters … --tasks …` | `bot:bot` | `run_vps_deploy_feed_probe_nl.yml:285-320` |
| `shred-listener.service` | `venv/bin/python /home/bot/shred_probe/shred_listener.py $WS_ARG` | `bot:bot` | `run_vps_deploy_shred_listener_nl.yml:245-281` |

Все шесть: `Type=simple`, `After/Wants=network-online.target`,
`WantedBy=multi-user.target`, `StandardOutput/StandardError=journal`,
`EnvironmentFile=/etc/bloom-executor/env` (кроме сторожа продаж и зонда
обнаружения — у них свои каталоги env).

**Ужесточения (systemd sandbox).** У детектора и продавца:
`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
`ProtectHome=read-only`, `ReadWritePaths=/home/bot/bloom_executor_live_data`,
`ProtectKernelTunables`, `ProtectControlGroups`, `RestrictSUIDSGID`,
`LockPersonality`, `TasksMax=64`, `RestartSec=5`. Память: детектор
`MemoryMax=1G`, продавец `MemoryMax=512M`.

У сторожа продаж — только `NoNewPrivileges=true`, плюс
`StartLimitIntervalSec=600`, `StartLimitBurst=5`, `RestartSec=10`,
`EnvironmentFile=/etc/dbot-guard/env`.

У слушателя шредов — те же Protect\*, плюс плата за то, что он не мешает
торговле: `Nice=15`, `CPUWeight=20`, `IOWeight=20`, `CPUQuota=35%`,
`MemoryMax=400M`, `ReadWritePaths=/home/bot/shred_probe_data`.

Именно этот список шести юнитов считает «нашим» бэкап хоста
(`run_arhiv_hosta.yml:62`) и сторож живучести
(`deploy/zhivuchest_na_hoste.py:38-39`).

### 2.2. Таймеры ХОСТА — пять пар, установщик `deploy/hostovye_tajmery.py`

Шаблон `.service` — `текст_службы()` (`deploy/hostovye_tajmery.py:224-266`):
`Type=oneshot`, `User=bot`, `Group=bot`,
`EnvironmentFile=/etc/bloom-executor/env`, `SyslogIdentifier=<имя>`,
`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
`ProtectHome=read-only`, **`ReadWritePaths=/home/bot/host_tajmery`** (писать —
только в свой угол; каталог состояния боевых служб таймерам ЗАКРЫТ),
`ProtectKernelTunables`, `ProtectControlGroups`, `RestrictSUIDSGID`,
`LockPersonality`, `TasksMax=64`.

Шаблон `.timer` — `текст_таймера()` (там же:269-296): `AccuracySec=30s`,
**`Persistent=true`** (догон после перезагрузки), `Unit=<имя>.service`,
`WantedBy=timers.target`, и **пояс назван прямо в строке**:
`OnCalendar=<когда> UTC`.

| юнит (пара .service/.timer) | OnCalendar | что запускает | предел времени / памяти |
|---|---|---|---|
| `bloom-uchet-chas` | `*:23 UTC` | `python -u /home/bot/host_tajmery/uchet/analysis/solana_ledger_run.py`, рабочий каталог `…/uchet` | `TimeoutStartSec=3300` (55 мин, больше бюджета конвейера 50 мин), `MemoryMax=1G` |
| `bloom-zhivuchest-chas` | `*:38 UTC` | `python -u /home/bot/host_tajmery/zhivuchest_na_hoste.py --out /home/bot/host_tajmery/zhivuchest/zhivuchest.json` | `120` c, `256M` |
| `bloom-vygruzka-sutki` | `*-*-* 06:00:00 UTC` | `/bin/sh /home/bot/host_tajmery/vygruzka_sutki.sh` | `1800` c, `512M` |
| `bloom-uborshchik-chas` | `*:53 UTC` | `python -u …/uchet/analysis/close_empty_token_accounts.py --out …/close_accounts_plan.json --result …/close_accounts_result.json --zhurnal …/uborshchik_sutki.json --wallet polosa --po-mintam --min-age-s 0 --max-batches 6` | `600` c, `512M` |
| `bloom-uborshchik-itog-sutki` | `*-*-* 21:55:00 UTC` | `python -u …/uchet/analysis/close_empty_token_accounts.py --zhurnal …/uborshchik_sutki.json --tg-itog segodnja` | `120` c, `256M` |

Определения работ — `deploy/hostovye_tajmery.py:103-221`.

Три вещи, которые НЕ случайны:

* **минуты разные нарочно** (`deploy/hostovye_tajmery.py:101-102`): учёт держит
  сеть десятки минут, остальным нельзя попадать на него;
* **`21:55Z`** — за пять минут до границы суток полосы `22:00Z` (там же:154-156);
* у уборщика строка `Environment=CLOSE_ACCOUNTS_LIVE=1` стоит **ПОСЛЕ**
  `EnvironmentFile` — нарочно: явная строка юнита сильнее файла.

**Кто ставит таймеры.** Шаг «Хостовые таймеры» деплоя детектора:
`run_vps_deploy_bloom_detector_nl.yml:1306-1396` — создаёт каталоги, доставляет
модули со сверкой sha256, зовёт `--self-test`, затем
`hostovye_tajmery.py --ustanovit --vkljuchit` (`:1371`) и печатает
`systemctl list-timers`. Наличие таймеров проверяет
`run_vps_health_nl.yml:290-314` (имена вытаскивает регуляркой прямо из
установщика).

### 2.3. Таймеры строк владельцу — две пары

| юнит | OnCalendar | ExecStart | особенности |
|---|---|---|---|
| `bloom-stroka-balansov` | `*:14,44` (вход `minuty_balansov`, по умолчанию `14,44`) | `venv/bin/python -u /home/bot/bloom_executor/bloom_host_stroki.py --rezhim polchasa --poslat` | `Persistent=false` — опоздавшая строка о балансах хуже отсутствующей; `AccuracySec=10s` |
| `bloom-samoproverka-chas` | `*:10` (вход `minuta_samoproverki`) | `… --rezhim chas --poslat` | `Persistent=false`; единственный, кому дано право записи в каталог состояния: `ReadWritePaths=-$STATE_DIR/samoproverka_hosta.json` |

Оба: `Type=oneshot`, `User/Group=bot`,
**`SupplementaryGroups=systemd-journal`** (без этого `journalctl` чужого юнита
отдаёт пустоту и код 1), `Environment=PYTHONPATH=$CODE_DIR`,
`MemoryMax=256M`, `TasksMax=32`, остальные Protect\* как у боевых.

Ставит `run_vps_stroki_tajmery_nl.yml:113-186`, включает `--now` шаг `:213-222`.
Пояс в `OnCalendar` здесь не назван — но обе строки минутные (`*:MM`), поэтому
зона хоста на них не влияет.

### 2.4. Прочее на хосте

| юнит / файл | что | кто ставит |
|---|---|---|
| `/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf` | сдвиг автообновлений прочь от торгового окна | `run_vps_autoupdate_guard_nl.yml:104`, тело в `deploy/systemd/apt-daily-upgrade-override.conf` |
| `/etc/logrotate.d/bloom-seller` | `/home/bot/bloom_executor_live_data/*.jsonl`: `daily`, `rotate 14`, `compress`, `copytruncate` | `run_vps_deploy_bloom_seller_nl.yml:663-674` |
| `/etc/logrotate.d/dbot-sold-guard` | `/home/bot/dbot_sold_position_guard_live_data/*.jsonl`: `size 100M`, `rotate 5`, `delaycompress`, `su bot bot` | `run_vps_deploy_sold_position_guard_nl.yml:184-198` |
| `/etc/logrotate.d/dbot-detect-probe` | `/home/bot/dbot_detect_probe/*.jsonl`: `size 200M` | `run_vps_deploy_detect_probe_nl.yml:140-145` |
| `/etc/needrestart/conf.d/90-bloom-no-restart.conf` | needrestart не трогает наши службы | `deploy/needrestart/90-bloom-no-restart.conf` |
| `actions.runner.*.service` | раннер GitHub `[self-hosted, nl-host]` | `run_lab_bootstrap_nl.yml` (снимает его там же:337-345) |
| `p5-live-bot.service` | **наследие** старого стенда, к bloom-хозяйству отношения не имеет | `run_vps_bootstrap_nl.yml:140` |
| `triton-zond-*` | разовые `systemd-run --unit=…`, не постоянные юниты | `run_triton_zond_24h_nl.yml:174` |

### 2.5. CRONTAB хоста — хозяйство СТАРОГО стенда, к bloom отношения не имеет

**Главное сразу: ни одна служба и ни один таймер bloom через cron не ходят.** Всё
bloom-хозяйство — systemd (разделы 2.1–2.3): пять пар хостовых таймеров ставит
`deploy/hostovye_tajmery.py`, две пары строк владельцу —
`run_vps_stroki_tajmery_nl.yml`. Хостовой cron для bloom **заменён таймерами
systemd**, и в документе про bloom он больше не упоминается нигде. Но cron на
хосте ЖИВ — остался от стенда P5/P6/фандинга и от регистраторов, — и его строки
надо знать: они жгут тот же процессор, тот же диск и тот же узел, что торговля.

Crontab здесь — ВСЕГДА crontab пользователя `bot`
(`/var/spool/cron/crontabs/bot`). В `/etc/cron.d` не пишет ни один прогон.

| строка cron | что | ставит | убирает |
|---|---|---|---|
| `6 * * * * /home/bot/bin/p5_hourly_snapshot.sh` | часовой снимок P5 | `run_vps_setup_hourly_cron_nl.yml:101-112` | `run_vps_stop_p5_cron_nl.yml:39-58` — **убрана** |
| `21 * * * * /home/bot/bin/funding_hourly_snapshot.sh` | часовой логгер спреда фандинга | `run_vps_setup_funding_cron_nl.yml:117` | `run_vps_remove_funding_cron_nl.yml:36-61` — **убрана** |
| `36 * * * * /home/bot/bin/p6_hourly_snapshot.sh` | часовой снимок P6 | `run_vps_setup_p6_cron_nl.yml:107` | `run_vps_remove_p6_cron_nl.yml:35-60` — **убрана** |
| `51 9 * * * /home/bot/bin/turnover_watch_daily.sh` | суточный дозор оборота/субсидии | `run_vps_setup_turnover_watch_cron_nl.yml:118` | прогона на снятие НЕТ |
| `*/1 * * * * /home/bot/bin/sleeping_refs_live_divergence_minute.sh` | **минутный** замер расхождения | `run_vps_setup_live_divergence_cron_nl.yml:102` | прогона на снятие НЕТ |
| `3,18,33,48 * * * * …/trend_collector_snapshot_15min.sh` | регистратор трендов, раз в 15 мин | `run_vps_deploy_two_collectors.yml:188` | прогона на снятие НЕТ |
| `10 3 * * * …/trend_collector_select_pools_daily.sh` | суточный отбор пулов | там же `:189` | — |
| `2-59/5 * * * * …/predmarket_collector_snapshot_5min.sh` | предмаркет, раз в 5 мин | там же `:190` | — |
| `20 3 * * * …/predmarket_collector_select_pairs_daily.sh` | суточный отбор пар | там же `:191` | — |

Три вещи, на которых тут горят.

* **Четыре строки регистраторов стоят на ТОРГОВОМ хосте**, а по правилу 1.2 им
  место на lab: их поставил `run_vps_deploy_two_collectors.yml:184-194`
  (`root@45.59.170.76`), и прогона, который бы их снял, в ветке нет.
* **`run_vps_setup_hourly_cron_nl.yml` ЗАМЕНЯЕТ crontab целиком, а не дописывает**
  (`:106` — `printf … > /tmp/bot_cron.txt`, затем `crontab` этим файлом на `:108`).
  Повторный его запуск СНЕСЁТ все остальные строки. Все прогоны, написанные
  позже, читают текущий crontab и дописывают (`setup_funding:107-120`,
  `setup_turnover_watch:108-121`, `setup_live_divergence:92-105`,
  `two_collectors:188-192`).
* **Прогоны-дописыватели держат «якорь» и без него отказываются работать.**
  `setup_funding` ищет строку P5 `:06` (`:111`), `setup_p6` и
  `setup_turnover_watch` — строку funding `:21` (`:101`, `:112`),
  `setup_live_divergence` — строку turnover_watch `:51` (`:95-96`). Строки `:06`,
  `:21` и `:36` УБРАНЫ, значит первые три прогона теперь остановятся со «СТОП:
  строка … НЕ найдена» — и это правильное поведение, а не поломка.

**Как посмотреть** (всё только чтение):

```bash
crontab -l                          # root
crontab -u bot -l                   # он же: sudo -u bot crontab -l
cat /var/spool/cron/crontabs/bot    # сырой файл -- видно и то, что crontab -l прячет
ls -l /etc/cron.d/                  # ожидается пусто от нас
systemctl status cron --no-pager    # жива ли сама служба cron
```

Готовое чтение прогоном, без правок: `run_vps_diag_cron_nl.yml:31-58` (время
хоста, статус службы, crontab `bot`, `CRON` из syslog, лог обёртки, права
скрипта). В бэкап хоста crontab попадает обоими:
`run_backup_hosta_polnyy.yml:197-200` → `tmp/backup_meta/crontab-root.txt`,
`crontab-bot.txt`, `cron.d.txt`.

---

## 3. ГДЕ ФАЙЛЫ

### 3.1. Главная таблица: журналы и признаки жизни

Пути заводятся в ОДНОМ месте: `analysis/bloom_exec_state.py:529-600`
(`ExecState.__init__`), база — `state_dir()` (там же:187-189), читает
`BLOOM_STATE_DIR`. Запись `.jsonl` — только дописыванием с fsync,
`append_jsonl_fsync()` (там же:243); запись `.json` — целиком или никак,
`atomic_write_json()` (там же:228-240).

Все пути ниже — внутри `/home/bot/bloom_executor_live_data`, если не сказано иное.

| путь | кто пишет | формат | что в строке |
|---|---|---|---|
| **`decisions.jsonl`** — ЖУРНАЛ РЕШЕНИЙ | `ExecState.log_decision` — `analysis/bloom_exec_state.py:1730-1734`; зовут `analysis/bloom_detector.py:4052` (главная воронка), `:3943`, `:4112`, `:4124`, `:4151`, `:6432`, `:7139`; `analysis/bloom_executor.py:229`, `:244` | jsonl (+ `.1.gz`…) | см. 3.2 |
| **`positions.jsonl`** — ЖУРНАЛ ПОЗИЦИЙ | `write_intent` — `analysis/bloom_exec_state.py:1567-1608`; `update_position` — там же:1628-1644 | jsonl (+ `.1.gz`…), строки **слитые по `client_order_id`** | см. 3.3 |
| **`seller.jsonl`** — ЖУРНАЛ СТОРОЖА ПРОДАЖ | `SellGuard.log` — `analysis/bloom_seller.py:1622-1625` | jsonl (+ `.1.gz`…) | `ts_utc` + строка круга (`handle`, `:2377-2407`): `client_order_id`, `mint`, `state_before`, `action`, `why_not`, `failures`, `balance_raw`, `balance_ui`, `zero_streak`, `attempt`, `jupiter`, `outcome`, `signature`, `telegram`. Служебные виды по полю `stage`: `doklad_sell_ne_ushel`, `doklad_buy_pered_sell`, `sverka_po_cepi`, `itog_po_cepi_ne_zapisan` |
| `trevogi.jsonl` | `log_trevoga` — `analysis/bloom_exec_state.py:1736-1747`; зовёт `analysis/bloom_detector.py:3812` | jsonl | `schema_version`, `ts_utc`, `chto`, `kluch` (напр. `gruppy:net-politik`, `tishina:nol-pokupok`), **`poslano`** (факт отправки, не намерение), `why_not` |
| `zapusk_zhurnal.jsonl` | `analysis/c1_zapusk_zhurnal.py:463-474`, строка от `zapis_iz_uvedomlenija` (`:543-582`) | jsonl | `ts_utc`, `sozdanie` (`create`/`create_v2`), `signature`, `signature_why_not`, `kupil_v_sozdanii`, `sol_sozdatelya`, `gotov_cherez_ms`, `nash_slot_seti`, `net_slot_age_s`, `nash_slot_gotovnosti`, `otryv_slotov`, `dlina_slota_s`, `otryv_why_not`, `bajt`, `pokupok_net` |
| `nashi_sluzhebnye_podpisi.jsonl` | `zapisat_sluzhebnuju_podpis` — `analysis/bloom_exec_state.py:1610-1625` | jsonl | `schema_version`, `ts_utc`, `signature`, `kto`, `pochemu` |
| `api_calls.jsonl` | `analysis/bloom_exec_state.py:597` | jsonl | вызовы API |
| **`detector_status.json`** — ПРИЗНАК ЖИЗНИ ДЕТЕКТОРА | `признак_жизни()` → `atomic_write_json(статус_путь())` — `analysis/bloom_detector.py:3374-3418`, запись `:3709`, путь `:2368-2369`. **Раз в пульс, 60 с** | json | см. 3.4 |
| **`seller_heartbeat.json`** — ПРИЗНАК ЖИЗНИ ПРОДАВЦА | `SellGuard.heartbeat` — `analysis/bloom_seller.py:5088-5140`. **Каждый круг**, `BLOOM_SELLER_LOOP_S`, по умолчанию `DEFAULT_LOOP_EVERY_S = 15.0` (`:207`) | json | `schema_version`, `updated_utc`, `updated_ts`, `mode` (`live`/`dry-run`), `positions_in_cycle`, `pozicii_why_not`, `porjadok_why_not`, `pozicij_upalo`, `upavshie_why_not`, **`last_cycle[]`** = `{client_order_id, mint, action, why_not, balance_raw, attempt}`, `vtoroe_mnenie` |
| `net_slot.json` | `записать_часы_слотов` — `analysis/bloom_detector.py:2374`, сама запись `:2396-2401`. На пути уведомления, **каждые 0.25 с** (`СЛОТ_ФАЙЛ_КАЖДЫЕ_S = 0.25`, `:199`), а не раз в 60 с | json | В ФАЙЛЕ ровно четыре ключа: `net_slot`, `ts`, `age_s`, `notifications` (`:2397-2400`). Полей `slot` и `why_not` в файле НЕТ — их строит ЧИТАТЕЛЬ, и он в ДРУГОМ модуле: `слот_сети_из_файла` (`analysis/bloom_seller.py:780`) отдаёт `{slot, age_s, why_not, откуда}` (`:788`), возраст берёт НАИБОЛЬШИМ из `ts` и `mtime`, а строку «в файле часов слотов нет `net_slot`» ставит `:799-801` |
| `samoproverka_hosta.json` | `analysis/bloom_host_stroki.py:220` в режиме `chas` — единственный разрешённый ему на запись путь | json | вердикт часовой самопроверки хоста |
| **`pnl_<ГГГГ-ММ-ДД>.json`** — PNL/УЧЁТ | `add_pnl` — `analysis/bloom_exec_state.py:1843-1850`, путь `pnl_path()` `:1827-1829`. **Дата по `Europe/Madrid`** | json | `realized_sol`, `spent_sol`, `buys`, `sells`, `tz_unavailable`; при битом файле `corrupt` |
| `counters.json` | `save_counters`, `note_api_result` `:1800-1806`, `note_sell_outcome` `:1808-1823` | json | `api_error_streak`, `api_error_last_code`, `api_error_last_ts`, `unsold_streak`, `unsold_streak_<lane>` |
| `stopy_grupp.json` | защёлка суточных стопов групп, `analysis/bloom_exec_state.py:581` (зачем — врезка `:572-580`) | json | `{группа: {…, "сутки": …}}` |
| `avarii_grupp.json` | аварии групп (Правило 16), `analysis/bloom_exec_state.py:586` | json | снимается ТОЛЬКО словом владельца, часов нет |
| `bloom_budget.json`, `dedup.sqlite` | `analysis/bloom_exec_state.py:599-600` | json / sqlite | недельный бюджет; дедуп подписей |
| `own_sell_sim_count.json`, `own_sell_obkatany.json` | `analysis/bloom_seller.py:950`, `:999` | json | счёт симуляций своей продажи; обкатанные типы пулов |
| `helius_usage/<служба>.json` | `CreditMeter` — `analysis/bloom_detector.py:1487-1489`, `analysis/bloom_seller.py:328-330` | json | расход кредитов по службам: `bloom_detector.json`, `bloom_seller.json` |
| `KILL_BY_TELEGRAM`, `KILL_SELL_BY_TELEGRAM`, `KILL_OWN_SEND` | рубильники, которые служба пишет **сама** (от `bot`): `analysis/bloom_exec_state.py:538`, `:550`, `:554` | файл-метка | наличие файла = запрет |

### 3.2. Что в строке `decisions.jsonl`

Добавляются ВСЕГДА: `schema_version`, `ts_utc`.

Тело решения (`решение()` — `analysis/bloom_detector.py:1314-1321`):
`signature`, `source`, `mint`, `slot`, `kind`, `spend_sol_eq`, `spend_mint`,
`spend_ui`, `first_entry`, `dex_programs`, `token_program`, `route`.
По месту (`:1333-1339`): `source_pool`, `source_pool_candidates`,
`pool_why_not`, `quote_mints`, `flags`, `rent_new_accounts_sol`,
`native_delta_sol`, `received_ui`.

Дальше: `slot_lag` (`:1344`), `action` (`buy`/`skip`), `code`, `reason`,
`filter` (`:1346`, `:1379`), `dokupka`, `dokupka_why` (`:1364-1365`),
`route_transfer_fee_bps`, `token_fee_bps`, `route_tax_why_not` (`:1402-1418`).

Довешивает детектор (`:4012-4038`): `test_source`, `parse_ms`, `gate_ms`,
`via`, `parsed_from`, `tx_version`, `t_recv_ts`, `t_recv_utc`, `t_decide_ts`,
`decide_latency_ms`, `source_slot`, `net_slot_at_decision`, `net_slot_age_s`,
`rate_note`, `rate_age_s`, `rate_state`, `rate_source`, **`source_task`**,
`mode`.

Отдельные ВИДЫ строк — по полю `stage`: `exec_result` (+`exec_code`),
`mint_tax` (+`taxed`, `tax_bps`, `token_program`),
`bloom_skipped_by_group` (+**`group`**, `why`), `lane_pair`,
`lane_control_summary`, `lane_control_pair`, `leg_pool_skip`, `ws_hour`,
`ws_return`, `skip_price`.

### 3.3. Что в строке `positions.jsonl`

**Позиция — это НЕ одна строка.** Файл дописывается: сначала намерение, потом
обновления по `client_order_id`. Состояние позиции = слияние всех её строк по
порядку (`ExecState.positions()`).

Намерение (`write_intent`, `analysis/bloom_exec_state.py:1567-1608`):
`schema_version`, `client_order_id`, `state="intent"`, `mint`, `source_sig`,
`source_slot`, `sol_in`, `pool`, `program`, `taxed`, `tax_bps`, `mode`,
`sell_after_s`, `ts_intent`, `ts_intent_utc`, `wallet`; по месту `source`,
`lane`, `lane_group` (`:1596-1607`).

Обновление (`update_position`): `client_order_id`, `ts_update`,
`ts_update_utc` + поля вызова.

Учёт закрытия (`_учесть_закрытие`, `analysis/bloom_exec_state.py:1646`):
`pnl_counted`, `pnl_counted_sol`, `pnl_counted_spend_sol`, `pnl_count_why_not`,
`uncountable_shared_mint`.

Закрытие (`analysis/bloom_seller.py:2433-2436`, `:2734-2737`): `state`,
`ts_closed`, `closed_reason`, `closed_sol_net`, `zero_streak`.

Продажа: `sell_attempts`, `ts_last_sell_attempt`, `last_sell_signatures`,
`balance_read_failed_at`, `balance_read_why_not`.

**Состояния.** Открытыми считаются `STATES_OPEN = ("intent", "bought",
"selling", "unsold")` — `analysis/bloom_exec_state.py:86`. НЕ открытые:
`closed` (`:167`) и **`spisana`** (`:180`). Про `spisana` обязательно читать
раздел 7, дефект II.

### 3.4. Что в `detector_status.json`

`schema_version`, `updated_utc`, `updated_ts`, `alive_since_utc`, `mode`,
`sources`, `source_groups` (внутри `file` — живой путь файла групп),
`sender_connections`, `bloom_skipped_by_group`, `sources_from`,
`sources_generation`, `subscribed_addresses`, `credits`, `subscribe_method`,
`subscribe_drops`, `signals_seen`, `to_buy`, `by_code`,
`by_code_test_source`, `stale_slots`, `block_position`, `leg_cache`,
`two_step`, `telegram_commands`, `bloom_keepalive`,
`trevogi_zhurnal` (последние 20), `own_send` → `limits`, `lane_state`,
**`lane_state_by_group`**.

### 3.5. `/etc/bloom-executor` — окружение и рубильники root

| путь | что |
|---|---|
| `env` | окружение ВСЕХ служб, права `600 root:root`. **Только ИМЕНА**: `BLOOM_*`, `LANE_*`, `HELIUS_*`, `DBOT_API_KEY`, `OWN_SEND_WALLET`, `OWN_SEND_WALLET_KEY`, `EXECUTOR_WALLET`, `EXEC_WALLET_KEY`, `BLOOM_SOURCE_GROUPS`, `BLOOM_STATE_DIR`, `BLOOM_KILL_FILE`, `BLOOM_KILL_SELL_FILE`, `BLOOM_LANE_NONCE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_LOG_CHAT_ID`, `ZEROX_API`, `BLOCKRAZOR_AUTH_TOKEN`, `NOZOMI_API_KEY`, `ASTRALANE_API_KEY`, `TRITON_X_TOKEN` и др. Полный белый список имён — `run_vps_env_bloom_nl.yml:51` и `run_vps_env_flag_nl.yml:96`. **Значения не приводятся** |
| `env.bak.<ГГГГММДДTччммссZ>` | копия перед КАЖДОЙ правкой, `cp -a` — `run_vps_env_flag_nl.yml:135`; читаются назад до 40 копий (`run_vps_env_bloom_nl.yml:85`) |
| `KILL` | ОБЩИЙ рубильник: глушит ВСЁ, включая полосу. `kill_file()` — `analysis/bloom_exec_state.py:192-194`. Пишет `run_vps_bloom_kill_nl.yml` |
| `KILL_BLOOM` | рубильник ТОЛЬКО площадки Bloom; полоса продолжает торговать. `analysis/bloom_exec_state.py:544-546`; `run_vps_bloom_kill_nl.yml:89-104` |
| `feed_probe_env`, `shred_listener_env` | необязательные добавки к env зондов (`EnvironmentFile=-`) |

Отдельные каталоги env: `/etc/dbot-guard/env` (сторож продаж DBot, права
`600 root:root` — `run_vps_deploy_sold_position_guard_nl.yml:145`),
`/etc/dbot-detect-probe/env`.

### 3.6. `/home/bot/data` — ФАЙЛ ГРУПП и данные, доставленные деплоем

| путь | что |
|---|---|
| **`sources_2026-09-25.json`** | **ФАЙЛ ГРУПП**, живой. Путь приезжает службам через имя `BLOOM_SOURCE_GROUPS`; читается `analysis/bloom_detector.py:14931-14943`. **Деплой детектора его НЕ трогает** (`run_vps_deploy_bloom_detector_nl.yml:457-458`) |
| **`sources_2026-09-25.json.bak-*`** | копии правок файла групп. Уборка «оставить 10 последних» — `run_bak_grupp_ubrat_nl.yml:37`, `:98`, `:132` |
| `senders.json` | реестр отправителей — там **ИМЕНА** секретов, не значения |
| `c2_leg_pools_2*.json`, `c2_pool_samples/` | котировочные пулы и образцы для тени |
| `idl/pump.json`, `idl/pump_amm.json` | IDL pump.fun; **без них врезка кривой v2/v3 отказывает на каждом сигнале** |
| `clmm_konfigi.json`, `dlmm_shag_korziny.json`, `nogi_shablony.json` | конфиги строителей |
| `sender_regions.json`, `leader_regions.json` | точки отправителей и регионы лидеров |
| `solana_buyer_200/prior/current/buyer_100/dex_labels.json` | метки DEX |

Структура файла групп: `generated_utc`, `decision`, и по каждой группе
`groups.<имя>.{lane_size, lane_trades, bloom_trades, bloom_sol, fanout,
min_target_sol, max_slots_from_source, skip_flippers, allow_taxed_route,
slippage, min_pool_sol_reserve, slippage_thin_pool, thin_pool_below_sol,
lane_open_max, hold_slots, lane_pools, pool_senders, pool_nonce, zabor_s0,
note, addresses, lane_sol}`.

Доставка: `run_vps_deploy_bloom_detector_nl.yml:408-459`
(`tar -czf - … | ssh … tar -xzf - -C /home/bot`).

### 3.7. `/home/bot/host_tajmery` — ВЫГРУЗКИ и результаты хостовых таймеров

Каталоги создаёт `run_vps_deploy_bloom_detector_nl.yml:1308`, модули кладёт
`:1322-1332`.

| путь | кто пишет | формат | что внутри |
|---|---|---|---|
| `hostovye_tajmery.py`, `zhivuchest_na_hoste.py` | деплой | py | установщик таймеров и сторож живучести |
| `deploy/checks/sdelki_polosy_vygruzka.py`, `analysis/bloom_own_send.py` | деплой | py | раскладка **зеркало ветки** нарочно: самопроверка выгрузки ищет модуль отправки как `<три каталога вверх>/analysis/bloom_own_send.py` (`deploy/hostovye_tajmery.py:85-93`) |
| `vygruzka_sutki.sh` | установщик, `текст_обёртки()` — `deploy/hostovye_tajmery.py:299-333` | sh, 755 | окно `вчера 06:00Z .. сегодня 06:00Z`, запись через `$OUT.chast` + `mv` |
| **`vygruzki/sdelki_polosy_<ГГГГ-ММ-ДД>.json`** | таймер `bloom-vygruzka-sutki` | json | свод, `deploy/checks/sdelki_polosy_vygruzka.py:1564-1588`: `снято_utc`, `версия` (`pravilo14v`), `с`, `до`, `полные_поля`, `строк_просмотрено`, `сделок`, `отказов`, `отказов_по_допуску`, `отказов_без_подписи`, `строк_с_итогом_по_цепи`, `строк_не_сверено`, `подпись_сводки`, `put_prodazhi_po_sutkam`, `strok_s_vhodom_po_cepi`, `strok_s_vyhodom_po_cepi`, `с_севшей_подписью`, `с_именем_токена`, `с_минтом_и_слотом`, **`ряды[]`** (в ряду: `mint`, `source_slot`, `landed_sig`, `token_name`, `put_prodazhi`, `put_prodazhi_vid`, `otkaz_vid`, `otkaz_po_dopusku`, `otkaz_podpis`, `sverka_ok`, `itog_po_cepi_nativ_sol`, `vhod_sol_po_cepi`, `vyhod_sol_po_cepi`) |
| **`zhivuchest/zhivuchest.json`** | таймер `bloom-zhivuchest-chas`, `собрать()` — `deploy/zhivuchest_na_hoste.py:164-184`, запись атомарная `:187-195` | json | `utc`, `кем`, `чего_не_знает`, `порог_признака_с` (**600**), `службы[]` (имена из `:38-39`), `признаки[]` (четыре пары путь↔служба из `:43-48`), `тревоги[]`, `итог` |
| `uchet/analysis/*.py` | деплой | py | `solana_ledger_run.py`, `close_empty_token_accounts.py`, `bloom_exec_state.py`, `bloom_notify.py`, `bloom_source_groups.py` |
| `uchet/data/*.json` | таймер учёта | json | `ledger_status.json`, `chain_tx_cache.json` (засевается из ветки один раз, `run_vps_deploy_bloom_detector_nl.yml:1349-1355`), `solana_dbot_realized_ledger.json`, `solana_trades_all.json`, `solana_source_stats.json`, `solana_task_stats.json`, `dbot_follow_*_raw.json`. Имена — `analysis/solana_ledger_run.py:124-132`, `REPO_ROOT` там же `:58` (то есть `…/uchet/data/`) |
| `uborshchik_sutki.json` | таймер уборщика | json | сколько ренты вернул уборщик за сутки + признак «строка послана» |
| `close_accounts_plan.json`, `close_accounts_result.json` | таймер уборщика | json | план и результат закрытия пустых токен-счетов |
| `leader_regions.json` | деплой | json | карта регионов лидеров; **без неё выгрузка оставляет `region_lidera` с причиной, а не выдумывает** |

### 3.8. Остальные каталоги служб

| путь | что |
|---|---|
| `/home/bot/dbot_sold_guard/dbot_sold_position_guard.py` | код сторожа продаж DBot |
| `/home/bot/dbot_sold_position_guard_live_data/` | `GUARD_STATE_DIR` (`run_vps_deploy_sold_position_guard_nl.yml:134`): `dbot_sold_position_guard.sqlite3` (состояние), **`dbot_sold_position_guard_audit.jsonl`** — пути в `analysis/dbot_sold_position_guard.py:1034-1036`. Виды записей по полю `event`: `pool_route`, `pool_route_failed`, `rescue`, `rescue_failed`, `position_closed`, `stuck_alert`, `sell_attempt` (`:693`, `:696`, `:741`, `:744`, `:817`, `:837`, `:891`); поля `task`, `wallet`, `mint` |
| `/home/bot/dbot_detect_probe/` | `solana_detect_probe.py`, `status.json` (признак жизни), `*.jsonl` замеров |
| `/home/bot/grpc_feed_probe_data/` | `heartbeat.json` и замеры зонда гонки потоков |
| `/home/bot/shred_probe/`, `/home/bot/shred_probe_data/` | слушатель шредов и его состояние |
| `/home/bot/robinhood-chain-alpha/venv/` | **единственный** venv: им работают все службы и все таймеры |
| `/home/bot/bin/` | вспомогательные скрипты (входит в список бэкапа, `run_backup_hosta_polnyy.yml:367`) |

---

## 4. СТРУКТУРА КАТАЛОГОВ НА ХОСТЕ

```
/home/bot/
├── bloom_executor/                     CODE_DIR, bot:bot 644
│   ├── bloom_detector.py  bloom_seller.py  bloom_executor.py  bloom_exec_state.py
│   ├── bloom_own_send.py  bloom_api.py  bloom_notify.py  bloom_trevogi.py
│   ├── bloom_host_stroki.py  bloom_telegram_cmd.py  bloom_reconcile.py
│   ├── c1_*/c2_*/c3_* (тень, строители, сборка), podbivka_*   (~60 модулей,
│   │                        список: run_vps_deploy_bloom_detector_nl.yml:366)
│   ├── konfig_snapshot.json            снимок задач DBot как откат
│   └── konfig.json                     для зонда гонки потоков
│
├── bloom_executor_live_data/           STATE_DIR, bot:bot. ЕДИНСТВЕННЫЙ путь,
│   │                                   открытый боевым службам на запись
│   ├── decisions.jsonl   positions.jsonl   seller.jsonl   trevogi.jsonl
│   ├── zapusk_zhurnal.jsonl  nashi_sluzhebnye_podpisi.jsonl  api_calls.jsonl
│   ├── *.jsonl.1.gz … *.jsonl.14.gz    logrotate: daily, rotate 14, copytruncate
│   ├── detector_status.json  seller_heartbeat.json  net_slot.json
│   ├── samoproverka_hosta.json         пишет только bloom-samoproverka-chas
│   ├── pnl_<Мадрид-дата>.json  counters.json  bloom_budget.json  dedup.sqlite
│   ├── stopy_grupp.json  avarii_grupp.json
│   ├── own_sell_sim_count.json  own_sell_obkatany.json
│   ├── helius_usage/bloom_detector.json, bloom_seller.json
│   └── KILL_BY_TELEGRAM  KILL_SELL_BY_TELEGRAM  KILL_OWN_SEND
│                                       (их пишет сама служба, от bot)
│
├── data/                               доставляется деплоем, bot:bot
│   ├── sources_2026-09-25.json         ФАЙЛ ГРУПП (деплой его не трогает)
│   ├── sources_2026-09-25.json.bak-*   копии правок, уборка до 10 последних
│   ├── senders.json                    реестр отправителей: ИМЕНА секретов
│   ├── idl/pump.json  idl/pump_amm.json
│   ├── c2_leg_pools_2*.json  c2_pool_samples/
│   ├── clmm_konfigi.json  dlmm_shag_korziny.json  nogi_shablony.json
│   └── sender_regions.json  leader_regions.json
│       solana_buyer_200/prior/current/buyer_100/dex_labels.json
│
├── host_tajmery/                       TAJM_DIR -- ВНЕ каталога состояния
│   │                                   нарочно: чужая запись рядом с
│   │                                   позициями не нужна
│   ├── hostovye_tajmery.py  zhivuchest_na_hoste.py  vygruzka_sutki.sh (755)
│   ├── deploy/checks/sdelki_polosy_vygruzka.py     раскладка = зеркало ветки
│   ├── analysis/bloom_own_send.py                  (иначе самопроверка падает)
│   ├── uchet/analysis/{solana_ledger_run, close_empty_token_accounts,
│   │                   bloom_exec_state, bloom_notify, bloom_source_groups}.py
│   ├── uchet/data/{ledger_status, chain_tx_cache, solana_dbot_realized_ledger,
│   │               solana_trades_all, solana_source_stats, solana_task_stats}.json
│   ├── vygruzki/sdelki_polosy_<дата>.json
│   ├── zhivuchest/zhivuchest.json
│   ├── uborshchik_sutki.json  close_accounts_plan.json  close_accounts_result.json
│   └── leader_regions.json
│
├── dbot_sold_guard/dbot_sold_position_guard.py
├── dbot_sold_position_guard_live_data/ GUARD_STATE_DIR
│   ├── dbot_sold_position_guard.sqlite3
│   ├── dbot_sold_position_guard_audit.jsonl  (logrotate: 100M, rotate 5, su bot bot)
│   └── dbot_sold_position_guard_sample.json
├── dbot_detect_probe/{solana_detect_probe.py, status.json, *.jsonl}
├── grpc_feed_probe_data/heartbeat.json ; feed_probe/grpc_feed_probe.py
├── shred_probe/shred_listener.py ; shred_probe_data/
├── bin/                                вспомогательные скрипты
└── robinhood-chain-alpha/venv/         ОДИН venv на все службы и все таймеры
                                        (клон ветки НЕПОЛНЫЙ: git push хост не умеет)

/etc/bloom-executor/                    root:root, 600 на env
├── env                                 окружение всех служб
│                                       (только ИМЕНА, значения не приводятся)
├── env.bak.<ГГГГММДДTччммссZ>          копия перед каждой правкой (cp -a)
├── KILL                                общий рубильник: глушит ВСЁ, включая полосу
├── KILL_BLOOM                          только площадка Bloom; полоса торгует
└── feed_probe_env, shred_listener_env  необязательные (EnvironmentFile=-)

/etc/dbot-guard/env          сторож продаж DBot (600 root:root)
/etc/dbot-detect-probe/env   зонд обнаружения
/etc/systemd/system/         bloom-*.service/.timer, dbot-*.service,
                             grpc-*.service, shred-*.service,
                             actions.runner.*.service,
                             apt-daily-upgrade.timer.d/override.conf
/etc/logrotate.d/            bloom-seller, dbot-sold-guard, dbot-detect-probe
/etc/needrestart/conf.d/90-bloom-no-restart.conf
```

### 4.1. Чего в каталогах нет и быть не должно

Ключей, приватных ключей, сидов и значений секретов. Бэкап хоста исключает их
ТРЕМЯ слоями (`run_backup_hosta_polnyy.yml:14-25`):

1. `tar --exclude` по именам: `env`, `env.*`, `*.key`, `*.pem`, `id_*`,
   `*wallet*.json`, `*secret*`, `*keypair*`;
2. выгрузка env **только именами** (блок `:202-212`, сама строка `sed` --
   `:210`);
3. проверка готового архива — `deploy/checks/arhiv_bez_klyuchey.py`.

Публичные адреса отбираются в `publichnye_adresa.txt` (`:215-247`, сам отбор --
`:244-245`), и различитель у них **имя и цепь, а НЕ длина**. Так было не всегда:
первый вариант 12.10 брал значение по длине 32..44 знака base58, считая, что
приватный ключ ed25519 (87-88 знаков) в это окно не влезет. **Это неверно:
32-байтный СИД в base58 -- ровно 43-44 знака**, то есть по длине ключ от адреса
не отличается. Теперь: имя обязано содержать `NONCE`/`ADDRESS`/`PUBKEY`/
`RECIPIENT` и НЕ содержать `KEY`/`SECRET`/`PRIV`/`SEED`/`MNEMONIC`/`TOKEN`/
`PASS`, а каждый отобранный адрес сверяется по цепи
(`deploy/checks/adresa_sushchestvujut.py`): счёт существует ЛИБО у адреса есть
история подписей. Сид -- случайная точка: у неё нет ни того, ни другого.

---

## 5. КАК ЧИТАТЬ ЖУРНАЛЫ

Две оговорки, без которых числа врут.

**(1) Ротация.** logrotate стоит `daily rotate 14 compress copytruncate` —
сутки уезжают в `*.jsonl.1.gz`. Читая только текущий файл, теряешь историю (на
этом уже горело: `analysis/bloom_exec_state.py:1241-1256`). Поэтому все команды
ниже — через обёртку `zh`:

```bash
cd /home/bot/bloom_executor_live_data
# один журнал целиком, ротированные СНАЧАЛА (порядок важен для слияния позиций)
zh() { for f in $(ls -1r "$1".*.gz 2>/dev/null); do zcat "$f"; done; cat "$1"; }
```

**ПРЕДУПРЕЖДЕНИЕ: `zh` и служба читают РАЗНОЕ, и числа законно расходятся.**
Обёртка `zh` сливает ВСЕ ротированные `.gz` (при `rotate 14` это до
четырнадцати файлов). Служба так НЕ делает: `_ротированные_позиции`
(`analysis/bloom_exec_state.py:1241`) берёт **ровно ОДИН, самый свежий**
ротированный файл — так и написано в докстринге (`:1251-1255`: «суточное окно им
закрывается целиком, а тянуть всю историю в каждый вызов нельзя»), а выбирает его
сортировкой по `mtime` с `рот[0]` (`:1259-1261`) и держит в кэше по
(путь, `mtime`, размер) (`:1267-1272`). Этот один файл и попадает в `positions()`
(`:1473`), а из неё — в `open_positions` (`:1530`, фильтр по `STATES_OPEN` на
`:1544`), то есть во ВСЕ гейты.

Отсюда прямое следствие для разделов 5.3 и 5.5: **ручной счёт открытых и
списанных по `zh` может НЕ СОЙТИСЬ с тем, что видит гейт** — гейт видит только
текущий файл и `.1.gz`, а всё из `.2.gz` и дальше в его глазах не существует. Расхождение в эту
сторону (ручной счёт больше гейтового) — НЕ поломка, а разная глубина чтения.
Хочешь ровно гейтовое число — считай по текущему файлу плюс ОДИН самый свежий
`.gz`:

```bash
cd /home/bot/bloom_executor_live_data
# ТО ЖЕ окно, что у гейта: один самый свежий ротированный + текущий
zh1() { f=$(ls -1t "$1".*.gz 2>/dev/null | head -1); \
        [ -n "$f" ] && zcat "$f"; cat "$1"; }
```

**(2) Позиция — это НЕ одна строка.** `positions.jsonl` дописывается: намерение,
потом обновления по `client_order_id`. Состояние позиции = слияние всех её строк
по порядку.

### 5.1. Последние решения по группе

```bash
cd /home/bot/bloom_executor_live_data
GRUPPA=cand1
FG=/home/bot/data/sources_2026-09-25.json

# адреса группы берём из ФАЙЛА ГРУПП (поле addresses, НЕ sources)
jq -r --arg g "$GRUPPA" '.groups[$g].addresses[]' "$FG" > /tmp/adresa_$GRUPPA.txt
wc -l < /tmp/adresa_$GRUPPA.txt

zh decisions.jsonl | grep -F -f /tmp/adresa_$GRUPPA.txt \
  | jq -c 'select(.action) | {ts_utc, source_task, source: .source[0:8], mint: .mint[0:8],
                              action, code, reason, spend_sol_eq, slot_lag, decide_latency_ms}' \
  | tail -30
```

Строки «Bloom не позван по группе» лежат ОТДЕЛЬНЫМ видом и несут поле `group`:

```bash
zh decisions.jsonl | jq -c 'select(.stage=="bloom_skipped_by_group")
                            | {ts_utc, group, source: .source[0:8], why}' | tail -10
```

Какие вообще группы живут и что в них — из признака жизни детектора:

```bash
jq '.source_groups' detector_status.json
```

### 5.2. Отказы с причинами

```bash
cd /home/bot/bloom_executor_live_data
# коды отказов по частоте (вся доступная история, включая ротированные)
zh decisions.jsonl | jq -r 'select(.action=="skip") | .code' | sort | uniq -c | sort -rn

# только сегодня, с текстом причины и чьим фильтром
DEN=$(date -u +%Y-%m-%d)
zh decisions.jsonl | jq -c --arg d "$DEN" \
  'select(.action=="skip" and (.ts_utc|startswith($d)))
   | {ts_utc, code, filter, reason, source_task, mint: .mint[0:8], spend_sol_eq}' | tail -40

# отказы ПО ДОПУСКУ отдельно (фильтр задачи DBot против наших лимитов)
zh decisions.jsonl | jq -r 'select(.action=="skip") | "\(.filter // "?")\t\(.code)"' \
  | sort | uniq -c | sort -rn

# сводка кодов, как её видит сама служба (счётчик в памяти процесса)
jq '{mode, signals_seen, to_buy, by_code, by_code_test_source, bloom_skipped_by_group}' \
   detector_status.json
```

Коды-константы — `analysis/bloom_exec_state.py:140-166`: `PAUSED_UNSOLD`,
`PAUSED_RATE_LIMITED`, `WEEK_BUDGET`, `MAX_OPEN_POSITIONS`, `SKIPPED_DUP_MINT`,
`MAX_BUY_TIMES_PER_TOKEN_REACHED`, `SIGNATURE_SEEN`, `DAILY_LOSS_LIMIT`,
`INSUFFICIENT_BALANCE`. Код `STALE` — `analysis/bloom_detector.py:256`.

### 5.3. Открытые позиции

Открытым считается `state` из `STATES_OPEN = ("intent", "bought", "selling",
"unsold")` (`analysis/bloom_exec_state.py:86`); `closed` (`:167`) и
**`spisana`** (`:180`) — НЕ открытые.

```bash
cd /home/bot/bloom_executor_live_data
zh positions.jsonl | jq -s '
  reduce .[] as $r ({}; .[$r.client_order_id] = ((.[$r.client_order_id] // {}) + $r))
  | [ .[] | select(.state=="intent" or .state=="bought"
                   or .state=="selling" or .state=="unsold") ]
  | {открытых: length,
     позиции: map({client_order_id, mint, state, lane, lane_group, mode, wallet,
                   sol_in, ts_intent_utc, sell_attempts})}'
```

Короткий ответ через признаки жизни (так же, как это делает гейт перезапуска
`deploy/checks/pered_perezapuskom_sluzhby.py:261-268`):

```bash
jq '.own_send.lane_state_by_group
    | to_entries
    | map({группа: .key, open: .value.open, stuck: .value.stuck,
           pnl_chain_sol: .value.pnl_chain_sol, day: .value.day})' detector_status.json
jq '{updated_utc, positions_in_cycle, pozicii_why_not}' seller_heartbeat.json
```

**ВАЖНО.** Токены в состоянии `spisana` ни один из этих запросов не покажет:
`open=0`, `stuck=0`, а токен лежит на кошельке полосы. Это известный дефект
(раздел 7, дефект II), и поэтому список списанных снимается ОТДЕЛЬНЫМ проходом:

```bash
zh positions.jsonl | jq -s '
  reduce .[] as $r ({}; .[$r.client_order_id] = ((.[$r.client_order_id] // {}) + $r))
  | [ .[] | select(.state=="spisana")
          | {client_order_id, mint, wallet, lane_group, sol_in, lane_bought_raw,
             ts_intent_utc, spisana_since, spisana_reason, spisana_ostatok_raw,
             ts_spisannaya_opros, closed_sol_net} ]'
```

Готовый прогон того же — `deploy/checks/konservaciya_sostojanie.py`. **НА ХОСТЕ
ЕГО НЕТ.** В списке доставки деплоя детектора
(`run_vps_deploy_bloom_detector_nl.yml:366`) этого файла нет, в `$CODE_DIR` он не
лежит и пути `/home/bot/bloom_executor/konservaciya_sostojanie.py` не существует.
Он живёт ТОЛЬКО в ветке, и ставит его на время прогона
`run_konservaciya_ostanovka_nl.yml`: кладёт файл в `/tmp` (`:146-147`, без `-n` —
иначе уезжает пустой) и оттуда зовёт (`:162` обычным видом, `:166` с `--json`).

Правильных способов два:

1. **прогоном** — `run_konservaciya_ostanovka_nl.yml` (он печатает снимок ДО
   остановки служб сам);
2. **копией руками** — скопировать в `/tmp` и позвать оттуда:

```bash
# файл едет с машины, где есть ветка; на хосте его НЕТ
scp deploy/checks/konservaciya_sostojanie.py root@45.59.170.76:/tmp/
cd /home/bot/bloom_executor
PYTHONPATH=/home/bot/bloom_executor BLOOM_STATE_DIR=/home/bot/bloom_executor_live_data \
  /home/bot/robinhood-chain-alpha/venv/bin/python \
  /tmp/konservaciya_sostojanie.py --json
# печатает pozicij_v_zhurnale, otkrytyh, po_sostojanijam,
# spisana_v_STATES_OPEN (:55), списанных своим проходом (:77-96),
# minty_spisannyh (:97-99)
```

### 5.4. Что сделано с позицией в последнем круге

Это ровно то, зачем в признаке жизни сторожа есть `last_cycle`
(`analysis/bloom_seller.py:5122-5132`): ветки ожидания в журнал не пишут, и
«журнал пуст» без этого поля не отличить от «сторож позиции не видел».

```bash
cd /home/bot/bloom_executor_live_data
jq '{updated_utc, mode, positions_in_cycle, pozicii_why_not, porjadok_why_not,
     pozicij_upalo, upavshie_why_not, last_cycle}' seller_heartbeat.json

# вся история по одной позиции: решения, продажи, обновления записи
CID=<client_order_id>
zh seller.jsonl    | jq -c --arg c "$CID" 'select(.client_order_id==$c or .cid==$c)' | tail -40
zh positions.jsonl | jq -c --arg c "$CID" 'select(.client_order_id==$c)'            | tail -40
zh decisions.jsonl | jq -c --arg c "$CID" 'select(.client_order_id==$c)'            | tail -20

# свежесть и возраст признаков (порог тот же, что у сторожа живучести: 600 с)
for F in detector_status.json seller_heartbeat.json; do
  echo "$F: $(( $(date -u +%s) - $(stat -c %Y $F) )) c nazad"
done
jq '{utc, итог, тревоги}' /home/bot/host_tajmery/zhivuchest/zhivuchest.json
```

### 5.5. Суточный итог

Сутки учёта — по `Europe/Madrid` (`DAY_TZ`,
`analysis/bloom_exec_state.py:48`), то есть граница `22:00Z` летом. Имя файла
pnl — ровно эта дата.

```bash
cd /home/bot/bloom_executor_live_data
DEN=$(TZ=Europe/Madrid date +%Y-%m-%d)
cat pnl_$DEN.json          # realized_sol, spent_sol, buys, sells, tz_unavailable

# итог полосы по группам, ПО ЦЕПИ (Правило 13) -- это число сравнивает групповой стоп
jq '.own_send.lane_state_by_group
    | to_entries | map({группа: .key,
                        pnl_chain_sol: .value.pnl_chain_sol,
                        pnl_all_groups_sol: .value.pnl_all_groups_sol,
                        pnl_chain_all_groups_sol: .value.pnl_chain_all_groups_sol,
                        pnl_chain_n: .value.pnl_chain_n,
                        pnl_chain_fields_n: .value.pnl_chain_fields_n,
                        pnl_chain_open_n: .value.pnl_chain_open_n,
                        day: .value.day, day_how: .value.day_how,
                        open: .value.open, stuck: .value.stuck})' detector_status.json

# закрытые за сутки, своим счётом по журналу
zh positions.jsonl | jq -s --arg d "$DEN" '
  reduce .[] as $r ({}; .[$r.client_order_id] = ((.[$r.client_order_id] // {}) + $r))
  | [ .[] | select(.state=="closed" and ((.ts_intent_utc // "")|startswith($d))) ]
  | {закрытых: length,
     итог_sol: (map(.pnl_counted_sol // 0) | add),
     расход_sol: (map(.pnl_counted_spend_sol // 0) | add),
     несчитаемых: (map(select(.uncountable_shared_mint)) | length)}'

# суточная выгрузка сделок полосы
jq '{снято_utc, версия, с, до, сделок, отказов, отказов_по_допуску,
     строк_с_итогом_по_цепи, строк_не_сверено, подпись_сводки, put_prodazhi_po_sutkam}' \
   /home/bot/host_tajmery/vygruzki/sdelki_polosy_$(date -u +%Y-%m-%d).json

# рента, возвращённая уборщиком за сутки
jq . /home/bot/host_tajmery/uborshchik_sutki.json
# счёт по цепи от конвейера учёта
jq . /home/bot/host_tajmery/uchet/data/ledger_status.json
```

### 5.6. Упавшие покупки, которых суточный счёт не видит

Это проверка на дефект I (раздел 7). Такие позиции закрыты, расход был, а в
`pnl_<день>.json` его нет:

```bash
cd /home/bot/bloom_executor_live_data
zh positions.jsonl | jq -s '
  reduce .[] as $r ({}; .[$r.client_order_id] = ((.[$r.client_order_id] // {}) + $r))
  | [ .[] | select(.state=="closed"
                   and ((.closed_reason // "")|startswith("продавать нечего"))
                   and (.closed_sol_net == null) and (.pnl_counted == null)) ]
  | {штук: length, расход_sol_in: (map(.sol_in // 0) | add),
     позиции: map({client_order_id, mint, sol_in, ts_intent_utc, closed_reason})}'

# и счётчик непроданных: при живых упавших покупках он стоит 0
jq '{unsold_streak, api_error_streak, api_error_last_code}' counters.json
```

### 5.7. Тревоги, рубильники, журнал systemd

```bash
cd /home/bot/bloom_executor_live_data
# ПОСЛАННЫЕ тревоги (poslano=true/false с причиной)
zh trevogi.jsonl | jq -c '{ts_utc, kluch, chto, poslano, why_not}' | tail -30
jq '.trevogi_zhurnal' detector_status.json

# рубильники: пять файлов, любой = запрет
for F in /etc/bloom-executor/KILL /etc/bloom-executor/KILL_BLOOM \
         KILL_BY_TELEGRAM KILL_SELL_BY_TELEGRAM KILL_OWN_SEND; do
  [ -e "$F" ] && echo "СТОИТ: $F  $(ls -l "$F")" || echo "нет: $F"
done

# журналы служб и таймеров
journalctl -u bloom-detector -n 200 --no-pager
journalctl -u bloom-seller   -n 200 --no-pager
journalctl -u dbot-sold-guard -n 200 --no-pager
journalctl -t bloom-uchet-chas --since '-24h' --no-pager   # SyslogIdentifier таймера
journalctl -t bloom-uborshchik-chas --since '-24h' --no-pager
systemctl list-timers 'bloom-*' --no-pager
systemctl show bloom-detector -p NRestarts -p MemoryCurrent -p MemoryPeak --value

# сторож продаж DBot -- свой журнал, свой каталог
cd /home/bot/dbot_sold_position_guard_live_data
jq -c 'select(.event) | {ts: .ts_utc, event, task, wallet, mint}' \
  dbot_sold_position_guard_audit.jsonl | tail -40
```

### 5.8. Три журнала, на которые команд не было: кредиты Helius, вызовы API, журнал запусков

**(1) `helius_usage/<служба>.json` — РАСХОД КРЕДИТОВ, то есть ДЕНЬГИ.** Пишет
`CreditMeter.add` (`analysis/solana_rpc_client.py:285`, запись `:305-353`), файл
на службу: имя из `usage_path_for` (`:42-44`), каталог заводят
`analysis/bloom_detector.py:1487-1489` и `analysis/bloom_seller.py:328-330`.
Поля — РУССКИЕ: внутри `дни.<ГГГГ-ММ-ДД>.<служба>` лежат `кредитов_за_день`,
`байт_за_день`, `по_часам.<ГГГГ-ММ-ДДTЧЧZ>.{кредитов, байт}`, `группа`,
`бюджет_за_день`, `потрачено_группой`, `доля_бюджета`, `выше_70_процентов`,
`останавливать_при_пороге` (`:317-346`); в корне — `обновлено_utc`, `тариф`,
`бюджеты_за_день` (`:347-349`). Порог пометки — `WARN_AT = 0.70` (`:229`),
`STOP_AT_WARN = False` (`:233`), то есть пометка есть, а автостопа нет.

```bash
cd /home/bot/bloom_executor_live_data/helius_usage
DEN=$(date -u +%Y-%m-%d)

# ВНИМАНИЕ: поля тут РУССКИЕ, а jq не берёт нелатинское имя через точку --
# `.дни` это синтаксическая ошибка. Только скобками: `.["дни"]`. Проверено.

# за сегодня по всем службам сразу: кредиты, байты, доля бюджета, флаг 70%
for F in *.json; do
  jq -r --arg d "$DEN" --arg f "$F" '
    (.["дни"][$d] // {}) | to_entries[]
    | [$f, .key, (.value["кредитов_за_день"]|tostring), (.value["байт_за_день"]|tostring),
       (.value["бюджет_за_день"]|tostring), (.value["доля_бюджета"]|tostring),
       (.value["выше_70_процентов"]|tostring)] | @tsv' "$F"
done

# по часам одной службы -- видно, какой час сжёг бюджет
jq -r --arg d "$DEN" '.["дни"][$d]["bloom_detector"]["по_часам"]
  | to_entries | sort_by(.key)[]
  | [.key, (.value["кредитов"]|tostring), (.value["байт"]|tostring)] | @tsv' \
  bloom_detector.json

# сумма по всем осколкам за сегодня (бюджет у группы ОБЩИЙ -- считать надо так же)
jq -s --arg d "$DEN" '[.[] | (.["дни"][$d] // {}) | to_entries[]
  | .value["кредитов_за_день"]] | add // 0' *.json
```

**(2) `api_calls.jsonl` — ВЫЗОВЫ ПЛОЩАДКИ Bloom.** Путь заводит
`analysis/bloom_exec_state.py:597`, а пишет **другой модуль**:
`BloomAPI._log_call` (`analysis/bloom_api.py:401-407`) — `ts_utc` плюс тело
строки. Два вида по полю `stage`: `request` (`:445-447`: `path`,
`client_order_id`, `why`, `mode` = `dry-run`/`live`, `body`; у сухого прогона ещё
`result`) и `response` (`:480`, `:500`, `:511`, `:546`: `code` — код HTTP,
`rate_headers`, `ok`, `order_id`, `signatures`, `why_not`, `caveat`, у сетевого
отказа `network`). **Ключа в теле нет** — он только в заголовке
(`:444`, самопроверка `:695`). Поля `bloom_ms` здесь НЕТ: время ответа площадки
пишется в ПОЗИЦИЮ, а не сюда (`deploy/checks/nesevshie_iz_zhurnala.py:29-32`).

```bash
cd /home/bot/bloom_executor_live_data
# коды ответов площадки по частоте (вся история, включая ротированные)
zh api_calls.jsonl | jq -r 'select(.stage=="response") | "\(.code)\t\(.ok)"' \
  | sort | uniq -c | sort -rn

# последние отказы с причиной и оговоркой
zh api_calls.jsonl | jq -c 'select(.stage=="response" and (.ok|not))
  | {ts_utc, client_order_id, code, why_not, caveat, network}' | tail -30

# запрос и ответ одной сделки рядом (тело печатать можно: ключа в нём нет)
CID=<client_order_id>
zh api_calls.jsonl | jq -c --arg c "$CID" 'select(.client_order_id==$c)
  | {ts_utc, stage, mode, why, code, order_id, signatures, why_not}'

# заголовки лимитов: не режет ли площадка частоту
zh api_calls.jsonl | jq -c 'select(.rate_headers != null and .rate_headers != {})
  | {ts_utc, code, rate_headers}' | tail -10
```

**(3) `zapusk_zhurnal.jsonl` — ТЕНЕВОЙ ЖУРНАЛ ЗАПУСКОВ pump.fun, БЕЗ ПОКУПОК.**
Пишет `zapisat` (`analysis/c1_zapusk_zhurnal.py:463-474`) — дописыванием, без
чтения файла; имя файла — `ИМЯ_ЖУРНАЛА` (`:55`), каталог — `BLOOM_STATE_DIR`
(`put_zhurnala`, `:458-460`). Строку собирает `zapis_iz_uvedomlenija`
(`:543-582`): `ts_utc`, `sozdanie` (`create`/`create_v2`), `signature`,
`signature_why_not`, `kupil_v_sozdanii`, `sol_sozdatelya`, `gotov_cherez_ms`,
`nash_slot_seti`, `net_slot_age_s`, `nash_slot_gotovnosti`, `otryv_slotov`,
`dlina_slota_s`, `otryv_why_not`, `bajt`, `pokupok_net`.

Готовый свод у самого модуля — `--svod <файл> [с] [до]` (`:862-869`); считает
`strok_vsego`, `sozdanij`, `s_pokupkoj_sozdatelya`, `sol_sozdateley`,
`otryv_slotov.{min,max,mediana,bez_chisla}`, `bajt`, `mb`, `kreditov`,
`bez_bajt`, `pervaja_utc`, `poslednjaja_utc`, `razvidnosti` (`:483-539`).
Кредиты он считает ИЗ САМИХ СТРОК (`:534`), а не из счётчика процесса, — счётчик
обнуляется перезапуском.

```bash
cd /home/bot/bloom_executor
S=$(python3 -c "import time; print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()-3600)))")

# свод за последний час и за весь журнал. Модули на хосте лежат ПЛОСКО в
# $CODE_DIR (деплой кладёт их `cat > $CODE_DIR/<файл>`,
# run_vps_deploy_bloom_detector_nl.yml:378) -- каталога analysis/ на хосте НЕТ
PYTHONPATH=/home/bot/bloom_executor /home/bot/robinhood-chain-alpha/venv/bin/python \
  /home/bot/bloom_executor/c1_zapusk_zhurnal.py --svod \
  /home/bot/bloom_executor_live_data/zapusk_zhurnal.jsonl "$S"
PYTHONPATH=/home/bot/bloom_executor /home/bot/robinhood-chain-alpha/venv/bin/python \
  /home/bot/bloom_executor/c1_zapusk_zhurnal.py --svod \
  /home/bot/bloom_executor_live_data/zapusk_zhurnal.jsonl

# руками, по строкам: наш отрыв в слотах и создатели, купившие в создании
cd /home/bot/bloom_executor_live_data
zh zapusk_zhurnal.jsonl | jq -c 'select(.sozdanie)
  | {ts_utc, sozdanie, otryv_slotov, otryv_why_not, gotov_cherez_ms,
     kupil_v_sozdanii, sol_sozdatelya, bajt}' | tail -30

# разновидности создания и строки без подписи (их к цепи не привязать)
zh zapusk_zhurnal.jsonl | jq -r 'select(.sozdanie) | .sozdanie' | sort | uniq -c
zh zapusk_zhurnal.jsonl | jq -c 'select(.signature_why_not) | {ts_utc, signature_why_not}' \
  | tail -10
```

Тот же свод делает за нас часовая проверка хоста:
`run_vps_health_nl.yml:486-490` (окно в час и весь журнал), поэтому в её отчёте
эти числа уже есть.

---

## 6. ГРАБЛИ

Пятнадцать разделов с собранными числами, плюс шестнадцатый пункт — три
незакрытых дефекта, они вынесены в раздел 7.

---

### Грабля 1. STALE-догон: порог 10 слотов, а не 3; догоняющая пачка после перезапуска

**В чём грабля.** Порог устаревания сигнала стоит на **10 слотах**, и это
нарочно. При этом перезапуск службы даёт догоняющую пачку сигналов с лагом
**18–25 слотов**, которую порог съедает ЦЕЛИКОМ. Поэтому перезапускать можно
только при нуле позиций.

**Где в коде.** `analysis/bloom_detector.py:353` —
`МАКС_ОТСТАВАНИЕ_СЛОТОВ = ST.env_int("BLOOM_STALE_SLOTS", 10)`; `:256` — код
отказа `STALE`; `:1308` — параметр решения; `:1344-1349` — сам отказ; `:3415` —
вывод в признак жизни. Счётчик:
`deploy/checks/dnevnye_schetchiki.py:26`, `:229-233`.

**Числа.** Текст отказа: «сигнал отстал на N слотов при пороге 10 — цена уже не
та». Порог отсекается **ДО** тормозов (иначе подпись заняла бы «уже видели» и
исказила сверку, `analysis/bloom_detector.py:1311-1313`). Замер в комментарии
`:349-352`: при **3 слотах** терялись сделки, которые DBot берёт на S+2 и на
которых сделал x5 — пример `Omakase 17:44:03Z, 8.211 SOL-экв., отстал на 5
слотов`. От опоздания защищает не порог, а min-out: наценка **0.2** у `leader`,
**0.4** у `batch5` и `lane_s0`.

Обратная сторона — `data/pravka_grupp_plan_zabor_vol4vw.json:4`: перезапуск
стоит **~25 слотов** активности источника, измерено 10.10 22:14Z — догоняющая
пачка **40 сигналов, лаг 18–25 слотов при пороге 10**, то есть все в `STALE`.

Отдельного предохранителя на догоняющую пачку в детекторе **в коде не нашёл** —
есть только в зонде `analysis/preprocessed_ab_zond.py:519-521`.

**Как проверить.** В признаке жизни поле `stale_slots` должно быть **10**
(прогон `run_vps_priznak_zhizni_nl.yml`).
`deploy/checks/dnevnye_schetchiki.py:232` печатает «ВНИМАНИЕ: покупка при
отставании N слотов при пороге 10 — фильтр STALE не сработал» — эта строка
обязана ОТСУТСТВОВАТЬ. `utro_itog.py` даёт медиану/p90/долю > 3 по `slot_lag`.

**Рядом — вторая грабля того же куста.** `net_slot_age_s` из признака жизни —
лестница со ступенью **до 220 слотов**, поэтому часы слотов пишутся отдельным
файлом `net_slot.json` каждые **0.25 с** (`analysis/bloom_detector.py:199`;
пишет `записать_часы_слотов` `:2374`, зовётся из круга детектора `:8761`). Без этого сделка 19:05:05Z с удержанием 108 слотов держалась 200
— **53.7 с вместо 29.2**, всегда в сторону «позже».

---

### Грабля 2. `lane_open_max` — предел берётся у ГРУППЫ, а открытые считаются ПО ВСЕМУ КОШЕЛЬКУ

**В чём грабля.** Число `lane_open_max` читается у той группы, которая сейчас
просит вход, а открытые позиции складываются по **всем** группам сразу. Поэтому
чужая открытая позиция закрывает вход группе с низким пределом.

**Где в коде.** `analysis/bloom_own_send.py:823-852` (`предел_открытых`),
`:5115` (умолчание), `:5323-5333` (счёт открытых), `:5787` и `:5962` (ветвь
гейта), `analysis/bloom_source_groups.py:364` (поле политики).

**Числа.** `ЛИМИТ_ОТКРЫТЫХ = 1` — умолчание кода. Ноль, отрицательное и мусор
**не выключают** группу, а уходят в это же 1
(`analysis/bloom_own_send.py:852`); выключает только `lane_trades`.

Живьём 11.10 10:53:06Z у `nabor` стояло `lane_open_max 3`; на замере поднимали
до **6**. По `docs/ZASHCHITY_POKUPKI.md:76` — **3** у торгующих (`batch5`,
`cand1`, `cand1_03`, `cand1_05`, `cand2`, `cand3`, `lane_s0`, `leader`,
`sniper_src`) и **1** у `kandidaty`/`log_only`/`off`.

**Почему по кошельку.** Сказано словами на `analysis/bloom_own_send.py:5323-5325`:
«кошелёк у полосы один, и две покупки одновременно он не вынесет, из какой бы
группы они ни пришли».

**Цена неверного понимания.** Замер
`docs/doklad_2026-10-04_paket_D_krug_001.md:39`: у `krug_001` предел 1, открытых
по кошельку **2**, вход закрыт, хотя «своих» у группы ноль. Дальше
`docs/doklad_2026-10-11_host_golova_zabor.md:1071`, `:1100`: пришлось поднимать
предел **обеим** группам до 6, иначе каждая фактически получала меньше, чем
написано.

**Две поправки к счёту.** **Зависшая** позиция (не продана за
`СРОК_ЗАВИСАНИЯ_S = 120.0` с, `analysis/bloom_own_send.py:5122`) места не
занимает, и **списанная** (`ST.STATE_SPISANA`) тоже не занимает (`:5326-5333`,
слово владельца 04.10 23:03).

**Как проверить.** Самопроверка `analysis/bloom_own_send.py:13419-13436`: при
пределе 3 и двух открытых пускает, при трёх — отказ со строкой «при пределе 3»;
группе без поля те же **две чужие** открытые закрывают вход с «при пределе 1».
Живьём — `source_groups.policies.<группа>.lane_open_max` в признаке жизни (так
читает `.github/workflows/run_gruppa_cand1_nl.yml:132`) и
`deploy/checks/gruppy_v_boyu.py:55-56`.

---

### Грабля 3. `pool_senders` — только СПИСОК строк; строка отбрасывается целиком и МОЛЧА

**В чём грабля.** Поле читается как список имён отправителей. Строка, пустой
список или один элемент не-строка превращают его в `None`, то есть во «все
подключённые» — и чаевые уходят **всем шести** вместо трёх.

**Где в коде.** `analysis/bloom_source_groups.py:541-557`
(`_список_отправителей`), `:424` (сборка политики), `:148-154` (умолчание
`None`); применение на денежном пути `analysis/bloom_own_send.py:7434-7462`;
запрет на правку строкой `deploy/checks/pravka_grupp.py:42-48`.

**Правило «всё или ничего».** Любой элемент не-строка — **весь список
отбрасывается** (`analysis/bloom_source_groups.py:548-552`). Имена чистятся,
повторы убираются, список **сортируется** (`:555-557`), поэтому порядок в файле
потребителю не виден.

**Числа.** Подключённых отправителей **шесть**: `astralane`, `blockrazor`,
`helius`, `jito`, `nozomi`, `zeroslot` (`data/senders.json`). Боевой набор
группы забора — **три**: `astralane`, `helius`, `jito`.

Цена названа числами в `data/pravka_grupp_plan_zabor_vol4vw.json:4`: шесть
чаевых против трёх — **0.004101 SOL против 0.002001**, то есть **41 % билета
против 20 %** при билете 0.01. Почему так: в режиме blockhash **одна**
транзакция несёт чаевые всем перечисленным, и севшая платит **всем**
(`analysis/bloom_own_send.py:7437-7441`), то есть набор отправителей — это цена
сделки, а не маршрут.

**Живые случаи.** 02.10 у `cand1_05` список задали **строкой** — правка прошла
самопроверку зелёной, а читатель полосы молча её отбросил
(`deploy/checks/pravka_grupp.py:208-210`, `:367-378`). 11.10 06:47Z записали
`['helius','astralane','jito']`, читается сортированный — прогон упал
самопроверкой и откатил файл целиком.

**Как проверить.** Править только через `deploy/checks/pravka_grupp.py` — он
отказывает **кодом 11**, если `lane_pools` или `pool_senders` заданы не списком.
В записи решения смотреть поля `pool_senders_gruppy`,
`pool_senders_net_takih`, `pool_senders_why_not`
(`analysis/bloom_own_send.py:7448-7461`): названное группой имя, которого нет
среди подключённых, уходит отдельным списком и НЕ молчит. Самопроверка модуля
групп — `analysis/bloom_source_groups.py:856-867`.

---

### Грабля 4. `lane_pools` — список ПОЛНЫЙ И ЗАПРЕЩАЮЩИЙ, 11 имён перечисляются явно

**В чём грабля.** Список в файле групп не ДОБАВЛЯЕТ типы к постоянным, а
**ЗАМЕНЯЕТ** их целиком: что не названо — выключено. Поэтому писать его по
памяти нельзя.

**Где в коде.** `analysis/bloom_own_send.py:1193-1200` (`ИМЕНА_ТИПОВ` +
`ИМЯ_ДВУХШАГОВОГО`), `:1203-1236` (`_типы_из_файла`), `:1238-1243`
(`_тип_включён`), `:1675-1706` (`типы_полосы`), `:1708-1713`
(`двухшаговый_включён`), `:1066` (`ПУЛЫ_ПОЛОСЫ`), `:1333-1336`
(`ДОП_ТИПЫ_ПОЛОСЫ`).

**Числа.** **10 типов пулов** в `ИМЕНА_ТИПОВ` (`:1193-1196`): `bonding`,
`damm2`, `dbc`, `dlmm`, `pump_amm`, `cpmm`, `launchlab`, `clmm`, `whirlpool`,
`amm_v4`. **Плюс одиннадцатое имя `two_step`** (`:1200`) — не тип пула, а способ
сборки, спрашивается отдельной функцией (`:1708`). **Итого 11 имён.**

Без списка работает прежнее: постоянные **два** (`PUMP_AMM`, `CPMM`) плюс флаги
окружения `LANE_<ТИП>` / `LANE_<ТИП>_GROUPS`.

**Две беды, обе замерены.**

1. **Забытый в списке тип молча перестаёт торговать**
   (`deploy/checks/lane_pools_polnyj_spisok.py:6-8`); не назвать `two_step`
   значит его выключить (`:11-13`).
2. **Без `lane_pools` новая группа не купит НИЧЕГО**: типы уходят на флаги
   окружения, а переменных `LANE_<ТИП>` на хосте **нет ни одной**
   (`docs/doklad_2026-10-11_host_golova_zabor.md:999-1001`).

Отдельно: пустой список читается как «поля нет»
(`analysis/bloom_own_send.py:1233-1235`), а пустая политика создаётся **с кодом
0** — «забыл поле» ни одна проверка не поймает. По признаку жизни сторож умеет
**11** типов, котирует **10** (`docs/ZASHCHITY_POKUPKI.md:82`).

**Как проверить.** Не писать руками:
`deploy/checks/lane_pools_polnyj_spisok.py` считает полный список **тем же
кодом, которым решает полоса** (`OS.типы_полосы(группа)` +
`OS.двухшаговый_включён`), и только ДОБАВЛЯЕТ новый тип (`предложение`,
`:58-60`). Прогон `run_lane_pools_spisok_nl.yml` (предложение в файл `--out`),
применяет человек прогоном `run_lane_pools_vkljuchit_nl.yml`. Зелёная строка
применения — «сверено списком, 11 шт.». Неизвестная программа печатается как
`НЕИЗВЕСТНЫЙ:<адрес>` (`:51-53`) — это тоже отказ, а не мелочь.

---

### Грабля 5. `zabor_s0` — забор S+0: только явное `true` у группы, кольцо blockhash-ей, промах 0

**В чём грабля.** Забор S+0 — срок жизни покупки **до блока источника**:
опоздавшая транзакция выбрасывается сетью без комиссии. Включается **только**
полем группы, явным `true`; общего флага окружения нет нарочно.

**Где в коде.** `analysis/bloom_own_send.py:7093-7108` (`zabor_s0_vklyuchyon`),
`:6957-6963` (кольцо и его числа), `:7039-7091`
(`blockhash_do_bloka_istochnika`), `:8022-8075` (путь сделки), `:8049`
(`zabor_s0_slotov_mimo`); `analysis/bloom_source_groups.py:415` (**`is True`**, а
не `bool()`); признак жизни `analysis/bloom_detector.py:3591-3609`.

**Числа.** `ГЛУБИНА_КОЛЬЦА = 400` записей (`:6957`, ~60 с при опросе раз в
150 мс), `СРОК_КОЛЬЦА_S = 90.0` (`:6958`),
`ЖИЗНЬ_BLOCKHASH_БЛОКОВ = 150` (`:6959`, предел сети, не наш выбор),
`ПРОМАХ_КОЛЬЦА_ПРЕДЕЛ = 0` (`:6963`) — годно **только точное попадание** в слот
источника. Обычный тёплый хеш живёт `СРОК_BLOCKHASH_S = 30.0` с (`:6934`) при
сетевых ~60 с (запас вдвое). Рубильник часов кольца —
`BLOOM_ZABOR_KOLCO_TEMPOM`, цена **397 059 кредитов в сутки ≈ 1.99 $/сут** по
тарифу 5 $ за миллион.

**Почему явно и почему `is True`.** `bool("нет")` — это `True`, и забор
включился бы от любой строки в файле
(`analysis/bloom_source_groups.py:413-416`).

**Почему нельзя откат на тёплый хеш.** Он вернул бы **+150 блоков**, то есть
прежнее поведение под именем забора, и владелец читал бы «забор работает» там,
где он не работал (`analysis/bloom_own_send.py:7046-7051`).

**Почему промах 0.** Высота выводится по ближайшей записи кольца **снизу**, и
проходом 10.10 промах доходил до **39 блоков** — принималось молча, а срок жизни
выходил раньше задуманного. Без рубильника кольца забор **отказывает до
подписи**: кольцо наполнялось раз в 12 с — одна высота из ~55, а забору нужна
каждая.

**Живьём забор ни разу не попал точно**: поле `zabor_s0_slotov_mimo` во всём
журнале встречается дважды — 11.10 07:02:13Z (промах 3) и 07:10:29Z (промах 2)
при пределе 0.

Забор **несовместим с режимом nonce**: отказ до сборки, если `pool_nonce` просит
сама группа (`analysis/bloom_own_send.py:7509-7521`, `:8036-8042`).

**Как проверить.** Признак жизни: `zabor_s0_kolco`, `zabor_s0_kolco_pochemu`,
`zabor_s0_kolco_tempom`, `zabor_s0_kolco_temp_s`,
`zabor_s0_kolco_kreditov_v_sutki`. В журнале решений искать `zabor_s0` и
`zabor_s0_slotov_mimo`. Самопроверки — `analysis/bloom_own_send.py:15630-15649`,
`:15942`; опыт забора — `run_zabor_s0_opyt_nl.yml`. После правки окружения
**нужен перезапуск** (часы кольца читают рубильник один раз при старте), и
делать его только при нуле позиций — см. граблю 1.

---

### Грабля 6. 1232 байта — предел сети; живой запас на pump AMM 18 байт

**В чём грабля.** Solana не принимает пакет больше 1232 байт. Размер проверяется
**до подписи и до отправки**, потому что для полосы отвергнутая отправка =
упавшая сделка.

**Где в коде.** Константа `analysis/bloom_own_send.py:1744`
(`ПРЕДЕЛ_РАЗМЕРА_TX = 1232`), комментарий с замерами `:1735-1743`. Проверки:
`:3378-3390` (основная сборка), `:1639-1644` (маршрут Jupiter), `:4390-4394`
(вариант пула). Та же константа отдельно:
`analysis/bloom_lane_two_step.py:45`,
`analysis/close_empty_token_accounts.py:92`,
`deploy/checks/usdc_noga_razmer_cu.py:40`.

**Числа.** **1232** — предел сети. Каждые чаевые добавляют **~49 байт**, пять
чаевых пула — **~+196 байт**. Замер 25.09 на сохранённых образцах: CPMM с одними
чаевыми **767** байт (с пятью **963** — влезает), pump.fun AMM **1132** (с пятью
**1328** — НЕ влезает). Потолок чаевых пула
`ПОТОЛОК_ЧАЕВЫХ_ПУЛА_SOL = 0.005` (`analysis/bloom_own_send.py:1733`). Nonce
стоит лишних **74 байта** (`:7540`).

Живые размеры по журналу 10–11.10
(`docs/doklad_2026-10-11_host_golova_zabor.md:1847-1855`): кривая **949**, pump
AMM **1214** — то есть **запас 18 байт**; вторые чаевые дают **+49**, и
самопроверка на этом краснеет числом **1262**
(`analysis/bloom_own_send.py:14227-14230`). Чисел 949/1214/18 в самом коде нет —
они из журнала.

**Что будет неправильно.** Отвергнутый сетью пакет у полосы считается упавшей
отправкой, **три подряд — стоп полосы** (`analysis/bloom_own_send.py:1766-1769`).
Отказ по пределу — всегда «всё или ничего»: размер вариантов одинаков (вариант
отличается только инструкцией чаевых), и по журналу 01–08.10 из **391** отказа
во всех 391 «ни один вариант не собрался», частичных **0**; размеры **1246**
(159 раз) и **1247** (18 раз); последний отказ 08.10 15:34:20Z, с 09.10 16:29Z
отказов ноль (0/42).

Отдельно: USDC-нога **без таблиц адресов не влезает ни на одном типе** — замер
**1326…1703** байта на 32 сделках (`analysis/c2_usdc_noga.py:1031`,
`analysis/c3_usdc_noga_signaly.py:13`); свой двухшаговый путь без нашей таблицы
— **1453…1594**, с таблицей **915…1056**
(`analysis/bloom_own_send.py:7817-7820`). С таблицей замер прошёл с запасом
почти вдвое: **643…702** байта против 1232 (`docs/usdc_noga_kak_vstroit.md:277`).

**Как проверить.** Прогон `run_vps_usdc_noga_razmer_nl.yml` →
`deploy/checks/usdc_noga_razmer_cu.py`: таблица по типам, размер против **1232**
и CU по `simulateTransaction` против **250 000** (`BLOOM_LANE_CU_TWO_STEP`);
самопроверка `:309` держит само число. В журнале решений искать `too_big: true`
и строки «при пределе сети 1232» — их должно быть ноль. На pump AMM веер обязан
нести **одни** чаевые.

---

### Грабля 7. Ротация счетов чаевых BlockRazor — десять рабочих, четыре снятых

**В чём грабля.** Счёт чаевых выбирается **разный от сделки к сделке** из
нынешних десяти, и его правильность решает **только документация сервиса**, а не
цепь: перевод на снятый из документации счёт BlockRazor за чаевые не считает.

**Где в коде.** `analysis/bloom_senders.py:249-260` (`счёт_чаевых`), `:39`
(`ФАЙЛ_РЕЕСТРА = "data/senders.json"`), `:119-127` (исключение отказывающего),
`:832-846` (самопроверка); `analysis/c2_zabor_s0_opyt.py:1480-1500` (проверка на
хосте); данные — `data/senders.json`, узел `blockrazor`.

**Числа.** Рабочих счетов **10** (из документации, снятой 10.10), снятых —
**4** (`tip_accounts_ubrany`), они же **позиции 5–14** прежнего списка из 14.
Выбор — по семени: `счета[sum(bytearray(семя)) % len(счета)]`, семя = **подпись
источника** (`analysis/bloom_own_send.py:7396`, `:8151`:
`семя=источник_подпись or ключ_операции`), без семени — `random.choice`.
Минимум чаевых BlockRazor **100 000** лампортов (измеренный, не из
документации), формат `blockrazor_sendTransaction`, ключ в заголовке — имя
секрета `BLOCKRAZOR_AUTH_TOKEN` (значение не приводится). Исключение
отказывающего: `ОТКАЗОВ_ПОДРЯД_ПРЕДЕЛ = 5`
(`analysis/bloom_senders.py:126`), `ВОЗВРАТ_ЧЕРЕЗ_S = 900.0` (`:127`).

**Живой отказ.** С 10.10 13:49:37Z сервис отказывал на **каждой** отправке
текстом «Insufficient tip … at least 100000 lamports», и причиной был **адрес**,
а не сумма: четыре пробы на снятом счёте отказали при **100 000, 101 000,
200 000 и 1 000 000** лампортов и при обоих порядках инструкции чаевых; проба
10.10 21:56:58Z на счёте из нынешнего списка — **2 из 2** ушли в сеть (http 200).
Ночью 09→10.10 те же четыре счёта принимали **11** отправок подряд.

Отдельно записано, почему цепь это не отличает: на снятые счета по-прежнему идут
чужие переводы, «живой на цепи» ничего не говорит о том, считает ли их сервис
своими.

**Зачем ротация.** Требование самой документации BlockRazor и Nozomi: один и тот
же счёт на каждой отправке — лишняя точка соперничества за запись
(`analysis/bloom_senders.py:251-253`).

**Цена молчания.** Отказывающему отправителю в обычном (не nonce) режиме пула мы
**всё равно платим чаевые** — одна транзакция несёт их всем, и севшая платит
всем (`analysis/bloom_senders.py:100-103`).

**Как проверить.** Самопроверка `analysis/bloom_senders.py:836-846`: снятых
среди рабочих нет; счёт выбирается **только** из нынешних десяти на 40 разных
семенах. На хосте — `analysis/c2_zabor_s0_opyt.py:1488-1500`: «рабочих счетов
BlockRazor ровно **десять**»; если `tip_accounts_ubrany` пусто — реестр старого
вида (до деплоя 10.10), и проверка так и говорит, допуская 4 или 14. Источник
истины на проверку:
`data/docs/senders/blockrazor_priority_fee_and_tip_2026-10-10.md`.

---

### Грабля 8. 0slot — только ams: ключ зарегистрирован лишь на европейской точке

**В чём грабля.** Региональная отправка через `zeroslot` запрещена реестром:
ключ отвечает **только** на европейской точке, на остальных — 403.

**Где в коде.** Запрет: `data/senders.json`, узел `zeroslot`, поля
`region_podmena: false` и `region_podmena_pochemu`. Чтение запрета:
`analysis/bloom_region_send.py:234-249`. Точки: `data/sender_regions.json`
(`точки.zeroslot`), рабочий адрес `urls_by_region` в `data/senders.json`.
Самопроверки с адресом: `analysis/bloom_senders.py:954`, `:979`, `:1200`,
`:1231`; `analysis/bloom_region_send.py:347`.

**Числа.** Рабочая точка — **ams1.0slot.trade**, по **http, а не https** («так у
них быстрее», письмо 25.09). В реестре `urls_by_region` только EU: `ams`, `fra`
(de1), `fra2` (de2), `lon`. В карте точек `data/sender_regions.json` у
`zeroslot` **6 хостов**: ams, ams1, de — `EU`; jp — `Asia`; la — `US-West`;
ny — `US-East`.

Прочие числа узла: темп **5** вызовов в секунду (иначе 419), минимум чаевых
**1 000 000** лампортов (0.001 SOL, не 0.0001), `tip_first: true` (инструкция
чаевых первой), разрешён **только** метод `sendTransaction` (иначе 403), таймаут
соединения **65 с**, есть `/health`, счетов чаевых **5**, имя секрета ключа —
`ZEROX_API` (значение не приводится), ключ уходит **параметром строки запроса**
`api-key`.

**Замер** из `region_podmena_pochemu` (журнал решений, **35** отправок
14:10–20:49Z): EU ams — принял **25**, отказал **0**; Asia jp — **0 и 5**;
US-East ny — **0 и 4**; US-West la — **0 и 1**. Отказ всегда один:
`403 {"code": 403, "message": "api-key does not exist"}`. Денег это не стоило
(отказанная отправка не платит ни комиссии, ни чаевых), но **путь доставки
терялся у ~29 % сигналов**.

**Зачем регионы вообще.** Замеры 26.09 — доля S+0 по лидерам EU **31.6 %**
(18 из 57), Asia **0 %** (0 из 10), US-East **12.5 %** (1 из 8)
(`analysis/bloom_region_send.py:3-9`). Снять поле — когда 0slot откроет ключ на
ny/la/jp.

Сбой чтения реестра оставляет подмену **разрешённой** нарочно
(`analysis/bloom_region_send.py:237-239`): запрет — это правка пути доставки, и
вводить его из-за непрочитанного файла нельзя.

**Как проверить.** `data/senders.json` → `zeroslot.region_podmena` должно быть
`false`; региональная отправка в целом выключена по умолчанию и требует
`BLOOM_REGION_SEND=1` (`analysis/bloom_region_send.py:18-19`). Живость точек —
`deploy/checks/zhivy_li_regionalnye_tochki.py` через
`run_regiony_pered_vkljucheniem_nl.yml`, таймаут **2.5 с**; **405 и 404 на GET —
живая точка**, иначе утром получится «все точки мёртвые» на исправных серверах
(`:79-83`). Ключ режется из печатаемого адреса в `адрес_без_запроса` (`:58-77`).
В записи позиции смотреть `lane_region_point` / `lane_region_senders`
(`analysis/bloom_own_send.py:8531`).

---

### Грабля 9. Рента при продаже через Jupiter (закрытие пустых токен-счетов)

**В чём грабля.** Транзакцию продажи строим НЕ мы: у Bloom её собирает и
подписывает площадка, у Jupiter приходит готовое сообщение с таблицами адресов,
куда дописать `CloseAccount` нельзя, не переклеив таблицы
(`analysis/bloom_close_on_sell.py:1-27`). Поэтому счёт минта после продажи
остаётся пустым, и в нём заперта рента. Закрывается он ОТДЕЛЬНОЙ своей
транзакцией «сразу следом». Граблей три слоя:

1. **путь по умолчанию ВЫКЛЮЧЕН** — нужен `BLOOM_CLOSE_ON_SELL=1`
   (`analysis/bloom_close_on_sell.py:50-51`; в снимке безопасного env хоста флаг
   стоит в 1 — `data/env_bloom_safe.txt:2`, деплой пишет его
   `run_vps_deploy_bloom_detector_nl.yml:788-789`);
2. **кошелёк берётся ПОЗИЦИИ, а не исполнителя**
   (`analysis/bloom_seller.py:1650-1656`): у позиции полосы поля `wallet` нет, и
   по прежней строке закрытие шло на кошелёк исполнителя, где счёта минта нет
   вовсе;
3. зов `закрыть_счёт_после_продажи` в горячем пути стоит РОВНО ОДИН —
   `analysis/bloom_seller.py:2453`, в ветке «остаток ноль дважды подряд» пути
   **Bloom** (`handle`). В пути **ПОЛОСЫ** (`обработать_полосу`, закрытие
   подтверждённой продажей `analysis/bloom_seller.py:2734-2740`) этого зова НЕТ
   ни одного. То есть рента после продажи полосы (а полоса продаёт Jupiter'ом по
   правилу 4) этим путём не возвращается — её забирает только часовой уборщик.

**Где в коде.** `analysis/bloom_seller.py:1637`
(`закрыть_счёт_после_продажи`), `:1653`, `:2448-2455`;
`analysis/bloom_close_on_sell.py:50` (`включено`), `:69` (`выгодно`), `:80`
(`годные_счета`), `:92` (`нет_открытой_позиции`), `:121`
(`закрыть_после_продажи`); `analysis/close_empty_token_accounts.py:69`, `:80`,
`:89-94`, `:153` (`окно_без_покупок`), `:199` (`можно_сейчас`), `:217`
(`минты_занятые`), `:501` (`вернётся_лампортов`); таймеры
`deploy/hostovye_tajmery.py:147-158` и `:190-213`.

**Числа и константы.**

| что | значение | где |
|---|---|---|
| рента обычного счёта SPL | `РЕНТА_ЛАМПОРТОВ = 2_039_280` = `РЕНТА_ОБЫЧНОГО = 0.00203928` SOL | `analysis/close_empty_token_accounts.py:93-94` |
| тариф своей транзакции закрытия | `ТАРИФ_ЛАМПОРТЫ = 5_000` (одна подпись) = 0.2 % от возврата | `analysis/bloom_close_on_sell.py:42` |
| порог выгоды | `ПОРОГ_ВЫГОДЫ_ЛАМПОРТЫ = 50_000` (десять тарифов): мусорный счёт на 6 000 лампортов не закрываем | `analysis/bloom_close_on_sell.py:46` |
| ожидание посадки | `ОЖИДАНИЕ_S = 25.0` | `analysis/bloom_close_on_sell.py:47` |
| счетов в пакете уборщика | `В_ПАКЕТЕ = 12`, `--max-batches 6` → 72 счёта за прогон, шесть транзакций по 676 байт | `analysis/close_empty_token_accounts.py:80` |
| цена приоритета пакета | `CU_ЗАКРЫТИЯ = 100_000` × `ЦЕНА_CU_МИКРО = 100_000` / 1e6 = 10 000 лампортов (0.00001 SOL) = 0.055 % от 0.018 SOL возврата пакета | `analysis/close_empty_token_accounts.py:90-91` |
| неприкасаемые минты | `НЕ_ЗАКРЫВАТЬ_МИНТЫ = (WSOL, USDC)` | `analysis/close_empty_token_accounts.py:69` |
| окно молодости | `ЗАПАС_НА_ПРОДАВЦА_S = 600.0`, `ВОЗРАСТ_ПО_УМОЛЧАНИЮ_S = 1800.0` | `analysis/close_empty_token_accounts.py:74-75` |
| пол поштучного правила | `СРОК_СДЕЛКИ_ПОШТУЧНО_S = 600.0` (политики групп дают 643 с, побеждает большее, `:301`) | `analysis/close_empty_token_accounts.py:79` |

USDC — постоянный счёт первой ноги двухшаговой покупки (`usdc_noga_alt.json`),
его закрытие ради 0.00204 SOL уронило бы сборку USDC-ноги на денежном пути
(`analysis/close_empty_token_accounts.py:60-69`).

Расписание: уборщик `*:53` (раз в час, предел 600 с, 512M), строка владельцу
`*-*-* 21:55:00` — за 5 минут до границы суток полосы 22:00Z.

**Что будет, если неправильно (замеры из комментариев).**

* **кошелёк исполнителя вместо полосы**: 27.09 за двадцать минут осталось ПЯТЬ
  незакрытых счетов по 0.00203928 SOL, а в отчёте стояло «не закрыт»
  (`analysis/bloom_seller.py:1653-1656`, `:7838-7841`);
* **общее окно вместо поштучного правила**: 04.10 уборка не сработала НИ РАЗУ —
  записи за 04.10 в `uborshchik_sutki.json` нет вовсе при 89 пустых счетах и
  0.139 SOL ренты (замер 13:51Z); на кошельке полосы 03.10 — **427 счетов и
  0.63 SOL** (`analysis/close_empty_token_accounts.py:287`,
  `deploy/hostovye_tajmery.py:168-190`);
* **без приоритетной цены закрытие НЕ САДИТСЯ**: 05.10 в 00:06 и 00:09Z два
  прогона подряд — первый пакет сел, второй не сел за 30 с ни разу, те же
  18 166 080 лампортов ренты ушли в следующий прогон
  (`analysis/close_empty_token_accounts.py:81-88`);
* **масштаб**: рента новых счетов — 0.62 п.п. на сделку, около **60 %** всех
  издержек полосы и **~0.2 SOL в сутки**, которые ЛЕЖАТ, а не потеряны
  (`deploy/hostovye_tajmery.py:161-164`);
* отказ закрытия НИКОГДА не роняет круг сторожа и не мешает закрытию позиции:
  деньги уже в кошельке (`analysis/bloom_seller.py:1638-1642`, `:2447-2452`);
  текст исключения наружу не идёт — только имя класса (`:1676-1682`).

**Как проверить.** `deploy/checks/renta_pri_prodazhe.py` — достаёт поле
`close_on_sell` из журнала позиций потоком (сколько счетов закрыто, сколько
лампортов вернулось, подпись, причина отказа); двухпроходная склейка по
`client_order_id`, читает и `.gz`-обороты. Самопроверка пути продажи:
`analysis/bloom_seller.py:7790-7850`. Самопроверка уборщика:
`analysis/close_empty_token_accounts.py:913`, `:1144`, `:1165` (цена приоритета
не больше 1 % от ренты пакета). Суточная строка:
`analysis/close_empty_token_accounts.py:1425-1427`.

---

### Грабля 10. Кэш `positions()` и замок — почему это деньги

**В чём грабля.** `ExecState.positions()` переигрывает журнал и кэширует
СОБРАННЫЙ словарь. Годность кэша проверяется не списком точек записи (забытую
точку никто не заметит), а теми самыми объектами, из которых словарь собран:
кэш годен ровно пока `_ряды_позиций()` отдаёт ТОТ ЖЕ список той же длины
(сравнение `is`), а `_ротированные_позиции()` — тот же словарь той же длины.

**Длины для метки снимаются ДО сборки.** Если снять их ПОСЛЕ (как было: `len()`
стоял в самой строке присваивания кэша), дописка чужого потока между концом
слияния и чтением `len()` кладёт словарь из N строк под метку N+1 — и каждый
следующий зов при `len(ряды) == N+1` оказывается ПОПАДАНИЕМ на словарь БЕЗ
последних строк. Бьёт ровно в замок от двойной покупки.

**Где в коде.** `analysis/bloom_exec_state.py:1418` (`positions`), `:1455-1462`
(длины снимаются до сборки — в докстринге), `:1483-1493` (комментарий у самой
метки), `:1494` (`n_ряды, n_рот = len(ряды), len(рот)`), `:1495-1507` (сверка и
присваивание ОДНИМ кортежем), `:1508`
(`return {cid: dict(п) for ...}` — отдаётся копия), `:559` (инициализация
`self._кэш_словаря_позиций`), `:1510` (`_собрать_словарь_позиций`), `:1316-1373`
(`_ряды_позиций`: дочитывание прироста, отпечаток префикса), `:1377`
(`_отпечаток_префикса`, `ОТПЕЧАТОК_БАЙТ = 256`), `:1544` (`open_positions`).

Замок полосы: `analysis/bloom_own_send.py:3534`
(`_ЗАМОК = threading.Lock()`), `:4876` (`with _ЗАМОК:` — ключ операции,
рубильник, пределы полосы и резервирующая запись позиции), `:4897`
(`позиции = состояние.positions()` ВНУТРИ замка), `:7258` (второй зов, подшаг
«разбор и размер»).

**Числа.** Замер на стенде 20 тыс. строк журнала + 5 тыс. ротированных, 1000
сделок, зовы вперемешку в одном процессе: прежний зов **10.884 мс** медианой, с
кэшем **1.731 мс при попадании** и **11.998 мс при промахе**. Дочитывание
прироста 0.02 мс, чтение ротации 0.02 мс — почти всё остальное пересборка.
Промах дороже прежнего на 1.1 мс (цена копии), попадание дешевле на 9.2 мс:
правка окупается при одном попадании на девять промахов.

Зовов `positions()` на один денежный путь полосы — **ДВА**, плюс один на сигнал
в гейте площадки (`can_open_detailed` → `open_positions`). Отдельно кэш РЯДОВ:
разбор 5499 строк — 44.6 мс на `json.loads` и 3.9 мс на сборку словаря (92 %
цены в разборе); росло весь день — 14 мс в 22Z, 136 мс в 16Z. Отпечаток
префикса — голова и хвост по **256** байт.

**Что будет, если неправильно.**

* **метка на строку больше, чем в словаре**: воспроизведено — строка брони лежит
  на диске, есть в свежей сборке, а `positions()` не отдаёт её ни на одном из
  ТРЁХ зовов подряд; `open_positions(lane="own_send")` даёт **6 при семи**. А
  `positions()` зовётся внутри `with _ЗАМОК` рядом с `write_intent` именно
  затем, чтобы два сигнала не увидели оба «открытых 0» — то есть предел «одна
  открытая» перестаёт работать ровно тогда, когда он нужен
  (`analysis/bloom_own_send.py:4870-4876`). Со снятыми ДО сборки длинами ошибка
  возможна только в безопасную сторону: словарь бывает СВЕЖЕЕ метки — лишняя
  пересборка, а не устаревший ответ;
* **присваивание кэша по одному полю вместо кортежа**: потоков в службе много
  (поток на сигнал у полосы, догонялки в пуле потоков), и другой поток увидел бы
  словарь от одних рядов с меткой от других
  (`analysis/bloom_exec_state.py:1500-1506`);
* **годность кэша РЯДОВ по времени файла**: «кэш["время"] == время ИЛИ префикс
  цел» проходило на машине разработчика (mtime в наносекундах), а на хостовом
  бегунке гранулярность грубее — две записи В ОДНУ СЕКУНДУ дают ОДИНАКОВОЕ
  время, «или» замыкалось на нём, и перезапись файла в большую длину читалась
  как дописывание: в ответе оставались прежние сделки, поля новой записи
  терялись. **Деплой 02.10 13:59Z упал на этих трёх проверках: 172/175 на хосте
  против 175/175 локально.** Решение принимает ТОЛЬКО отпечаток префикса, время
  осталось сведением для отчёта;
* **прежняя дыра «ИЛИ размер > смещение»** (замер 02.10, рассвет): полная
  перезапись файла БОЛЬШЕГО размера читалась как дописывание — наружу уходила
  старая позиция, новой не было ни одной.

**Как проверить.** Самопроверка `analysis/bloom_exec_state.py` — «правка
отданной позиции не меняет следующий ответ» (копия поверхностная: внешний
словарь новый, словарь каждой сделки новый); порядок ключей сверяется БАЙТАМИ
(`json.dumps` без `sort_keys`). На хосте:
`deploy/checks/konservaciya_sostojanie.py --json` печатает
`pozicij_v_zhurnale` и `otkrytyh` из одного снимка — расхождение с
`lane_positions()` видно сразу.

**Отдельная грабля внутри граблей.** Номера строк в докстринге
`analysis/bloom_exec_state.py:1425-1427` и в комментарии `:1489-1491`
**УСТАРЕЛИ** (ссылаются на `bloom_own_send.py:7185`, `:4824`, `:4805`, `:4823`,
`:4864`); живые места — `analysis/bloom_own_send.py:7258`, `:4897` и замок
`:4876`. Ссылки в комментариях дрейфуют при каждой правке — **сверять по
именам, а не по номерам**.

---

### Грабля 11. PDA через solders: `PanicException` — НЕ `Exception`

**В чём грабля.** Опора вывода PDA — `solders` (Rust), запас — перебор bump на
Python. За пределами определения Solana `solders` НЕ отказывает, а **ПАНИКУЕТ**:
`pyo3_runtime.PanicException` — подкласс `BaseException`, он пролетает сквозь
все наши `except Exception` и **убивает ПОТОК детектора**. Поэтому ловля вокруг
`find_program_address` обязана быть `except BaseException`. А перебор в тех же
условиях молча отдаёт адрес, **которого цепь не выведет** — слать по нему
нечего. Отсюда: вне пределов — `ОшибкаСборки` с названной границей и значением,
и отказ стоит **ДО** выбора движка.

**Где в коде.** `analysis/c3_pump_sborka.py:67-77` (почему `BaseException`),
`:154` (`na_krivoj` — предикат перебора), `:180` (`_pda_pereborom`), `:203`
(`pda`), `:226-228` (отказ ДО выбора движка), `:229-238` (три проверки предела),
`:239-254` (`solders` + `except BaseException`), `:255` (возврат к перебору),
`:256` (`ata`).

Денежные дороги ловят отказ как `Exception` и кладут строкой в `why_not`:
`analysis/bloom_own_send.py:3375`, `:4383`,
`analysis/c3_prodavec_sborka.py:1049`. Запас живой:
`deploy/checks/pereezdy_po_cepi.py` выводит PDA и о `solders` не знает вовсе;
`c3_pump_sborka.py --self-test` гоняется ОТДЕЛЬНЫМ процессом (паника убивает
поток, а не прогон) — `run_bonding_sim_check_nl.yml:206` (задача `pump_sborka`),
`:255` (`krug_sborki`), `:274` (`otkazy_puti`); процесс этот НЕ голый python3, а
venv прогона, куда `solders` ставится явно (`:171`). В гейте деплоя детектора
этой самопроверки НЕТ вовсе: `run_vps_deploy_bloom_detector_nl.yml` модуль только
ДОСТАВЛЯЕТ (список `:366`, обоснование `:356-360`), а `--self-test` на нём не
зовёт.

**Числа.** Перебор — **0.794 мс** на вывод против **0.0133 мс** у `solders`, **в
60 раз**; у разновидности `buy_exact_quote_in_v3` выводов **девять**. Bump далеко
не всегда 255: по живым наборам 255 у 123 случаев, 254 у 47, и вниз до **246** —
перебор считает sha256 и две степени по модулю 2^255-19 на КАЖДЫЙ bump.

Ловля замерена: **50 паник подряд — `BaseException` поймал 50 из 50**; сквозь
`except Exception` паника НЕ ловится вовсе. Предикат `na_krivoj` не равен
проверке Solana на **26** нарочных значениях (неканонические кодировки y при
y ≥ p и взведённый знаковый бит при x²=0) — все 26 ошибаются в БЕЗОПАСНУЮ
сторону (принимают bump, который Solana отвергла бы); обратного направления не
нашлось на **200 тыс.** случайных значений. Из выхода sha256 такие значения
недостижимы.

**Что будет, если неправильно.**

* `except Exception` вокруг `find_program_address` — **смерть потока
  детектора** (процесс жив, поток без ловли до конца НЕ доходит), а не отказ
  сборки сигнала;
* перебор вместо отказа — молчаливый адрес, которого цепь не выведет:
  транзакция уйдёт в никуда;
* разбор семян ВНУТРИ перебора (как было) — на одноразовом итераторе второй
  виток хешировал НОЛЬ семян и отдавал адрес нуля семян
  (`analysis/c3_pump_sborka.py:184-190`; аннотация `list` этого не держала,
  проверка теперь стоит);
* расширять `try` вокруг зовов `pda` нельзя: в `sobrat_scheta`
  (`analysis/c3_pump_sborka.py:379-383`) `try` значит «счёт ещё не вывелся,
  пробуй следующий проход», и отказ по пределу (он постоянен) выродился бы в
  глухой `WHY_НЕТ_В_КОНТЕКСТЕ` вместо названной причины;
* отдельно: `НУЛЕВОЙ_АДРЕС = "1" * 32` (`analysis/c3_pump_sborka.py:120`) — b58
  от 32 нулей даёт адрес System Program, он НЕПУСТОЙ и ровно 32 байта, проверка
  на пустоту его не берёт; замерено, что `_semja` отдаёт 32 нулевых байта,
  сборка идёт до конца и выводит `creator_vault` на выдуманном хранилище —
  комиссия создателя ушла бы туда.

**Как проверить.** `python3 analysis/c3_pump_sborka.py --self-test` ОТДЕЛЬНЫМ
процессом (равенство обеих дорог по адресу И по bump закреплено самопроверкой; в
строке итога печатается, чем считалось:
`'solders' if _ЕСТЬ_SOLDERS else 'перебор'` — `:1287`). Денежный путь без
`solders` недостижим и так: `analysis/c2_swap_build.py:47-53` импортирует его на
верхнем уровне безусловно.

---

### Грабля 12. Граница семени 32 (предел PDA Solana) — и почему порог `<=`, а не `<`

**В чём грабля.** Предел PDA замерен, а не взят из головы, и состоит из ТРЁХ
условий сразу; за любым из них `solders` паникует, а перебор молча врёт. Порог
длины семени — **НЕ строгий**: на денежном пути есть семя РОВНО 32 байта, и
строгое `<` отказывало бы на нём.

**Где в коде.** `analysis/c3_pump_sborka.py:78-92` (замер границы, 12 случаев),
`:88-92` (почему `<=`), `:93` (`_МАКС_СЕМЯН = 15`), `:94`
(`_МАКС_ДЛИНА_СЕМЕНИ = 32`), `:111` (`WHY_ВНЕ_ПРЕДЕЛА`), `:229-238` (три отказа
с названными границей и значением), `:411-413` (постоянные адреса IDL собираются
заранее: у `fee_config` семена — константы, и он выводится раньше остальных).

**Определение**, замеренное 12 случаями, каждый отдельным подпроцессом,
`solders 0.29.0`:

```
len(b58d(programma)) == 32  И  len(семена) <= 15  И  все len(семя) <= 32
```

* `find_program_address` выводит **15** семян (bump 255) и ПАНИКУЕТ на **16** —
  bump это шестнадцатое семя, а Solana берёт 16 всего;
* семя **ровно 32** байта выводится (bump 254), **33** — паника;
* `program_id` **31** и **33** байта паникуют в самом `Pubkey.from_bytes`
  (`TryFromSliceError`);
* предела на СУММУ семян нет: **15 × 32 = 480** байт выводится, 16 × 32 паникует
  числом семян;
* нижних границ нет: 0 семян и семя 0 байт законны (пустое семя закрыто выше, в
  `_semja`);
* порог `<=`: в IDL **22 константных семени ровно 32 байта**, и среди них
  `fee_config` ГОРЯЧЕЙ разновидности `buy_exact_quote_in_v3`.

**Что будет, если неправильно.** Строгое `<` на длине семени — отказ на
ДЕНЕЖНОМ пути: горячая разновидность `buy_exact_quote_in_v3` не собралась бы ни
разу (её `fee_config` — ровно 32 байта). Отсутствие проверок — паника `solders`
(смерть потока, грабля 11) либо адрес от перебора, которого цепь не выведет.
Слово владельца 11.10: «pda отказывать вне определения Solana — безопасная
сторона: не слать то, что цепь не выведет»
(`analysis/c3_pump_sborka.py:62-65`).

**Как проверить.** `python3 analysis/c3_pump_sborka.py --self-test`: отказы по
пределу НАЗЫВАЮТ границу и значение («семян N, а Solana берёт не больше 15
(bump — шестнадцатое семя)»; «семя N длиной M байт, а Solana берёт не больше
32»; «program_id N байт, а не 32»). Пересчёт границы вручную — 12 подпроцессов
по схеме из `analysis/c3_pump_sborka.py:78-87`.

---

### Грабля 13. Правило 4 (продажа): два пути, порядок по котировке, пол продажи

**В чём грабля.** Выход полосы — ОДНА функция, а не дерево развилок. Решение —
одним сравнением двух чисел: берутся ДВЕ котировки (Jupiter — только котировка,
без подписи; наш пул — по резервам нашей же покупки), идём туда, где БОЛЬШЕ
лампортов, нижняя граница транзакции — **90 % от СВЕЖЕЙ котировки выбранного
пути**.

**Пункт 1 правила — «продаём всегда, когда пришло время выхода, по любой
цене»**, и поэтому Jupiter стоит в списке путей ВСЕГДА, даже без числа
(котировка могла не прийти из-за 429/5xx/упавшей подписи котировочного заказа —
это отсутствие ЧИСЛА, а не отсутствие пути); без числа он идёт ПОСЛЕДНИМ.

Котировки сравниваются по ОЖИДАЕМОМУ выходу (у Jupiter `outAmount`, у пула выход
по резервам до проскальзывания) — сравнивать ожидаемое с гарантированным значило
бы занижать пул на размер чужого проскальзывания.

**Где в коде.** `analysis/bloom_seller.py:637-654` (правило 4 (г) и списание —
живой случай), `:655-663` (редакция владельца 05.10 00:08, пункты 1-5),
`:665-673` (константы), `:676` (`нет_маршрута_у_jup` — одно написание на модуль),
`:3105` (**единственный** зов из горячего пути полосы), `:3139`
(`правило_4_продажа`), `:3176-3195` (котировка под `try` — она не причина не
продать), `:3201-3210` (котировочный заказ Jupiter под `try`), `:3236-3246`
(числа и причины — в запись, оба пути: зов `update_position` `:3232-3238`),
`:3239-3252` (порядок путей: кто больше, тот первый), `:3253-3283` (цикл путей; граница своего пути
`max(90 % котировки, минимум группы)`), `:3336` (`правило_4г_и_списание`),
`:3482` (`_полоса_unsold`), `:3123` (зов 4 (г)), `:3131` (тревога «не
собралась»). Пауза без маршрута — `analysis/bloom_vtoroe_mnenie.py:143`
(`ПОВТОР_БЕЗ_МАРШРУТА_S = 15.0`), читается в
`analysis/bloom_seller.py:2971-2985`.

**Числа и константы** (все через env, ниже — значения по умолчанию):

| константа | env | по умолчанию | где |
|---|---|---|---|
| `ПОЛ_ПРОДАЖИ_PCT` | `BLOOM_LANE_POL_PRODAZHI_PCT` | **90.0** % от свежей котировки | `analysis/bloom_seller.py:665` |
| `ПОВТОР_ПРОДАЖИ_S` | `BLOOM_LANE_POVTOR_PRODAZHI_S` | **15.0** с | `:666` |
| `ПРОСКАЛЬЗЫВАНИЕ_МАКС` | `BLOOM_LANE_SLIPPAGE_MAX` | **0.5** | `:667` |
| `СРОК_НЕПРОДАНА_S` | `BLOOM_LANE_NEPRODANA_S` | **120.0** с → строка в TG (4 (г)) | `:668` |
| `ПОЛ_СПИСАНИЯ_PCT` | `BLOOM_LANE_SPISANIE_POL_PCT` | **5.0** % от входа | `:669` |
| `СРОК_СПИСАНИЯ_S` | `BLOOM_LANE_SPISANIE_SROK_S` | **120.0** с подряд | `:670` |
| `ОПРОС_СПИСАННОЙ_S` | `BLOOM_LANE_SPISANNAYA_OPROS_S` | **3600.0** с (фон) | `:673` |
| `DEFAULT_DUST_RAW` | `BLOOM_DUST_RAW` | **1000** сырых единиц | `:258` |

**Ветки б/г/д.** Признак `sell_own_first` и ветки (а)-(д) **УБРАНЫ** словом
владельца 05.10 00:08; на пути полосы стоит РОВНО ОДИН зов правила
(`_тело_полосы.count("self.правило_4_продажа(") == 1`, самопроверка
`analysis/bloom_seller.py:8751-8757`). Остались строки в TG по меткам УСПЕХА:
`pravilo_4b_prodano` → `_строка_4б_в_tg` (`:2745`), `pravilo_4d_prodano` →
`_строка_4д_в_tg` (`:2753`) — обе посылаются ПОСЛЕ подтверждения по цепи, строк
уйдёт ровно столько, сколько было продаж.

**Списание** (правило 5 того же слова) — ДВА условия сразу: маршрута у Jupiter
нет ВОВСЕ **и** котировка нашего пула < 5 % от входа, и так 120 с подряд;
условие оборвалось — отметка `ts_spisanie_s` СНИМАЕТСЯ (иначе две минуты
набирались бы по кусочкам за сутки).

**Замер из комментария.** Живой счёт прежнего дерева — позиция **D6eyhD2o**
(группа `cand1_03`, вход **0.5 SOL**, куплена 04.10 в 19:52:10Z): Jupiter
отказывал полом по доле от входа (**11.0 %**), наш пул рядом давал **63.4 %**, а
свой путь не пускала дверь круга 0.01 правила 8 (тип 6EF8 не обкатан).
**Полтора часа и 11 отказов подряд при живом покупателе на 0.317 SOL.**
`otkazov_pola 11` к 21:23Z, `unsold_polls 10`, состояние так и осталось
`bought`. Владельцу ушла РОВНО ОДНА строка — «продажа не собралась» в 19:52:42Z.

Если считать пол от входа, а не от свежей котировки, пул с 63.4 % проигрывал бы
Jupiter'у вдвое на ровном месте. Если не ловить исключение котировки —
`zagruzit_idl` бросает вне всякого `try`, и исключение проходило насквозь
правило 4, `handle` и `cycle` до `main`.

**Как проверить.** `deploy/checks/prodat_pravilo4_raz.py` (разовый прогон
правила на хосте). Самопроверки в `analysis/bloom_seller.py:8995-9130`: (а)
правило 4 (г) зовётся из горячего пути ДО тревоги «не собралась»; (б) ему отдают
ОБА числа круга (`доля_пула_pct` и ответ Jupiter); (в) списанная идёт фоновым
кругом; (1) пул 63.4 % + маршрут есть → НЕ списана, но строка 4 (г) всё равно
уходит; (2) рагпул (маршрута нет, пул 3 %) → выдержка, потом списание.

По журналу: поля `pravilo4_pool_lamports`, `pravilo4_jup_lamports`,
`pravilo4_pool_why_not`, `jup_why_not`, `jup_net_marshruta`, `ts_pravilo4`, узел
`правило_4.попытки` с путём/котировкой/границей.

> **Внимание.** Самопроверка (в) зеленит фоновый круг списанной ПО ТЕКСТУ тела,
> а не по достижимости. Фактически эта ветка недостижима — раздел 7, дефект II.

---

### Грабля 14. Коды 6040 / 6042 — отказы по допуску цены

**В чём грабля.** Это НЕ наша ошибка расчёта и не отказ узла: программа пула
отказывает ровно тогда, когда её выход в НАШЕМ слоте оказался меньше нашего
`min_out`. Приходят ПОСЛЕ отправки (транзакция села и упала), поэтому доля
считается от ПОПЫТОК покупки, а не от сигналов. Два кода — по типу пула, и номер
инструкции в раскладке ошибки НЕ ПОСТОЯНЕН: в признак он не идёт.

**Где в коде.** `deploy/checks/otkazy_dopuska_iz_zhurnala.py:43-46` (словарь
кодов), `:92` (`текст_ошибки` — только поля ошибки), `:111` (`код_допуска`,
граница слова), `:130` (`номер_инструкции` — для отчёта, не для признака),
`:137` (`тип_пула`), `:147` (`попытка_покупки`);
`analysis/otkazy_dopuska_po_cepi.py:1-31` (разбор по цепи), `:81`
(`левое_правое_6042`); `analysis/nesevshie_po_cepi.py:104-106`;
`analysis/bloom_exec_state.py:266` и `:2355` (Custom 6040: с кошелька полосы
списано ровно 0.001005 — приоритет плюс тариф); расчёт минимума —
`analysis/c2_swap_build.py:1689` (`min_out_from_reserves`).

**Числа.**

* **6042** — `BuySlippageBelowMinTokensOut`, программа кривой pump.fun
  `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`, инструкция 6;
* **6040** — `BuySlippageBelowMinBaseAmountOut`, Pump AMM
  `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`, инструкция 7 (встречались 7, 7,
  7, 8);
* у LaunchLab кодов выше **6023** нет — отсюда и проверено по цепи, что 6040
  принадлежит Pump AMM;
* граница слова обязательна: **60420 — не 6042**, 6001 — другой код;
* допуск: **35 %** у `cand1_03`, `cand3`, `krug_001` (поля `slippage` в файле
  групп нет — умолчание кода **0.35**), **40 %** у `lane_s0` (своё число 0.4);
* в лог программа печатает `Left: X, Right: Y` — X её выход, Y наш `min_out`.

**Замер** (8 последних отказов, `docs/doklad_2026-10-04_otkazy_dopuska.md`). Эти
сутки полосы: 140 попыток, 6042 — **2/30 = 6.667 %** попыток кривой, 6040 —
**4/102 = 3.922 %** попыток Pump AMM; прошлые сутки: 1/66 = 1.515 % и
2/94 = 2.128 %; вечернее окно с 16:00Z: 18.182 % и 7.143 % (но это 2 случая из
11 и 1 из 14).

Уход цены во всех восьми случаях **БОЛЬШЕ допуска: 35.256 / 35.601 / 43.992 /
45.297 / 46.451 / 48.961 / 50.247 / 68.648 %** при допуске 35 и 40 %. Задержка
«слот источника → наш слот» в самих отказах — **0-2 слота** (1,1,2,1,0,1,1,1),
медиана по суткам **1 слот**, p90 2, max 6 (вчера 7).

**Расхождение пересчитанного ожидания с боевым — 0.000 % во всех восьми, совпало
до единицы.** Вывод прогона: «отказы рыночные, расчёт минимума исправен, ничего
не меняю»; у двух сегодняшних 6042 промах узкий — **0.26 и 0.60 процентных
пункта** сверх допуска.

**Что делать и как проверить.** Допуск — ТОРГОВЫЙ параметр, без слова владельца
НЕ ТРОГАТЬ. Порядок разбора:

1. `deploy/checks/otkazy_dopuska_iz_zhurnala.py` на хосте — выжимка по журналу,
   27/27 самопроверок, окна передаются ФАЙЛОМ (в команде root ни одного знака из
   входов прогона);
2. `analysis/otkazy_dopuska_po_cepi.py` на облачном бегунке — 31/31, числа
   Left/Right из лога, программа инструкции по цепи, пересчёт ожидания ОДНИМ
   боевым путём (`identify_pool` → `pool_program` → `extract_template` →
   `min_out_from_reserves`) от состояния ПОСЛЕ сделки источника; выход
   `data/otkazy_6042_razbor.json`.

**Грабля разбора:** читать транзакции ТОЛЬКО `encoding=jsonParsed` — с
`encoding=json` счета приходят индексами, `c2_pool_programs.pool_program`
программу пула не узнаёт, и прогон 17:27:39Z отдал восемь неразобранных случаев
при живом `pool_vault`.

Если «уход меньше допуска, а отказ был» — это уже дефект расчёта
`min_tokens_out`, и его видно числом.

---

### Грабля 15. Двойная продажа при отказе диска

**В чём грабля.** Оба отправителя сперва ОТПРАВЛЯЮТ, и только потом пишут на
диск (`sendTransaction` → `update_position`/`log`; `JUP.продать` → то же). Отказ
диска (**ENOSPC, EROFS, EIO**) приходит УЖЕ ПОСЛЕ ухода байтов, а ловля в цикле
путей правила 4 читает его как «путь отказал» и идёт на ВТОРОЙ путь — те же
токены уходят в цепь ДВАЖДЫ одним кругом, и строка выглядит как УДАЧНАЯ продажа.

**Защита — два рубежа.** (1) пометка ухода в сеть В ПАМЯТИ, выставляемая ДО
всякого обращения к диску (в этот момент память — единственное, что ещё
работает); (2) проверка этой пометки в `except` цикла путей: если байты уже
ушли, второй путь НЕ пробуется вовсе. Прежняя защита «пауза 45 с» здесь не
стоит: её отметку `ts_jup_attempt` пишет ровно та запись, которая и отказала.

**Где в коде.** `analysis/bloom_seller.py:1822` (`_пометить_уход_в_сеть`,
докстринг `:1823-1832`), `:1833` (`self._ушло_в_сеть = {...}`), `:1838`
(`_записать_после_отправки` — каждый зов под своим `try`, тревога НЕ через
диск), `:3258` (`self._ушло_в_сеть = None` — снимается ПЕРЕД каждой попыткой,
иначе чужая пометка запретила бы второй путь навсегда), `:3284-3320` (**ВТОРОЙ
РУБЕЖ** в `except` цикла путей: `упал_после_отправки=True`, `why_not` начинается
с «ОТПРАВЛЕНО, НО НЕ ЗАПИСАНО», ранний `return из_`), `:1859` (та же тревога из
`_записать_после_отправки`).

Места постановки пометки: `:4215` (свой путь в пул — сразу после
`sendTransaction`, ДО `update_position`/`log`), `:4989` (Jupiter — **ТОЛЬКО при
подписи**: ответ без транзакции — как раз случай, когда повторить НУЖНО).

**Числа.** Замер на поставленном коде: **отправок в цепь 2 вместо 1 — в обе
стороны** (и когда первым шёл свой путь, и когда Jupiter). Нашёл противник 11.10
в уже готовой правке.

Самопроверки: `analysis/bloom_seller.py:9500-9560` (пять кругов падения → одна
тревога), `:9560-9578` — текстом по НАСТОЯЩИМ телам `продать_своим_в_пул` и
`продать_через_jupiter`: пометка стоит ДО первого обращения к диску
(`index("_пометить_уход_в_сеть") < index("_записать_после_отправки")`) и ПОСЛЕ
отправки нет ни одной голой `self.state.update_position(` — всё под своим `try`
у помощника. Шесть мест ловли сторожатся текстом: в `cycle` — `≥ 5` блоков
`except Exception as сбой` плюс признак жизни под `try`, в правиле 4 — `≥ 3`
(`:9397-9408`).

**Что будет, если неправильно.**

* продажа тех же токенов дважды: вторая транзакция либо упадёт, спалив
  приоритет, либо продаст то, чего уже нет, а в журнале окажется «удачная
  продажа»;
* если тревогу послать не своим каналом — тревога идёт НЕ через диск, и при
  мёртвом диске это единственный живой канал
  (`analysis/bloom_seller.py:1842-1846`);
* если `_строка_падения` позовёт `_тревога_продажа_не_собралась` — одно падение
  сожжёт ОДИН ВЫСТРЕЛ НА ПОЗИЦИЮ ЗА ВСЮ ЕЁ ЖИЗНЬ (метка
  `alarm_sell_failed_sent`), и НАСТОЯЩИЙ отказ обоих путей после него до
  владельца не дойдёт (замерено: пять кругов падения → одна тревога, потом
  настоящий отказ → `skipped`).

**Как проверить.** Текст тревоги в TG: **«ОТПРАВЛЕНО, НО НЕ ЗАПИСАНО»** с путём
и первыми 12 знаками подписи — искать её в журнале оповещателя и в
`seller.jsonl`. В записи позиции: `упал_после_отправки: true`, `signature` при
`ok: false`, `отправлено_не_записано` / `не_записано`. В строке правила 4:
`попытки[].упал_после_отправки`. Самопроверка —
`python3 analysis/bloom_seller.py --self-test`, блоки «ОТКАЗ ПОСЛЕ ОТПРАВКИ: НЕ
ПРОДАТЬ ТЕ ЖЕ ТОКЕНЫ ДВАЖДЫ» (`:9409`+) и «ПАДЕНИЕ НЕ ЖЖЁТ ОДНОРАЗОВУЮ ТРЕВОГУ»
(`:9580`+).

---

### Грабля 16. Три незакрытых дефекта

Три известных дефекта вынесены ОТДЕЛЬНЫМ разделом ниже, потому что они НЕ
чинятся: списание упавшей позиции, состояние `spisana` вне круга и вне гейта,
зовы без ловли в `handle` и `обработать_полосу`. См. раздел 7.

---

## 7. ИЗВЕСТНЫЕ НЕЗАКРЫТЫЕ ДЕФЕКТЫ

> **Все три — известные дефекты. НЕ ЧИНЯТСЯ ПО СЛОВУ ВЛАДЕЛЬЦА ОТ 12.10, ПРОЕКТ
> КОНСЕРВИРУЕТСЯ.** Правок в код не внесено. Ниже — точные места, чем грозит и
> как заметить. Тот, кто поднимет систему заново, должен знать о них ДО первой
> живой сделки.

---

### Дефект I. Упавшая позиция не доходит до `правило_4г_и_списание`

**Место в коде.** Позиция, покупка которой не села (`lane_bought_raw` так и не
появился, остаток минта по цепи ноль), уходит в ветку «продавать нечего» и
закрывается состоянием `closed` ЗАДОЛГО до зова правила 4 (г): он стоит в самом
конце метода, за `return`'ом этой ветки.

* ветка: `analysis/bloom_seller.py:2907` (`if количество < self.dust_raw:`),
  `:2908-2920` (подтверждение нуля на finalized — ТОЛЬКО если `куплено > 0`),
  `:2925-2934`
  (`update_position(cid, state=STATE_CLOSED, ts_closed=now,
  closed_reason="продавать нечего: остаток минта 0 меньше порога крошки 1000")`
  — **без `closed_sol_net`**), `:2939-2946` (тревога «закрыта без продажи» —
  тоже ТОЛЬКО если `куплено > 0`), `:2947-2948` (`log` + `return`);
* правило 4 (г) недостижимо из этой ветки: зов стоит на
  `analysis/bloom_seller.py:3123`, то есть на **176 строк НИЖЕ** `return`'а;
* суточный счёт: `analysis/bloom_exec_state.py:1646` (`_учесть_закрытие`),
  `:1653` (`if state != STATE_CLOSED: return {}`), `:1665-1672`
  (`чисто = closed_sol_net` из полей → из прежней записи → из
  `last_sell_outcome.sol_delta_net`; **`if чисто is None: return {}`**), `:1698`
  (единственный в службе `add_pnl`);
* счётчик: `analysis/bloom_exec_state.py:1808` (`note_sell_outcome`), `:1821`
  (`c["unsold_streak"] = 0 if sold else +1`), `:1944-1946` (автопауза покупок по
  стрику), `:606`
  (`self.unsold_streak_max = env_int("BLOOM_UNSOLD_STREAK", DEFAULT_UNSOLD_STREAK)`),
  `:57` (`DEFAULT_UNSOLD_STREAK = 3`).

**Следствия.** Позиция не становится `unsold`, не списывается,
`note_sell_outcome` не зовётся ВОВСЕ, `unsold_streak` остаётся 0, и убыток в
суточный счёт НЕ попадает — `_учесть_закрытие` требует непустой
`closed_sol_net`, а эта ветка его не пишет.

**Константы.** `DEFAULT_DUST_RAW = 1000` (`analysis/bloom_seller.py:258`, env
`BLOOM_DUST_RAW`); `DEFAULT_UNSOLD_STREAK = 3`
(`analysis/bloom_exec_state.py:57`, env `BLOOM_UNSOLD_STREAK`);
`BLOOM_BLOCK_POS_TRIES = 3` (предел попыток чтения количества,
`analysis/bloom_seller.py:2787-2788`); `ЗАПАС_ЧТЕНИЯ_КОЛИЧЕСТВА_S`
(`analysis/bloom_seller.py:215`); `КОД_ПАУЗА_НЕПРОДАНО` — код отказа гейта по
стрику.

**Чем грозит.** Расход (вход + приоритет + тариф) состоялся, а в
`pnl_<день>.json` его нет: **суточный стоп по убытку** (`КОД_ДНЕВНОЙ_УБЫТОК`,
env `BLOOM_DAILY_LOSS_SOL` / `LANE_DAILY_LOSS_SOL`) такую серию НЕ ВИДИТ.
**Автопауза покупок по непроданным** тоже не сработает: при пороге 3 счётчик
`unsold_streak` остаётся 0 сколько бы упавших покупок ни было подряд.

Исторический масштаб той же болезни замерен в соседней ветке: **28.09 пять пар
вывели из суточного счёта 0.794226 SOL, и стоп 0.45 их не увидел вовсе**
(`analysis/bloom_seller.py:2933-2938`).

Тревога «закрыта без продажи» здесь тоже молчит — она под условием
`куплено > 0`, а у упавшей покупки `lane_bought_raw` пуст.

**Как заметить.** В `positions.jsonl` искать `state=closed` с `closed_reason`,
начинающимся на «продавать нечего», у которых НЕТ `closed_sol_net` и НЕТ
`pnl_counted` — готовая команда в разделе 5.6. Сверка: сумма `sol_in` таких
позиций за сутки против `pnl_<день>.json.spent_sol` — расхождение и есть
невидимый суточному стопу убыток. Счётчик:
`ExecState.counters()["unsold_streak"]` при живых упавших покупках стоит 0
(самопроверки, фиксирующие именно это поведение:
`analysis/bloom_seller.py:5520`, `:5724`, `:7756`). Снимок:
`deploy/checks/konservaciya_sostojanie.py --json` → `po_sostojanijam`.

---

### Дефект II. Состояние `spisana` вне круга и вне гейта

**Место в коде.** `STATES_OPEN` не содержит `STATE_SPISANA`. Из этого ДВА
следствия:

1. `cycle()` перебирает `open_positions()`, значит списанная позиция в `handle`
   → `обработать_полосу` **не попадает НИКОГДА** — обещанный фоновый опрос раз в
   час физически недостижим, хотя ветка под него написана и самопроверкой
   закрыта;
2. гейт «нуль позиций» списанную НЕ ВИДИТ: `open=0`, `stuck=0`, а токен лежит на
   кошельке полосы.

Точные места:

* `analysis/bloom_exec_state.py:86` —
  `STATES_OPEN = ("intent", "bought", "selling", "unsold")` (`spisana`
  ОТСУТСТВУЕТ);
* `analysis/bloom_exec_state.py:180` — `STATE_SPISANA = "spisana"`, обоснование
  `:168-179`;
* `analysis/bloom_exec_state.py:1544` — `open_positions`:
  `[p for p in self.positions().values() if p.get("state") in STATES_OPEN]`;
* `analysis/bloom_seller.py:5288` — `cycle`:
  `открытые = self.state.open_positions()`, далее `:5311-5315` и `:5337-5339` —
  только эти позиции попадают в `handle`;
* **МЁРТВАЯ ВЕТКА:** `analysis/bloom_seller.py:2690-2704` —
  `if pos.get("state") == STATE_SPISANA:` с паузой `ОПРОС_СПИСАННОЙ_S` и записью
  `ts_spisannaya_opros`; `analysis/bloom_seller.py:673` —
  `ОПРОС_СПИСАННОЙ_S = env_float("BLOOM_LANE_SPISANNAYA_OPROS_S", 3600.0)`;
  самопроверка, которая её «зеленит» по ТЕКСТУ тела, а не по достижимости:
  `analysis/bloom_seller.py:9013`;
* постановка состояния: `analysis/bloom_seller.py:3421-3428`
  (`state=STATE_SPISANA`, `spisana_since`, `spisana_reason`,
  `closed_sol_net=0.0`, `spisana_ostatok_raw`, `ts_spisanie_s=None`);
* дефект зафиксирован в прогоне снимка:
  `deploy/checks/konservaciya_sostojanie.py:9-15` («STATES_OPEN не содержит
  STATE_SPISANA … гейт "нуль позиций" её не видит: open=0, stuck=0 … это
  известный незакрытый дефект»), `:55` (`spisana_v_STATES_OPEN`), `:77-96`
  (списанные СВОИМ проходом по журналу), `:97-99` (`minty_spisannyh`);
* уборщик намеренно считает списанную свободной:
  `analysis/close_empty_token_accounts.py:220-224` («Списанная (spisana) и
  закрытая сюда НЕ входят нарочно: деньги по ним уже сочтены»).

**Константы.** `STATES_OPEN` — 4 элемента; `STATE_SPISANA = "spisana"`;
`ОПРОС_СПИСАННОЙ_S = 3600.0` с (env `BLOOM_LANE_SPISANNAYA_OPROS_S`);
`ПОЛ_СПИСАНИЯ_PCT = 5.0` %; `СРОК_СПИСАНИЯ_S = 120.0` с;
`closed_sol_net = 0.0` (полный убыток числом в записи).

**Чем грозит.**

1. Обещание «продажу пробуем в фоне, вдруг пул оживёт» НЕ ИСПОЛНЯЕТСЯ: пул может
   ожить, а запроса не будет ни одного — токен лежит мёртвым грузом, а в тревоге
   владельцу написано обратное (`analysis/bloom_seller.py:3432-3438`).
2. **Перед консервацией гейт «позиций 0» скажет «можно», хотя на кошельке
   полосы лежат токены списанных позиций** — именно поэтому список списанных
   снимается ОТДЕЛЬНЫМ проходом (раздел 5.3).
3. Производное наблюдение из того же места, тоже НЕ чинится: `_учесть_закрытие`
   книжит только `state == STATE_CLOSED`
   (`analysis/bloom_exec_state.py:1653`), поэтому `closed_sol_net = 0.0`,
   записанный при списании, в `pnl_<день>.json` НЕ попадает, и обещание
   докстринга «полный убыток … для суточного стопа»
   (`analysis/bloom_seller.py:3364-3367`) суточным стопом не подхватывается.

**Как заметить.**

```bash
# скрипта на хосте НЕТ (в список доставки деплоя он не входит) -- он уезжает
# в /tmp прогоном run_konservaciya_ostanovka_nl.yml (:146-147) или копией руками
cd /home/bot/bloom_executor
PYTHONPATH=/home/bot/bloom_executor BLOOM_STATE_DIR=/home/bot/bloom_executor_live_data \
  /home/bot/robinhood-chain-alpha/venv/bin/python /tmp/konservaciya_sostojanie.py
```

печатает `STATES_OPEN = [...]; STATE_SPISANA = 'spisana'; списана входит в
открытые: False` и затем `--- СПИСАННЫЕ: N ---` с `client_order_id`, `mint`,
`lane_bought_raw`, `spisana_since`, `ts_spisannaya_opros`.

**Признак мёртвой ветки:** у списанных позиций поле **`ts_spisannaya_opros`
НИКОГДА не обновляется после момента списания** — оно пишется только из
недостижимой ветки `analysis/bloom_seller.py:2703`.

Токены искать на кошельке полосы по `minty_spisannyh` и `lane_bought_raw`.

---

### Дефект III. Зовы без ловли в `handle` и `обработать_полосу`

**Место в коде.** В `handle` НЕТ НИ ОДНОГО блока `try`, в `обработать_полосу` их
ДВА на почти 500 строк. Все остальные обращения к диску, к узлу, к оповещателю и
к модулям путей — голые. Падение любого из них ловится только обёрткой в `cycle`
(`_строка_падения`), то есть позиция теряет ВЕСЬ оставшийся круг: всё, что стояло
ниже точки падения, в этом круге не выполнится. Обёртка в `cycle` — правка 11.10,
она спасла партию от одного `raise`, но внутренние зовы под неё не разложены.

**Разбор AST по рабочему файлу** (счёт `ast.Try` внутри `FunctionDef`):

| метод | строки | `try`-блоков | непокрытых зовов |
|---|---|---|---|
| `handle` | `analysis/bloom_seller.py:2370-2598` | **0** | **28** |
| `обработать_полосу` | `analysis/bloom_seller.py:2652-3137` | **2** (`:2726` — `дописать_итог_по_цепи`; `:3072`) | **37** |
| `cycle` (для сравнения — как сделано ПРАВИЛЬНО) | `analysis/bloom_seller.py:5281-5377` | **7** (позиции, порядок, две партии, две тревоги, признак жизни) | — |

Непокрытые зовы в `handle`, в том числе денежные: `:2391` `balance_reader`,
`:2400`/`:2433`/`:2457`/`:2462`/`:2465`/`:2540`/`:2589` `state.update_position`,
`:2415` `доложить_прошлую_попытку`, `:2432` `доложить_закрытие`,
`:2444`/`:2543` `state.note_sell_outcome`, `:2453`
`закрыть_счёт_после_продажи`, `:2476`/`:2551` `оповещатель.послать`, `:2504`
`продать_своим_первым`, `:2512` `продать_через_jupiter`, `:2526`
`правило_4_второе_мнение`, `:2556` `telegram`, `:2573` `api.swap`,
`:2405`/`:2477`/`:2554`/`:2597` `log`.

Непокрытые зовы в `обработать_полосу`, в том числе: `:2710`
`доложить_прошлую_попытку`, `:2722` `догнать_натив_покупки`, `:2734`
`update_position`, `:2738` `note_sell_outcome`, `:2745`/`:2753`
`_строка_4б_в_tg`/`_строка_4д_в_tg`, `:2764` `оповещатель.послать`, `:2830`
`balance_reader`, `:2850` `_дождаться_остатка`, `:2884`/`:2914`
`_ноль_подтверждён`, `:2940` `_тревога`, `:2978` `тревога_без_маршрута`,
`:3051` `продать_своим_двухшаговым`, **`:3105` `правило_4_продажа`**,
**`:3123` `правило_4г_и_списание`**, `:3131`
`_тревога_продажа_не_собралась`, `:3132` `_почему_свой_путь_не_пошёл`.

Внутри самого правила 4 ловля есть: **четыре** `try` — у
`правило_4_продажа` (`analysis/bloom_seller.py:3139-3334`) они на `:3188`,
`:3201`, `:3259`, `:3307`; у `правило_4г_и_списание` (`:3336-3463`) — один, на
`:3429`. Счёт тем же разбором AST, что и таблица выше.

**Числа.** `handle`: 0 `try` / 28 голых зовов на 229 строк.
`обработать_полосу`: 2 `try` / 37 голых зовов на 486 строк. Сторож круга:
`БЮДЖЕТ_ХВОСТА_КРУГА_S = 2.0` с (`analysis/bloom_seller.py:234`), пауза круга
`15.0` с; комментарий `:5304-5309`: «цена обёртки — единицы наносекунд на
позицию … ценой не является».

**Чем грозит.** Падение, например, `balance_reader`
(`analysis/bloom_seller.py:2830`) или `update_position` (`:2734`) обрывает круг
позиции ДО зова правила 4 (`:3105`) и правила 4 (г) (`:3123`) — то есть в этом
круге **позиция не продаётся** и строка владельцу не уходит, а в сводке стоит
одна строка падения. Обёртка `cycle` честно пишет `obrabotka_upala`,
`pozicij_upalo` и `upavshie_why_not[0:10]` в признак жизни (`:5347-5356`), но
сама продажа в этом круге потеряна.

Комментарий в `cycle` говорит прямо: «выше по стеку `try` не было ни в одном
звене, первая ловля — `main()`» (`:5303-5309`); после правки 11.10 первая ловля
— `cycle`, а звенья внутри `handle` так и остались голыми.

**Как заметить.** В сводке круга и в признаке жизни: поля `pozicij_upalo`,
`upavshie_why_not` (до 10 записей с `client_order_id`, `mint`, `why_not`). В
`seller.jsonl` — строки с `obrabotka_upala: true` от `_строка_падения` (там же
тревога с троттлингом по `self._жалобы`, ОТДЕЛЬНАЯ от
`_тревога_продажа_не_собралась`, чтобы падение не сожгло единственный выстрел).

Структурно — пересчитать своими руками:

```bash
cd /home/bot/bloom_executor
python3 - <<'EOF'
import ast
t = ast.parse(open('bloom_seller.py').read())
for n in ast.walk(t):
    if isinstance(n, ast.FunctionDef) and n.name in ('handle', 'обработать_полосу', 'cycle'):
        print(n.name, 'строки', n.lineno, '-', n.end_lineno,
              'try:', sum(1 for x in ast.walk(n) if isinstance(x, ast.Try)))
EOF
# ожидается: handle 0, обработать_полосу 2, cycle 7
```

---

```
ЧТО НЕ ДЕЛАЛОСЬ при составлении этого документа: код не правился, денежный путь
не тронут, SSH к хостам не использовался -- всё взято чтением ветки
claude/nifty-sagan-r0polg. Значений ключей, сидов и секретов в документе нет;
про env и секреты -- только ИМЕНА и назначение.
```
