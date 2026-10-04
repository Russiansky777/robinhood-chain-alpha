#!/bin/sh
# ЧАСЫ ЛАБОРАТОРИИ: дёрнуть названные прогоны GitHub по порядку.
#
# ЗАЧЕМ (слово владельца 04.10 вечером, п.3): расписания GitHub переносятся на
# таймеры lab-miami, потому что cron GitHub в этом репозитории врёт. Замер за
# ночь 03->04.10: по учёту пропущено четыре слота подряд дважды, по живучести
# не отработали 06:38Z, 07:38Z и 08:38Z, по конвейеру -- ни один. Таймеры хоста
# за ту же ночь не пропустили ни одного часа.
#
# ЧАСЫ, А НЕ РАБОТНИК -- И ЭТО РЕШЕНИЕ, А НЕ УПРОЩЕНИЕ. Сама работа остаётся
# там, где лежат её секреты: учёт и применитель -- на бегунке торгового хоста
# (им нужны HELIUS_API, DBOT_API_KEY, ключ ssh до хоста и право пуша в ветку),
# конвейер Code-2 -- на облачном. Перенести работу сюда значило бы положить на
# лабораторию ключи Helius, ключ записи в репозиторий и ключ ssh до торгового
# хоста -- а bootstrap лаборатории отдельным шагом проверяет, что там нет ни
# ключей кошельков, ни env полосы, ни торговых служб. Владелец запретил ставить
# ключ записи на ТОРГОВЫЙ хост; размазывать его по лаборатории вместо этого --
# не то, о чём он просил. Здесь нужен ровно один допуск: actions:write на один
# репозиторий. Он не даёт ни читать кошельки, ни писать в ветку.
#
# ТОКЕН НЕ ПЕЧАТАЕТСЯ НИ РАЗУ. Он читается из /etc/lab-raspisanie.env (0600) и
# уходит только в заголовок curl. В журнал идут имя прогона, ветка, код HTTP и
# итог -- ничего больше.
#
# ЖДЁМ КОНЦА КАЖДОГО ШАГА, А НЕ СТРЕЛЯЕМ ВСЕМИ СРАЗУ. Применитель обязан идти
# ПОСЛЕ конвейера (слово владельца), а конвейер идёт около двух часов. Поэтому
# шаги -- цепочкой: дёрнули, дождались, дёрнули следующий.
#
# Вызов:  lab_raspisanie_dispatch.sh <прогон.yml@ветка> [ещё@ветка ...]
# Пример: lab_raspisanie_dispatch.sh run_podbivka2_konveyer.yml@claude/podbivka
set -u

ENV_FAJL=/etc/lab-raspisanie.env
REPO=${LAB_REPO:-Russiansky777/robinhood-chain-alpha}
# Предел ожидания одного шага. Конвейер 03.10 шёл 1 ч 48 мин, поэтому по
# умолчанию три часа: меньше означало бы бросить его на середине и дёрнуть
# применитель по пустому файлу.
PREDEL_S=${LAB_PREDEL_S:-10800}
SHAG_S=${LAB_SHAG_S:-30}

if [ ! -r "$ENV_FAJL" ]; then
  echo "OTKAZ: net $ENV_FAJL -- dostupa k GitHub net, nichego ne djorgaju" >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$ENV_FAJL"
if [ -z "${LAB_DISPATCH_TOKEN:-}" ]; then
  echo "OTKAZ: v $ENV_FAJL net LAB_DISPATCH_TOKEN" >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  echo "OTKAZ: ne nazvan ni odin progon" >&2
  exit 2
fi

API=https://api.github.com

# Последний прогон этого файла -- его номер нужен ДО отправки, чтобы потом
# отличить свой запуск от чужого. Сравнение по номеру, а не по времени: часы
# лаборатории и часы GitHub могут расходиться.
poslednij_nomer() {
  curl -s -m 30 \
    -H "Authorization: Bearer $LAB_DISPATCH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "$API/repos/$REPO/actions/workflows/$1/runs?per_page=1" 2>/dev/null \
  | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("-1"); raise SystemExit
r = (d.get("workflow_runs") or [None])[0]
print(r["id"] if r else 0)' 2>/dev/null || echo -1
}

sostojanie() {
  curl -s -m 30 \
    -H "Authorization: Bearer $LAB_DISPATCH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "$API/repos/$REPO/actions/runs/$1" 2>/dev/null \
  | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("nejasno nejasno"); raise SystemExit
print((d.get("status") or "nejasno"), (d.get("conclusion") or "net"))' 2>/dev/null \
  || echo "nejasno nejasno"
}

ne_otpravleno=0

for SHAG in "$@"; do
  PROGON=$(printf '%s' "$SHAG" | cut -d@ -f1)
  VETKA=$(printf '%s' "$SHAG" | cut -s -d@ -f2)
  [ -n "$VETKA" ] || VETKA=claude/robinhood-copytrading-hypothesis-awjj0g
  echo "--- $PROGON @ $VETKA"

  BYLO=$(poslednij_nomer "$PROGON")
  KOD=$(curl -s -m 30 -o /tmp/lab_dispatch_otvet.txt -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer $LAB_DISPATCH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "$API/repos/$REPO/actions/workflows/$PROGON/dispatches" \
    -d "{\"ref\":\"$VETKA\"}" 2>/dev/null || echo 000)
  echo "dispatch: http $KOD"
  if [ "$KOD" != "204" ]; then
    # Тело ответа GitHub токена не содержит, но на всякий случай -- только
    # первые 200 знаков и только при отказе.
    echo "OTKAZ dispatch: $(head -c 200 /tmp/lab_dispatch_otvet.txt 2>/dev/null)" >&2
    rm -f /tmp/lab_dispatch_otvet.txt
    ne_otpravleno=$((ne_otpravleno + 1))
    continue
  fi
  rm -f /tmp/lab_dispatch_otvet.txt

  # Свой запуск: ждём, пока последний номер перестанет быть прежним.
  NOMER=0
  ZHDALI=0
  while [ "$ZHDALI" -lt 180 ]; do
    SEJCHAS=$(poslednij_nomer "$PROGON")
    if [ "$SEJCHAS" != "$BYLO" ] && [ "$SEJCHAS" != "-1" ] && [ "$SEJCHAS" != "0" ]; then
      NOMER=$SEJCHAS
      break
    fi
    sleep 5
    ZHDALI=$((ZHDALI + 5))
  done
  if [ "$NOMER" = "0" ]; then
    echo "progon otpravlen, no svoj zapusk za 180 s ne opoznan -- dalshe ne zhdu"
    continue
  fi
  echo "zapusk: $NOMER"

  ZHDALI=0
  while [ "$ZHDALI" -lt "$PREDEL_S" ]; do
    # БЕЗ "set --": он переписывает $@, по которому идёт внешний цикл. Я
    # проверил, что POSIX-цикл это переживает (список разворачивается один
    # раз), но читающему это не видно, а молчаливая зависимость от такого
    # свойства -- ровно та ловушка, в которую потом и попадают.
    SOST=$(sostojanie "$NOMER")
    STATUS=$(printf '%s' "$SOST" | cut -d' ' -f1)
    ITOG=$(printf '%s' "$SOST" | cut -d' ' -f2)
    [ -n "$STATUS" ] || STATUS=nejasno
    [ -n "$ITOG" ] || ITOG=net
    if [ "$STATUS" = "completed" ]; then
      echo "itog: $ITOG (zhdali ${ZHDALI} s)"
      break
    fi
    sleep "$SHAG_S"
    ZHDALI=$((ZHDALI + SHAG_S))
  done
  if [ "$ZHDALI" -ge "$PREDEL_S" ]; then
    echo "predel ozhidanija ${PREDEL_S} s ischerpan, progon $NOMER vsjo idjot"
  fi
done

if [ "$ne_otpravleno" -gt 0 ]; then
  echo "ne otpravleno progonov: $ne_otpravleno" >&2
  exit 1
fi
exit 0
