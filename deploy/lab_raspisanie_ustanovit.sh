#!/bin/sh
# ПОСТАВИТЬ ТАЙМЕРЫ РАСПИСАНИЙ НА ЛАБОРАТОРИИ. Запускается НА lab-miami.
#
# ЗАЧЕМ ФАЙЛОМ, А НЕ heredoc'ом ВНУТРИ ПРОГОНА. Два повода, и оба уже стоили
# времени в этом репозитории:
#   1. heredoc в блочном скаляре YAML однажды сломал весь файл -- строки
#      скрипта встают в нулевую колонку и закрывают блок;
#   2. имена ветвей приходят ВХОДАМИ прогона. Подставлять вход прямо в команду
#      ssh -- это чужая команда на хосте при одной кавычке во входе. Code-3
#      закрыл ровно такую дыру в своём прогоне 04.10. Здесь ветки приходят
#      ДОВОДАМИ этого скрипта, то есть remote shell их как код не читает.
#
# Вызов: lab_raspisanie_ustanovit.sh <ветка_рабочая> <ветка_конвейера>
set -eu

VETKA_R=${1:?nuzhna rabochaja vetka}
VETKA_K=${2:?nuzhna vetka konveyera}
CHASY=/usr/local/sbin/lab_raspisanie_dispatch.sh

[ -x "$CHASY" ] || { echo "OTKAZ: net $CHASY" >&2; exit 1; }
sh -n "$CHASY" || { echo "OTKAZ: $CHASY ne razbiraetsja" >&2; exit 1; }

# ВРЕМЯ ВСЕГДА UTC. Лаборатория живёт по EDT: OnCalendar без пометки взял бы
# местное время, и 07:17 означало бы 11:17Z -- расписание молча уехало бы на
# четыре часа. Пометка UTC стоит у каждого таймера.
edinica() {
  IMJA=$1
  OPIS=$2
  DOVODY=$3
  KOGDA=$4
  {
    echo '[Unit]'
    echo "Description=$OPIS"
    echo ''
    echo '[Service]'
    echo 'Type=oneshot'
    echo "ExecStart=$CHASY $DOVODY"
    echo 'Nice=10'
    # Предел службы ВЫШЕ предела ожидания внутри скрипта (10800 с): иначе
    # systemd убил бы её на середине ожидания конвейера.
    echo 'TimeoutStartSec=11400'
  } > "/etc/systemd/system/$IMJA.service"
  {
    echo '[Unit]'
    echo "Description=Tajmer: $OPIS"
    echo ''
    echo '[Timer]'
    echo "OnCalendar=$KOGDA"
    echo 'AccuracySec=1min'
    # Persistent: пропущенный из-за перезагрузки запуск отрабатывает сразу
    # после подъёма машины, а не теряется до следующего слота.
    echo 'Persistent=true'
    echo "Unit=$IMJA.service"
    echo ''
    echo '[Install]'
    echo 'WantedBy=timers.target'
  } > "/etc/systemd/system/$IMJA.timer"
}

# МИНУТЫ НЕ НА НУЛЕ И НЕ РЯДОМ. :09 и :39 разведены на полчаса, чтобы учёт и
# здоровье не ходили в GitHub одновременно, и ни один не стоит на :00, где
# у GitHub самая длинная очередь.
edinica lab-uchjot-chas \
  'Uchjot: dogon kopii reestra raz v chas' \
  "ledger_hourly.yml@$VETKA_R" \
  '*-*-* *:09:00 UTC'
edinica lab-zdorovje-chas \
  'Proverka zdorovja NL-hosta raz v chas' \
  "run_vps_health_nl.yml@$VETKA_R" \
  '*-*-* *:39:00 UTC'
# ОДИН ТАЙМЕР НА ДВА ПРОГОНА, А НЕ ДВА ТАЙМЕРА С ОТСТУПОМ. Владелец сказал
# "применитель -- после конвейера". Конвейер 03.10 шёл 1 ч 48 мин, и любой
# жёсткий отступ был бы догадкой о его длине. Скрипт часов дожидается конца
# первого прогона и только потом дёргает второй.
edinica lab-konveyer-sutki \
  'Konveyer Code-2, a za nim primenitel' \
  "run_podbivka2_konveyer.yml@$VETKA_K run_konveyer_sutki_nl.yml@$VETKA_R" \
  '*-*-* 07:17:00 UTC'

systemctl daemon-reload
for T in lab-uchjot-chas lab-zdorovje-chas lab-konveyer-sutki; do
  systemctl enable --now "$T.timer"
done

echo '--- postavleno ---'
for T in lab-uchjot-chas lab-zdorovje-chas lab-konveyer-sutki; do
  printf '%s LoadState=%s ActiveState=%s\n' "$T.timer" \
    "$(systemctl show "$T.timer" -p LoadState --value)" \
    "$(systemctl show "$T.timer" -p ActiveState --value)"
  printf '  ExecStart: %s\n' "$(systemctl show "$T.service" -p ExecStart --value)"
done
systemctl list-timers lab-uchjot-chas.timer lab-zdorovje-chas.timer \
  lab-konveyer-sutki.timer --no-pager
