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
ПАРАМЕТРЫ=$RABOTA/params
КЛЮЧ=$RABOTA/kljuch
ENV_DIR=/etc/bloom-executor
CODE_DIR=/home/bot/bloom_executor
VENV=/home/bot/robinhood-chain-alpha/venv/bin

if [ ! -s "$ПАРАМЕТРЫ" ]; then
  echo "SBOY: net faila parametrov $ПАРАМЕТРЫ"
  exit 1
fi
# shellcheck disable=SC1090
. "$ПАРАМЕТРЫ"

: "${OTPRAVITEL:?SBOY: OTPRAVITEL ne zadan}"
: "${RAUNDOV:?SBOY: RAUNDOV ne zadan}"
: "${REZHIM:?SBOY: REZHIM ne zadan}"

case "$OTPRAVITEL" in
  helius|astralane|triton) ;;
  *) echo "SBOY: neizvestnyj otpravitel: $OTPRAVITEL"; exit 1;;
esac
case "$RAUNDOV" in
  ''|*[!0-9]*) echo "SBOY: raundov ne chislo"; exit 1;;
esac
if [ "$RAUNDOV" -lt 1 ] || [ "$RAUNDOV" -gt 20 ]; then
  echo "SBOY: raundov vne 1..20: $RAUNDOV"
  exit 1
fi
case "$REZHIM" in
  kolco|opyt) ;;
  *) echo "SBOY: rezhim ne kolco i ne opyt: $REZHIM"; exit 1;;
esac

if [ ! -s "$RABOTA/c2_zabor_s0_opyt.py" ]; then
  echo "SBOY: skript opyta ne dostavlen v $RABOTA"
  exit 1
fi

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
if [ "$REZHIM" = opyt ]; then
  if [ ! -s "$КЛЮЧ" ]; then
    echo "SBOY: sekret LIVE_TESTS ne dostavlen (net $КЛЮЧ)"
    exit 1
  fi
  ПРАВА=$(stat -c %a "$КЛЮЧ")
  if [ "$ПРАВА" != 600 ]; then
    echo "SBOY: u faila sekreta prava $ПРАВА, a dolzhny byt 600"
    exit 1
  fi
  BLOOM_LIVE_TESTS_KEY=$(cat "$КЛЮЧ")
  export BLOOM_LIVE_TESTS_KEY
  if [ -z "$BLOOM_LIVE_TESTS_KEY" ]; then
    echo "SBOY: sekret pustoj"
    exit 1
  fi
  echo "sekret: prinyat v okruzhenie iz faila 600, dlina $(printf %s "$BLOOM_LIVE_TESTS_KEY" | wc -c) znakov"
else
  echo "rezhim kolco: sekret ne nuzhen i ne chitaetsya"
fi

# САМОПРОВЕРКА МОДУЛЯ -- НА ТОМ ЖЕ ПИТОНЕ, ЧТО И ОПЫТ. Ей нужен solders, и
# проверять надо именно этот интерпретатор: сломанное окружение должно вскрыться
# ДО первой отправки, а не после списанной комиссии.
"$VENV/python" "$RABOTA/c2_zabor_s0_opyt.py" --self-test | tail -2

echo "zapusk: rezhim=$REZHIM otpravitel=$OTPRAVITEL raundov=$RAUNDOV"

# ЗАПУСК ОТ bot, А НЕ ОТ root. -E сохраняет окружение, то есть и рубильник, и
# секрет доходят, оставаясь вне argv. PYTHONPATH -- код службы: опыт зовёт
# bloom_senders, bloom_own_send, c2_swap_build и bloom_detector, и они должны быть
# ТЕ ЖЕ, что у полосы.
set +e
sudo -u bot -E env PYTHONPATH="$CODE_DIR" BLOOM_CODE_DIR="$CODE_DIR" \
  "$VENV/python" "$RABOTA/c2_zabor_s0_opyt.py" \
  --rezhim "$REZHIM" \
  --otpravitel "$OTPRAVITEL" \
  --raundov "$RAUNDOV" \
  --out "$RABOTA/out.json"
КОД=$?
set -e

# СЕКРЕТ УБИРАЕТСЯ СРАЗУ, а не в конце прогона: между шагами он на диске не нужен.
if [ -f "$КЛЮЧ" ]; then
  shred -u "$КЛЮЧ" 2>/dev/null || rm -f "$КЛЮЧ"
fi
unset BLOOM_LIVE_TESTS_KEY

echo "kod vozvrata opyta: $КОД"
exit "$КОД"
