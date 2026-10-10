#!/usr/bin/env bash
# Опыт забора S+0 НА ХОСТЕ. Скрипт приезжает по `ssh ... bash -s < этот_файл`,
# поэтому в команде ssh нет НИ ОДНОЙ кавычки -- ни двойной, ни одинарной.
#
# ПОЧЕМУ ТАК, А НЕ ОДНОЙ СТРОКОЙ В ssh. Двойные кавычки вокруг многострочной
# команды ssh дважды за 30.09 ломали прогоны: внутри них подставляется и ОБОЛОЧКА
# БЕГУНКА, и удалённая, и любой знак доллара, обратная кавычка или перевод строки
# меняет смысл молча. Файл, приехавший по stdin, разбирает только удалённый bash,
# и его можно проверить `bash -n` ЗАРАНЕЕ, не запуская ничего.
#
# СЕКРЕТ. Ключ кошелька опыта лежит рядом файлом 600 и уходит в ОКРУЖЕНИЕ, а не в
# argv: в argv его видно в `ps` любому на хосте. В журнал он не печатается ни разу;
# прогон отдельно проверяет, что его нет в выводе.
set -euo pipefail

RABOTA=/tmp/zabor_s0_opyt
# IMENA PEREMENNYH -- TOLKO ASCII. Bash kirillicheskie imena ne prinimaet:
# stroka ispolnjaetsja kak komanda i dajot 'command not found' (kod 127).
# Progon kolca 30.09 15:40Z na etom i upal: 'PARAMETRY=...: No such file or
# directory', exit 127, do hosta delo ne doshlo vovse. Zatronuty byli i
# KLJUCH s PRAVA, to est proverka prav 600 na sekret tozhe ne rabotala by.
PARAMETRY=$RABOTA/params
KLJUCH=$RABOTA/kljuch
ENV_DIR=/etc/bloom-executor
CODE_DIR=/home/bot/bloom_executor
VENV=/home/bot/robinhood-chain-alpha/venv/bin

if [ ! -s "$PARAMETRY" ]; then
  echo "SBOY: net faila parametrov $PARAMETRY"
  exit 1
fi
# shellcheck disable=SC1090
. "$PARAMETRY"

: "${OTPRAVITEL:?SBOY: OTPRAVITEL ne zadan}"
: "${RAUNDOV:?SBOY: RAUNDOV ne zadan}"
: "${REZHIM:?SBOY: REZHIM ne zadan}"

case "$OTPRAVITEL" in
  helius|astralane|blockrazor|jito|nozomi|zeroslot|triton) ;;
  *) echo "SBOY: neizvestnyj otpravitel: $OTPRAVITEL"; exit 1;;
esac
case "$REZHIM" in
  kolco|opyt|gonka) ;;
  *) echo "SBOY: rezhim ne kolco, ne opyt i ne gonka: $REZHIM"; exit 1;;
esac
# GONKA: SPISOK OTPRAVITELEJ, I KAZHDOE IMJA PROVERJAETSJA. Opechatka v imeni
# dolzhna byt otkazom zdes, a ne "otpravitelju ne naznacheny chaevye" posle
# nachala raunda.
if [ "$REZHIM" = gonka ]; then
  : "${OTPRAVITELI:?SBOY: OTPRAVITELI ne zadany}"
  for O in $(echo "$OTPRAVITELI" | tr ',' ' '); do
    case "$O" in
      helius|astralane|blockrazor|jito|nozomi|zeroslot|triton) ;;
      *) echo "SBOY: neizvestnyj otpravitel v spiske: $O"; exit 1;;
    esac
  done
fi
case "$RAUNDOV" in
  ''|*[!0-9]*) echo "SBOY: raundov ne chislo"; exit 1;;
esac
# GRANICA RAUNDOV RAZNAJA. U opyta odna otpravka na raund, u gonki -- po odnoj
# KAZHDOMU otpravitelju, i vladelec prosil 25 raundov (nochnoj paket). Dengi
# storozhit ne eta granica, a potolok rashoda v module: on schitaet cenu DO
# pervoj otpravki. Granica zdes -- tolko ot promaha vvodom.
PREDEL_RAUNDOV=20
if [ "$REZHIM" = gonka ]; then
  PREDEL_RAUNDOV=30
fi
if [ "$RAUNDOV" -lt 1 ] || [ "$RAUNDOV" -gt "$PREDEL_RAUNDOV" ]; then
  echo "SBOY: raundov vne 1..$PREDEL_RAUNDOV pri rezhime $REZHIM: $RAUNDOV"
  exit 1
fi

if [ ! -s "$RABOTA/c2_zabor_s0_opyt.py" ]; then
  echo "SBOY: skript opyta ne dostavlen v $RABOTA"
  exit 1
fi
# MODUL GONKI TOZHE DOLZHEN PRIEHAT. Bez etoj proverki rezhim gonka upal by
# pozzhe i mutnee -- ModuleNotFoundError iz-pod sudo, uzhe posle samoproverki.
if [ "$REZHIM" = gonka ] && [ ! -s "$RABOTA/c2_zabor_gonka.py" ]; then
  echo "SBOY: modul gonki c2_zabor_gonka.py ne dostavlen v $RABOTA"
  exit 1
fi

# DOSTUP DLJA bot. Progon sozdajot $RABOTA rezhimom 700 ot root i kladjot fajly
# s umask 077, to est 600 root:root. A i samoproverka, i SAM OPYT zapuskajutsja
# cherez sudo -u bot -- i bot ni prochitat skript, ni zapisat out.json v etot
# katalog ne mozhet: "Permission denied", kod 2. Progon kolca 30.09 15:44Z upal
# imenno tak. Eto sorvalo by i opyt: on idjot tem zhe sudo -u bot, prosto gejt
# samoproverki padal ranshe i do nego delo ne dohodilo.
#
# Dajom dostup GRUPPE bot, a ne vsem: katalog 770 i skript 640 s gruppoj bot.
# FAJL SEKRETA ETIM NE OTKRYVAETSJA: on ostajotsja 600 root:root, i bot ego ne
# chitaet -- sekret v opyt popadaet cherez okruzhenie (sudo -E), a chitaet ego
# root. Prava 600 na njom proverjajutsja otdelno nizhe.
chgrp bot "$RABOTA" && chmod 770 "$RABOTA"
chgrp bot "$RABOTA/c2_zabor_s0_opyt.py" && chmod 640 "$RABOTA/c2_zabor_s0_opyt.py"
if [ -s "$RABOTA/c2_zabor_gonka.py" ]; then
  chgrp bot "$RABOTA/c2_zabor_gonka.py" && chmod 640 "$RABOTA/c2_zabor_gonka.py"
fi
echo "dostup dlja bot: katalog $(stat -c %a:%U:%G "$RABOTA"), skript $(stat -c %a:%U:%G "$RABOTA/c2_zabor_s0_opyt.py")"

# ОКРУЖЕНИЕ СЛУЖБЫ -- ТОЛЬКО ЧИТАЕМ. Узел, отправители и их ключи берутся оттуда
# же, откуда их берёт полоса: иначе опыт мерил бы не ту дорогу.
if [ ! -r "$ENV_DIR/env" ]; then
  echo "SBOY: net $ENV_DIR/env"
  exit 1
fi
set -a
# shellcheck disable=SC1091
. "$ENV_DIR/env"
set +a

# РУБИЛЬНИК ОПЫТА. Без него сам скрипт отказывает и ничего не отправляет.
export BLOOM_ZABOR_S0_OPYT=1

# КЛЮЧ -- ИЗ ФАЙЛА В ОКРУЖЕНИЕ. Только в режиме opyt: кольцу он не нужен вовсе.
if [ "$REZHIM" = opyt ] || [ "$REZHIM" = gonka ]; then
  if [ ! -s "$KLJUCH" ]; then
    echo "SBOY: sekret LIVE_TESTS ne dostavlen (net $KLJUCH)"
    exit 1
  fi
  PRAVA=$(stat -c %a "$KLJUCH")
  if [ "$PRAVA" != 600 ]; then
    echo "SBOY: u faila sekreta prava $PRAVA, a dolzhny byt 600"
    exit 1
  fi
  BLOOM_LIVE_TESTS_KEY=$(cat "$KLJUCH")
  export BLOOM_LIVE_TESTS_KEY
  if [ -z "$BLOOM_LIVE_TESTS_KEY" ]; then
    echo "SBOY: sekret pustoj"
    exit 1
  fi
  echo "sekret: prinyat v okruzhenie iz faila 600, dlina $(printf %s "$BLOOM_LIVE_TESTS_KEY" | wc -c) znakov"
else
  echo "rezhim kolco: sekret ne nuzhen i ne chitaetsya"
fi

# РАБОЧИЙ КАТАЛОГ -- КАТАЛОГ КОДА СЛУЖБЫ, А НЕ ДОМАШНИЙ КАТАЛОГ root.
# ЗАЧЕМ. Реестр отправителей bloom_senders ищет "data/senders.json" ОТНОСИТЕЛЬНО
# рабочего каталога. Команда приезжает по ssh от root, то есть рабочий каталог --
# /root, где лежит свой data/senders.json с правами root. Опыт идёт от bot, и
# каждая отправка падала с "PermissionError: [Errno 13] Permission denied:
# 'data/senders.json'": 30.09 16:14Z в сеть не ушло НИ ОДНОЙ из шести попыток.
# Полоса работает из каталога кода службы -- опыт должен стоять там же, иначе он
# меряет не ту дорогу. Пути $RABOTA и $KLJUCH абсолютные, их это не задевает.
cd "$CODE_DIR" || { echo "SBOY: net katalaga koda $CODE_DIR"; exit 1; }
echo "rabochij katalog opyta: $(pwd)"

# САМОПРОВЕРКА МОДУЛЯ -- НА ТОМ ЖЕ ПИТОНЕ, ЧТО И ОПЫТ. Ей нужен solders, и
# проверять надо именно этот интерпретатор: сломанное окружение должно вскрыться
# ДО первой отправки, а не после списанной комиссии.
# PYTHONPATH SAMOPROVERKE NUZHEN TOT ZHE, CHTO OPYTU. Bez nego ona padaet
# ModuleNotFoundError: No module named 'c2_swap_build' -- modul sborki lezhit v
# kataloge koda sluzhby, a v rabochij katalog kopiruetsja tolko sam zond.
# Progon kolca 30.09 15:43Z upal imenno tak. Zamysel kommentarija vyshe veren
# ("na tom zhe pitone, chto i opyt"), no odnogo pitona malo -- nuzhna ta zhe
# SREDA. I eto ne kosmetika: samoproverka -- gejt PERED pervoj otpravkoj, i v
# takom vide opyt ne proshjol by nikogda.
sudo -u bot -E env PYTHONPATH="$CODE_DIR" BLOOM_CODE_DIR="$CODE_DIR" \
  "$VENV/python" "$RABOTA/c2_zabor_s0_opyt.py" --self-test | tail -2
# U GONKI SVOJA SAMOPROVERKA, i ona tozhe gejt PERED otpravkami: schjot ceny,
# potolok, barjer, pauza po slotam.
if [ "$REZHIM" = gonka ]; then
  sudo -u bot -E env PYTHONPATH="$CODE_DIR:$RABOTA" BLOOM_CODE_DIR="$CODE_DIR" \
    "$VENV/python" "$RABOTA/c2_zabor_gonka.py" --self-test | tail -2
fi

echo "zapusk: rezhim=$REZHIM otpravitel=$OTPRAVITEL raundov=$RAUNDOV otpraviteli=${OTPRAVITELI:-net}"

# ЗАПУСК ОТ bot, А НЕ ОТ root. -E сохраняет окружение, то есть и рубильник, и
# секрет доходят, оставаясь вне argv. PYTHONPATH -- код службы: опыт зовёт
# bloom_senders, bloom_own_send, c2_swap_build и bloom_detector, и они должны быть
# ТЕ ЖЕ, что у полосы.
set +e
sudo -u bot -E env PYTHONPATH="$CODE_DIR:$RABOTA" BLOOM_CODE_DIR="$CODE_DIR" \
  "$VENV/python" "$RABOTA/c2_zabor_s0_opyt.py" \
  --rezhim "$REZHIM" \
  --otpravitel "$OTPRAVITEL" \
  --otpraviteli "${OTPRAVITELI:-helius,astralane}" \
  --raundov "$RAUNDOV" \
  --chaevye-lamporty "${CHAEVYE:-0}" \
  --out "$RABOTA/out.json"
KOD=$?
set -e

# СЕКРЕТ УБИРАЕТСЯ СРАЗУ, а не в конце прогона: между шагами он на диске не нужен.
if [ -f "$KLJUCH" ]; then
  shred -u "$KLJUCH" 2>/dev/null || rm -f "$KLJUCH"
fi
unset BLOOM_LIVE_TESTS_KEY

echo "kod vozvrata opyta: $KOD"
exit "$KOD"
