# Полный бэкап торгового хоста -- манифест

Хост 45.59.170.76, снято прогоном run_backup_hosta_polnyy.yml
2026-10-12T00:56:26Z.

| поле | значение |
|---|---|
| архив | `host_backup_full.tar.gz` |
| размер | 1211619501 байт (1155 МБ) |
| sha256 архива | `b3acdad1087644d659c6945ad493299a7973b15aeaf93b39d82d4e3a32b1ee98` |
| частей по 95 МБ | 13 |
| артефакт | host-backup-full, retention 90 дней |
| ветка частей | backup/host-2026-10-12 |
| прогон | https://github.com/Russiansky777/robinhood-chain-alpha/actions/runs/38189721929 |
| коммит ветки на момент снятия | d4c9be557c290009c9ec68f25eca01c2c67f650c |

## Что внутри

| путь в архиве | что это |
|---|---|
| `home/bot/bloom_executor/` | код на хосте (как он есть, доставленный деплоем) |
| `home/bot/bloom_executor_live_data/` | состояние: журнал решений `decisions.jsonl`, журнал позиций `positions.jsonl`, журнал сторожа продаж `seller.jsonl`, тревоги `trevogi.jsonl`, признаки жизни `detector_status.json` и `seller_heartbeat.json`, учёт Helius `helius_usage/` |
| `home/bot/data/` | файл групп `sources_2026-09-25.json` и ВСЕ его `.bak-*`, реестр отправителей `senders.json` (там ИМЕНА секретов, не значения), IDL, образцы пулов |
| `home/bot/host_tajmery/` | результаты хостовых таймеров: учёт, живучесть, суточные выгрузки `vygruzki/sdelki_polosy_*.json` |
| `tmp/backup_meta/systemd/` | юниты и таймеры systemd целиком, `list-timers`, состояние служб |
| `tmp/backup_meta/crontab-*.txt` | crontab root и bot |
| `tmp/backup_meta/env_IMENA_tolko.txt` | окружение хоста -- ТОЛЬКО ИМЕНА переменных |
| `tmp/backup_meta/publichnye_adresa.txt` | nonce-счета и адреса получателей из окружения (каждый сверен по цепи: счёт существует) |
| `tmp/backup_meta/publichnye_koshelki.txt` | публичные адреса кошельков -- из констант кода, а не из окружения |
| `tmp/backup_meta/telegram_IMENA.txt` | конфиг Telegram -- только имена переменных |
| `tmp/backup_meta/pip_freeze_*.txt`, `uname.txt`, `os-release.txt`, `nproc.txt`, `df.txt` | система |
| `tmp/backup_meta/konfigi/` | конфиги служб |
| `tmp/backup_meta/prava_katalogov.txt` | владелец и права каталогов -- без них восстановленный хост не поднимется |

Рядом с архивом, отдельными файлами прогона:
`diff_hosta_protiv_vetki.txt` (код хоста против ветки),
`zamer_do.txt` (размеры и место на хосте до упаковки),
`arhiv_proverka.txt` (проверка, что ключей внутри нет).

## Чего в архиве НЕТ намеренно

Файлов окружения с значениями (`/etc/bloom-executor/env*`),
любых `*.key`, `*.pem`, `id_*`, кошельковых json, `*secret*`,
`*keypair*`. Это слово владельца, а не осторожность:
"Ключи и секреты -- никогда; env -- только именами".

## Как восстановить

Из артефакта:

```bash
echo 'b3acdad1087644d659c6945ad493299a7973b15aeaf93b39d82d4e3a32b1ee98  host_backup_full.tar.gz' | sha256sum -c -
tar xzf host_backup_full.tar.gz -C /
# затем права из tmp/backup_meta/prava_katalogov.txt,
# затем systemctl daemon-reload и enable нужных юнитов,
# затем /etc/bloom-executor/env завести ЗАНОВО по именам из
# tmp/backup_meta/env_IMENA_tolko.txt -- значения в бэкапе
# отсутствуют намеренно, их берут из docs/RAZVERTYVANIE_S_NULYA.md
```

Из частей в ветке:

```bash
git clone -b backup/host-2026-10-12 <origin> backup && cd backup/backup
sha256sum -c chasti_sha256.txt      # все части целы
cat chast_* > host_backup_full.tar.gz
echo 'b3acdad1087644d659c6945ad493299a7973b15aeaf93b39d82d4e3a32b1ee98  host_backup_full.tar.gz' | sha256sum -c -
```

## sha256 частей

```
012375a303b2b277e6292072b6b752a6a1d2740de5b792d8afe596e7944dea55  chast_000
e80ce4b06310b8e845b3fb4604f1e26a45f62841ca68083dfb2f7680edc23eb5  chast_001
378ea83982dafe521b62a8e13397e5fe70c18022b272049b14e3aa8cf9fbf0b7  chast_002
caa5792113056743b0887985e8a71db263e3d67a85e5d7ed88e7e4c4238dbb70  chast_003
ef6e36013f5974c823c4e25b26218e3b7e3a15dfeb13651493273b03d9253913  chast_004
b9ff47ab3db5e08e2dab3c7f0ea6a13511ee1fc518d08a875321b96bceebc2e2  chast_005
8ec733ca8370ff8d7663a774adffa4f83db0e882e38010239bc40b64e946a5ba  chast_006
2813844edb87561e4f7ef60db0decd980dbba690bde56a6cae983fdd27ef7995  chast_007
447af419873655894a309ee389ed0ac3b88d808e8eba7d7109c74ca868032776  chast_008
d44fc16a3c23afa653b85117e8a3d413289f961202e2b1abd650a09a2038e3ac  chast_009
d85053bf33ac00130efd9062a8406102717cd5c17f720a15d3e24b4d0667d4fe  chast_010
963105972be6894d76dc360e1a7a13fe0b7a5e3c3febea1e6be8904bbc310478  chast_011
92a77162f44682dbf8a56b3badc6de151b954c69eed56a81ac68b6c51ae3d895  chast_012
```
