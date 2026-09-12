#!/usr/bin/env bash
# deploy_feed_relay.sh -- ОДНА попытка поднять официальный Nitro feed
# relay (Offchain Labs, offchainlabs/nitro-node, --entrypoint relay) на
# Ohio: ОДНО подключение к публичному фиду секвенсера, локальная раздача
# на 127.0.0.1:9642 (НЕ на внешний интерфейс -- только loopback).
#
# ЧЕСТНАЯ ОСТОРОЖНОСТЬ (владелец, 2026-09-12, после разбора): у Nitro-
# клиента фида задокументирован ВСТРОЕННЫЙ реконнект с backoff 1с->64с,
# БЕЗ известного флага, чтобы это отключить/ограничить. Если просто
# поднять контейнер с restart-policy сразу, а апстрим-хендшейк словит
# 403 -- получим не "одна попытка, потом 12 часов ожидания", а
# "попытка примерно раз в 64 секунды НАВСЕГДА" -- ровно то, чего этот
# проект избегает. Поэтому:
#   1. Контейнер стартует БЕЗ restart-policy (--restart=no, по умолчанию).
#   2. task5_feed_relay_validate.py ВНЕШНЕ проверяет (не парсит логи
#      relay -- формат не подтверждён вживую), реально ли идут сообщения
#      через localhost:9642.
#   3. Успех -> restart-policy включается ТОЛЬКО ТЕПЕРЬ (docker update),
#      relay остаётся жить и переживает падения/перезагрузку хоста.
#   4. Провал -> контейнер ОСТАНАВЛИВАЕТСЯ и УДАЛЯЕТСЯ -- ничего не
#      остаётся ретраить апстрим самостоятельно. Логи контейнера
#      сохраняются для разбора причины.
#
# Запуск на Ohio: sudo bash deploy_feed_relay.sh
set -euo pipefail

IMAGE="${IMAGE:-offchainlabs/nitro-node:v3.11.2-3599aca}"
FEED_INPUT_URL="${FEED_INPUT_URL:-wss://feed.mainnet.chain.robinhood.com}"
CHAIN_ID="${CHAIN_ID:-4663}"
CONTAINER_NAME="${CONTAINER_NAME:-task5-feed-relay}"
RELAY_PORT="${RELAY_PORT:-9642}"
VALIDATE_TIMEOUT_S="${VALIDATE_TIMEOUT_S:-25}"
REPO_DIR="${REPO_DIR:-/home/bot/robinhood-chain-alpha}"
OUT="${OUT:-/home/bot/data/task5_feed_relay_deploy_result.json}"

echo "=== deploy_feed_relay.sh: одна попытка, $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден -- устанавливаю (apt, официальный пакет docker.io)"
  apt-get update -qq
  apt-get install -y -qq docker.io
  systemctl enable --now docker
fi

# Идемпотентность: убрать предыдущий контейнер с тем же именем, если есть
# (например, от прошлой неудачной попытки).
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo "--- pull ---"
docker pull "$IMAGE"

# КРИТИЧНО: relay -- отдельный Go-бинарник (не наш Python-код), его
# подключение к публичному фиду НЕ пройдёт через record_feed_connect_attempt()
# автоматически. Записываем попытку в ОБЩИЙ state-файл ЯВНО, ДО запуска
# контейнера -- иначе следующая проверка "12 часов с последней попытки"
# не будет знать об этой попытке вообще.
cd "$REPO_DIR/analysis"
"$REPO_DIR/venv/bin/python3" -c "
from task5_bot_feed_client import record_feed_connect_attempt
record_feed_connect_attempt()
print('recorded feed connect attempt (relay upstream) in shared state file')
"

echo "--- запуск БЕЗ restart-policy (--restart=no) ---"
# ЧЕСТНО: только --node.feed.output.addr подтверждён реальной
# документацией (docs.arbitrum.io/run-arbitrum-node/run-feed-relay) --
# порт 9642 там же задокументирован как ФИКСИРОВАННЫЙ порт relay-сервера
# (не флаг). Не добавляем непроверенный --node.feed.output.port -- риск,
# что контейнер откажется стартовать из-за нераспознанного аргумента.
docker run -d --name "$CONTAINER_NAME" --restart=no \
  -p 127.0.0.1:"$RELAY_PORT":9642 \
  --entrypoint relay "$IMAGE" \
  --node.feed.output.addr=0.0.0.0 \
  --node.feed.input.url="$FEED_INPUT_URL" \
  --chain.id="$CHAIN_ID"

echo "--- жду 3с инициализации контейнера перед валидацией ---"
sleep 3

echo "--- внешняя валидация (task5_feed_relay_validate.py, ${VALIDATE_TIMEOUT_S}с) ---"
cd "$REPO_DIR/analysis"
VALIDATE_JSON_FILE="$(mktemp)"
CONTAINER_LOGS_FILE="$(mktemp)"
set +e
"$REPO_DIR/venv/bin/python3" task5_feed_relay_validate.py "$VALIDATE_TIMEOUT_S" 1 > "$VALIDATE_JSON_FILE"
VALIDATE_STATUS=$?
set -e
cat "$VALIDATE_JSON_FILE"

docker logs "$CONTAINER_NAME" > "$CONTAINER_LOGS_FILE" 2>&1 || true

if [ "$VALIDATE_STATUS" -eq 0 ]; then
  echo "--- ВАЛИДАЦИЯ ПРОШЛА -- включаю restart-policy (unless-stopped) ---"
  docker update --restart=unless-stopped "$CONTAINER_NAME"
  RESULT_OK=true
  RESULT_ACTION="restart_policy_enabled"
else
  echo "--- ВАЛИДАЦИЯ НЕ ПРОШЛА -- останавливаю и удаляю контейнер, НЕ включаю restart-policy ---"
  docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
  RESULT_OK=false
  RESULT_ACTION="container_stopped_and_removed"
fi

mkdir -p "$(dirname "$OUT")"
RESULT_OK="$RESULT_OK" RESULT_ACTION="$RESULT_ACTION" IMAGE="$IMAGE" FEED_INPUT_URL="$FEED_INPUT_URL" \
VALIDATE_JSON_FILE="$VALIDATE_JSON_FILE" CONTAINER_LOGS_FILE="$CONTAINER_LOGS_FILE" OUT="$OUT" \
python3 - <<'PY'
import json
import os

validate = json.load(open(os.environ["VALIDATE_JSON_FILE"]))
logs_tail = open(os.environ["CONTAINER_LOGS_FILE"], errors="replace").read()[-4000:]
result = {
    "ok": os.environ["RESULT_OK"] == "true",
    "action": os.environ["RESULT_ACTION"],
    "image": os.environ["IMAGE"],
    "feed_input_url": os.environ["FEED_INPUT_URL"],
    "validate_result": validate,
    "container_logs_tail": logs_tail,
}
with open(os.environ["OUT"], "w") as f:
    json.dump(result, f, indent=2)
print(json.dumps(result, indent=2))
PY
rm -f "$VALIDATE_JSON_FILE" "$CONTAINER_LOGS_FILE"
