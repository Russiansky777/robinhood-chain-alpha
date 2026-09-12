#!/usr/bin/env bash
# deploy_feed_relay.sh -- поднять официальный Nitro feed relay (Offchain
# Labs, offchainlabs/nitro-node, --entrypoint relay) на Ohio: ОДНО
# постоянное подключение к публичному фиду секвенсера, локальная
# раздача на 127.0.0.1:9642 (НЕ на внешний интерфейс -- только loopback).
#
# Владелец, 2026-09-12 (после разбора и уточнения): "не гадать про 12
# часов" (docs.robinhood.com/chain/connecting подтверждает: точное число
# НЕ публикуется вообще) -- "Relay поднять сразу, не ждать. Если
# упрётся в 403 -- не перезапускать вручную, оставить его встроенный
# backoff. Он подключится сам, когда окно откроется, и с этого момента
# соединение постоянное."
#
# ПОЭТОМУ (изменено относительно первой версии этого скрипта):
# контейнер запускается СРАЗУ с --restart=unless-stopped и НЕ
# останавливается/удаляется при неудачной первой попытке. Если
# апстрим-хендшейк 403 -- relay сам, своим ВСТРОЕННЫМ backoff (1с->64с,
# задокументировано Arbitrum, без известного флага отключения),
# продолжит пробовать -- мы НЕ вмешиваемся руками, не перезапускаем
# контейнер сами. task5_feed_relay_validate.py делает ОДНУ короткую
# ВНЕШНЮЮ проверку (не парсит логи relay -- формат не подтверждён
# вживую) сразу после старта -- ЧИСТО информационно (сработало прямо
# сейчас или ещё нет), не решает, жить контейнеру или нет.
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

echo "=== deploy_feed_relay.sh: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден -- устанавливаю (apt, официальный пакет docker.io)"
  apt-get update -qq
  apt-get install -y -qq docker.io
  systemctl enable --now docker
fi

# Идемпотентность: убрать предыдущий контейнер с тем же именем, если есть.
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo "--- pull ---"
docker pull "$IMAGE"

# КРИТИЧНО: relay -- отдельный Go-бинарник (не наш Python-код), его
# подключение к публичному фиду НЕ пройдёт через record_feed_connect_attempt()
# автоматически. Записываем попытку в ОБЩИЙ state-файл ЯВНО, ДО запуска
# контейнера -- для истории/диагностики (сама попытка теперь не гейтится
# на этот state, но знать реальное время попытки полезно).
cd "$REPO_DIR/analysis"
"$REPO_DIR/venv/bin/python3" -c "
from task5_bot_feed_client import record_feed_connect_attempt
record_feed_connect_attempt()
print('recorded feed connect attempt (relay upstream) in shared state file')
"

echo "--- запуск с --restart=unless-stopped (владелец: 'поднять сразу, не ждать, не перезапускать вручную при 403') ---"
# ЧЕСТНО: только --node.feed.output.addr подтверждён реальной
# документацией (docs.arbitrum.io/run-arbitrum-node/run-feed-relay) --
# порт 9642 там же задокументирован как ФИКСИРОВАННЫЙ порт relay-сервера
# (не флаг). Не добавляем непроверенный --node.feed.output.port -- риск,
# что контейнер откажется стартовать из-за нераспознанного аргумента.
docker run -d --name "$CONTAINER_NAME" --restart=unless-stopped \
  -p 127.0.0.1:"$RELAY_PORT":9642 \
  --entrypoint relay "$IMAGE" \
  --node.feed.output.addr=0.0.0.0 \
  --node.feed.input.url="$FEED_INPUT_URL" \
  --chain.id="$CHAIN_ID"

echo "--- жду 3с инициализации контейнера перед проверкой ---"
sleep 3

echo "--- ИНФОРМАЦИОННАЯ внешняя проверка (task5_feed_relay_validate.py, ${VALIDATE_TIMEOUT_S}с) -- НЕ решает судьбу контейнера ---"
cd "$REPO_DIR/analysis"
VALIDATE_JSON_FILE="$(mktemp)"
CONTAINER_LOGS_FILE="$(mktemp)"
set +e
"$REPO_DIR/venv/bin/python3" task5_feed_relay_validate.py "$VALIDATE_TIMEOUT_S" 1 > "$VALIDATE_JSON_FILE"
VALIDATE_STATUS=$?
set -e
cat "$VALIDATE_JSON_FILE"

docker logs "$CONTAINER_NAME" > "$CONTAINER_LOGS_FILE" 2>&1 || true
CONTAINER_STATUS="$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo unknown)"

if [ "$VALIDATE_STATUS" -eq 0 ]; then
  echo "--- РЕАЛЬНО РАБОТАЕТ ПРЯМО СЕЙЧАС: сообщения идут через localhost:${RELAY_PORT} ---"
  RESULT_ACTION="connected_now"
else
  echo "--- ЕЩЁ НЕ ПОДКЛЮЧЁН (0 сообщений за ${VALIDATE_TIMEOUT_S}с) -- контейнер ОСТАЁТСЯ работать, "
  echo "    встроенный backoff relay'я (1с->64с, задокументировано, без нашего вмешательства) "
  echo "    продолжит пробовать сам. НЕ перезапускаем и НЕ трогаем контейнер вручную."
  RESULT_ACTION="left_running_relay_own_backoff"
fi
echo "--- container status: ${CONTAINER_STATUS}, restart-policy: unless-stopped ---"

mkdir -p "$(dirname "$OUT")"
RESULT_ACTION="$RESULT_ACTION" CONTAINER_STATUS="$CONTAINER_STATUS" IMAGE="$IMAGE" FEED_INPUT_URL="$FEED_INPUT_URL" \
VALIDATE_JSON_FILE="$VALIDATE_JSON_FILE" CONTAINER_LOGS_FILE="$CONTAINER_LOGS_FILE" OUT="$OUT" \
python3 - <<'PY'
import json
import os

validate = json.load(open(os.environ["VALIDATE_JSON_FILE"]))
logs_tail = open(os.environ["CONTAINER_LOGS_FILE"], errors="replace").read()[-4000:]
result = {
    "connected_now": validate.get("ok", False),
    "action": os.environ["RESULT_ACTION"],
    "container_status": os.environ["CONTAINER_STATUS"],
    "restart_policy": "unless-stopped",
    "image": os.environ["IMAGE"],
    "feed_input_url": os.environ["FEED_INPUT_URL"],
    "validate_result": validate,
    "container_logs_tail": logs_tail,
    "note": "Контейнер оставлен работать в любом случае -- владелец, 2026-09-12: "
            "'не перезапускать вручную, оставить встроенный backoff, он подключится сам, "
            "когда окно откроется'. connected_now=false НЕ означает провал деплоя -- "
            "означает 'ещё не подключился, продолжает пробовать сам'.",
}
with open(os.environ["OUT"], "w") as f:
    json.dump(result, f, indent=2)
print(json.dumps(result, indent=2))
PY
rm -f "$VALIDATE_JSON_FILE" "$CONTAINER_LOGS_FILE"
