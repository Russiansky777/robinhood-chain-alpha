# Зонд deshred против Helius WS: запуск на хосте NL (для Code-1)

Зонд `analysis/podbivka_zond_deshred.py` (ветка claude/podbivka) **только слушает**: ничего не подписывает, не отправляет,
денег и ключей кошельков не касается. Два потока в одном процессе: Triton gRPC `SubscribeDeshred` и Helius WS
`logsSubscribe` (processed) на одних и тех же адресах источников. По каждой транзакции -- время прихода по обоим
каналам на стенных и монотонных часах хоста.

## Что положить на хост

- `analysis/podbivka_zond_deshred.py` с ветки claude/podbivka (коммит этой страницы или новее).
- `analysis/proto/geyser_pb2.py`, `geyser_pb2_grpc.py`, `solana_storage_pb2.py`, `solana_storage_pb2_grpc.py` --
  **уже есть на ветке Code-1, байт в байт те же** (SubscribeDeshred в них есть). Зонд ищет их в `analysis/proto`
  рядом с собой или в `GEYSER_PROTO_DIR`.
- `data/leader_regions.json` ветки Code-1 (карта лидер → регион; сейчас эпоха 1045). Путь -- `--leader-regions` или
  `LEADER_REGIONS_FILE`.
- Адреса: `data/podbivka/zond_deshred/adresa_istochnikov.txt` (34 адреса: группы Code-1 + кандидаты + снайперы из
  `data/podbivka/istochniki_code1_kandidaty.json`) -- или `--iz-grupp` с модулем `bloom_source_groups` на хосте.
- Python-пакеты: `grpcio`, `protobuf` (как у зонда преконфов). WS -- на стандартной библиотеке.

## Переменные окружения

| переменная | что | обязательна |
|---|---|---|
| `TRITON_DESHRED_URL` (или `_FILE`) | точка Triton Shred Streaming (https://…; путь в адресе отбрасывается, берётся хост:порт) | да |
| `TRITON_DESHRED_TOKEN` (или `_FILE`) | ключ Triton, уходит в метаданные `x-token`; нет его -- берётся `TRITON_X_TOKEN` | да |
| `HELIUS_API_KEY2` (или `_FILE`) | ключ Helius для WS и расписания лидеров; нет его -- `HELIUS_API_KEY` | да |
| `TRITON_STATE_DIR` | каталог кэша расписаний лидеров (или `--state-dir`) | желательно |

Значения ключей зонд не печатает и не пишет в журнал: только «задан» и длину; тексты ошибок проходят вычистку.

## Команда (час теста)

```
python3 analysis/podbivka_zond_deshred.py --self-test          # без сети; должно быть «всё сошлось (0 ошибок)»

python3 analysis/podbivka_zond_deshred.py \
    --accounts-file data/podbivka/zond_deshred/adresa_istochnikov.txt \
    --leader-regions data/leader_regions.json \
    --seconds 3600 \
    --out deshred_<stamp>.jsonl \
    --status status_deshred.json \
    --state-dir <каталог>
```

Первая строка вывода -- проверка: `triton_key.ok`, `helius_key.ok`, `triton_host`, `leaders_in_map`; если чего-то нет --
`СБОЙ: …` и выход с кодом 1, потоки не открываются. Нагрузка на Helius: 34 подписки WS (не быстрее 8 в секунду при
старте, `--ws-temp`), одно соединение WS, `getSlot` + `getLeaderSchedule` раз в эпоху.

## Что пишет

- `--out` -- журнал JSONL, одна строка на событие: `feed` (`deshred` / `helius_ws`), `kind` (`transaction`, `subscribed`,
  `stream_error`, `reconnected`, `ping`…), `t_recv` (стенные часы), `t_mono` (монотонные), `utc`, `signature`, `slot`,
  у шредов -- `matched` (какой из наших адресов поймал фильтр), у WS -- `err` (упала ли транзакция, да/нет) и адрес подписки. Разрывы и
  переподключения пишутся строками, не молча.
- `--status` -- признак жизни раз в 30 с: число событий по каналам, секунд до конца, ошибки расписания,
  `finished: true` в конце.
- В stdout в конце -- тот же признак и `stopped_why`.

## Как погасить

- Сам: через `--seconds` (3600) -- закрывает потоки, дописывает признак с `finished: true`, код выхода 0.
- Раньше срока: `SIGTERM` или `Ctrl-C` (SIGINT) -- то же аккуратное закрытие (потоки, журнал, признак).
- Код 1 и `СБОЙ: <причина>` -- Triton отказал по правам, пределу или аргументу (причина в `stopped_why`, без ключей).
  Обрыв связи зонд не гасит: пишет `stream_error`, переподключается и пишет `reconnected`.

## Объём журнала за час

Оценка снизу по цепи: у этих 34 адресов за 28–29.09 не меньше 18 175 подписей за 48 ч (≈ 380 в час; два кошелька --
6qudAN2k и 7JVQMwRj -- упёрлись в предел выборки 3 000, у них больше). На транзакцию -- 2 строки (по строке на канал),
строка ≈ 370 байт (по образцу `data/podbivka/zond_deshred/obrazec.jsonl`). Итого **от ~0.3 МБ за час**; шреды приходят
и по транзакциям, которые потом упадут, так что строк deshred может быть больше, чем WS. Сжатый gzip -- в разы меньше.

## Что вернуть в репозиторий

Журнал часа (`deshred_<stamp>.jsonl`, при > 50 МБ -- `.jsonl.gz`), `status_deshred.json` и файлы расписаний из
`--state-dir` -- коммитом на ветку Code-1 (например `data/zond_deshred/`). Свод (p50/p90 опережения, доля «шреды
раньше», ложные и пропущенные, по регионам и по лидерам BAM / Harmonic против остальных) Code-2 считает сам, по
журналу; на хосте свод запускать не нужно.
