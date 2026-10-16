#!/bin/sh
# СТОРОЖ ДИСКА ЛАБОРАТОРНОГО ЯЩИКА. Раз в час: мало места -- остановить
# таймеры регистратора и положить маркер, чтобы это было видно из ветки.
#
# ОДИН СКРИПТ НА ОБА ЯЩИКА, И ЭТО СЛОВО ВЛАДЕЛЬЦА ("тот же скрипт, что на
# miami"). Разное у них только три вещи, и все три приходят переменными
# окружения: корень клона, список таймеров и путь маркера. Умолчания --
# миамские, чтобы прежняя установка на lab-miami не поменяла поведения ни на
# букву; lab-ams передаёт свои значения в юните.
#
# ЗАЧЕМ. 13-16.10 диск был занят на 100 %, и регистратор при ЖИВЫХ таймерах
# молча терял снимки: 12.10 -- 598 МБ, 13.10 -- 256, 14.10 -- 4.7, 15.10 --
# 0.8, и три файла по 0 байт. Таймеры тикали каждую минуту, служба отчитывалась
# успехом, а данных не было. Молчаливая потеря -- худший исход: её не видно ни
# в одном отчёте, пока кто-нибудь не посмотрит размеры по дням.
#
# ПОЧЕМУ ОСТАНОВКА, А НЕ ПРЕДУПРЕЖДЕНИЕ. Писать снимки на полный диск нечем, и
# каждая минута работы без места -- это ещё одна строка, которой не будет.
# Остановленный таймер честнее работающего вслепую: его видно systemctl-ом, а
# маркер уезжает в ветку суточным пушем.
#
# ТОЛЬКО ЧТЕНИЕ ДИСКА И ДВЕ ЗАПИСИ: остановка таймеров и маркер. Ключей не
# просит, в сеть не ходит, торгового хоста не касается.
set -eu

POROG_GB="${STOROZH_POROG_GB:-10}"
KOREN="${PODB_KOREN:-/opt/robinhood-chain-alpha}"
METKA="${STOROZH_METKA:-$KOREN/data/trending/DISK_STOP}"
TAJMERY="${STOROZH_TAJMERY:-podbivka-trending-min.timer podbivka-trending-5min.timer podbivka-trending-pervye.timer}"
# ВТОРОЙ СЛЕД -- НА tmpfs, И ЭТО НЕ ПЕРЕСТРАХОВКА. Единственным состоянием был
# файл маркера ВНУТРИ тех самых данных, из-за которых диск и кончился: на
# по-настоящему полном диске (ровно случай lab-miami 13-16.10, 0 байт
# свободно) запись маркера падает на ENOSPC, таймеры уже остановлены, маркера
# нет -- и ветка возврата, закрытая условием "есть маркер", не откроется
# никогда. /run -- tmpfs, место там есть всегда; маркер в дереве остаётся для
# ветки и для человека, но решение о возврате принимается по любому из двух.
SOSTOJANIE="${STOROZH_SOSTOJANIE:-/run/lab_storozh_diska.ostanovleno}"
echo "koren: $KOREN"
echo "marker: $METKA"
echo "sostojanie: $SOSTOJANIE"
echo "tajmery: $TAJMERY"

# НАЗВАННЫЕ ТАЙМЕРЫ ДОЛЖНЫ СУЩЕСТВОВАТЬ. Умолчания миамские, поэтому запуск
# этого скрипта на lab-ams без Environment юнита глушил бы несуществующие
# podbivka-trending-*, отказ проглатывался бы "|| echo", и все пять таймеров
# Polymarket продолжали бы писать на полный диск -- при бодром "marker
# polozhen" в журнале. Молчаливая промашка по имени хуже отказа.
IZVESTNO=0
NEIZVESTNY=""
for T in $TAJMERY; do
  if [ "$(systemctl show -p LoadState --value "$T" 2>/dev/null || true)" = "loaded" ]; then
    IZVESTNO=$((IZVESTNO + 1))
  else
    NEIZVESTNY="$NEIZVESTNY $T"
  fi
done
[ -z "$NEIZVESTNY" ] || echo "VNIMANIE: systemd ne znaet tajmerov:$NEIZVESTNY"
if [ "$IZVESTNO" -eq 0 ]; then
  echo "SBOJ: ni odnogo iz nazvannyh tajmerov na etoj mashine net -- storozh napravlen ne tuda, nichego ne delaju"
  exit 1
fi

# df -Pk, А НЕ df -h: -P даёт одну строку на файловую систему (без переноса
# при длинном имени устройства), -k -- килобайты, то есть число, а не "116G",
# которое пришлось бы разбирать. Смотрим корень: именно он кончился.
SVOB_KB=$(df -Pk / | awk 'NR==2 {print $4}')
SVOB_GB=$((SVOB_KB / 1048576))
SEJCHAS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "$SEJCHAS storozh diska: svobodno ${SVOB_GB} GB, porog ${POROG_GB} GB"

if [ "$SVOB_GB" -ge "$POROG_GB" ]; then
  # МАРКЕР СНИМАЕТСЯ САМ, когда место вернулось: иначе один давний случай
  # держал бы регистратор выключенным навсегда, и чинить это пришлось бы руками.
  if [ -f "$METKA" ] || [ -f "$SOSTOJANIE" ]; then
    echo "mesta snova hvataet -- vkljuchaju tajmery obratno"
    # ПОРЯДОК ОБРАТНЫЙ ПРЕЖНЕМУ, И ЭТО ГЛАВНОЕ. Раньше маркер снимался ДО
    # пуска, а отказ пуска проглатывался "|| echo" при коде выхода 0: таймеры
    # остаются стоять, маркер снят, суточный пуш убирает его и из ветки --
    # ветка чистая, юнит зелёный, регистратор мёртв, и след остаётся только в
    # журнале ящика, которого никто не читает. Теперь сначала пуск, потом
    # ПРОВЕРКА is-active по каждому имени, и лишь затем снятие следов.
    # shellcheck disable=SC2086
    systemctl start $TAJMERY || echo "ne vyshlo zapustit chast tajmerov"
    NE_VERNULIS=""
    for T in $TAJMERY; do
      SOST=$(systemctl is-active "$T" 2>&1 || true)
      printf '  %s: active=%s\n' "$T" "$SOST"
      [ "$SOST" = "active" ] || NE_VERNULIS="$NE_VERNULIS $T"
    done
    if [ -n "$NE_VERNULIS" ]; then
      echo "SBOJ: ne vernulis tajmery:$NE_VERNULIS -- marker i sostojanie OSTAVLJAJU, chtoby eto bylo vidno iz vetki"
      exit 1
    fi
    echo "vse tajmery aktivny -- snimaju marker $METKA"
    rm -f "$METKA" "$SOSTOJANIE"
  fi
  exit 0
fi

echo "MALO MESTA: ostanavlivaju tajmery registratora"
# shellcheck disable=SC2086
systemctl stop $TAJMERY || echo "ne vyshlo ostanovit chast tajmerov"
for T in $TAJMERY; do
  printf '  %s: active=%s\n' "$T" "$(systemctl is-active "$T" 2>&1)"
done

# СЛЕД НА tmpfs -- ПЕРВЫМ ДЕЛОМ: он один гарантированно ложится на полном
# диске, и именно он открывает ветку возврата.
printf '%s ostanovleny: %s\n' "$SEJCHAS" "$TAJMERY" > "$SOSTOJANIE" \
  || echo "VNIMANIE: ne lozhitsja dazhe sostojanie $SOSTOJANIE"

# КОРЕНЬ ДОЛЖЕН БЫТЬ НА МЕСТЕ: без этого mkdir -p сочинил бы чужое дерево
# (например /opt/robinhood-chain-alpha на ящике, где такого клона нет) и
# положил бы маркер туда, куда суточный пуш не смотрит.
if [ -d "$KOREN" ]; then
  mkdir -p "$(dirname "$METKA")" || echo "VNIMANIE: ne sozdajotsja katalog markera"
else
  echo "VNIMANIE: kornja $KOREN net -- marker ne kladu, chuzhogo dereva ne sochinjaju"
fi

# МАРКЕР -- ЧИТАЕМЫЙ ТЕКСТ, А НЕ ПУСТОЙ ФАЙЛ: тот, кто увидит его в ветке,
# должен сразу знать когда, сколько было места и что именно остановлено.
#
# ПИШЕМ ЧЕРЕЗ ВРЕМЕННЫЙ ФАЙЛ И mv, И ЭТО НЕ ОБРЯД: на полном диске запись
# обрывается на середине, и в ветку уехал бы ОБРЕЗАННЫЙ JSON, то есть маркер,
# который не разберёт ни один читатель. mv внутри той же файловой системы --
# одно действие: либо старый файл, либо целый новый.
CHERNOVIK="$METKA.chernovik"
if [ -d "$(dirname "$METKA")" ]; then
  {
    echo "{"
    echo "  \"chto\": \"storozh diska ostanovil registrator\","
    echo "  \"kogda\": \"$SEJCHAS\","
    echo "  \"svobodno_gb\": $SVOB_GB,"
    echo "  \"porog_gb\": $POROG_GB,"
    echo "  \"ostanovleno\": \"$TAJMERY\","
    echo "  \"pochemu\": \"bez mesta registrator molcha terjaet snimki -- eto uzhe bylo na lab-miami 13-16.10\","
    echo "  \"chto_delat\": \"osvobodit mesto; marker snimetsja sam na sledujushchej chasovoj proverke, i tajmery vkljuchatsja obratno\""
    echo "}"
  } > "$CHERNOVIK" 2>/dev/null || true
  if [ -s "$CHERNOVIK" ] && tail -1 "$CHERNOVIK" | grep -q '^}$'; then
    mv -f "$CHERNOVIK" "$METKA"
    echo "marker polozhen: $METKA"
    cat "$METKA"
  else
    rm -f "$CHERNOVIK"
    # КОД ВЫХОДА НЕНУЛЕВОЙ, И ЭТО ЕДИНСТВЕННЫЙ ВИДИМЫЙ СИГНАЛ. Таймеры
    # остановлены, состояние на tmpfs лежит, а маркер в ветку не поедет --
    # значит снаружи об этом узнать больше нечем. Пусть служба честно
    # покраснеет в systemctl, чем промолчит зелёным.
    echo "VNIMANIE: marker ne zapisalsja celikom (mesta net?) -- v vetku poedet tolko eta stroka zhurnala; sostojanie lezhit v $SOSTOJANIE"
    exit 1
  fi
else
  echo "VNIMANIE: kataloga markera net -- marker ne polozhen; sostojanie lezhit v $SOSTOJANIE"
  exit 1
fi
