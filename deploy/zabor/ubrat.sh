#!/usr/bin/env bash
# Убрать за собой: секрет затереть, рабочий каталог снести. Вызывается ВСЕГДА,
# в том числе когда опыт упал.
set -uo pipefail
RABOTA=/tmp/zabor_s0_opyt
if [ -f "$RABOTA/kljuch" ]; then
  shred -u "$RABOTA/kljuch" 2>/dev/null || rm -f "$RABOTA/kljuch"
fi
rm -rf "$RABOTA"
echo "ubrano: $RABOTA"
