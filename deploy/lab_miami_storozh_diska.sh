#!/bin/sh
# СТОРОЖ ДИСКА lab-miami. Раз в час: мало места -- остановить таймеры
# регистратора и положить маркер, чтобы это было видно из ветки.
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
METKA="$KOREN/data/trending/DISK_STOP"
TAJMERY="podbivka-trending-min.timer podbivka-trending-5min.timer podbivka-trending-pervye.timer"

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
  if [ -f "$METKA" ]; then
    echo "mesta snova hvataet -- snimaju marker $METKA"
    rm -f "$METKA"
    echo "tajmery vkljuchaju obratno"
    # shellcheck disable=SC2086
    systemctl start $TAJMERY || echo "ne vyshlo zapustit tajmery"
  fi
  exit 0
fi

echo "MALO MESTA: ostanavlivaju tajmery registratora"
# shellcheck disable=SC2086
systemctl stop $TAJMERY || echo "ne vyshlo ostanovit chast tajmerov"
for T in $TAJMERY; do
  printf '  %s: active=%s\n' "$T" "$(systemctl is-active "$T" 2>&1)"
done

mkdir -p "$(dirname "$METKA")"
# МАРКЕР -- ЧИТАЕМЫЙ ТЕКСТ, А НЕ ПУСТОЙ ФАЙЛ: тот, кто увидит его в ветке,
# должен сразу знать когда, сколько было места и что именно остановлено.
{
  echo "{"
  echo "  \"chto\": \"storozh diska lab-miami ostanovil registrator\","
  echo "  \"kogda\": \"$SEJCHAS\","
  echo "  \"svobodno_gb\": $SVOB_GB,"
  echo "  \"porog_gb\": $POROG_GB,"
  echo "  \"ostanovleno\": \"$TAJMERY\","
  echo "  \"pochemu\": \"bez mesta registrator molcha terjaet snimki -- eto uzhe bylo 13-16.10\","
  echo "  \"chto_delat\": \"osvobodit mesto; marker snimetsja sam na sledujushchem chasovom proverke, i tajmery vkljuchatsja obratno\""
  echo "}"
} > "$METKA"
echo "marker polozhen: $METKA"
cat "$METKA"
