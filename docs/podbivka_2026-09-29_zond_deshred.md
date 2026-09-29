# Зонд «шреды против WS по транзакции» -- код для хоста NL (ставит Code-1)

Код: `analysis/podbivka_zond_deshred.py` (ветка claude/podbivka). Только чтение: ничего не отправляет и не подписывает.

**Каналы.** gRPC `SubscribeDeshred` у Triton (analysis/proto/geyser.proto, фильтр `deshred_transactions.src`: `vote=false`,
`account_include` = адреса источников; ключ -- метаданные `x-token`; на ping сервера зонд отвечает ping) и Helius WS
`logsSubscribe` (`mentions` -- один адрес на подписку, `processed`) на тех же адресах. WS -- своя реализация на стандартной
библиотеке, лишних пакетов не нужно: хватает venv зонда преконфов (grpcio, protobuf).

**Строка журнала** -- форма зонда преконфов: `feed` (deshred / helius_ws), `region`, `kind`, `t_recv` (стенные), `t_mono`,
`utc`, `signature`, `slot`, `leader`, `leader_region`, `region_group`; у шредов `matched` (наши адреса в транзакции),
`created_at` (время сервера), у WS `address`, `err`. Плюс `stream_error` / `reconnected` / `subscribed` / `subscribe_rejected`.

**Лидер и регион.** getLeaderSchedule (Helius) раз на эпоху, файлом в `--state-dir`; регион -- карта Code-1
`data/leader_regions.json` (поле `по_лидеру`; ищется по `--leader-regions`, `LEADER_REGIONS_FILE`, корню кода и каталогам
PYTHONPATH). EU / US-East / US-West / Asia / прочее → EU / US / Asia / прочее; лидера нет в карте -- «неизвестно».

**Свод** (`--report-log`): первый приход подписи по каждому каналу; разность = t(WS) − t(шреды) по стенным часам, мс,
плюс -- шреды раньше. p50 / p90 / p10, доля «шреды раньше» -- по всем и по регионам лидера; только шреды / только WS;
наибольшее расхождение стенных и моно-часов (скачок NTP); задержка от `created_at` сервера до прихода.

**Запуск** (те же файлы ключей, что у зонда преконфов; значения не печатаются, ошибки проходят вычистку):

```
TRITON_X_TOKEN_FILE=$ZOND_DIR/token HELIUS_API_KEY_FILE=$ZOND_DIR/helius_key TRITON_DESHRED_URL=<точка Triton> \
PYTHONPATH=$KOD_HOSTA:$ZOND_DIR $VENV/bin/python podbivka_zond_deshred.py --accounts-file $ZOND_DIR/accounts.txt \
  --seconds 3600 --out $ZOND_DIR/deshred_$STAMP.jsonl --status $ZOND_DIR/status_deshred.json --state-dir $ZOND_DIR
$VENV/bin/python podbivka_zond_deshred.py --report-log $ZOND_DIR/deshred_*.jsonl --state-dir $ZOND_DIR \
  --report-out deshred_svod.json --report-md deshred_svod.md
```

Свой ключ шредов, если будет отдельный: `TRITON_DESHRED_TOKEN(_FILE)` (приоритет над TRITON_X_TOKEN). Стабы geyser_pb2 ищутся
в `analysis/proto` рядом с модулем, в корне кода или в `GEYSER_PROTO_DIR`. Нагрузка на Helius: getSlot + getLeaderSchedule
раз в эпоху; подписки WS -- не чаще 8 в секунду на все соединения (`--ws-temp`), по 100 адресов на соединение.
Отказ Triton UNAUTHENTICATED / PERMISSION_DENIED / RESOURCE_EXHAUSTED / INVALID_ARGUMENT / UNIMPLEMENTED закрывает зонд
с кодом 1; обрыв связи -- `stream_error` и переподключение.

**Самопроверка без сети** (`--self-test`, 34 проверки, всё сошлось): записанный образец
`data/podbivka/zond_deshred/obrazec.jsonl` -- сырые сообщения обоих каналов (protobuf шредов в base64, текст logsSubscribe)
с временем прихода; подписи, слоты и кошелёк настоящие (сделки 28.09), лидеры и времена заданы вручную -- это проверка счёта,
не замер. Образец проходит через те же разборщики, журнал и свод; p50 77.5 / p90 220 / доля 0.7 и разбивка по регионам
сверяются с независимым счётом по образцу. Отдельно: кадры WS (маска, ping→pong, фрагменты, 64-битная длина) через
socketpair; настоящий путь gRPC против локального сервера на 127.0.0.1 (ключ в x-token, фильтр, ответ на ping, разбор
транзакции, отказ UNAUTHENTICATED закрывает зонд); ключи не попадают ни в журнал, ни в текст ошибки.

Чего нет: замера. Числа появятся после прогона на хосте NL.
