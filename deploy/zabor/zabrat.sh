#!/usr/bin/env bash
# Забрать отчёт опыта с хоста. Отдельным скриптом -- чтобы в команде ssh не было
# кавычек вовсе: `ssh ... bash -s < zabrat.sh > out.json`.
set -euo pipefail
RABOTA=/tmp/zabor_s0_opyt
if [ -s "$RABOTA/out.json" ]; then
  cat "$RABOTA/out.json"
fi
